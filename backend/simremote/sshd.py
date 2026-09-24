"""基于 paramiko 的假 SSH/SFTP 服务端。

每台机器一个监听端口，对外表现为一台普通的 Linux 机器：

- 密码认证（``tracepilot`` / ``tracelens``）
- ``exec`` 通道：命令交给 :mod:`simremote.shell` 执行，返回 stdout/stderr/退出码
- ``sftp`` 子系统：只读，映射到本地模拟文件系统
- 长驻命令（``tail -n 0 -F``）保持通道打开并持续转发，供前端「实时监听」使用

只有读操作是被允许的：后端对日志/报表/事件配置的访问全部是只读的，
任何写请求都会被拒绝（返回权限错误），避免模拟机被误当成可写目标。
"""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import paramiko

from . import fleet
from .shell import RemoteShell

# 长驻命令识别：后端实时监听用的是 `LC_ALL=C exec tail -n 0 -F -- <path>`
_STREAM_MARKERS = ("tail -n 0 -F", "tail -F", "tail -f")
_READ_CHUNK = 65536
_CLOSE_GRACE_SECONDS = 0.2
_KEY_DIR = Path(__file__).resolve().parent / "run"


def _load_host_key(spec: fleet.MachineSpec) -> paramiko.RSAKey:
    """加载或生成**该机器自己的**主机密钥。

    每台机器一份：真实机台的 host key 各不相同，"交换主机指纹"才有实际内容 ——
    两台共用一把钥匙的话，known_hosts 里写谁的都是同一串，那一步就成空动作了。
    """
    _KEY_DIR.mkdir(parents=True, exist_ok=True)
    path = _KEY_DIR / f"ssh_host_rsa_key_{spec.key}"
    if path.exists():
        try:
            return paramiko.RSAKey(filename=str(path))
        except paramiko.SSHException:
            pass
    key = paramiko.RSAKey.generate(2048)
    key.write_private_key_file(str(path))
    return key


# --------------------------------------------------------------------------- SFTP


class SimSFTPHandle(paramiko.SFTPHandle):
    """只读文件句柄。stat() 必须返回真实 fstat，后端依赖它取文件大小。"""

    def stat(self):
        try:
            return paramiko.SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def chattr(self, attr):
        return paramiko.SFTP_PERMISSION_DENIED


class SimSFTPServer(paramiko.SFTPServerInterface):
    """把 SFTP 操作映射到本地模拟文件系统。只读。"""

    def __init__(self, server, *largs, **kwargs):
        self.shell: RemoteShell = kwargs.pop("shell", None) or RemoteShell(fleet.UPPER)
        super().__init__(server, *largs, **kwargs)
        self.home = f"/home/{fleet.SIM_USERNAME}"

    # ------------------------------------------------------------------ 路径

    def _local(self, path: str) -> Path:
        return self.shell.map_path(self.canonicalize(path))

    def canonicalize(self, path: str) -> str:
        text = str(path or "").strip()
        if text.startswith("~"):
            text = self.home + text[1:]
        if not text.startswith("/"):
            text = f"{self.home}/{text}"
        import posixpath

        return posixpath.normpath(text)

    # ------------------------------------------------------------------ 读

    def list_folder(self, path):
        local = self._local(path)
        try:
            entries = []
            for item in sorted(local.iterdir()):
                try:
                    stat_result = item.stat()
                except OSError:
                    continue
                entries.append(paramiko.SFTPAttributes.from_stat(stat_result, item.name))
            return entries
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def stat(self, path):
        local = self._local(path)
        try:
            return paramiko.SFTPAttributes.from_stat(local.stat())
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def lstat(self, path):
        local = self._local(path)
        try:
            return paramiko.SFTPAttributes.from_stat(local.lstat())
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def open(self, path, flags, attr):
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
        if flags & write_flags:
            return paramiko.SFTP_PERMISSION_DENIED
        local = self._local(path)
        try:
            handle = SimSFTPHandle(flags)
            handle.filename = str(local)
            handle.readfile = open(local, "rb")  # noqa: SIM115 - 句柄由 paramiko 关闭
            return handle
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def readlink(self, path):
        return paramiko.SFTP_OP_UNSUPPORTED

    # ------------------------------------------------------------------ 写（拒绝）

    def remove(self, path):
        return paramiko.SFTP_PERMISSION_DENIED

    def rename(self, oldpath, newpath):
        return paramiko.SFTP_PERMISSION_DENIED

    def mkdir(self, path, attr):
        return paramiko.SFTP_PERMISSION_DENIED

    def rmdir(self, path):
        return paramiko.SFTP_PERMISSION_DENIED

    def chattr(self, path, attr):
        return paramiko.SFTP_PERMISSION_DENIED

    def symlink(self, target_path, path):
        return paramiko.SFTP_PERMISSION_DENIED


# --------------------------------------------------------------------------- SSH


class SimSSHServer(paramiko.ServerInterface):
    def __init__(self, shell: RemoteShell, username: str, password: str) -> None:
        self.shell = shell
        self.spec = shell.spec
        self.username = username
        self.password = password
        self.exec_event = threading.Event()

    def check_auth_password(self, username: str, password: str) -> int:
        if username == self.username and password == self.password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_publickey(self, username: str, key) -> int:  # noqa: ANN001 - paramiko.PKey
        """公钥认证：比对 ``~/.ssh/authorized_keys``。

        这是"互信"能被**真实验证**的关键 —— 部署流程往下位机写入上位机的公钥后，
        上位机拿着自己的私钥连过来就会走这条路；公钥没铺好则这里直接拒绝。
        所以免密登录不是嘴上说说，是这条回调真的放行/拦截。
        """
        if username != self.username:
            return paramiko.AUTH_FAILED
        try:
            blob = key.get_base64()
        except Exception:  # noqa: BLE001 - 非法公钥一律当认证失败
            return paramiko.AUTH_FAILED
        if blob and blob in self._authorized_blobs():
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def _authorized_blobs(self) -> set[str]:
        """本机 ``authorized_keys`` 里的公钥部分（第二列）。

        每次认证都重读文件：部署流程刚写完密钥就要立刻登录验证，
        缓存住会让"刚铺完还连不上"这种真实故障被掩盖。
        """
        path = fleet.remote_to_local(self.spec, f"{self.spec.home}/.ssh/authorized_keys")
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return set()
        blobs: set[str] = set()
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split()
            if len(parts) >= 2:
                blobs.add(parts[1])
        return blobs

    def get_allowed_auths(self, username: str) -> str:
        return "publickey,password"

    def check_channel_request(self, kind: str, chanid: int) -> int:
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, channel, term, width, height, pixelwidth, pixelheight, modes) -> bool:
        return True

    def check_channel_shell_request(self, channel) -> bool:
        # 不提供交互式 shell；后端只用 exec 与 sftp。
        return False

    def check_channel_exec_request(self, channel, command) -> bool:
        text = command.decode("utf-8", errors="replace") if isinstance(command, bytes) else str(command)
        threading.Thread(target=self._serve_exec, args=(channel, text), daemon=True).start()
        self.exec_event.set()
        return True

    # ------------------------------------------------------------------ exec

    def _is_streaming(self, command: str) -> bool:
        return any(marker in command for marker in _STREAM_MARKERS)

    def _serve_exec(self, channel, command: str) -> None:
        try:
            if self._is_streaming(command):
                self._serve_stream(channel, command)
            else:
                self._serve_once(channel, command)
        except Exception:  # noqa: BLE001 - 服务端不能因单个通道异常而崩溃
            try:
                channel.close()
            except Exception:  # noqa: BLE001
                pass

    def _serve_once(self, channel, command: str) -> None:
        result = self.shell.execute(command)
        if result.stdout:
            channel.sendall(result.stdout)
        if result.stderr:
            channel.sendall_stderr(result.stderr)
        channel.send_exit_status(result.status)
        time.sleep(_CLOSE_GRACE_SECONDS)
        channel.close()

    def _serve_stream(self, channel, command: str) -> None:
        """长驻命令（tail -F）：持续把新内容推给客户端，直到客户端断开。"""
        process = self.shell.popen(command)
        stdout_fd = process.stdout.fileno() if process.stdout else None
        stderr_fd = process.stderr.fileno() if process.stderr else None
        fds = [fd for fd in (stdout_fd, stderr_fd) if fd is not None]
        try:
            while True:
                if channel.closed or not channel.send_ready():
                    time.sleep(0.05)
                    if channel.closed:
                        break
                    continue
                import select

                ready, _, _ = select.select(fds, [], [], 0.2)
                if not ready:
                    if process.poll() is not None:
                        break
                    continue
                for fd in ready:
                    try:
                        chunk = os.read(fd, _READ_CHUNK)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        if process.poll() is not None:
                            break
                        continue
                    if fd == stderr_fd:
                        channel.sendall_stderr(chunk)
                    else:
                        channel.sendall(chunk)
        finally:
            try:
                process.terminate()
                process.wait(timeout=2)
            except Exception:  # noqa: BLE001
                process.kill()
            try:
                if not channel.closed:
                    channel.send_exit_status(0)
                    time.sleep(_CLOSE_GRACE_SECONDS)
                    channel.close()
            except Exception:  # noqa: BLE001
                pass


class MachineServer:
    """一台机器的 SSH 监听服务。

    同一端口可以同时绑多个地址（网络 IP + 回环）：对外用局域网 IP 访问，
    本机健康检查 / selftest 走 127.0.0.1 —— 只要其中一个通就算服务在跑，
    不会因为网卡切换把本机探测也一起搞挂。
    """

    def __init__(
        self,
        spec: fleet.MachineSpec,
        host_key: paramiko.RSAKey,
        bind_hosts: tuple[str, ...] | str = ("127.0.0.1",),
    ) -> None:
        self.spec = spec
        self.host_key = host_key
        self.bind_hosts = (bind_hosts,) if isinstance(bind_hosts, str) else tuple(dict.fromkeys(bind_hosts))
        self.shell = RemoteShell(spec)
        self._socks: list[socket.socket] = []
        self._stop = threading.Event()

    def start(self) -> None:
        try:
            for bind_host in self.bind_hosts:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((bind_host, self.spec.ssh_port))
                sock.listen(64)
                sock.settimeout(0.5)
                self._socks.append(sock)
                threading.Thread(
                    target=self._accept_loop,
                    args=(sock,),
                    daemon=True,
                    name=f"sim-{self.spec.key}-{bind_host}",
                ).start()
        except OSError:
            # 绑到一半失败就把已经起来的关掉，不留半开状态
            self.stop()
            raise

    def _accept_loop(self, sock: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                client, _address = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_client, args=(client,), daemon=True).start()

    def _handle_client(self, client: socket.socket) -> None:
        transport = paramiko.Transport(client)
        try:
            transport.add_server_key(self.host_key)
            transport.set_subsystem_handler("sftp", paramiko.SFTPServer, SimSFTPServer, shell=self.shell)
            server = SimSSHServer(self.shell, fleet.SIM_USERNAME, fleet.SIM_PASSWORD)
            transport.start_server(server=server)
            while transport.is_active() and not self._stop.is_set():
                time.sleep(0.2)
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                transport.close()
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        self._stop.set()
        for sock in self._socks:
            try:
                sock.close()
            except OSError:
                pass
        self._socks.clear()


class FleetServer:
    """整个机群的监听服务集合。

    默认**每台机器绑自己的地址**（``spec.host`` + 回环）—— 上位机与下位机是两台
    不同 IP 的机器，让下位机也去绑上位机的地址只会把"谁是谁"搅浑。
    显式传 ``bind_hosts`` 时所有机器都用它（调试单地址时有用）。
    """

    def __init__(self, bind_hosts: tuple[str, ...] | str | None = None) -> None:
        if bind_hosts is None:
            self._explicit: tuple[str, ...] | None = None
        elif isinstance(bind_hosts, str):
            self._explicit = (bind_hosts,)
        else:
            self._explicit = tuple(dict.fromkeys(bind_hosts))
        self.machines = [
            MachineServer(
                spec, _load_host_key(spec), self._explicit or fleet.bind_hosts_for(spec)
            )
            for spec in fleet.FLEET
        ]
        # 汇总实际绑定地址（启动横幅 / 健康检查用）
        self.bind_hosts = tuple(
            dict.fromkeys(host for machine in self.machines for host in machine.bind_hosts)
        )

    def start(self) -> None:
        started: list[MachineServer] = []
        try:
            for machine in self.machines:
                machine.start()
                started.append(machine)
        except OSError:
            for machine in started:
                machine.stop()
            raise

    def serve_forever(self) -> None:
        self.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

    def stop(self) -> None:
        for machine in self.machines:
            machine.stop()


def serve(bind_hosts: tuple[str, ...] | str | None = None) -> None:
    FleetServer(bind_hosts).serve_forever()
