from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_short_scene_and_result_cache_are_wired():
    urls = (ROOT / "backend/config/urls.py").read_text(encoding="utf-8")
    views = (ROOT / "backend/apps/logsources/views.py").read_text(encoding="utf-8")
    assert 'router.register("log-scenes"' in urls
    assert 'LogSearchResultCache.get(environment.id, request_payload)' in views
    assert 'X-TraceLens-Result-Cache' in views


def test_semantic_source_missing_is_quiet():
    service = (ROOT / "backend/apps/logsources/services/semantic_source.py").read_text(encoding="utf-8")
    views = (ROOT / "backend/apps/logsources/views.py").read_text(encoding="utf-8")
    assert 'raise SemanticSourceNotFound' not in service
    assert 'class SemanticSourceNotFound' not in service
    assert 'return None' in service
    assert 'semantic.source.api.not_found' not in views
