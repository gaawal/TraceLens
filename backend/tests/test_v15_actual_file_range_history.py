from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_file_index():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    django = types.ModuleType("django")
    django_conf = types.ModuleType("django.conf")
    django_conf.settings = types.SimpleNamespace()
    sys.modules.setdefault("django", django)
    sys.modules["django.conf"] = django_conf

    ssh_stub = types.ModuleType("apps.common.services.ssh")
    ssh_stub.SshOperationError = RuntimeError
    sys.modules["apps.common.services.ssh"] = ssh_stub

    redis_stub = types.ModuleType("apps.logsources.services.redis_store")
    class RedisLogStore:
        @classmethod
        def get_json(cls, key): return None
        @classmethod
        def set_json(cls, key, value, ttl): return True
    redis_stub.RedisLogStore = RedisLogStore
    sys.modules["apps.logsources.services.redis_store"] = redis_stub
    return _load("file_index_runtime_v15", ROOT / "apps/logsources/services/file_index.py")


def test_current_log_is_only_selected_for_unarchived_tail():
    module = _load_file_index()
    fm = "RSSSD"
    archived = module.PhysicalCandidate(
        module.RemoteFileStat("/RSSSD_20260801000000.log", "RSSSD_20260801000000.log", 1, 10, "1785542400"),
        datetime(2026, 8, 1, 0, 0), "archived", frozenset({fm}),
    )
    current = module.PhysicalCandidate(
        module.RemoteFileStat("/RSSSD.log", "RSSSD.log", 2, 999, "1786150800"),
        None, "current", frozenset({fm}),
    )
    historical = module._select_physical_candidates(
        [archived, current], fms={fm},
        start=datetime(2026, 7, 10, 0, 0), end=datetime(2026, 7, 10, 1, 0),
    )
    assert historical == [archived]

    live_tail = module._select_physical_candidates(
        [archived, current], fms={fm},
        start=datetime(2026, 8, 1, 1, 0), end=datetime(2026, 8, 1, 2, 0),
    )
    assert live_tail == [current]


def test_real_first_last_range_is_final_decision():
    module = _load_file_index()
    old_current_index = {
        "start_time": "2026-07-01T00:00:00.000000",
        "end_time": "2026-08-08T10:00:00.000000",
    }
    assert module._indexed_range_overlaps(
        old_current_index,
        datetime(2026, 7, 10, 0, 0),
        datetime(2026, 7, 10, 1, 0),
    )
    recent_only = {
        "start_time": "2026-08-08T09:00:00.000000",
        "end_time": "2026-08-08T10:00:00.000000",
    }
    assert not module._indexed_range_overlaps(
        recent_only,
        datetime(2026, 7, 10, 0, 0),
        datetime(2026, 7, 10, 1, 0),
    )


def test_normal_logs_use_filename_time_fast_index():
    source = (ROOT / "apps/logsources/services/file_index.py").read_text(encoding="utf-8")
    assert "strategy=filename_time_fast_index" in source
    assert "fm + '_*.tar.gz'" in source
    assert "skip_unparseable_archive" in source
    assert "filename timestamps are authoritative" in source
