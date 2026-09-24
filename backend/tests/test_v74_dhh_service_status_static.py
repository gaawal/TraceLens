from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VIEWS = (ROOT / "backend/apps/environments/views.py").read_text(encoding="utf-8")
RESOURCE_API = (ROOT / "frontend/src/api/resourceApi.ts").read_text(encoding="utf-8")
RESOURCE_PAGE = (ROOT / "frontend/src/components/EnvironmentResourcePage.tsx").read_text(encoding="utf-8")


def test_dhh_uses_dedicated_java_process_probe():
    assert "java -jar /home/root/SW/bin/sw/LDSEquipmentDataProcessService.jar" in VIEWS
    assert "DHH_PROCESS_PROBE_COMMAND" in VIEWS
    assert 'service_running = result.exit_status == 0' in VIEWS


def test_dhh_is_not_probed_as_tb_simulator_lower():
    assert 'normal_relations = [relation for relation in relations if not _is_dhh_relation(relation)]' in VIEWS
    assert 'dhh_relation = next((relation for relation in relations if _is_dhh_relation(relation)), None)' in VIEWS


def test_small_network_dhh_is_skipped():
    assert '_small_network_dhh_runtime' in VIEWS
    assert 'environment.runtime_status.dhh_skipped' in VIEWS


def test_frontend_uses_service_running_for_dhh_status():
    assert 'service_running: boolean' in RESOURCE_API
    assert 'dhh.service_running' in RESOURCE_PAGE
    assert "dhh.service_running ? '运行中' : '未运行'" in RESOURCE_PAGE
