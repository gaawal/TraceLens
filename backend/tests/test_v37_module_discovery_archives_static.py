from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_module_discovery_accepts_current_rotated_and_daily_archive_names():
    source = (ROOT / "apps/logsources/services/remote_logs.py").read_text(encoding="utf-8")
    assert "def _normal_module_name" in source
    assert 'basename.endswith(".tar.gz")' in source
    assert 'parse_archive_timestamp(match.group("stamp"), reference)' in source
    assert "name_terms.append(\"-name '*.tar.gz'\")" in source
    assert 'fm = _normal_module_name(filename, direct_rules, reference)' in source
    assert "tar -tzf" not in source[source.index("def _normal_module_name"):source.index("def _discover_log_modules")]


def test_scan_failure_state_is_preserved_for_frontend():
    source = (ROOT / "apps/logsources/services/remote_logs.py").read_text(encoding="utf-8")
    assert '"scan_status": catalog.status' in source
    assert 'cache_state="stale" if has_cache else "error"' in source
