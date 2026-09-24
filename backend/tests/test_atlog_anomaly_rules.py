from apps.atlog.anomaly_rules import matched_rule_keywords, normalize_anomaly_rules


def test_anomaly_rule_matching_preserves_ui_semantics():
    rules = normalize_anomaly_rules([
        {"keyword": "MyError", "caseSensitive": True, "wholeWord": True, "enabled": True},
        {"keyword": "timeout", "case_sensitive": False, "whole_word": False, "enabled": True},
        {"keyword": "ignored", "enabled": False},
    ])
    assert matched_rule_keywords("prefix MyError suffix", rules) == ["MyError"]
    assert matched_rule_keywords("prefix myerror suffix", rules) == []
    assert matched_rule_keywords("request TIMEOUT after 3s", rules) == ["timeout"]
    assert matched_rule_keywords("MyErrorCode", rules) == []


def test_empty_rules_do_not_fall_back_to_built_in_keywords():
    assert matched_rule_keywords("ERROR FATAL timeout exception", []) == []


def test_query_case_logs_filters_debug_rows_by_anomaly_rules(monkeypatch):
    from apps.atlog import services

    monkeypatch.setattr(services, "normalize_base_url", lambda value: "http://example/ATLog_root/case/")
    monkeypatch.setattr(services, "discover_case_log_files_for_query", lambda base, **kwargs: [
        {
            "relative_path": "full_logs/log/debug/SUB/MOD/current.log",
            "component": "MOD",
            "subsystem": "SUB",
            "module": "MOD",
        }
    ])
    monkeypatch.setattr(services, "discover_case_log_catalog", lambda base: [{"subsystem": "SUB", "modules": ["MOD"]}])
    monkeypatch.setattr(services, "_safe_child_url", lambda base, relative: base + relative)
    from apps.logsources.services.unified_search import UrlWindowResult

    monkeypatch.setattr(
        services,
        "read_url_log_window",
        lambda artifact, start, end, operation_id="-": UrlWindowResult(
            "[2026-09-08 10:00:01.000] [MOD] [INFO] normal line\n"
            "[2026-09-08 10:00:02.000] [MOD] [INFO] USER_DEFINED_BAD happened\n"
            "[2026-09-08 10:00:03.000] [MOD] [ERROR] built in looking error but not configured\n",
            False,
            "range-binary-seek",
            2048,
        ),
    )

    result = services.query_case_logs(
        "http://example/ATLog_root/case/",
        start_time="2026-09-08 10:00:00",
        end_time="2026-09-08 10:00:05",
        targets=[{"subsystem": "SUB", "module": "MOD"}],
        anomaly_rules=[{"keyword": "USER_DEFINED_BAD", "case_sensitive": True, "whole_word": False, "enabled": True}],
        include_event=False,
        max_lines=100,
    )
    assert result["count"] == 1
    assert result["rows"][0]["message"] == "USER_DEFINED_BAD happened"
    assert result["rows"][0]["matched_anomaly_rules"] == ["USER_DEFINED_BAD"]
