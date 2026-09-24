from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REMOTE = (ROOT / "backend/apps/logsources/services/remote_logs.py").read_text(encoding="utf-8")
VIEWS = (ROOT / "backend/apps/logsources/views.py").read_text(encoding="utf-8")
RESULT_CACHE = (ROOT / "backend/apps/logsources/services/search_result_cache.py").read_text(encoding="utf-8")


def test_directed_query_refreshes_stale_catalog_once_on_missing_target():
    assert "missing_catalog_pairs = set(normal_requested_pairs) - known_catalog_pairs" in REMOTE
    assert "def _refresh_catalog_target_for_query(" in REMOTE
    assert "目录索引缺少所选模块，正在自动刷新" in REMOTE
    assert "log.plan.catalog_miss_auto_refresh" in REMOTE
    assert "cached_grouped = _refresh_catalog_target_for_query(" in REMOTE


def test_empty_final_results_never_hide_new_logs():
    assert 'if max(0, int(meta.get("result_count") or 0)) == 0:' in RESULT_CACHE
    assert "data_path.unlink(missing_ok=True)" in RESULT_CACHE
    assert "if cache_enabled and result_count > 0:" in VIEWS
    assert "log.result_cache.skip_empty" in VIEWS


def test_manual_catalog_refresh_invalidates_environment_result_cache():
    assert "def invalidate_environment(cls, environment_id: int)" in RESULT_CACHE
    assert "LogSearchResultCache.invalidate_environment(environment.id)" in VIEWS
    assert 'payload.setdefault("cache", {})["result_cache_invalidated"] = removed' in VIEWS
