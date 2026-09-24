import importlib.util
import sys
import tempfile
import types
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "backend/apps/logsources/services/search_result_cache.py"


def load_cache_class():
    django = types.ModuleType("django")
    django_conf = types.ModuleType("django.conf")
    class Settings:
        BASE_DIR = Path(tempfile.gettempdir()) / "tracelens-v086-test"
        TRACELENS_SEARCH_RESULT_CACHE_TTL = 24 * 3600
        TRACELENS_SEARCH_RESULT_CACHE_MAX_BYTES = 2 * 1024 * 1024 * 1024
        TRACELENS_SEARCH_RESULT_CACHE_TOTAL_BYTES = 10 * 1024 * 1024 * 1024
        TRACELENS_SEARCH_RESULT_CACHE_RECENT_GUARD_SECONDS = 15 * 60
    django_conf.settings = Settings()
    sys.modules.setdefault("django", django)
    sys.modules["django.conf"] = django_conf
    spec = importlib.util.spec_from_file_location("v086_search_result_cache", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module.LogSearchResultCache


def test_result_cache_identity_ignores_selection_order():
    cache = load_cache_class()
    left = {
        "start_time": "2026-08-12T09:00:00+08:00",
        "end_time": "2026-08-12T10:00:00+08:00",
        "source_categories": ["executor", "debug"],
        "subsystems": ["wspm", "rspm"],
        "fms": ["b", "a"],
        "fm_targets": [
            {"subsystem": "wspm", "fm": "b", "kind": "normal"},
            {"subsystem": "wspm", "fm": "a", "kind": "executor"},
        ],
        "keyword": "fault",
    }
    right = {
        "end_time": "2026-08-12T10:00:00+08:00",
        "start_time": "2026-08-12T09:00:00+08:00",
        "source_categories": ["debug", "executor", "debug"],
        "fm_targets": [
            {"fm": "a", "subsystem": "wspm", "kind": "executor"},
            {"fm": "b", "subsystem": "wspm", "kind": "normal"},
        ],
        "keyword": "fault",
        "subsystems": ["rspm", "wspm"],
        "fms": ["a", "b"],
    }
    assert cache.identity(3, left) == cache.identity(3, right)


def test_final_result_cache_defaults_are_disk_friendly():
    cache = load_cache_class()
    assert cache.max_bytes() >= 1024 * 1024 * 1024
    assert cache.ttl() >= 24 * 3600


def test_scene_ids_are_short_numeric_sequences():
    source = (ROOT / "backend/apps/logsources/services/log_scene.py").read_text(encoding="utf-8")
    assert 'RedisLogStore.increment("tracelens:scene:sequence")' in source
    assert "token_urlsafe" not in source


def test_recent_window_bypasses_final_result_cache():
    cache = load_cache_class()
    now = datetime.now()
    assert cache.allow_final_cache(now - timedelta(minutes=2)) is False
    assert cache.allow_final_cache(now - timedelta(hours=1)) is True
