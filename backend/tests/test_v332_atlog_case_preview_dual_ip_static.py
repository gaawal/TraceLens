from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_atlog_dual_ip_environment_data_is_exposed_to_case_preview():
    api = (ROOT.parent / "frontend/src/api/resourceApi.ts").read_text(encoding="utf-8")
    resource = (ROOT.parent / "frontend/src/components/EnvironmentResourcePage.tsx").read_text(encoding="utf-8")
    assert "topology_ip: string" in api
    assert "ssh_host: string" in api
    assert "station?.topology_ip" in resource
    assert "业务IP：{machine.host}" in resource
    assert "大网IP：{logIp}" in resource


def test_atlog_log_reader_uses_topology_ip_without_changing_login_identity():
    logs = (ROOT / "apps/logsources/services/remote_logs.py").read_text(encoding="utf-8")
    ssh = (ROOT / "apps/common/services/ssh.py").read_text(encoding="utf-8")
    env = (ROOT / "apps/environments/views.py").read_text(encoding="utf-8")
    assert "metadata.get(\"topology_ip\")" in logs
    assert "_tracelens_host_override" in logs
    assert "_effective_machine_host" in ssh
    assert "_tracelens_host_override" in ssh
    assert "_is_atlog_dual_ip_relation" in env
    assert "_skip_lower_ssh_probe(relation)" in env
    assert "_runtime_probe_machine(relation)" in env
    assert "probe_machine = _runtime_probe_machine(relation)" in env


def test_atlog_runtime_status_uses_reachable_topology_ip_and_station_xml_keeps_mapping():
    env = (ROOT / "apps/environments/views.py").read_text(encoding="utf-8")
    discovery = (ROOT / "apps/environments/services/discovery.py").read_text(encoding="utf-8")
    assert "_tracelens_host_override" in env
    assert '"stations_xml_ip": station.host' in discovery
    assert '"source": "atlog"' in discovery
    assert '"topology_ip": str((atlog_relation.metadata or {}).get("topology_ip") or "")' in discovery
