from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = (ROOT / 'apps/reports/services.py').read_text(encoding='utf-8')
FILE_INDEX = (ROOT / 'apps/logsources/services/file_index.py').read_text(encoding='utf-8')
REMOTE = (ROOT / 'apps/logsources/services/remote_logs.py').read_text(encoding='utf-8')
VIEWS = (ROOT / 'apps/reports/views.py').read_text(encoding='utf-8')
SETTINGS = (ROOT / 'config/settings.py').read_text(encoding='utf-8')

# tar stdout may be bytes or str; the parser and stream must accept both.
assert 'bytes | bytearray | memoryview | str' in FILE_INDEX
assert 'LINE_TIME_RE_TEXT' in FILE_INDEX
assert 'while True:' in REMOTE
assert 'if not line:' in REMOTE
assert 'line.encode("utf-8", errors="replace") if isinstance(line, str) else bytes(line)' in REMOTE

# CPD catalog and current-range summaries are bounded-parallel and cached.
assert 'ThreadPoolExecutor' in REPORTS
assert 'TRACELENS_CPD_SCAN_WORKERS' in SETTINGS
assert 'TRACELENS_CPD_PARSE_WORKERS' in SETTINGS
assert 'cpd.dataset.parallel_parse.start' in REPORTS
assert 'get_json_many' in REPORTS
assert ':dataset:{identity}' in REPORTS
assert 'def load_report_dataset' in REPORTS
assert 'url_path="dataset"' in VIEWS
assert 're.findall(r"(?<!\\d)(\\d{17}|\\d{14})(?!\\d)"' in REPORTS
print('v0.18 CPD parallel dataset + tar text stream static test passed')
