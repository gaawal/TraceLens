from pathlib import Path
import ast
import re

ROOT = Path(__file__).resolve().parents[1]


def test_v04_global_catalog_contracts():
    models = (ROOT / "apps/logsources/models.py").read_text(encoding="utf-8")
    remote = (ROOT / "apps/logsources/services/remote_logs.py").read_text(encoding="utf-8")
    views = (ROOT / "apps/logsources/views.py").read_text(encoding="utf-8")
    urls = (ROOT / "config/urls.py").read_text(encoding="utf-8")
    migration = (ROOT / "apps/logsources/migrations/0003_global_log_dictionary.py").read_text(encoding="utf-8")

    assert "class LogSubsystemDefinition" in models
    assert "class LogFmDefinition" in models
    assert "last_discovered_at" in models
    assert "def _merge_global_catalog" in remote
    assert "ignore_conflicts=True" in remote
    assert "catalog.items.all().delete()" not in remote
    assert "strategy=single_find_subsystem_module_dedupe" in remote
    assert '"global_catalog"' in remote
    assert "class LogSubsystemDefinitionViewSet" in views
    assert "class LogFmDefinitionViewSet" in views
    assert 'router.register("log-subsystems"' in urls
    assert 'router.register("log-fms"' in urls
    assert "seed_global_dictionary" in migration
    assert "LogResourceCatalogItem" in migration

    # 自动目录发现只接受不含数字和下划线的子系统/模块名，避免备份目录进入全局字典。
    assert "def _is_discovery_catalog_name_allowed" in remote
    assert 're.search(r"[\\d_]", name) is None' in remote
    assert "ignore_nonproduction_name" in remote

    tree = ast.parse(remote)
    helper = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_is_discovery_catalog_name_allowed"
    )
    namespace = {"re": re}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), "remote_logs.py", "exec"), namespace)
    allowed = namespace["_is_discovery_catalog_name_allowed"]
    assert allowed("RSRCP")
    assert allowed("RSSSD")
    assert not allowed("backup_1")
    assert not allowed("backup1")
    assert not allowed("RSRCP_20260905")
    assert not allowed("RS2")
    assert not allowed("FM_A")


if __name__ == "__main__":
    test_v04_global_catalog_contracts()
    print("v0.4 global catalog static contracts passed")
