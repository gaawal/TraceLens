from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def text(rel): return (ROOT/rel).read_text(encoding='utf-8')
def test_contracts():
    models=text('apps/environments/models.py')
    remote=text('apps/logsources/services/remote_logs.py')
    cache=text('apps/logsources/services/cache_identity.py')
    reports=text('apps/reports/services.py')
    report_parser=text('apps/reports/parser.py')
    views=text('apps/environments/views.py')
    urls=text('config/urls.py')
    assert '/log/{username}/debug' in models
    assert '/log/{username}/run/' in models
    assert '/log/{username}/debug/elog' in models
    assert "-mindepth 2 -maxdepth 2 -type f" in remote and "-name '*.log'" in remote
    assert 'parse_log_name(filename, reference)' in remote
    assert 'port-' not in cache
    assert '"v3"' in cache
    assert 'environment_id' not in cache
    assert 'EnvironmentFolder' in models
    assert "grep '[t]b_simulator'" in views
    assert '/data/{username}/report/cpd_report' in reports
    for field in ('Start Time','Stop Time','Test Run Result','Results Validation','Measurement Quality','MCs Status'):
        assert field in report_parser
    assert 'router.register("cpd-reports"' in urls
    assert 'router.register("environment-folders"' in urls
if __name__=='__main__': test_contracts(); print('v0.10 resource/report static contracts passed')
