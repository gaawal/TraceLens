from pathlib import Path


def test_log_format_rule_route_and_parser_are_packaged():
    backend = Path(__file__).resolve().parents[1]
    urls = (backend / "config" / "urls.py").read_text(encoding="utf-8")
    assert 'router.register("log-format-rules", LogFormatParserRuleViewSet' in urls
    assert (backend / "apps" / "logsources" / "services" / "log_format_parser.py").is_file()
    assert (backend / "apps" / "logsources" / "migrations" / "0007_log_format_parser_rules.py").is_file()


def test_log_format_rule_frontend_is_packaged():
    project = Path(__file__).resolve().parents[2]
    panel = project / "frontend" / "src" / "components" / "LogFormatRulesPanel.tsx"
    api = project / "frontend" / "src" / "api" / "resourceApi.ts"
    assert panel.is_file()
    assert 'log-format-rules' in api.read_text(encoding="utf-8")
