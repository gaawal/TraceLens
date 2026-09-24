from pathlib import Path

SOURCE = Path(__file__).parents[1] / "apps" / "logsources" / "services" / "remote_logs.py"
TEXT = SOURCE.read_text(encoding="utf-8")


def test_dhh_log_paths_are_independent_from_ssh_username():
    assert 'def _dhh_root_for_profile' in TEXT
    assert 'settings_obj.dhh_debug_log_root' in TEXT
    assert 'settings_obj.dhh_executor_log_root' in TEXT
    assert 'settings_obj.dhh_run_log_root' in TEXT


def test_dhh_ssh_username_is_not_hardcoded_for_connection():
    # The override is only for path expansion; ssh_session still receives the DHH machine.
    assert 'with ssh_session(target.machine) as lease:' in TEXT
