from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_v05_catalog_efficiency_contracts():
    remote = (ROOT / "apps/logsources/services/remote_logs.py").read_text(encoding="utf-8")

    # 目录发现只执行一次远程 find，进入子系统一级并从当前/时间戳 .log 文件名提取模块。
    assert "-mindepth 2 -maxdepth 2 -type f" in remote
    assert "-name '*.log' -print" in remote
    assert "def _discover_log_modules" in remote
    assert "_normal_module_name(filename, direct_rules, reference)" in remote
    assert "tar -tzf" not in remote[remote.index("def _discover_log_modules"):remote.index("def _global_catalog_tree")]

    # 普通缓存读取不应反向重建全局字典；只有真实扫描才追加。
    assert remote.count("_merge_global_catalog(grouped, module_kind=module_kind)") >= 1
    assert "管理员在配置页的删除/禁用操作" in remote
    assert "catalog.status == LogCatalogStatus.SUCCESS or has_cached_items" in remote

    # 日志文件计划优先使用数据库目录，避免重新遍历根目录全部子系统。
    assert "def _cached_subsystems_for_target" in remote
    assert "strategy=fingerprint_index" in remote
    assert "fallback=single_root_listing" in remote
    assert "_cached_fms_by_subsystem" in remote

    # 扫描和环境缓存都只追加，不自动删除已有目录项。
    assert "ignore_conflicts=True" in remote
    assert "catalog.items.all().delete()" not in remote


if __name__ == "__main__":
    test_v05_catalog_efficiency_contracts()
    print("v0.5 catalog efficiency static contracts passed")
