from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_log_audit_no_result_and_client_result_contract():
    models = (ROOT / 'apps/audits/models.py').read_text(encoding='utf-8')
    views = (ROOT / 'apps/audits/views.py').read_text(encoding='utf-8')
    log_views = (ROOT / 'apps/logsources/views.py').read_text(encoding='utf-8')
    remote = (ROOT / 'apps/logsources/services/remote_logs.py').read_text(encoding='utf-8')
    assert 'NO_RESULT = "no_result", "无结果"' in models
    assert 'result_count = models.PositiveIntegerField' in models
    assert 'url_path="client-result"' in views
    assert 'AuditResult.NO_RESULT' in log_views
    assert '__TRACELENS_LOG_CATEGORY__=' in remote
    assert 'result_count=total_lines' in remote
    assert 'params.get("order") or "desc"' in views
    assert 'order_by("created_at", "id")' in views
    assert 'order_by("-created_at", "-id")' in views
