from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_cancel_api_immediately_finishes_audit_and_cancel_wins_abort_race():
    service = (ROOT / 'apps/audits/services.py').read_text(encoding='utf-8')
    views = (ROOT / 'apps/logsources/views.py').read_text(encoding='utf-8')
    assert 'def cancel_running_log_search_audits(' in service
    assert 'result__in=[AuditResult.RUNNING, AuditResult.FAILED]' in service
    assert 'exclude(result=AuditResult.CANCELLED)' in service
    assert 'cancel_running_log_search_audits(' in views
    assert 'log.audit.cancel_failed' in views
