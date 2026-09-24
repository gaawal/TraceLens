from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_environment_status_and_version_use_mapped_topology_ip():
    views = (ROOT / "backend/apps/environments/views.py").read_text()
    discovery = (ROOT / "backend/apps/environments/services/discovery.py").read_text()
    assert "probe_machine = _runtime_probe_machine(relation)" in views
    assert "and not _is_atlog_dual_ip_relation(relation)" in views
    assert "mapped_host = str(metadata.get(\"topology_ip\") or \"\").strip()" in discovery
    assert 'setattr(probe_machine, "_tracelens_host_override", mapped_host)' in discovery
    assert "read_machine_software_version(probe_machine, settings_obj)" in discovery


def test_environment_preview_uses_mapping_for_status_decision():
    page = (ROOT / "frontend/src/components/EnvironmentResourcePage.tsx").read_text()
    assert "const smallNetwork = !logIp &&" in page
    assert "machine.station?.topology_ip" in page
