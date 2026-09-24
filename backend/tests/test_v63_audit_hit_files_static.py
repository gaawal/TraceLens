from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_audit_persists_selected_plan_files_and_exposes_them():
    model = (ROOT / "backend/apps/audits/models.py").read_text(encoding="utf-8")
    serializer = (ROOT / "backend/apps/audits/serializers.py").read_text(encoding="utf-8")
    service = (ROOT / "backend/apps/audits/services.py").read_text(encoding="utf-8")
    view = (ROOT / "backend/apps/logsources/views.py").read_text(encoding="utf-8")
    assert 'matched_files = models.JSONField("命中文件"' in model
    assert '"matched_files"' in serializer
    assert "def record_log_search_artifacts" in service
    assert '"path": str(getattr(artifact, "path"' in service
    assert '"member": str(getattr(artifact, "member_name"' in service
    assert "record_log_search_artifacts(audit, plan)" in view
    assert "log.audit.files_failed" in view


def test_audit_navigation_is_short_and_table_lists_files():
    app = (ROOT / "frontend/src/App.tsx").read_text(encoding="utf-8")
    page = (ROOT / "frontend/src/components/LogAuditPage.tsx").read_text(encoding="utf-8")
    assert "<ClipboardList size={16} /> 审计</button>" in app
    assert "<ClipboardList size={16} /> 日志审计</button>" not in app
    assert "<th>命中文件</th>" in page
    assert "auditFileLabel" in page
    assert "auditFileTitle" in page
    assert "colSpan={13}" in page
