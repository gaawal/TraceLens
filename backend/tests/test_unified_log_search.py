from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from apps.logsources.services import unified_search
from apps.logsources.services.unified_search import UrlLogArtifact, read_url_log_window


def _artifact() -> UrlLogArtifact:
    return UrlLogArtifact(
        url="http://example/case/full_logs/log/debug/SPM/WPOS.log",
        source_path="full_logs/log/debug/SPM/WPOS.log",
        source_category="debug",
        subsystem="SPM",
        fm="WPOS",
    )


def test_url_window_large_file_uses_range_seek_and_exact_time_filter(monkeypatch):
    artifact = _artifact()
    start = datetime.fromisoformat("2026-09-11 10:00:05")
    end = datetime.fromisoformat("2026-09-11 10:00:06")
    monkeypatch.setattr(unified_search, "_head_size", lambda url: 64 * 1024 * 1024)
    monkeypatch.setattr(unified_search, "_find_http_start_offset", lambda url, size, target, operation_id="-": (32 * 1024 * 1024, True))

    calls: list[tuple[int, int]] = []

    def fake_range(url: str, byte_start: int, byte_end: int):
        calls.append((byte_start, byte_end))
        return (
            b"[2026-09-11 10:00:04.900] [INFO] before\n"
            b"[2026-09-11 10:00:05.100] [INFO] wanted-1\n"
            b"continuation\n"
            b"[2026-09-11 10:00:06.000] [ERROR] wanted-2\n"
            b"[2026-09-11 10:00:06.100] [INFO] after\n",
            True,
        )

    monkeypatch.setattr(unified_search, "_fetch_range", fake_range)
    result = read_url_log_window(artifact, start, end)

    assert result.mode == "range-binary-seek"
    assert result.start_offset == 32 * 1024 * 1024
    assert calls and calls[0][0] == 32 * 1024 * 1024
    assert "before" not in result.text
    assert "wanted-1" in result.text
    assert "continuation" in result.text
    assert "wanted-2" in result.text
    assert "after" not in result.text


def test_url_window_small_file_still_filters_exact_requested_interval(monkeypatch):
    artifact = _artifact()
    start = datetime.fromisoformat("2026-09-11 10:00:05")
    end = datetime.fromisoformat("2026-09-11 10:00:06")
    content = (
        b"[2026-09-11 10:00:04.999] [INFO] before\n"
        b"[2026-09-11 10:00:05.000] [INFO] first\n"
        b"[2026-09-11 10:00:06.000] [INFO] last\n"
        b"[2026-09-11 10:00:06.001] [INFO] after\n"
    )
    monkeypatch.setattr(unified_search, "_head_size", lambda url: len(content))
    monkeypatch.setattr(unified_search, "_request", lambda url, **kwargs: (200, content, len(content), ""))

    result = read_url_log_window(artifact, start, end)

    assert result.mode == "small-full"
    assert "before" not in result.text
    assert "first" in result.text
    assert "last" in result.text
    assert "after" not in result.text


def test_url_window_stops_immediately_when_file_starts_after_requested_end(monkeypatch):
    artifact = _artifact()
    start = datetime.fromisoformat("2026-09-11 09:00:00")
    end = datetime.fromisoformat("2026-09-11 09:05:00")
    monkeypatch.setattr(unified_search, "_head_size", lambda url: 64 * 1024 * 1024)
    monkeypatch.setattr(unified_search, "_find_http_start_offset", lambda url, size, target, operation_id="-": (0, True))
    calls = []

    def fake_range(url: str, byte_start: int, byte_end: int):
        calls.append((byte_start, byte_end))
        return b"[2026-09-11 10:00:00.000] [INFO] later\n", True

    monkeypatch.setattr(unified_search, "_fetch_range", fake_range)
    result = read_url_log_window(artifact, start, end)

    assert result.text == ""
    assert len(calls) == 1
