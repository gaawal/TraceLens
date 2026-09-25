"""远端日志窗口读取必须保留多行日志的续行。

背景：解析是按行做的，一行必须自己带时间戳才算一条新日志。真实日志里一条记录
常常打好几行（Python 堆栈、JSON dump、被换行拆开的正文）才出现下一个时间戳。
过去读取器把这些「没有时间戳的行」直接 `continue` 掉，于是**日志正文被截断**。

这里用假 SFTP 句柄直接验证两条读取路径：
* 正向（binary seek 之后继续读）——窗口内记录的续行跟出来；
* 反向（recent-tail，从文件尾部往前扫）——续行先于它的时间戳行出现，
  读取器要把它按文件顺序接回那条记录，且窗口外记录的续行不能混进来。
"""
from __future__ import annotations

import io
import types
from contextlib import contextmanager
from datetime import datetime

from apps.logsources.services import remote_logs


LINES = [
    b"[2026-09-25 10:00:00.000] [INFO] [CPFR] [1] [2] [cpfr] [normal] [cpfr:A:1] [A] first record\n",
    b"[2026-09-25 10:00:01.000] [ERROR] [CPFR] [1] [2] [cpfr] [normal] [cpfr:B:2] [B] multi-line record begins here:\n",
    b"Traceback (most recent call last):\n",
    b'  File "cpfr/b.py", line 2, in B\n',
    b"BError: flow below limit\n",
    b"[2026-09-25 10:00:02.000] [INFO] [CPFR] [1] [2] [cpfr] [normal] [cpfr:C:3] [C] last record\n",
]
PAYLOAD = b"".join(LINES)
START = datetime(2026, 9, 25, 10, 0, 1)
END = datetime(2026, 9, 25, 10, 0, 1, 500000)


class _FakeHandle:
    def __init__(self, data: bytes):
        self._data = data
        self._io = io.BytesIO(data)

    def readline(self, limit: int = -1) -> bytes:
        return self._io.readline(limit)

    def read(self, size: int = -1) -> bytes:
        return self._io.read(size)

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._io.seek(offset, whence)

    def tell(self) -> int:
        return self._io.tell()

    def stat(self):
        return types.SimpleNamespace(st_size=len(self._data))

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@contextmanager
def _fake_ssh_session(_machine):
    handle = _FakeHandle(PAYLOAD)
    lease = types.SimpleNamespace(session_id="fake", reused=False, sftp=types.SimpleNamespace(open=lambda *_a, **_k: handle))
    yield lease


def test_forward_reader_keeps_multi_line_continuations():
    handle = _FakeHandle(PAYLOAD)
    chunks = list(remote_logs._stream_direct_forward_from_offset(
        handle, offset=0, start=START, end=END, operation_id="-",
    ))
    text = b"".join(chunks).decode()
    assert "multi-line record begins here" in text
    assert "Traceback (most recent call last):" in text, "窗口内记录的续行被丢掉了"
    assert "BError: flow below limit" in text
    # 窗口前/后的记录仍然被排除。
    assert "first record" not in text
    assert "last record" not in text


def test_reverse_reader_reattaches_continuations_in_file_order(monkeypatch):
    monkeypatch.setattr(remote_logs, "_find_direct_time_offset", lambda *a, **k: None)
    monkeypatch.setattr(remote_logs, "ssh_session", _fake_ssh_session)
    artifact = types.SimpleNamespace(path="/log/cpfr.log", kind="current", member_name="", machine_id=1,
                                     source_category="debug", subsystem="cpfr", fm="cpfr", identity="x")
    chunks = [chunk for chunk in remote_logs._stream_direct(types.SimpleNamespace(id=1), artifact, START, END, "-") if chunk.strip()]
    lines = [chunk.decode().rstrip("\n") for chunk in chunks]
    assert any("multi-line record begins here" in line for line in lines)
    header = next(index for index, line in enumerate(lines) if "multi-line record begins here" in line)
    # 文件顺序：头 → Traceback → File → Error（反向扫描时要在这里翻回来）。
    assert "Traceback (most recent call last):" in lines[header + 1]
    assert "File \"cpfr/b.py\"" in lines[header + 2]
    assert "BError: flow below limit" in lines[header + 3]
    assert not any("first record" in line for line in lines), "窗口之前的记录不应出现"
    assert not any("last record" in line for line in lines), "窗口之后的记录不应出现"


def test_reverse_reader_drops_continuations_of_out_of_window_records(monkeypatch):
    """窗口之后那条记录的续行不能被算进本次窗口。"""
    payload = PAYLOAD + b"  orphan continuation of the next record\n"
    monkeypatch.setattr(remote_logs, "_find_direct_time_offset", lambda *a, **k: None)
    handle = _FakeHandle(payload)

    @contextmanager
    def _session(_machine):
        yield types.SimpleNamespace(session_id="fake", reused=False, sftp=types.SimpleNamespace(open=lambda *_a, **_k: handle))

    monkeypatch.setattr(remote_logs, "ssh_session", _session)
    artifact = types.SimpleNamespace(path="/log/cpfr.log", kind="current", member_name="", machine_id=1,
                                     source_category="debug", subsystem="cpfr", fm="cpfr", identity="x")
    text = b"".join(chunk for chunk in remote_logs._stream_direct(types.SimpleNamespace(id=1), artifact, START, END, "-")).decode()
    assert "orphan continuation of the next record" not in text
    assert "Traceback (most recent call last):" in text


def test_tar_member_awk_window_keeps_continuations():
    """tar 成员的远端 awk 裁剪也必须保留续行（脚本行为用本机 awk 实跑一遍）。"""
    import shutil
    import subprocess

    if not shutil.which("awk"):
        return
    program = remote_logs._tar_window_awk_program()
    payload = b"".join(LINES) + b"  orphan continuation of the next record\n"
    completed = subprocess.run(
        ["awk", "-v", "s=2026-09-25 10:00:01", "-v", "e=2026-09-25 10:00:01.500000", program],
        input=payload, capture_output=True, check=True,
    )
    text = completed.stdout.decode()
    assert "multi-line record begins here" in text
    assert "Traceback (most recent call last):" in text
    assert "BError: flow below limit" in text
    assert "first record" not in text
    assert "last record" not in text, "窗口之后的时间戳行必须 exit"
    assert "orphan continuation of the next record" not in text, "窗口外记录的续行不能混进来"
