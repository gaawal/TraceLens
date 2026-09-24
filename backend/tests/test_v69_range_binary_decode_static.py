from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_log_window_has_three_day_server_guard():
    source = (ROOT / 'apps/logsources/serializers.py').read_text(encoding='utf-8')
    assert 'timedelta(days=3)' in source
    assert '单次日志检索时间范围最多支持 3 天' in source


def test_cpd_report_query_keeps_long_history_range():
    source = (ROOT / 'apps/reports/services.py').read_text(encoding='utf-8')
    assert 'end_dt - start_dt > timedelta(days=30)' in source
    assert 'CPD 报告查询时间范围最多支持 30 天' in source


def test_large_direct_logs_use_timestamp_byte_bisection():
    source = (ROOT / 'apps/logsources/services/remote_logs.py').read_text(encoding='utf-8')
    assert '_find_direct_time_offset' in source
    assert '_probe_timestamp_after_offset' in source
    assert 'log.read.direct.binary_seek' in source
    assert '_DIRECT_BINARY_SEEK_MIN_BYTES' in source
    assert 'strategy=binary_seek' in source


def test_tar_members_are_remote_time_pruned_and_binary_safe():
    source = (ROOT / 'apps/logsources/services/remote_logs.py').read_text(encoding='utf-8')
    assert 'LC_ALL=C awk' in source
    assert 'substr($0,2,19)' in source
    assert 'line.encode("utf-8", errors="replace")' in source


def test_bad_cache_utf8_is_a_cache_miss_not_search_failure():
    source = (ROOT / 'apps/logsources/services/redis_store.py').read_text(encoding='utf-8')
    assert 'decode("utf-8", errors="replace")' in source
    assert 'redis.cache.invalid_json' in source
