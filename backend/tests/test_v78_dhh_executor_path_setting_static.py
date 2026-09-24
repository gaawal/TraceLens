from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")

def test_dhh_executor_root_is_a_persisted_resource_setting():
    models = text("backend/apps/environments/models.py")
    serializers = text("backend/apps/environments/serializers.py")
    migration = text("backend/apps/environments/migrations/0011_resource_settings_dhh_executor_log_root.py")
    assert "dhh_executor_log_root" in models
    assert "/data/sync/log/debug/elog/" in models
    assert '"dhh_executor_log_root"' in serializers
    assert "/data/sync/log/debug/elog/" in migration

def test_dhh_executor_search_uses_dedicated_root_not_username_template():
    remote = text("backend/apps/logsources/services/remote_logs.py")
    assert "settings_obj.dhh_executor_log_root" in remote
    assert 'settings_obj.dhh_debug_log_root' in remote
    assert 'settings_obj.dhh_run_log_root' in remote

def test_frontend_exposes_dhh_executor_root_setting():
    api = text("frontend/src/api/resourceApi.ts")
    page = text("frontend/src/components/PlatformSettingsPage.tsx")
    assert "dhh_executor_log_root: string" in api
    assert "DHH 执行器日志根目录" in page
    assert "/data/sync/log/debug/elog/" in page
