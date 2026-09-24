from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
settings = (ROOT / 'config/settings.py').read_text(encoding='utf-8')
discovery = (ROOT / 'apps/environments/services/discovery.py').read_text(encoding='utf-8')
reports = (ROOT / 'apps/reports/services.py').read_text(encoding='utf-8')
report_views = (ROOT / 'apps/reports/views.py').read_text(encoding='utf-8')

assert 'x-tracelens-operation-id' in settings
assert 'CORS_EXPOSE_HEADERS' in settings
assert 'X-TraceLens-Operation-ID' in settings
assert 'Current(?:\\s+[A-Za-z0-9_.-]+)*\\s+Version' in discovery
assert '上位机 {host}' in discovery
assert 'Current Version: / Current <Product> Version:' in discovery
assert 'def list_all_reports(' in reports
assert "-mindepth 3 -maxdepth 3 -type f -name '*.rpt'" in reports
assert 'list_all_reports' in report_views
print('v0.11 CORS / version / CPD overview static test passed')
