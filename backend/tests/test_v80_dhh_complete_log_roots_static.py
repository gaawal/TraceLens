from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_dhh_has_three_persisted_dedicated_log_roots():
    models = text("backend/apps/environments/models.py")
    serializers = text("backend/apps/environments/serializers.py")
    migration = text("backend/apps/environments/migrations/0012_resource_settings_dhh_debug_run_roots.py")
    for field, value in (
        ("dhh_debug_log_root", "/data/sync/log/debug/"),
        ("dhh_executor_log_root", "/data/sync/log/debug/elog/"),
        ("dhh_run_log_root", "/data/sync/log/run/"),
    ):
        assert field in models
        assert field in serializers
        assert value in models
    assert "dhh_debug_log_root" in migration
    assert "dhh_run_log_root" in migration


def test_dhh_query_prefers_upper_and_uses_dhh_as_missing_module_fallback():
    remote = text("backend/apps/logsources/services/remote_logs.py")
    assert "def _dhh_root_for_profile" in remote
    assert "settings_obj.dhh_debug_log_root" in remote
    assert "settings_obj.dhh_executor_log_root" in remote
    assert "settings_obj.dhh_run_log_root" in remote
    assert "_LOG_ROOT_ROUTING_UPPER_FIRST" in remote
    assert "_LOG_ROOT_ROUTING_DHH_FALLBACK" in remote
    assert "log.plan.dhh.upper_first" in remote
    assert "_missing_dhh_fallback_request" in remote
    assert "reason=upper_has_requested_logs" in remote
    assert "reason=small_network" in remote


def test_topology_settings_show_all_three_dhh_roots():
    api = text("frontend/src/api/resourceApi.ts")
    page = text("frontend/src/components/PlatformSettingsPage.tsx")
    for field in ("dhh_debug_log_root", "dhh_executor_log_root", "dhh_run_log_root"):
        assert f"{field}: string" in api
    assert "DHH 调试日志根目录" in page
    assert "DHH 执行器日志根目录" in page
    assert "DHH 运行日志根目录" in page
    assert "/data/sync/log/debug/" in page
    assert "/data/sync/log/debug/elog/" in page
    assert "/data/sync/log/run/" in page
    assert "优先使用上位机路径规则" in page
    assert "调试日志只要上位机在当前时间窗找到候选日志，就不再查询 DHH" in page
    assert "192.* 小网跳过 DHH 回退" in page
