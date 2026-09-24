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
_HOST_KEY_FILE = _KEY_DIR / "ssh_host_rsa_key"


def _load_host_key() -> paramiko.RSAKey:
    """加载或生成主机密钥（生成一次后复用，避免每次启动都重算）。"""
    _KEY_DIR.mkdir(parents=True, exist_ok=True)
    if _HOST_KEY_FILE.exists():
        try:
            return paramiko.RSAKey(filename=str(_HOST_KEY_FILE))
        except paramiko.SSHException:
            pass
    key = paramiko.RSAKey.generate(2048)
    key.write_private_key_file(str(_HOST_KEY_FILE))
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
        self.username = username
        self.password = password
        self.exec_event = threading.Event()

    def check_auth_password(self, username: str, password: str) -> int:
        if username == self.username and password == self.password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username: str) -> str:
        return "password"

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
    """一台机器的 SSH 监听服务。"""

    def __init__(self, spec: fleet.MachineSpec, host_key: paramiko.RSAKey, bind_host: str = "127.0.0.1") -> None:
        self.spec = spec
        self.host_key = host_key
        self.bind_host = bind_host
        self.shell = RemoteShell(spec)
        self._sock: socket.socket | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.bind_host, self.spec.ssh_port))
        sock.listen(64)
        sock.settimeout(0.5)
        self._sock = sock
        threading.Thread(target=self._accept_loop, daemon=True, name=f"sim-{self.spec.key}").start()

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                client, _address = self._sock.accept()
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
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass


class FleetServer:
    """整个机群的监听服务集合。"""

    def __init__(self, bind_host: str = "127.0.0.1") -> None:
        self.host_key = _load_host_key()
        self.machines = [MachineServer(spec, self.host_key, bind_host) for spec in fleet.FLEET]

    def start(self) -> None:
        for machine in self.machines:
            machine.start()

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


def serve(bind_host: str = "127.0.0.1") -> None:
    FleetServer(bind_host).serve_forever()
