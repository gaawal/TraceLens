from __future__ import annotations

import logging
import re
from datetime import datetime

logger = logging.getLogger("tracelens.cpd_report_parser")


def _value(text: str, label: str) -> str:
    match = re.search(rf"(?m)^\s*{re.escape(label)}\s*:\s*(.*?)\s*$", text)
    return match.group(1).strip() if match else ""


def parse_report_timestamp(value: str) -> str:
    """Convert CPD timestamp to TraceLens editable time text."""
    raw = value.strip()
    if not raw:
        return ""
    match = re.match(
        r"^(?P<base>[A-Za-z]{3},\s+\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\s+\d{2}:\d{2}:\d{2})"
        r"(?:\s+(?P<micros>\d{1,6})us)?(?:\s+[+-]\d{4})?$",
        raw,
    )
    if match:
        try:
            dt = datetime.strptime(match.group("base"), "%a, %d %b %Y %H:%M:%S")
            micros = int((match.group("micros") or "0").ljust(6, "0")[:6])
            return dt.replace(microsecond=micros).strftime("%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            pass
    normalized = raw.replace("T", " ")
    try:
        return datetime.fromisoformat(normalized).strftime("%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        logger.warning("cpd.report.time_unparsed value=%s", raw)
        return raw


def parse_report_summary(text: str, *, file_name: str, full_path: str, modified_at: float | None = None) -> dict:
    start_raw = _value(text, "Start Time")
    stop_raw = _value(text, "Stop Time")
    return {
        "file_name": file_name,
        "full_path": _value(text, "Report Full Path") or full_path,
        "cpd_name": _value(text, "CPD Name"),
        "operator": _value(text, "Operator"),
        "software_ver": _value(text, "Software Ver"),
        "report_date": _value(text, "Report Date"),
        "report_time": _value(text, "Report Time"),
        "measure_log": _value(text, "Measure Log"),
        "start_time_raw": start_raw,
        "stop_time_raw": stop_raw,
        "start_time": parse_report_timestamp(start_raw),
        "stop_time": parse_report_timestamp(stop_raw),
        "execution_time": _value(text, "Execution Time"),
        "test_run_result": _value(text, "Test Run Result"),
        "results_validation": _value(text, "Results Validation"),
        "measurement_quality": _value(text, "Measurement Quality"),
        "mcs_status": _value(text, "MCs Status"),
        "modified_at": datetime.fromtimestamp(modified_at).isoformat(timespec="seconds") if modified_at else "",
    }
