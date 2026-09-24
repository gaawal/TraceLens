from __future__ import annotations

import atexit
import hashlib
import io
import logging
import shlex
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

import paramiko
from django.conf import settings

from apps.machines.models import AuthenticationType, Machine

logger = logging.getLogger("tracelens.ssh")


class SshOperationError(RuntimeError):
    """Raised when an SSH/SFTP operation fails."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    stdout: str
    stderr: str
    exit_status: int


@dataclass(slots=True)
class SessionLease:
    client: paramiko.SSHClient
    sftp: paramiko.SFTPClient
    session_id: str
    reused: bool


@dataclass(slots=True)
class _SessionEntry:
    key: str
    fingerprint: str
    client: paramiko.SSHClient
    sftp: paramiko.SFTPClient
    session_id: str
    created_at: float
    last_used_at: float
    borrowed: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock)

    def active(self) -> bool:
        try:
            transport = self.client.get_transport()
            channel_closed = bool(getattr(self.sftp.sock, "closed", False))
            return bool(transport and transport.is_active() and not channel_closed)
        except Exception:
            return False

    def close(self, reason: str) -> None:
        logger.info("ssh.session.close session=%s key=%s reason=%s", self.session_id, self.key, reason)
        try:
            self.sftp.close()
        except Exception:
            logger.debug("ssh.sftp.close_failed session=%s", self.session_id, exc_info=True)
        try:
            self.client.close()
        except Exception:
            logger.debug("ssh.client.close_failed session=%s", self.session_id, exc_info=True)


def _private_key_from_text(value: str, passphrase: str | None = None):
    errors: list[str] = []
    for key_type in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            return key_type.from_private_key(io.StringIO(value), password=passphrase or None)
        except Exception as exc:  # Paramiko raises different subclasses per key type.
            errors.append(str(exc))
    raise SshOperationError("无法解析私钥：" + "; ".join(errors[-2:]))


def _effective_machine_host(machine: Machine) -> str:
    """Return a transient host override when a resource has a separate log IP."""
    return str(getattr(machine, "_tracelens_host_override", "") or machine.host or "").strip()


def _machine_key(machine: Machine) -> str:
    host = _effective_machine_host(machine)
    if machine.pk:
        return f"machine:{machine.pk}:{host}"
    return f"draft:{host}:{machine.ssh_port}:{machine.username}"


def _machine_fingerprint(machine: Machine) -> str:
    """Return a stable digest for the effective connection configuration.

    Encrypted database values cannot be used here because Fernet generates a new
    ciphertext whenever the same secret is saved. A digest of the decrypted
    values lets a successfully tested draft session be promoted to the saved
    machine without opening a second SSH connection.
    """
    try:
        password = machine.get_password()
    except Exception:
        password = ""
    try:
        private_key = machine.get_private_key()
    except Exception:
        private_key = ""
    try:
        passphrase = machine.get_private_key_passphrase()
    except Exception:
        passphrase = ""
    raw = "|".join(
        [
            _effective_machine_host(machine),
            str(machine.ssh_port),
            machine.username,
            machine.auth_type,
            password,
            private_key,
            passphrase,
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_ssh_client(machine: Machine) -> paramiko.SSHClient:
    logger.info(
        "ssh.connect.start machine=%s host=%s port=%s user=%s auth=%s",
        machine.pk or "draft",
        _effective_machine_host(machine),
        machine.ssh_port,
        machine.username,
        machine.auth_type,
    )
    client = paramiko.SSHClient()
    known_hosts = getattr(settings, "TRACELENS_KNOWN_HOSTS_FILE", "")
    if known_hosts:
        try:
            client.load_host_keys(known_hosts)
        except OSError as exc:
            raise SshOperationError(f"无法读取 known_hosts：{exc}") from exc
    else:
        client.load_system_host_keys()

    if getattr(settings, "TRACELENS_SSH_AUTO_ADD_HOST_KEY", True):
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    else:
        client.set_missing_host_key_policy(paramiko.RejectPolicy())

    timeout = getattr(settings, "TRACELENS_SSH_CONNECT_TIMEOUT", 10)
    kwargs: dict = {
        "hostname": _effective_machine_host(machine),
        "port": machine.ssh_port,
        "username": machine.username or None,
        "timeout": timeout,
        "banner_timeout": timeout,
        "auth_timeout": timeout,
        "look_for_keys": False,
        "allow_agent": False,
    }
    if machine.auth_type == AuthenticationType.PASSWORD:
        kwargs["password"] = machine.get_password()
    elif machine.auth_type == AuthenticationType.PRIVATE_KEY:
        kwargs["pkey"] = _private_key_from_text(
            machine.get_private_key(), machine.get_private_key_passphrase()
        )
    else:
        kwargs["look_for_keys"] = True
        kwargs["allow_agent"] = True

    started = time.monotonic()
    try:
        client.connect(**kwargs)
        transport = client.get_transport()
        if transport:
            transport.set_keepalive(getattr(settings, "TRACELENS_SSH_KEEPALIVE", 30))
    except Exception as exc:
        client.close()
        logger.exception(
            "ssh.connect.failed machine=%s host=%s elapsed_ms=%d",
            machine.pk or "draft",
            _effective_machine_host(machine),
            int((time.monotonic() - started) * 1000),
        )
        raise SshOperationError(f"SSH 连接失败：{exc}") from exc
    logger.info(
        "ssh.connect.success machine=%s host=%s elapsed_ms=%d",
        machine.pk or "draft",
        _effective_machine_host(machine),
        int((time.monotonic() - started) * 1000),
    )
    return client


class SshSessionPool:
    """Process-local SSH/SFTP pool.

    A lease holds a per-machine lock because Paramiko's SFTPClient is not safe for
    concurrent reads. Reusing the authenticated Transport removes the expensive
    handshake from directory scans, version reads, planning and log streaming.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _SessionEntry] = {}
        self._lock = threading.RLock()
        self._reaper_stop = threading.Event()
        self._reaper_thread = threading.Thread(
            target=self._reaper_loop,
            daemon=True,
            name="ssh-session-idle-reaper",
        )
        self._reaper_thread.start()

    @property
    def idle_timeout(self) -> int:
        return max(10, int(getattr(settings, "TRACELENS_SSH_SESSION_IDLE_TIMEOUT", 120)))

    @property
    def reap_interval(self) -> int:
        return max(5, int(getattr(settings, "TRACELENS_SSH_SESSION_REAP_INTERVAL", 15)))

    def _reaper_loop(self) -> None:
        # Do not rely on a future SSH request to clean stale sessions. When nobody
        # is using TraceLens, this daemon still closes idle authenticated transports.
        while not self._reaper_stop.is_set():
            try:
                interval = self.reap_interval
            except Exception:
                interval = 15
            if self._reaper_stop.wait(interval):
                break
            try:
                with self._lock:
                    self._cleanup_locked()
            except Exception:
                logger.exception("ssh.session.reaper_failed")

    @property
    def max_sessions(self) -> int:
        return getattr(settings, "TRACELENS_SSH_SESSION_MAX", 32)

    def _cleanup_locked(self) -> None:
        now = time.monotonic()
        expired = [
            key
            for key, entry in self._entries.items()
            if entry.borrowed == 0 and (now - entry.last_used_at > self.idle_timeout or not entry.active())
        ]
        for key in expired:
            entry = self._entries.pop(key)
            entry.close("idle_or_inactive")
        if len(self._entries) <= self.max_sessions:
            return
        # Never close a session while a streaming response or SFTP operation is
        # borrowing it. If every session is busy, temporarily allow the pool to
        # exceed the soft capacity and clean it up on a later acquisition.
        removable = sorted(
            (entry for entry in self._entries.values() if entry.borrowed == 0),
            key=lambda item: item.last_used_at,
        )
        overflow = len(self._entries) - self.max_sessions
        for entry in removable[:overflow]:
            self._entries.pop(entry.key, None)
            entry.close("pool_capacity")

    def _new_entry(self, machine: Machine, key: str, fingerprint: str) -> _SessionEntry:
        client = build_ssh_client(machine)
        try:
            sftp = client.open_sftp()
        except Exception as exc:
            client.close()
            raise SshOperationError(f"SFTP 通道创建失败：{exc}") from exc
        now = time.monotonic()
        entry = _SessionEntry(
            key=key,
            fingerprint=fingerprint,
            client=client,
            sftp=sftp,
            session_id=uuid.uuid4().hex[:10],
            created_at=now,
            last_used_at=now,
        )
        logger.info(
            "ssh.session.created session=%s machine=%s host=%s",
            entry.session_id,
            machine.pk or "draft",
            machine.host,
        )
        return entry

    @contextmanager
    def acquire(self, machine: Machine) -> Iterator[SessionLease]:
        key = _machine_key(machine)
        fingerprint = _machine_fingerprint(machine)
        with self._lock:
            self._cleanup_locked()
            entry = self._entries.get(key)
            if entry and entry.fingerprint != fingerprint:
                self._entries.pop(key, None)
                entry.close("machine_configuration_changed")
                entry = None
            if entry and not entry.active():
                self._entries.pop(key, None)
                entry.close("inactive_before_borrow")
                entry = None
            reused = entry is not None
            if entry is None:
                entry = self._new_entry(machine, key, fingerprint)
                self._entries[key] = entry

        entry.lock.acquire()
        try:
            if not entry.active():
                # Release the stale entry lock before replacing the pooled object.
                entry.lock.release()
                with self._lock:
                    self._entries.pop(key, None)
                    entry.close("inactive_after_lock")
                    entry = self._new_entry(machine, key, fingerprint)
                    self._entries[key] = entry
                entry.lock.acquire()
                reused = False
            entry.borrowed += 1
            entry.last_used_at = time.monotonic()
            logger.info(
                "ssh.session.borrow session=%s machine=%s reused=%s",
                entry.session_id,
                machine.pk or "draft",
                reused,
            )
            yield SessionLease(entry.client, entry.sftp, entry.session_id, reused)
        except Exception:
            logger.exception(
                "ssh.session.operation_failed session=%s machine=%s",
                entry.session_id,
                machine.pk or "draft",
            )
            # A broken transport must never remain in the pool.
            if not entry.active():
                self.invalidate(machine, "operation_failed_transport_inactive")
            raise
        finally:
            entry.last_used_at = time.monotonic()
            entry.borrowed = max(0, entry.borrowed - 1)
            try:
                entry.lock.release()
            except RuntimeError:
                pass

    def promote_draft(self, machine: Machine) -> bool:
        """Move a tested draft session to a newly saved machine key.

        The draft key is derived from host/port/username. Promotion only occurs
        when the effective credential fingerprint is identical, so a changed
        password or key can never inherit an unrelated authenticated session.
        """
        if not machine.pk:
            return False
        draft_key = f"draft:{machine.host}:{machine.ssh_port}:{machine.username}"
        target_key = _machine_key(machine)
        fingerprint = _machine_fingerprint(machine)
        with self._lock:
            entry = self._entries.get(draft_key)
            if not entry or entry.borrowed or not entry.active() or entry.fingerprint != fingerprint:
                return False
            previous = self._entries.pop(target_key, None)
            self._entries.pop(draft_key, None)
            if previous and previous is not entry:
                previous.close("replaced_by_promoted_draft")
            entry.key = target_key
            entry.fingerprint = fingerprint
            entry.last_used_at = time.monotonic()
            self._entries[target_key] = entry
        logger.info(
            "ssh.session.promoted session=%s machine=%s host=%s",
            entry.session_id,
            machine.pk,
            machine.host,
        )
        return True

    def invalidate(self, machine: Machine, reason: str = "manual") -> None:
        key = _machine_key(machine)
        with self._lock:
            entry = self._entries.pop(key, None)
        if entry:
            entry.close(reason)

    def close_all(self) -> None:
        self._reaper_stop.set()
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            entry.close("process_shutdown")

    def stats(self) -> list[dict]:
        with self._lock:
            self._cleanup_locked()
            now = time.monotonic()
            return [
                {
                    "session_id": entry.session_id,
                    "key": entry.key,
                    "age_seconds": int(now - entry.created_at),
                    "idle_seconds": int(now - entry.last_used_at),
                    "active": entry.active(),
                }
                for entry in self._entries.values()
            ]


SSH_SESSION_POOL = SshSessionPool()
atexit.register(SSH_SESSION_POOL.close_all)


@contextmanager
def ssh_session(machine: Machine) -> Iterator[SessionLease]:
    with SSH_SESSION_POOL.acquire(machine) as lease:
        yield lease


@contextmanager
def ssh_client(machine: Machine) -> Iterator[paramiko.SSHClient]:
    """Compatibility wrapper. The underlying authenticated session is pooled."""
    with ssh_session(machine) as lease:
        yield lease.client


def test_connection(machine: Machine, command: str | None = None) -> CommandResult:
    """Test a draft/saved machine without retaining the session in the pool."""
    client = build_ssh_client(machine)
    try:
        test_command = command or "printf '%s\\n' \"$(hostname)\"; uname -srm"
        _, stdout, stderr = client.exec_command(test_command, timeout=15)
        stdout_data = stdout.read().decode("utf-8", errors="replace")
        stderr_data = stderr.read().decode("utf-8", errors="replace")
        status = stdout.channel.recv_exit_status()
        return CommandResult(stdout_data, stderr_data, status)
    finally:
        client.close()


def execute(machine: Machine, command: str, timeout: int = 30) -> CommandResult:
    started = time.monotonic()
    logger.info(
        "ssh.command.start machine=%s host=%s timeout=%s command=%s",
        machine.pk,
        machine.host,
        timeout,
        command[:240],
    )
    with ssh_session(machine) as lease:
        try:
            _, stdout, stderr = lease.client.exec_command(command, timeout=timeout)
            stdout_data = stdout.read().decode("utf-8", errors="replace")
            stderr_data = stderr.read().decode("utf-8", errors="replace")
            exit_status = stdout.channel.recv_exit_status()
            logger.info(
                "ssh.command.finish machine=%s session=%s exit=%s elapsed_ms=%d stdout_bytes=%d stderr_bytes=%d",
                machine.pk,
                lease.session_id,
                exit_status,
                int((time.monotonic() - started) * 1000),
                len(stdout_data.encode("utf-8")),
                len(stderr_data.encode("utf-8")),
            )
            return CommandResult(stdout=stdout_data, stderr=stderr_data, exit_status=exit_status)
        except (TimeoutError, socket.timeout) as exc:
            # A timed-out command may leave a usable-looking Transport with a wedged
            # channel. Drop the pooled session so the next operation reconnects cleanly.
            SSH_SESSION_POOL.invalidate(machine, "command_timeout")
            raise SshOperationError(f"远程命令执行超时，已断开 SSH：{exc}") from exc
        except Exception as exc:
            if "timed out" in str(exc).lower() or "timeout" in str(exc).lower():
                SSH_SESSION_POOL.invalidate(machine, "command_timeout")
            raise SshOperationError(f"远程命令执行失败：{exc}") from exc


def _shell_path(path: str) -> str:
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def read_text(machine: Machine, path: str, max_bytes: int = 5_000_000) -> str:
    logger.info("ssh.file.read.start machine=%s path=%s max_bytes=%s", machine.pk, path, max_bytes)
    result = execute(machine, f"cat -- {_shell_path(path)}", timeout=30)
    if result.exit_status != 0:
        raise SshOperationError(f"读取远程文件失败 {path}：{result.stderr.strip()}")
    data = result.stdout.encode("utf-8")
    if len(data) > max_bytes:
        raise SshOperationError(f"远程文件超过允许大小 {max_bytes} 字节。")
    logger.info("ssh.file.read.finish machine=%s path=%s bytes=%d", machine.pk, path, len(data))
    return result.stdout
