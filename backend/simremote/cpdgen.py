"""生成远端 CPD 测校资产：报告 ``.rpt`` + 数据表格 ``.xlsx``。

字段与取值都必须对齐后端/前端，否则界面上会看到空表或 UNKNOWN：

* **报告字段** → ``apps/reports/parser.py::parse_report_summary``
  逐行 ``Label: value``；其中 Start/Stop Time 必须是
  ``Tue, 23 Sep 2026 09:12:33 123456us +0800`` 这种形态。
* **取值词表** → ``frontend/src/components/CpdReportPage.tsx``
  ``test_run_result`` / ``results_validation`` / ``measurement_quality``
  取 ``OK | FAILED | UNKNOWN``；``mcs_status`` 取 ``SAVED | NOT SAVED | UNKNOWN``。
* **数据表格** → ``apps/reports/cpd_data_service.py``
  首个 20 行内要有一列名为 ``timestamp`` / ``time`` / ``date`` / ``时间`` / ``时间戳``；
  行时间必须落在报告的 ``[Start Time, Stop Time]`` 内，否则前端按报告的
  时间窗过滤后是一张空表（这是最容易踩的坑）。

命名语义与日志一致：报告名带上**开始时刻**，方便一眼对上是哪一次测校。
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from openpyxl import Workbook

from . import fleet

# 远端根、相对路径、内容、行数
Artifact = tuple[str, Path, bytes, int]

# 报告时间戳用英文缩写（不依赖 locale 的 %a/%b）
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# 一次测校持续时长
REPORT_SPAN_MINUTES = 12
# 两次测校之间的间隔（含一次测校时长）
REPORT_GAP_MINUTES = 25

_X_SPEC_NM = 1.20
_Y_SPEC_NM = 1.20
_Z_SPEC_NM = 25.0


@dataclass(frozen=True)
class ReportPlan:
    subsystem: str
    module: str
    index: int
    start: datetime
    stop: datetime
    result: str
    validation: str
    quality: str
    mcs: str
    drifted: bool

    @property
    def stem(self) -> str:
        return f"CPD_{self.module.upper()}_{self.start:%Y%m%d}_{self.start:%H%M%S}"

    @property
    def report_name(self) -> str:
        return f"{self.stem}.rpt"

    @property
    def data_name(self) -> str:
        return f"{self.stem}.xlsx"

    @property
    def report_remote_path(self) -> str:
        return f"{fleet.CPD_REPORT_ROOT.format(username=fleet.SIM_USERNAME)}/{self.subsystem}/{self.module}/{self.report_name}"

    @property
    def measure_log(self) -> str:
        return f"{fleet.UPPER.debug_root}/{self.subsystem}/{self.module}.log"


def _report_stamp(moment: datetime) -> str:
    """CPD 报告时间戳：``Tue, 23 Sep 2026 09:12:33 123456us +0800``。"""
    return (
        f"{_WEEKDAYS[moment.weekday()]}, {moment.day:02d} {_MONTHS[moment.month - 1]} {moment.year} "
        f"{moment:%H:%M:%S} {moment.microsecond:06d}us +0800"
    )


def _execution_time(plan: ReportPlan) -> str:
    seconds = int((plan.stop - plan.start).total_seconds())
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def report_text(plan: ReportPlan) -> bytes:
    """报告正文。每行 ``Label: value``，标签必须与 parser 完全一致。"""
    lines = [
        "CPD Measurement Report",
        f"Report Full Path: {plan.report_remote_path}",
        f"CPD Name: {plan.stem}",
        f"Operator: {fleet.SIM_USERNAME}",
        f"Software Ver: {fleet.SOFTWARE_VERSION}",
        f"Report Date: {plan.stop:%Y-%m-%d}",
        f"Report Time: {plan.stop:%H:%M:%S}",
        f"Measure Log: {plan.measure_log}",
        f"Start Time: {_report_stamp(plan.start)}",
        f"Stop Time: {_report_stamp(plan.stop)}",
        f"Execution Time: {_execution_time(plan)}",
        f"Test Run Result: {plan.result}",
        f"Results Validation: {plan.validation}",
        f"Measurement Quality: {plan.quality}",
        f"MCs Status: {plan.mcs}",
        "",
        "Summary",
        f"  Subsystem: {plan.subsystem}",
        f"  Module: {plan.module}",
        f"  Samples: {int((plan.stop - plan.start).total_seconds() // fleet.CPD_SAMPLE_STEP_SECONDS) + 1}",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def workbook_bytes(plan: ReportPlan) -> tuple[bytes, int]:
    """测校数据表格。时间列名用 ``Timestamp``（后端按 casefold 匹配 ``timestamp``）。"""
    book = Workbook()
    sheet = book.active
    sheet.title = "Measurement"
    sheet.append([
        "Timestamp", "Wafer ID", "X Overlay [nm]", "Y Overlay [nm]",
        "Z Focus [nm]", "Dose Error [%]", "Stage Temp [C]", "Result",
    ])

    moment = plan.start
    index = 0
    rows = 0
    while moment <= plan.stop:
        # 漂移的报告让 X 向 overlay 超规格，让"FAILED"在数据里也成立
        drift = 0.0
        if plan.drifted:
            drift = 0.35 * (1 + index % 3) * (0.6 + 0.4 * ((index // 4) % 3))
        sheet.append([
            moment,
            f"W{1000 + index:04d}",
            round(0.18 + drift + 0.05 * ((index * 7) % 5) / 5, 3),
            round(-0.15 + 0.04 * ((index * 5) % 4) / 4, 3),
            round(2.5 + 0.6 * ((index * 3) % 3), 3),
            round(0.02 + 0.01 * ((index * 11) % 4), 3),
            round(22.4 + 0.1 * ((index * 2) % 5), 2),
            "FAILED" if plan.drifted and index % 2 == 0 else "OK",
        ])
        index += 1
        rows += 1
        moment = plan.start + timedelta(seconds=index * fleet.CPD_SAMPLE_STEP_SECONDS)

    for column in sheet.columns:
        width = max(len(str(cell.value)) for cell in column if cell.value is not None)
        sheet.column_dimensions[column[0].column_letter].width = min(28, max(12, width + 2))
    for cell in sheet["A"][1:]:
        cell.number_format = "yyyy-mm-dd hh:mm:ss"

    buffer = io.BytesIO()
    book.save(buffer)
    book.close()
    return buffer.getvalue(), rows


def module_plans(subsystem: str, module: str, *, now: datetime) -> list[ReportPlan]:
    """一个模块的 N 次测校，从新到旧排列。"""
    plans: list[ReportPlan] = []
    stop = (now - timedelta(minutes=5)).replace(microsecond=0)
    for index in range(fleet.CPD_REPORTS_PER_MODULE):
        start = stop - timedelta(minutes=REPORT_SPAN_MINUTES)
        # 让倒数第二次测校失败，界面上既有 OK 行也有 FAILED 行
        drifted = index == fleet.CPD_REPORTS_PER_MODULE - 2
        plans.append(ReportPlan(
            subsystem=subsystem,
            module=module,
            index=index,
            start=start,
            stop=stop,
            result="FAILED" if drifted else "OK",
            validation="FAILED" if drifted else "OK",
            quality="OK" if not drifted else "UNKNOWN",
            mcs="SAVED" if not drifted else "NOT SAVED",
            drifted=drifted,
        ))
        stop = start - timedelta(minutes=REPORT_GAP_MINUTES - REPORT_SPAN_MINUTES)
    return plans


def plan(spec: fleet.MachineSpec, *, now: datetime) -> list[Artifact]:
    """全部 CPD 产物（报告 + 数据表格），交给 loggen 统一落盘与清理。"""
    if spec.role != "upper":
        # 报告的 report_root() 与数据的 data_root() 都取自 environment.upper_machine
        return []
    artifacts: list[Artifact] = []
    for subsystem, module in fleet.all_cpd_modules():
        for item in module_plans(subsystem, module, now=now):
            report = report_text(item)
            artifacts.append((
                spec.cpd_report_root,
                Path(subsystem) / module / item.report_name,
                report,
                len(report.splitlines()),
            ))
            payload, _rows = workbook_bytes(item)
            artifacts.append((
                spec.cpd_data_root,
                Path(subsystem) / module / item.data_name,
                payload,
                0,
            ))
    return artifacts
