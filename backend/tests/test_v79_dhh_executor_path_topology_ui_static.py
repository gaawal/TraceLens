from pathlib import Path

PAGE = Path('frontend/src/components/PlatformSettingsPage.tsx').read_text(encoding='utf-8')


def test_dhh_executor_path_is_maintained_in_topology_section():
    topology = PAGE.index('上下位机拓扑与版本')
    dhh = PAGE.index('DHH 执行器日志根目录')
    log_paths = PAGE.index('日志类型与根目录模板')
    assert topology < dhh < log_paths
    assert '/data/sync/log/debug/elog/' in PAGE
    assert '不再继承上位机日志路径或用户名' in PAGE
