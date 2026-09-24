from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]


class AuditLogDiagnosticsStaticTest(unittest.TestCase):
    def test_backend_still_persists_diagnostics(self):
        model = (ROOT / "backend/apps/audits/models.py").read_text(encoding="utf-8")
        serializer = (ROOT / "backend/apps/audits/serializers.py").read_text(encoding="utf-8")
        services = (ROOT / "backend/apps/audits/services.py").read_text(encoding="utf-8")
        self.assertIn('diagnostics = models.JSONField("日志定位诊断"', model)
        self.assertIn('"diagnostics"', serializer)
        self.assertIn('"file_selected_no_rows"', services)
        self.assertIn('"no_candidate_file"', services)
        self.assertIn('record_log_search_artifacts', services)
        self.assertIn('record_log_search_cache_hit', services)

    def test_frontend_detail_is_matched_file_only(self):
        page = (ROOT / "frontend/src/components/LogAuditPage.tsx").read_text(encoding="utf-8")
        api = (ROOT / "frontend/src/api/resourceApi.ts").read_text(encoding="utf-8")
        css = (ROOT / "frontend/src/styles.css").read_text(encoding="utf-8")
        self.assertIn('function AuditMatchedFilesPanel', page)
        self.assertIn('<strong>命中文件</strong>', page)
        self.assertIn('文件时间：{auditFileTimeLabel(file)}', page)
        self.assertNotIn('日志定位诊断', page)
        self.assertNotIn('<strong>查询来源</strong>', page)
        self.assertNotIn('<strong>查询条件</strong>', page)
        self.assertNotIn('<strong>候选文件</strong>', page)
        self.assertNotIn('<strong>时间过滤</strong>', page)
        self.assertIn('audit-detail-row', page)
        self.assertIn('LogAuditDiagnostics', api)
        self.assertIn('.audit-detail-file-row', css)


if __name__ == "__main__":
    unittest.main()
