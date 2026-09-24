from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding='utf-8')


def test_executor_modules_are_typed_and_discovered_below_ip_subsystem():
    models = read('backend/apps/logsources/models.py')
    remote = read('backend/apps/logsources/services/remote_logs.py')
    index = read('backend/apps/logsources/services/file_index.py')
    panel = read('frontend/src/components/RemoteLogQueryPanel.tsx')

    assert 'class LogModuleKind' in models
    assert 'EXECUTOR = "executor", "执行器模块"' in models
    assert 'environment.machine_relations.select_related("target_machine")' in remote
    assert 'find {quoted_roots} -mindepth 2 -maxdepth 2' in remote
    assert 'if lower_ip not in executor_lower_hosts' in remote
    assert '_executor_module_name(filename' in remote
    assert 'modules_by_subsystem=executor_target_map' in remote
    assert 'module_name not in modules_by_subsystem.get(subsystem, set())' in index
    assert "fm.kind === 'executor' ? '（执行器）' : ''" in panel
    assert "kind: rawKind === 'executor' ? 'executor' : 'normal'" in panel
    assert "setSelectedTargets(new Set(next))" in panel
    assert "if (!previous.has('executor') && next.has('executor'))" not in panel
    assert "{ label: '3分钟', seconds: 3 * 60 }" in panel
    assert "disabled={!isExpanded}" in panel
    assert "if (isExpanded) toggleSubsystem(subsystem)" in panel
    assert "请先展开该子系统，再选择全部模块" in panel


def test_legacy_elog_subsystem_is_cleaned_by_migration():
    migration = read('backend/apps/logsources/migrations/0005_executor_module_kind.py')
    assert 'source_category="executor"' in migration
    assert 'subsystem__iexact="elog"' in migration
    assert 'name__iexact="elog"' in migration
