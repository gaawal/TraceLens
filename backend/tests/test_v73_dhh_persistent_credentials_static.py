from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_dhh_credentials_survive_topology_rescan():
    source = (ROOT / "backend/apps/environments/services/discovery.py").read_text(encoding="utf-8")
    assert 'if not machine.dhh_credentials_managed:' in source
    assert 'machine.username = "root"' in source
    assert 'machine.auth_type = "none"' in source
    assert 'machine.clear_credentials()' in source
    assert 'else:\n            _apply_lower_credentials(machine, settings_obj)' in source


def test_dhh_summary_exposes_credential_state_without_secret():
    source = (ROOT / "backend/apps/machines/serializers.py").read_text(encoding="utf-8")
    summary = source.split("class MachineSerializer", 1)[0]
    assert '"auth_type"' in summary
    assert '"has_credential"' in summary
    assert 'password' not in summary.lower()


def test_dhh_card_has_independent_editor():
    source = (ROOT / "frontend/src/components/EnvironmentResourcePage.tsx").read_text(encoding="utf-8")
    assert '编辑 DHH 资源' in source
    assert 'openDhhEdit' in source
    assert '默认用户名为 root' in source
    assert 'DHH IP 由 stations 自动识别' in source
    assert 'updateMachine(dhhEditDraft.machineId' in source


def test_dhh_edit_marks_credentials_as_managed():
    source = (ROOT / "backend/apps/machines/serializers.py").read_text(encoding="utf-8")
    assert 'dhh_connection_edited = is_dhh' in source
    assert 'instance.dhh_credentials_managed = True' in source
