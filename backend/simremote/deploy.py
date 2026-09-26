"""自动化模拟部署：上下位机互信（SSH 免密）与分步部署日志。

真实机台接入 TraceLens 前的准备动作，这里按同样的顺序模拟一遍：

    1. 网络连通性检查   上位机 ⇄ 下位机 的 SSH 端口是否真的能连上
    2. 生成 SSH 密钥对   两台机器各生成自己的 id_rsa / id_rsa.pub
    3. 交换主机指纹      把对方的主机密钥写进本机 known_hosts
    4. 双向分发公钥      把本机公钥写进对方的 authorized_keys
    5. 校验正向免密      上位机拿私钥登录下位机并读一次日志目录
    6. 校验反向免密      下位机拿私钥登录上位机并读一次日志根目录
    7. 写入环境资源      落库 + 落盘部署报告

**「互信」是可验证的，不是脚本自己写个成功**：第 5/6 步走的是真实 SSH 公钥
认证 —— 服务端 :meth:`simremote.sshd.SimSSHServer.check_auth_publickey` 真的去
比对对端的 ``authorized_keys``，公钥没铺好就一定连不上；连上之后还会真的执行
一条命令并把退出码带回来。

日志分两层：

* **控制台** —— 每个步骤一行进度、若干明细行、一行结果（含耗时），失败即中断；
* **落盘** —— 每台机器一份自己的部署日志 ``/log/<user>/deploy/deploy_<ts>.log``，
  同时把每个步骤作为**运行事件**追加进 ``event.log``，于是前端「运行日志」里
  也能看到这次部署。落盘内容**全英文**（与设备日志的语言约定一致）。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import socket
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator

import paramiko

from . import fleet

#: 密钥算法与位数 —— 与真实机台一致的 RSA 2048
KEY_BITS = 2048
_PRIVATE_KEY_NAME = "id_rsa"
_PUBLIC_KEY_NAME = "id_rsa.pub"
_AUTHORIZED_KEYS_NAME = "authorized_keys"
_KNOWN_HOSTS_NAME = "known_hosts"


class DeployError(RuntimeError):
    """部署步骤的**预期失败**（可读原因），与代码缺陷区分开。"""


@dataclass(frozen=True)
class StepDefinition:
    key: str
    english: str  # 落盘用（日志全英文）
    title: str  # 控制台用


STEP_DEFS: tuple[StepDefinition, ...] = (
    StepDefinition("connectivity", "network connectivity check", "网络连通性检查"),
    StepDefinition("keygen", "generate ssh keypair", "生成 SSH 密钥对"),
    StepDefinition("hostkey", "exchange host fingerprints", "交换主机指纹"),
    StepDefinition("distribute", "distribute public keys", "双向分发公钥"),
    StepDefinition(
        "verify-forward", "verify passwordless upper->lower", "校验正向免密（上位机 → 下位机）"
    ),
    StepDefinition(
        "verify-reverse", "verify passwordless lower->upper", "校验反向免密（下位机 → 上位机）"
    ),
    StepDefinition("finalize", "write environment resources", "写入环境资源与部署报告"),
)

_STEP_BY_KEY = {item.key: item for item in STEP_DEFS}
TOTAL_STEPS = len(STEP_DEFS)


@dataclass
class StepRecord:
    index: int
    total: int
    key: str
    title: str
    english: str
    ok: bool = True
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class DeployReport:
    steps: list[StepRecord]
    started: datetime
    finished: datetime
    upper_host: str
    lower_host: str
    log_files: list[Path] = field(default_factory=list)
    seeded: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(step.ok for step in self.steps)

    @property
    def seconds(self) -> float:
        return (self.finished - self.started).total_seconds()


# --------------------------------------------------------------------------- 小工具


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _quiet_third_party_logs() -> None:
    """压制传输层噪声。

    部署会 ``django.setup()``（第 7 步要写环境资源），于是后端的 LOGGING 配置
    把 root logger 拉到 INFO —— ``Connected (version 2.0, client paramiko_4.0.0)``
    这类每步都来几行，会把"哪一步在干什么"的进度输出彻底冲散。
    """
    for name in ("paramiko", "tracelens"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _fingerprint(key: paramiko.PKey) -> str:
    """OpenSSH 风格的 SHA256 指纹（和 ``ssh-keygen -lf`` 显示的一致）。"""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def _probe_ssh(host: str, port: int, timeout: float = 5.0) -> str | None:
    """连一下并读回 SSH 版本横幅；连不上返回 None。"""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return None
    try:
        sock.settimeout(timeout)
        data = sock.recv(256)
    except OSError:
        return "已连接（未读到横幅）"
    finally:
        sock.close()
    return data.decode("utf-8", errors="replace").strip() or "已连接"


def _fetch_host_key(spec: fleet.MachineSpec, timeout: float = 6.0) -> paramiko.PKey | None:
    """握个手把对端的主机公钥取回来（``known_hosts`` 的素材）。"""
    try:
        sock = socket.create_connection((spec.host, spec.ssh_port), timeout=timeout)
    except OSError:
        return None
    transport = paramiko.Transport(sock)
    try:
        transport.start_client(timeout=timeout)
        return transport.get_remote_server_key()
    except Exception:  # noqa: BLE001 - 握手失败按"取不到"处理
        return None
    finally:
        transport.close()


def _known_hosts_name(spec: fleet.MachineSpec) -> str:
    """``known_hosts`` 里的主机名。

    SSH 不在标准端口时写成 ``[host]:port`` —— 这是 OpenSSH 的真实约定，
    模拟机和真实机台都用非 22 端口，所以必须带方括号。
    """
    return f"[{spec.host}]:{spec.ssh_port}"


def _write_unique_line(path: Path, line: str, *, field: int = 1) -> bool:
    """按指定字段去重后写入文件，返回是否真的新增。

    ``field=1`` 按公钥 blob 去重（``authorized_keys`` / ``known_hosts`` 的
    ``<type> <blob> <comment>`` 结构）；``field=0`` 按主机名去重（``known_hosts``
    里同一个 host 只该有一条）。
    """
    parts = line.split()
    token = parts[field] if len(parts) > field else line

    def token_of(text: str) -> str:
        fields = text.split()
        return fields[field] if len(fields) > field else text

    existing: list[str] = []
    if path.exists():
        existing = [item for item in path.read_text(encoding="utf-8").splitlines() if item.strip()]
    kept = [item for item in existing if token_of(item) != token]
    added = len(kept) == len(existing)
    kept.append(line)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return added


def _deploy_log_path(spec: fleet.MachineSpec, stamp: str) -> Path:
    return fleet.remote_to_local(spec, f"{spec.deploy_root}/deploy_{stamp}.log")


def _ensure_keypair(spec: fleet.MachineSpec) -> tuple[paramiko.RSAKey, bool]:
    """确保这台机器有 SSH 密钥对，返回 ``(私钥, 是否为复用)``。"""
    private = fleet.remote_to_local(spec, f"{spec.ssh_dir}/{_PRIVATE_KEY_NAME}")
    if private.exists():
        try:
            return paramiko.RSAKey(filename=str(private)), True
        except paramiko.SSHException:
            # 文件坏了就当没有，重新生成 —— 比直接报错更贴近"重跑一次部署"
            pass
    key = paramiko.RSAKey.generate(KEY_BITS)
    private.parent.mkdir(parents=True, exist_ok=True)
    key.write_private_key_file(str(private))
    try:
        os.chmod(private, 0o600)
    except OSError:
        pass
    public = fleet.remote_to_local(spec, f"{spec.ssh_dir}/{_PUBLIC_KEY_NAME}")
    public.write_text(
        f"ssh-rsa {key.get_base64()} {fleet.SIM_USERNAME}@{spec.name}\n", encoding="utf-8"
    )
    return key, False


def _public_key_line(spec: fleet.MachineSpec) -> str:
    public = fleet.remote_to_local(spec, f"{spec.ssh_dir}/{_PUBLIC_KEY_NAME}")
    if not public.exists():
        raise DeployError(f"{spec.name} 没有公钥文件（{_PUBLIC_KEY_NAME}）")
    line = public.read_text(encoding="utf-8").strip()
    if not line:
        raise DeployError(f"{spec.name} 的公钥文件是空的")
    return line


def _connect_with_password(spec: fleet.MachineSpec, timeout: float = 8.0) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        spec.host,
        port=spec.ssh_port,
        username=fleet.SIM_USERNAME,
        password=fleet.SIM_PASSWORD,
        timeout=timeout,
        allow_agent=False,
        look_for_keys=False,
    )
    return client


# --------------------------------------------------------------------------- 执行器


class DeployRunner:
    """按固定顺序跑完部署步骤，边跑边打印、边落盘。"""

    def __init__(
        self, *, seed_after: bool = True, write_logs: bool = True, quiet: bool = False
    ) -> None:
        self.seed_after = seed_after
        self.write_logs = write_logs
        self.quiet = quiet
        self.started = datetime.now()
        self.stamp = self.started.strftime("%Y%m%d%H%M%S")
        self.steps: list[StepRecord] = []
        self.private_keys: dict[str, paramiko.RSAKey] = {}
        self._current: StepRecord | None = None
        self._failed = False
        self.seeded = False

    # ------------------------------------------------------------------ 输出

    def _say(self, text: str = "") -> None:
        if not self.quiet:
            print(text)
            sys.stdout.flush()

    def note(self, text: str) -> None:
        """当前步骤的明细行：既打印也留档（落盘时**不写**，避免中文进日志）。"""
        if self._current is not None:
            self._current.notes.append(text)
        self._say(f"       {text}")

    @contextmanager
    def step(self, key: str) -> Iterator[StepRecord]:
        definition = _STEP_BY_KEY[key]
        record = StepRecord(
            index=len(self.steps) + 1,
            total=TOTAL_STEPS,
            key=key,
            title=definition.title,
            english=definition.english,
        )
        self._current = record
        started = time.perf_counter()
        self._say(f"[{record.index}/{TOTAL_STEPS}] {record.title}")
        try:
            yield record
        except DeployError as exc:
            record.ok = False
            record.error = str(exc)
            self._say(f"       ✗ {exc}")
        except Exception as exc:  # noqa: BLE001 - 单步异常不该炸掉整个流程
            record.ok = False
            record.error = f"{type(exc).__name__}: {exc}"
            self._say(f"       ✗ {record.error}")
        finally:
            record.seconds = time.perf_counter() - started
            self._current = None
            self.steps.append(record)
            if not record.ok:
                self._failed = True
            self._persist(record)
            if record.ok:
                self._say(f"       ✓ 通过（{record.seconds:.3f}s）")

    # ------------------------------------------------------------------ 落盘

    def _persist(self, record: StepRecord) -> None:
        if not self.write_logs:
            return
        state = "ok" if record.ok else "failed"
        for spec in fleet.FLEET:
            path = _deploy_log_path(spec, self.stamp)
            path.parent.mkdir(parents=True, exist_ok=True)
            line = (
                f"[{_stamp()}] [DEPLOY] [{spec.name}] "
                f"step {record.index}/{record.total} {record.english} {state} "
                f"({record.seconds:.3f}s)"
            )
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        self._append_run_event(record)

    def _append_run_event(self, record: StepRecord) -> None:
        """把步骤作为**运行事件**追加进 ``event.log``，前端「运行日志」可见。

        格式必须严格合 RUN_PATTERN（十三字段），否则会污染运行日志的解析。
        部署行是**追加在文件末尾**的，不影响自检对开头若干行的抽样。
        """
        path = fleet.remote_to_local(fleet.UPPER, f"{fleet.UPPER.run_root}/event.log")
        if not path.exists():
            return
        level = "INFO" if record.ok else "ERROR"
        code = 9000 + record.index
        verdict = "ok" if record.ok else "failed"
        line = (
            f"[{_stamp()}] [DEPLOY] [40002] [event] [process] [{level}] "
            f"[{code}] [] [] [] [DP{code}] [] [deploy] "
            f"{record.english} {verdict} ({record.seconds:.3f}s)"
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def _write_banner(self) -> None:
        if not self.write_logs:
            return
        upper, lower = fleet.UPPER, fleet.LOWER1
        summary = (
            f"upper={upper.host}:{upper.ssh_port} lower={lower.host}:{lower.ssh_port} "
            f"user={fleet.SIM_USERNAME}"
        )
        for spec in fleet.FLEET:
            path = _deploy_log_path(spec, self.stamp)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"[{_stamp()}] [DEPLOY] [{spec.name}] deploy start {summary}\n")

    def _write_summary(self, report: DeployReport) -> None:
        if not self.write_logs:
            return
        verdict = "ok" if report.ok else "failed"
        done = sum(1 for step in report.steps if step.ok)
        for spec in fleet.FLEET:
            path = _deploy_log_path(spec, self.stamp)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"[{_stamp()}] [DEPLOY] [{spec.name}] deploy finish {verdict} "
                    f"steps={done}/{TOTAL_STEPS} elapsed={report.seconds:.3f}s\n"
                )

    # ------------------------------------------------------------------ 步骤

    def _step_connectivity(self) -> None:
        upper, lower = fleet.UPPER, fleet.LOWER1
        # 箭头用 ASCII：全角箭头在不同终端占的列宽不一致，表格会歪。
        pairs = (
            (f"{upper.name} -> {lower.name}", (lower.host, lower.ssh_port)),
            (f"{lower.name} -> {upper.name}", (upper.host, upper.ssh_port)),
        )
        with self.step("connectivity"):
            for label, (host, port) in pairs:
                banner = _probe_ssh(host, port)
                if banner is None:
                    raise DeployError(f"{label} 不可达：{host}:{port}")
                self.note(f"{label:26s} {host}:{port:<5} 可达 · {banner}")

    def _step_keygen(self) -> None:
        with self.step("keygen"):
            for spec in fleet.FLEET:
                key, reused = _ensure_keypair(spec)
                self.private_keys[spec.key] = key
                action = "复用已有密钥" if reused else "新生成密钥"
                self.note(f"{spec.name:12s} {action}  {_fingerprint(key)}")

    def _step_hostkey(self) -> None:
        with self.step("hostkey"):
            for spec in fleet.FLEET:
                peer = fleet.peer_of(spec)
                host_key = _fetch_host_key(peer)
                if host_key is None:
                    raise DeployError(f"取不到 {peer.name}({peer.host}:{peer.ssh_port}) 的主机密钥")
                entry = f"{_known_hosts_name(peer)} {host_key.get_name()} {host_key.get_base64()}"
                path = fleet.remote_to_local(spec, f"{spec.ssh_dir}/{_KNOWN_HOSTS_NAME}")
                # 按主机名去重：换过主机密钥时要覆盖同一条，而不是叠加
                _write_unique_line(path, entry, field=0)
                self.note(
                    f"{spec.name:12s} 已记录 {peer.name:12s} 主机密钥  {_fingerprint(host_key)}"
                )

    def _step_distribute(self) -> None:
        upper, lower = fleet.UPPER, fleet.LOWER1
        with self.step("distribute"):
            for source, target in ((upper, lower), (lower, upper)):
                # 先用口令登录一次：这是「公钥分发」的前提，凭据不对就不该往下走
                try:
                    client = _connect_with_password(target)
                except paramiko.AuthenticationException as exc:
                    raise DeployError(f"{target.name} 口令认证被拒绝：{exc}") from exc
                except Exception as exc:  # noqa: BLE001
                    raise DeployError(f"{target.name} 口令登录失败：{exc}") from exc
                else:
                    client.close()

                line = _public_key_line(source)
                path = fleet.remote_to_local(target, f"{target.ssh_dir}/{_AUTHORIZED_KEYS_NAME}")
                added = _write_unique_line(path, line, field=1)
                action = "写入" if added else "已存在，跳过"
                self.note(
                    f"{source.name} 公钥 → {target.name} authorized_keys  {action}"
                    f"  {line.split()[1][:24]}…"
                )

    def _verify_passwordless(self, source: fleet.MachineSpec, target: fleet.MachineSpec) -> str:
        """用 ``source`` 的私钥登录 ``target``，并在 ``target`` 上读一次目录内容。

        只读一次 ``ls`` 是有意的：TraceLens 接入机台要的就是这个能力，
        能列出目录说明免密 + 只读通道都通了，比单纯"登录成功"更有说服力。

        ⚠️ **不能只探测 ``debug_root``**：子系统日志树只有上位机承载
        （见 ``fleet.hosts_subsystem_logs``），下位机上那棵树按设计就不存在。
        拿它当"免密可读"的判据，会让部署在校验正向免密（上位机 → 下位机）时**假失败**。
        按 ``debug_root`` → ``log_root`` → ``home`` 依次找第一个存在的目录，
        既保住了"读日志树"的语义，又不依赖某一台机器特有的目录。
        """
        key = self.private_keys.get(source.key)
        if key is None:
            raise DeployError(f"{source.name} 没有可用私钥，无法验证免密")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        candidates = list(dict.fromkeys([target.debug_root, target.log_root, target.home]))
        # 逐个探测：目录存在**并且非空**才算数（下位机上 ``debug`` 目录可能还在、
        # 只是已经被清空，那时它证明不了任何东西）。
        probe = (
            f'for d in {" ".join(candidates)}; do '
            f'if [ -d "$d" ]; then out=$(ls "$d"); '
            f'if [ -n "$out" ]; then echo "$d"; echo "$out"; exit 0; fi; fi; done; exit 1'
        )
        try:
            client.connect(
                target.host,
                port=target.ssh_port,
                username=fleet.SIM_USERNAME,
                pkey=key,
                timeout=8,
                allow_agent=False,
                look_for_keys=False,
            )
            _stdin, stdout, _stderr = client.exec_command(probe, timeout=10)
            output = stdout.read().decode("utf-8", errors="replace")
            status = stdout.channel.recv_exit_status()
        except paramiko.AuthenticationException as exc:
            raise DeployError(
                f"{source.name} -> {target.name} 公钥被拒（authorized_keys 未生效）：{exc}"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise DeployError(f"{source.name} -> {target.name} 免密登录失败：{exc}") from exc
        finally:
            client.close()

        lines = [line for line in output.splitlines() if line.strip()]
        if status != 0 or len(lines) < 2:
            raise DeployError(
                f"{source.name} -> {target.name} 登录成功但读不到日志目录"
                f"（已尝试 {', '.join(candidates)}，退出码 {status}）"
            )
        probed, items = lines[0].strip(), lines[1:]
        return (
            f"{source.name} -> {target.name}  免密登录成功 · "
            f"ls {probed} 返回 {len(items)} 项"
        )

    def _step_verify_forward(self) -> None:
        with self.step("verify-forward"):
            self.note(self._verify_passwordless(fleet.UPPER, fleet.LOWER1))

    def _step_verify_reverse(self) -> None:
        with self.step("verify-reverse"):
            self.note(self._verify_passwordless(fleet.LOWER1, fleet.UPPER))

    def _step_finalize(self) -> None:
        with self.step("finalize"):
            self.note(f"部署日志 {fleet.UPPER.deploy_root}/deploy_{self.stamp}.log（每台一份）")
            if not self.seed_after:
                self.note("已跳过环境资源写入（--no-seed）")
                return
            from . import seed as seed_module

            payload = seed_module.seed(refresh=True)
            self.note(
                f"环境资源 {payload['environment']} (#{payload['environment_id']})"
                f"  上位机 {payload['upper']}"
            )
            for item in payload["lowers"]:
                self.note(f"下位机 {item}")
            if payload.get("catalog_error"):
                raise DeployError(f"日志目录扫描失败：{payload['catalog_error']}")
            self.note(f"子系统 {'、'.join(payload['subsystems']) or '（无）'}")
            self.seeded = True

    # ------------------------------------------------------------------ 主流程

    def run(self) -> DeployReport:
        upper, lower = fleet.UPPER, fleet.LOWER1
        _quiet_third_party_logs()
        self._say()
        self._say(f"  上位机 {upper.name:12s} {upper.host}:{upper.ssh_port}")
        self._say(f"  下位机 {lower.name:12s} {lower.host}:{lower.ssh_port}")
        if not fleet.hosts_are_distinct():
            self._say(
                f"  ⚠ 两台机器共用一个地址（{upper.host}，靠端口区分）—— 互信仍可验证，"
                f"但不如两个独立 IP 贴近真机台。准备两个 IP：scripts/sim.sh alias"
            )
        self._say()

        self._write_banner()
        handlers = (
            self._step_connectivity,
            self._step_keygen,
            self._step_hostkey,
            self._step_distribute,
            self._step_verify_forward,
            self._step_verify_reverse,
            self._step_finalize,
        )
        for handler in handlers:
            handler()
            if self._failed:
                self._say()
                self._say("  部署中断：后续步骤依赖前一步的结果，已停止。")
                break

        report = DeployReport(
            steps=list(self.steps),
            started=self.started,
            finished=datetime.now(),
            upper_host=upper.host,
            lower_host=lower.host,
            log_files=[_deploy_log_path(spec, self.stamp) for spec in fleet.FLEET],
            seeded=self.seeded,
        )
        self._write_summary(report)
        return report


def deploy(
    *, seed_after: bool = True, write_logs: bool = True, quiet: bool = False
) -> DeployReport:
    """跑一遍完整部署（供 CLI 与自检共用）。"""
    return DeployRunner(seed_after=seed_after, write_logs=write_logs, quiet=quiet).run()
