from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from apps.atlog.services import (
    _ancestor_case_urls,
    _case_log_hierarchy,
    _case_log_kind,
    _event_row,
    parse_case_html_excerpt,
    parse_pytest_html_excerpt,
    parse_pytest_xml,
    parse_summary_report,
    parse_xytest_errors,
    describe_assertion,
    parse_environment_report_html,
    _atlog_environment_host,
    _public_environment_node,
    query_case_logs,
    normalize_base_url,
)




def test_case_log_hierarchy_uses_debug_subsystem_then_module():
    assert _case_log_hierarchy(
        "full_logs/log/debug/spm/RSPMCPD/RSPMCPD_20260908.log"
    ) == ("spm", "RSPMCPD")
    assert _case_log_hierarchy(
        "full_logs/log/debug/ws/WS.log"
    ) == ("ws", "WS")

def test_case_executor_hierarchy_is_elog_ip_subsystem_file_only():
    path = "full_logs/log/elog/192.3.4.41/rspm/RSPM_cp_xx.log"
    assert _case_log_hierarchy(path) == ("rspm", "RSPM")
    assert _case_log_kind(path) == "executor"
    # A directory literally named executor is not a physical execution-log alias.
    assert _case_log_kind("full_logs/log/executor/rspm/RSPM.log") == "debug"


def test_case_log_discovery_uses_debug_and_elog_ip_roots_only(monkeypatch):
    import apps.atlog.services as service

    base = "http://10.29.108.45/task_x/block_y/SN_CASE/"
    directory_map = {
        base + "full_logs/log/debug/": ["rspm/"],
        base + "full_logs/log/debug/rspm/": ["RSPM.log"],
        base + "full_logs/log/elog/": ["192.3.4.41/", "not-an-ip/", "executor/"],
        base + "full_logs/log/elog/192.3.4.41/": ["rspm/"],
        base + "full_logs/log/elog/192.3.4.41/rspm/": ["RSPM_cp_xx.log"],
    }
    service._discover_case_log_files_cached.cache_clear()
    monkeypatch.setattr(service, "list_directory", lambda url: directory_map.get(url, []))
    try:
        discovered = service.discover_case_log_files(base)
    finally:
        service._discover_case_log_files_cached.cache_clear()

    paths = {item["relative_path"] for item in discovered}
    assert "full_logs/log/debug/rspm/RSPM.log" in paths
    assert "full_logs/log/elog/192.3.4.41/rspm/RSPM_cp_xx.log" in paths
    assert not any("/executor/" in path for path in paths)
    executor = next(item for item in discovered if item["kind"] == "executor")
    assert executor["subsystem"] == "rspm"
    assert executor["module"] == "RSPM"


def test_case_log_discovery_treats_debug_elog_ip_as_routing_not_module(monkeypatch):
    import apps.atlog.services as service

    base = "http://10.29.108.45/task_x/block_y/SN_CASE/"
    directory_map = {
        base + "full_logs/log/debug/": ["elog/", "spm/"],
        base + "full_logs/log/debug/spm/": ["WPOS.log"],
        base + "full_logs/log/debug/elog/": ["192.3.4.41/"],
        base + "full_logs/log/debug/elog/192.3.4.41/": ["rspm/"],
        base + "full_logs/log/debug/elog/192.3.4.41/rspm/": ["RSPM_cp_xx.log"],
    }
    service._discover_case_log_files_cached.cache_clear()
    monkeypatch.setattr(service, "list_directory", lambda url: directory_map.get(url, []))
    try:
        discovered = service.discover_case_log_files(base)
    finally:
        service._discover_case_log_files_cached.cache_clear()

    assert {item["subsystem"] for item in discovered} == {"spm", "rspm"}
    assert {item["module"] for item in discovered} == {"WPOS", "RSPM"}
    assert all(item["subsystem"] != "elog" for item in discovered)
    assert all(item["module"] != "192.3.4.41" for item in discovered)
    executor = next(item for item in discovered if item["kind"] == "executor")
    assert executor["relative_path"] == "full_logs/log/debug/elog/192.3.4.41/rspm/RSPM_cp_xx.log"


def test_summary_report_prefers_matching_failed_case():
    xml = '''<testsuite tests="2" failures="1" errors="0" ignored="0" starttime="2026-08-15 19:48:38" endtime="2026-08-15 19:50:14">
      <testcase classname="pytest" name="SN_OTHER"><failure message="AssertionError: other">other body</failure></testcase>
      <testcase classname="SN_SPM_RSPMCPD_DSPRSAR_MalFunc_001" name="test_case"><failure message="AssertionError: expect OK, real FAILED">body</failure></testcase>
    </testsuite>'''
    parsed = parse_summary_report(xml, 'SN_SPM_RSPMCPD_DSPRSAR_MalFunc_001')
    assert parsed['status'] == 'failed'
    assert parsed['failure']['failure_message'] == 'AssertionError: expect OK, real FAILED'
    assert parsed['start_time'] == '2026-08-15 19:48:38'


def test_pytest_xml_extracts_location_and_call_chain():
    xml = '''<testsuite><testcase classname="SN_SPM_RSPMCPD_DSPRSAR_MalFunc_001" name="test_demo">
      <failure message="AssertionError: expect ResultStatus.OK, real ResultStatus.FAILED">/tmp/run_case.py:121: in run_cpd
/tmp/cpd_runner.py:444: in result_check
/tmp/util_std.py:54: in assert_equal
E AssertionError: expect ResultStatus.OK, real ResultStatus.FAILED</failure>
    </testcase></testsuite>'''
    parsed = parse_pytest_xml(xml, 'SN_SPM_RSPMCPD_DSPRSAR_MalFunc_001')
    assert parsed['failure_message'].startswith('AssertionError')
    assert parsed['location']['file'].endswith('util_std.py')
    assert parsed['location']['line'] == 54
    assert [row['function'] for row in parsed['call_chain']] == ['run_cpd', 'result_check', 'assert_equal']


def test_xytest_keeps_assertion_error_and_timestamp():
    text = '''[2026-08-15 19:49:37.100] [INFO] [T-1] hello
[2026-08-15 19:49:38.650] [ERROR] [T-29341] [cpd_runner.py:311] [AssertionError] expect: ResultStatus.OK, real: ResultStatus.FAILED, caller: result_check
'''
    rows = parse_xytest_errors(text, '2026-08-15 19:48:38', '2026-08-15 19:50:14')
    assert len(rows) == 1
    assert rows[0]['time'] == '2026-08-15 19:49:38.650'
    assert rows[0]['conclusion'].startswith('expect: ResultStatus.OK')


def test_pytest_html_extracts_second_result_row_text():
    html = '''<table id="results-table"><tbody>
      <tr><td>header</td></tr>
      <tr><td><div>line one<br/>line two <span>AssertionError</span></div></td></tr>
    </tbody></table>'''
    text = parse_pytest_html_excerpt(html)
    assert 'line one' in text
    assert 'line two AssertionError' in text


def test_event_row_detects_level_when_event_category_precedes_level():
    line = '[2026-08-15 19:49:38.650] [RSPM] [123] [file.cpp:10] [EVT] [ERROR] [E1] [D1] broken'
    row = _event_row(line, 1)
    assert row['component'] == 'RSPM'
    assert row['level'] == 'ERROR'
    assert row['message'] == 'broken'

def test_summary_report_deduplicates_same_failure_recorded_twice():
    xml = '''<testsuite tests="2" failures="2" errors="0">
      <testcase classname="pytest" name="test_demo"><failure message="AssertionError: same">body</failure></testcase>
      <testcase classname="SN_CASE" name="test_demo"><failure message="AssertionError: same">body</failure></testcase>
    </testsuite>'''
    parsed = parse_summary_report(xml, 'SN_CASE')
    assert parsed['reported_failures'] == 2
    assert parsed['failures'] == 1


def test_ancestor_case_urls_include_case_block_and_task():
    url = 'http://127.0.0.1/ATLog_root/task_123/block_456_1/SN_CASE_001/'
    values = _ancestor_case_urls(url)
    assert values[0].endswith('/SN_CASE_001/')
    assert values[1].endswith('/block_456_1/')
    assert values[2].endswith('/task_123/')


def test_pytest_html_prefers_current_case_over_placeholder_and_pytest_wrapper():
    html = '''<table id="results-table"><thead><tr><th>Result</th></tr></thead><tbody>
      <tr><td><div>No results found. Try to check the filters</div></td></tr>
      <tr class="result pytest"><td>pytest FAILED</td></tr>
      <tr class="detail"><td><div>pytest wrapper AssertionError</div></td></tr>
      <tr class="result case-sn_spm_rspmcpd_csprslp_func_001"><td>test_sn_spm_rspmcpd_csprslp_func_001 FAILED</td></tr>
      <tr class="detail"><td><div>real case log line 1<br/>AssertionError: real cause</div></td></tr>
    </tbody></table>'''
    text = parse_pytest_html_excerpt(html, 'SN_SPM_RSPMCPD_CSPRSLP_Func_001')
    assert 'real case log line 1' in text
    assert 'AssertionError: real cause' in text
    assert 'No results found' not in text
    assert 'pytest wrapper AssertionError' not in text


def test_summary_report_ignores_pytest_wrapper_when_case_record_exists():
    xml = '''<testsuite tests="2" failures="2" errors="0">
      <testcase classname="pytest" name="test_sn_spm_rspmcpd_csprslp_func_001"><failure message="AssertionError: wrapper">wrapper</failure></testcase>
      <testcase classname="SN_SPM_RSPMCPD_CSPRSLP_Func_001" name="test_sn_spm_rspmcpd_csprslp_func_001"><failure message="AssertionError: real">real body</failure></testcase>
    </testsuite>'''
    parsed = parse_summary_report(xml, 'SN_SPM_RSPMCPD_CSPRSLP_Func_001')
    assert parsed['tests'] == 1
    assert parsed['failures'] == 1
    assert parsed['failure']['failure_message'] == 'AssertionError: real'
    assert parsed['reported_tests'] == 2


def test_failures_report_excerpt_focuses_current_case():
    html = '''<html><body><h2>pytest</h2><div>pytest wrapper failure</div>
      <h2>SN_SPM_RSPMCPD_CSPRSLP_Func_001</h2><div>real failure detail</div>
      <h2>SN_OTHER_001</h2><div>other failure</div></body></html>'''
    text = parse_case_html_excerpt(html, 'SN_SPM_RSPMCPD_CSPRSLP_Func_001')
    assert 'real failure detail' in text
    assert 'other failure' not in text


def test_describe_assertion_translates_expect_real_to_chinese():
    parsed = describe_assertion('[AssertionError] expect: 5, real: 2, expect to be equal, caller: abnormal_pmls_check, error msg: WSPMSSDI业务状态')
    assert parsed['summary'] == 'WSPMSSDI业务状态：期望为 5，但实际为 2。'
    assert parsed['category'] == 'WSPMSSDI业务状态'
    assert parsed['caller'] == 'abnormal_pmls_check'
    assert parsed['expect'] == '5'
    assert parsed['real'] == '2'


def test_describe_assertion_keeps_unrecognized_text():
    parsed = describe_assertion('[AssertionError] device returned a vendor-specific invalid state')
    assert parsed['summary'] == 'device returned a vendor-specific invalid state'
    assert parsed['category'] == 'device returned a vendor-specific invalid state'


def test_describe_assertion_does_not_use_domain_keyword_taxonomy():
    parsed = describe_assertion('[AssertionError] expect: READY, real: INIT, expect to be equal, caller: boot_check, error msg: 设备初始化状态')
    assert parsed['category'] == '设备初始化状态'
    assert parsed['summary'] == '设备初始化状态：期望为 READY，但实际为 INIT。'
    assert '初始化/启动异常' not in parsed.values()


def test_describe_assertion_uses_exception_type_when_no_reason_text():
    parsed = describe_assertion('KeyError: missing_key')
    assert parsed['category'] == 'KeyError'


def test_parse_environment_report_html_extracts_topology_and_ssh_business_ips():
    html = """<h2>环境信息[9GPB]</h2>
    <p>环境TOPO: TB1.0_9GPB, SIM模式: sim0_sil</p>
    <h3>上位机SCH(L4-1): 10.29.108.21</h3><p>TICC-Agent: 10.29.108.21</p><p>root@192.4.4.48 dummy-upper-pass</p>
    <h3>数据服务器DHH(L4-2): 10.29.108.22</h3><p>root@192.4.4.80 dummy-dhh-pass</p>
    <h3>下位机</h3><h4>L3-SPUR_SUBRACK1_SLOT1_GPB: 10.29.108.11</h4><p><b>业务IP</b>: root@192.3.4.41, dummy-gpb-pass</p>"""
    parsed = parse_environment_report_html(html)
    assert parsed['environment_label'] == '9GPB'
    assert parsed['topology'] == 'TB1.0_9GPB'
    assert parsed['sim_mode'] == 'sim0_sil'
    assert parsed['upper']['topology_ip'] == '10.29.108.21'
    assert parsed['upper']['ssh_host'] == '192.4.4.48'
    assert parsed['upper']['username'] == 'root'
    assert parsed['upper']['password'] == 'dummy-upper-pass'
    assert parsed['dhh']['ssh_host'] == '192.4.4.80'
    assert parsed['lowers'][0]['topology_ip'] == '10.29.108.11'
    assert parsed['lowers'][0]['ssh_host'] == '192.3.4.41'
    assert parsed['lowers'][0]['password'] == 'dummy-gpb-pass'



def test_atlog_environment_host_uses_big_network_topology_ip_for_l4_nodes():
    upper = {'label': '上位机SCH(L4-1)', 'topology_ip': '10.29.108.21', 'ssh_host': '192.4.4.48', 'username': 'root', 'password': 'dummy'}
    dhh = {'label': 'DHH', 'topology_ip': '10.29.108.22', 'ssh_host': '192.4.4.80', 'username': 'root', 'password': 'dummy'}
    lower = {'label': 'L3-SPUR_SUBRACK1_SLOT1_GPB', 'topology_ip': '10.29.108.11', 'ssh_host': '192.3.4.41', 'username': 'root', 'password': 'dummy'}

    assert _atlog_environment_host(upper, 'upper') == '10.29.108.21'
    assert _atlog_environment_host(dhh, 'lower', station_type='DHH') == '10.29.108.22'
    assert _atlog_environment_host(lower, 'lower') == '192.3.4.41'

    public_upper = _public_environment_node(upper, role='upper')
    public_dhh = _public_environment_node(dhh, role='lower', station_type='DHH')
    public_lower = _public_environment_node(lower, role='lower')
    assert public_upper['environment_host'] == '10.29.108.21'
    assert public_upper['ssh_host'] == '192.4.4.48'
    assert public_dhh['environment_host'] == '10.29.108.22'
    assert public_lower['environment_host'] == '192.3.4.41'




def test_atlog_auto_machine_name_uses_resolved_host_ip_in_source():
    source = Path(__file__).resolve().parents[1].joinpath('apps/atlog/services.py').read_text(encoding='utf-8')
    assert 'name=host,' in source
    assert 'machine.name = host' in source
    assert 'startswith("ATLog 自动化用例环境")' in source

def test_query_case_logs_merges_event_and_selected_full_log(monkeypatch):
    import apps.atlog.services as service

    monkeypatch.setattr(service, 'query_event_log', lambda *args, **kwargs: {
        'components': ['RSPM'],
        'truncated': False,
        'rows': [{
            'line_number': 1,
            'time': '2026-08-15 19:49:38.650',
            'component': 'RSPM',
            'level': 'ERROR',
            'source': 'event.cpp:1',
            'message': 'event broken',
            'raw': '[2026-08-15 19:49:38.650] [RSPM] [1] [event.cpp:1] [EVT] [ERROR] [E1] broken',
        }],
    })
    monkeypatch.setattr(service, 'discover_case_log_files_for_query', lambda base, **kwargs: [
        {'relative_path': 'full_logs/log/debug/rspm/RSPM.log', 'component': 'RSPM', 'subsystem': 'rspm', 'module': 'RSPM', 'kind': 'debug'},
    ])
    monkeypatch.setattr(service, 'discover_case_log_catalog', lambda base: [
        {'subsystem': 'rspm', 'modules': ['RSPM']},
        {'subsystem': 'ws', 'modules': ['WS']},
    ])
    from apps.logsources.services.unified_search import UrlWindowResult
    monkeypatch.setattr(service, 'read_url_log_window', lambda artifact, start, end, operation_id='-': UrlWindowResult('[2026-08-15 19:49:39.000] [ERROR] full log broken\n', False, 'range-binary-seek', 1024))
    result = query_case_logs(
        'http://10.29.108.45/ATLog_root/task_x/block_y/SN_CASE/',
        start_time='2026-08-15 19:49:30',
        end_time='2026-08-15 19:50:00',
        targets=[{'subsystem': 'rspm', 'module': 'RSPM'}],
    )
    assert result['count'] == 2
    assert result['rows'][0]['source_kind'] == 'event'
    assert result['rows'][1]['source_kind'] == 'full'
    assert 'RSPM' in result['components']
    assert 'WS' not in result['components']
    assert {'subsystem': 'rspm', 'modules': ['RSPM']} in result['log_catalog']
    assert {'subsystem': 'ws', 'modules': ['WS']} in result['log_catalog']
    assert result['event_raw_text'].startswith('[2026-08-15 19:49:38.650]')


def test_analyze_case_probes_environment_html_even_when_autoindex_omits_chinese_filename(monkeypatch):
    import apps.atlog.services as service

    base = 'http://10.29.108.21/ATLog_root/task_3915251436923912194/block_3915251505643913440_1/SPM_WSPMDiagnose_LightSource_Malf_002/'
    env_url = base + '%E8%AF%A6%E7%BB%86%E6%97%A5%E5%BF%97%E9%93%BE%E6%8E%A5.html'
    html = '''<h2>环境信息[9GPB]</h2>
    <p>环境TOPO: TB1.0_9GPB, SIM模式: sim0_sil</p>
    <h3>上位机SCH(L4-1): 10.29.108.21</h3>
    <p>TICC-Agent: 10.29.108.21</p>
    <p>root@192.4.4.48 dummy-upper</p>
    <h3>数据服务器DHH(L4-2): 10.29.108.22</h3>
    <p>root@192.4.4.80 dummy-dhh</p>
    <h3>下位机</h3>
    <h4>L3-SPUR_SUBRACK1_SLOT1_GPB: 10.29.108.11</h4>
    <p><b>业务IP</b>: root@192.3.4.41, dummy-gpb</p>'''

    monkeypatch.setattr(service, 'list_directory', lambda url: ['event.log'])

    def fake_fetch(url, max_bytes=0):
        if url == env_url:
            return html
        raise FileNotFoundError(url)

    monkeypatch.setattr(service, 'fetch_text', fake_fetch)
    monkeypatch.setattr(service, 'ensure_atlog_environment', lambda info, report_base: service._public_environment_info(info))

    result = service.analyze_case(base)
    assert result['links']['详细日志链接.html'] == env_url
    assert result['environment']['upper']['topology_ip'] == '10.29.108.21'
    assert result['environment']['upper']['environment_host'] == '10.29.108.21'
    assert result['environment']['dhh']['topology_ip'] == '10.29.108.22'
    assert result['environment']['lowers'][0]['topology_ip'] == '10.29.108.11'


def test_direct_environment_html_url_normalizes_back_to_case_directory():
    direct = 'http://10.29.108.21/ATLog_root/task_3915251436923912194/block_3915251505643913440_1/SPM_WSPMDiagnose_LightSource_Malf_002/%E8%AF%A6%E7%BB%86%E6%97%A5%E5%BF%97%E9%93%BE%E6%8E%A5.html'
    expected = 'http://10.29.108.21/ATLog_root/task_3915251436923912194/block_3915251505643913440_1/SPM_WSPMDiagnose_LightSource_Malf_002/'
    assert normalize_base_url(direct) == expected


def test_query_case_logs_source_categories_can_disable_event_and_full_logs(monkeypatch):
    import apps.atlog.services as service

    calls = {'event': 0, 'read': 0}
    monkeypatch.setattr(service, 'query_event_log', lambda *args, **kwargs: calls.__setitem__('event', calls['event'] + 1) or {'components': [], 'truncated': False, 'rows': []})
    monkeypatch.setattr(service, 'discover_case_log_files_for_query', lambda base, **kwargs: [
        {'relative_path': 'full_logs/log/debug/rspm/RSPM.log', 'component': 'RSPM', 'subsystem': 'rspm', 'module': 'RSPM', 'kind': 'debug'},
    ])
    monkeypatch.setattr(service, 'discover_case_log_catalog', lambda base: [
        {'subsystem': 'rspm', 'modules': ['RSPM']},
    ])
    monkeypatch.setattr(service, 'read_url_log_window', lambda *args, **kwargs: calls.__setitem__('read', calls['read'] + 1) or None)
    result = query_case_logs(
        'http://10.29.108.45/ATLog_root/task_x/block_y/SN_CASE/',
        start_time='2026-08-15 19:49:30',
        end_time='2026-08-15 19:50:00',
        targets=[{'subsystem': 'rspm', 'module': 'RSPM'}],
        source_categories=[],
    )
    assert result['rows'] == []
    assert calls == {'event': 0, 'read': 0}
    assert result['log_catalog'] == [{'subsystem': 'rspm', 'modules': ['RSPM']}]


def test_query_case_logs_source_categories_filter_debug_vs_executor(monkeypatch):
    import apps.atlog.services as service

    from apps.logsources.services.unified_search import UrlWindowResult

    fetched = []
    monkeypatch.setattr(service, 'query_event_log', lambda *args, **kwargs: {'components': [], 'truncated': False, 'rows': []})
    monkeypatch.setattr(service, 'discover_case_log_files_for_query', lambda base, **kwargs: [
        {'relative_path': 'full_logs/log/elog/192.3.4.41/rspm/RSPM_cp_xx.log', 'component': 'RSPM', 'subsystem': 'rspm', 'module': 'RSPM', 'kind': 'executor'},
    ])
    monkeypatch.setattr(service, 'discover_case_log_catalog', lambda base: [
        {'subsystem': 'rspm', 'modules': ['RSPM']},
    ])
    def fake_read(artifact, start, end, operation_id='-'):
        fetched.append(artifact.url)
        return UrlWindowResult('[2026-08-15 19:49:39.000] [INFO] line\n', False, 'range-binary-seek', 1024)
    monkeypatch.setattr(service, 'read_url_log_window', fake_read)
    query_case_logs(
        'http://10.29.108.45/ATLog_root/task_x/block_y/SN_CASE/',
        start_time='2026-08-15 19:49:30', end_time='2026-08-15 19:50:00',
        targets=[{'subsystem': 'rspm', 'module': 'RSPM'}], source_categories=['executor'],
    )
    assert len(fetched) == 1
    assert '/elog/192.3.4.41/rspm/' in fetched[0]
    assert '/executor/' not in fetched[0]


def test_event_log_is_always_read_from_case_root_not_full_logs(monkeypatch):
    import apps.atlog.services as service

    base = 'http://10.29.108.21/ATLog_root/task_3915251436923912194/block_3915251505643913440_1/SPM_WSPMDiagnose_Measure_Malf_003/'
    expected_event = base + 'event.log'
    seen = []

    from apps.logsources.services.unified_search import UrlWindowResult

    def fake_window(artifact, start, end, operation_id='-'):
        seen.append(artifact.url)
        return UrlWindowResult('[2026-09-08 21:14:33.100] [WPOS] [INFO] state changed', False, 'range-binary-seek', 1024)

    monkeypatch.setattr(service, 'read_url_log_window', fake_window)
    result = service.query_event_log(
        base,
        start_time='2026-09-08 21:14:30',
        end_time='2026-09-08 21:14:40',
    )

    assert seen == [expected_event]
    assert result['event_url'] == expected_event
    assert result['source_location'] == 'case_root'
    assert '/full_logs/' not in result['event_url']


def test_case_log_discovery_keeps_debug_under_full_logs_log(monkeypatch):
    import apps.atlog.services as service

    base = 'http://10.29.108.21/ATLog_root/task_x/block_y/CASE_A/'
    visited = []

    def fake_list(url):
        visited.append(url)
        if url.endswith('/full_logs/log/debug/'):
            return ['SPM/']
        if url.endswith('/full_logs/log/debug/SPM/'):
            return ['WPOS/']
        if url.endswith('/full_logs/log/debug/SPM/WPOS/'):
            return ['WPOS.log']
        return []

    service._discover_case_log_files_cached.cache_clear()
    monkeypatch.setattr(service, 'list_directory', fake_list)
    rows = service.discover_case_log_files(base)
    service._discover_case_log_files_cached.cache_clear()

    assert any(item['relative_path'] == 'full_logs/log/debug/SPM/WPOS/WPOS.log' for item in rows)
    assert all(not url.endswith('/event.log') for url in visited)


def test_head_size_falls_back_to_range_get_when_head_is_unsupported(monkeypatch):
    import apps.atlog.services as service

    calls = []

    def fake_request(url, *, method='GET', headers=None, max_bytes=None):
        calls.append((method, headers or {}, max_bytes))
        if method == 'HEAD':
            raise service.AtLogError('HEAD not supported')
        return service.RemoteFile(
            url=url,
            status=206,
            content=b'x',
            content_length=1,
            content_range='bytes 0-0/33554432',
        )

    monkeypatch.setattr(service, '_request', fake_request)
    assert service._head_size('http://10.29.108.21/ATLog_root/task_x/block_y/SN_CASE/event.log') == 33554432
    assert calls[0][0] == 'HEAD'
    assert calls[1][1].get('Range') == 'bytes=0-0'


def test_debug_target_selection_does_not_filter_case_root_event_log(monkeypatch):
    import apps.atlog.services as service

    seen_modules = []

    def fake_event(*args, **kwargs):
        seen_modules.extend(kwargs.get('modules') or [])
        return {
            'components': ['WPOS'],
            'truncated': False,
            'rows': [{
                'line_number': 1,
                'time': '2026-08-15 19:49:38.650',
                'component': 'WPOS',
                'level': 'EVENT',
                'source': 'event.cpp:1',
                'message': 'state changed',
                'raw': '[2026-08-15 19:49:38.650] [WPOS] state changed',
            }],
            'event_url': 'http://10.29.108.21/ATLog_root/task_x/block_y/SN_CASE/event.log',
        }

    monkeypatch.setattr(service, 'query_event_log', fake_event)
    monkeypatch.setattr(service, 'discover_case_log_files', lambda base: [])
    result = service.query_case_logs(
        'http://10.29.108.21/ATLog_root/task_x/block_y/SN_CASE/',
        start_time='2026-08-15 19:49:30',
        end_time='2026-08-15 19:50:00',
        targets=[{'subsystem': 'rspm', 'module': 'RSPMCPD'}],
        source_categories=['event', 'debug'],
    )
    assert seen_modules == []
    assert result['event_query']['found'] is True
    assert result['event_query']['url'].endswith('/SN_CASE/event.log')
    assert any(row.get('component') == 'WPOS' for row in result['rows'])


def test_atlog_excel_import_debug_endpoint_logs_diagnostics_source():
    source = Path(__file__).resolve().parents[1].joinpath('apps/atlog/views.py').read_text(encoding='utf-8')
    assert 'url_path="excel-import-debug"' in source
    assert '[ATLog Excel Import] header_scan' in source
    assert '[ATLog Excel Import] row file=%s' in source
    assert '[ATLog Excel Import] summary' in source


def test_normalize_base_url_accepts_named_atlog_root():
    url = "http://10.29.101.237:80/ATLog_ltusr/task_3994852024326291458/block_3994852093045244159_1/SPM_WSPMScan_Measure_Func_004/"
    assert normalize_base_url(url) == url


def test_ancestor_case_urls_respect_named_atlog_root():
    url = "http://127.0.0.1/ATLog_ltusr/task_123/block_456_1/SN_CASE_001/"
    values = _ancestor_case_urls(url)
    assert values[0].endswith("/SN_CASE_001/")
    assert values[1].endswith("/block_456_1/")
    assert values[2].endswith("/task_123/")
    assert all("/ATLog_ltusr/" in value for value in values)


def test_normalize_base_url_accepts_arbitrary_case_directory_name():
    url = "http://127.0.0.1/custom_reports/task_123/block_456_1/SN_CASE_001/"
    assert normalize_base_url(url) == url


def test_ancestor_case_urls_do_not_depend_on_atlog_root_name():
    url = "http://127.0.0.1/custom_reports/task_123/block_456_1/SN_CASE_001/"
    values = _ancestor_case_urls(url)
    assert values == [
        "http://127.0.0.1/custom_reports/task_123/block_456_1/SN_CASE_001/",
        "http://127.0.0.1/custom_reports/task_123/block_456_1/",
        "http://127.0.0.1/custom_reports/task_123/",
    ]


def test_target_scoped_case_search_prunes_debug_files_by_time_before_read(monkeypatch):
    import apps.atlog.services as service

    base = "http://10.29.108.45/task_x/block_y/SN_CASE/"
    calls = []
    directory_map = {
        base + "full_logs/log/debug/rspm/": [
            "RSPM_20260910120000000.log",
            "RSPM_20260911120000000.log",
            "RSPM.log",
            "OTHER_20260910120000000.log",
        ],
    }

    def fake_list(url):
        calls.append(url)
        return directory_map.get(url, [])

    monkeypatch.setattr(service, "list_directory", fake_list)
    rows = service.discover_case_log_files_for_query(
        base,
        targets=[("rspm", "RSPM")],
        source_categories=["debug"],
        start=service._parse_timestamp("2026-09-10 11:00:00"),
        end=service._parse_timestamp("2026-09-10 11:30:00"),
    )

    assert [item["relative_path"] for item in rows] == [
        "full_logs/log/debug/rspm/RSPM_20260910120000000.log"
    ]
    assert calls == [base + "full_logs/log/debug/rspm/"]
    assert all("OTHER" not in item["relative_path"] for item in rows)


def test_target_scoped_executor_search_only_enters_selected_subsystem_under_ip(monkeypatch):
    import apps.atlog.services as service

    base = "http://10.29.108.45/task_x/block_y/SN_CASE/"
    directory_map = {
        base + "full_logs/log/debug/elog/": ["192.3.4.41/", "192.3.4.42/"],
        base + "full_logs/log/debug/elog/192.3.4.41/rspm/": [
            "RSPM_cp_xx_20260910120000000.log",
            "RSPM_cp_xx.log",
            "OTHER_cp_xx_20260910120000000.log",
        ],
        base + "full_logs/log/debug/elog/192.3.4.42/rspm/": [],
    }
    calls = []

    def fake_list(url):
        calls.append(url)
        return directory_map.get(url, [])

    monkeypatch.setattr(service, "list_directory", fake_list)
    rows = service.discover_case_log_files_for_query(
        base,
        targets=[("rspm", "RSPM")],
        source_categories=["executor"],
        start=service._parse_timestamp("2026-09-10 11:00:00"),
        end=service._parse_timestamp("2026-09-10 11:30:00"),
    )

    assert [item["relative_path"] for item in rows] == [
        "full_logs/log/debug/elog/192.3.4.41/rspm/RSPM_cp_xx_20260910120000000.log"
    ]
    assert not any(url.endswith("/192.3.4.41/") for url in calls)
    assert not any("/ws/" in url for url in calls)


def test_url_filename_window_selector_reuses_log_positioning_timestamp_parser():
    from datetime import datetime
    from apps.logsources.services.unified_search import select_url_log_names_for_window

    names = [
        "RSPM_20260910_120000.log",
        "RSPM_20260911_120000.log",
        "RSPM.log",
    ]
    selected = select_url_log_names_for_window(
        names,
        datetime(2026, 9, 10, 11, 0, 0),
        datetime(2026, 9, 10, 11, 30, 0),
    )
    assert selected == ["RSPM_20260910_120000.log"]


def test_url_current_artifacts_are_filtered_by_real_time_index(monkeypatch):
    from datetime import datetime
    import apps.logsources.services.unified_search as unified

    artifacts = [
        unified.UrlLogArtifact("http://host/old.log", "old.log", "debug", "rspm", "RSPM"),
        unified.UrlLogArtifact("http://host/hit.log", "hit.log", "debug", "rspm", "RSPM"),
        unified.UrlLogArtifact("http://host/future.log", "future.log", "debug", "rspm", "RSPM"),
    ]
    ranges = {
        "old.log": (datetime(2026, 9, 10, 9, 0), datetime(2026, 9, 10, 10, 0), 100),
        "hit.log": (datetime(2026, 9, 10, 10, 50), datetime(2026, 9, 10, 11, 20), 100),
        "future.log": (datetime(2026, 9, 10, 12, 0), datetime(2026, 9, 10, 13, 0), 100),
    }
    monkeypatch.setattr(
        unified,
        "probe_url_log_time_range",
        lambda artifact, operation_id="-": ranges[artifact.source_path],
    )

    selected = unified.filter_url_artifacts_for_window(
        artifacts,
        datetime(2026, 9, 10, 11, 0),
        datetime(2026, 9, 10, 11, 30),
    )
    assert [item.source_path for item in selected] == ["hit.log"]


def test_url_executor_current_files_reuse_log_positioning_module_stream_selection():
    from datetime import datetime
    from apps.logsources.services.unified_search import select_url_log_names_for_window

    names = [f"RSPM_cp_{index:02d}.log" for index in range(1, 94)]
    selected = select_url_log_names_for_window(
        names,
        datetime(2026, 9, 10, 11, 0),
        datetime(2026, 9, 10, 11, 30),
        fm_hint="RSPM",
        subsystem="rspm",
        source_category="executor",
    )
    assert selected == ["RSPM_cp_01.log"]
