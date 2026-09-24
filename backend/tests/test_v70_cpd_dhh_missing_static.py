from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_cpd_report_query_keeps_30_day_range():
    source = (ROOT / 'apps/reports/services.py').read_text(encoding='utf-8')
    assert 'end_dt - start_dt > timedelta(days=30)' in source
    assert 'CPD 报告查询时间范围最多支持 30 天' in source


def test_missing_log_directories_are_skipped():
    source = (ROOT / 'apps/logsources/services/file_index.py').read_text(encoding='utf-8')
    assert '_is_missing_directory_error' in source
    assert 'log.file_index.window_find.skip_missing' in source
    assert 'log.file_index.find.skip_missing' in source


def test_dhh_is_detected_and_executor_routes_to_dhh():
    stations = (ROOT / 'apps/environments/services/stations_parser.py').read_text(encoding='utf-8')
    remote = (ROOT / 'apps/logsources/services/remote_logs.py').read_text(encoding='utf-8')
    assert 'dhh: Station | None = None' in stations
    assert 'item.name.strip().lower() == "dhh"' in stations
    assert 'log.root.dhh.route' in remote
    assert 'log.root.dhh.skip_small_network' in remote
    assert '_executor_lower_machines' in remote


def test_dhh_is_separate_from_lch_list():
    source = (ROOT / 'apps/environments/serializers.py').read_text(encoding='utf-8')
    assert 'dhh_machine = serializers.SerializerMethodField()' in source
    assert 'is_dhh_environment = serializers.SerializerMethodField()' in source
    assert 'if station_name == "dhh" or station_type == "DHH"' in source
