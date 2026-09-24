import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.reports.parser import parse_report_summary

SAMPLE = r'''---------------------- Report Header -----------------------
CPD Name            : CSPWSZC
Operator            : EquipEng
Software Ver        : XY V100R001C10B071.FeaHwOpt07XSPM.01
Report File         : CSPWSZC_20260702142027.rpt
Report Full Path    : /data/root/report/cpd_report/wspm/cspwszc/CSPWSZC_20260702142027.rpt
Measure Log         : /data/root/cpd_data/wspm/cspwszc/CSPWSZC_20260702142027_20260702142029.clg
---------------------- Result Summary ----------------------
Start Time          : Thu, 02 Jul 2026 14:20:27 590823us +0800
Stop Time           : Thu, 02 Jul 2026 14:20:29 831128us +0800
Execution Time      : 0:00:02.240305
Test Run Result     : FAILED
Results Validation  : UNKNOWN
Measurement Quality : UNKNOWN
MCs Status          : NOT SAVED
'''

summary = parse_report_summary(SAMPLE, file_name='CSPWSZC_20260702142027.rpt', full_path='/tmp/a.rpt')
assert summary['start_time'] == '2026-07-02 14:20:27.590823'
assert summary['stop_time'] == '2026-07-02 14:20:29.831128'
assert summary['test_run_result'] == 'FAILED'
assert summary['results_validation'] == 'UNKNOWN'
assert summary['measurement_quality'] == 'UNKNOWN'
assert summary['mcs_status'] == 'NOT SAVED'
assert summary['measure_log'].endswith('.clg')
print('CPD report parser standalone test passed')
