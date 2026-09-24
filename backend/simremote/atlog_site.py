"""模拟 ATLog 用例报告站：本地 HTTP 服务 + 用例目录树。

后端 ``apps/atlog`` 的「用例 URL 分析」是**按 URL 抓一个用例目录**的：
先抓目录页（用 HTMLParser 读 ``<a href>``），再按固定文件名去取报告与日志。
这里生成这样一棵目录树，并用本地 HTTP 服务按 nginx autoindex 风格发布。

硬约束，逐条对应 ``apps/atlog/services.py``：

* ``normalize_base_url`` 要求 host 是内网/回环地址 → 监听**局域网 IP + 127.0.0.1**
  （公网/VPN 地址会被拒，所以对外一律用内网地址）；
* 目录链接必须带尾斜杠（``endswith("/")`` 判定 is_dir），``../`` 与 ``?`` 开头的会被忽略；
* ``case_id`` = 用例目录名；summary XML 里 testcase 的 ``classname``/``name`` 必须
  **包含**它（大小写不敏感的子串匹配），否则分析会落到别的用例上；
* pytest HTML 的文件名必须是 ``test_<case_id 小写>.html``（优先精确匹配）；
* 失败详情 XML 只能放 ``result/pytest-*.xml``；
* xytest.log 的错误行时间要落在 summary 的 start/end ±2 分钟内，否则被丢弃；
* event.log 是十三字段运行事件日志，且在**用例根**，不在 full_logs 里；
* full_logs 只认 ``log/debug/`` 与 ``log/debug/elog/<IPv4>/``（兼容 legacy ``log/elog/``），
  目录名命中 path markers（``executor``/``run``/``log``...）的会被过滤掉；
* ``详细日志链接.html`` 的上位机拓扑 IP 用 ``192.*``：会被正常解析，但
  ``ensure_atlog_environment`` 会跳过自动录入，不会给环境资源塞连不上的机器。

另外用**虚拟挂载**把只走 SSH 的 CPD 资产也发布成 HTTP URL（``/cpd/report/...``、
``/cpd/data/...``），这样「报告文件 URL 分析」对 ``.rpt`` 与 ``.xlsx`` 同样可用。
"""

from __future__ import annotations

import html
import posixpath
import socketserver
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape
from xml.sax.saxutils import quoteattr

from . import fleet, loggen

# 报告站的文件根。注意它不在 remote_fs 下：那是「远端机器」，这里是「报告站」。
SITE_ROOT = Path(__file__).resolve().parent / "atlog_site"

# 虚拟挂载前缀：把只能走 SSH 的 CPD 报告/测校数据也暴露成 HTTP URL
CPD_MOUNT = "/cpd"
CPD_REPORT_MOUNT = CPD_MOUNT + "/report"
CPD_DATA_MOUNT = CPD_MOUNT + "/data"

# 一次用例执行的时长
CASE_SPAN_MINUTES = 6
# full_logs 里日志行的密度
DEBUG_STEP_SECONDS = 20
EVENT_STEP_SECONDS = 30

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".xml": "text/xml; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".rpt": "text/plain; charset=utf-8",
    ".ini": "text/plain; charset=utf-8",
    ".cfg": "text/plain; charset=utf-8",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".json": "application/json; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}


@dataclass(frozen=True)
class CaseSpec:
    """一个模拟用例的静态描述。"""

    case_id: str
    subsystem: str
    module: str
    status: str  # failed / passed
    assertion: str
    call_chain: tuple[tuple[str, int, str], ...]
    xytest_message: str
    event_message: str
    lower_ip: str
    debug_layout: str  # flat: <子系统>/<模块>.log  nested: <子系统>/<模块>/<模块>.log


CASES: tuple[CaseSpec, ...] = (
    CaseSpec(
        case_id="SN_SPM_RSPMCPD_DSPRSAR_MalFunc_001",
        subsystem="spm",
        module="RSPMCPD",
        status="failed",
        assertion="expect ResultStatus.OK, real ResultStatus.FAILED",
        call_chain=(
            ("/data/atlog/run_case.py", 121, "run_cpd"),
            ("/data/atlog/cpd_runner.py", 444, "result_check"),
            ("/data/atlog/util_std.py", 54, "assert_equal"),
        ),
        xytest_message=(
            "[cpd_runner.py:311] [AssertionError] expect: ResultStatus.OK, "
            "real: ResultStatus.FAILED, caller: result_check"
        ),
        event_message="result check failed, overlay drift beyond specification",
        lower_ip="192.3.4.41",
        debug_layout="flat",
    ),
    CaseSpec(
        case_id="SN_SPM_WPOS_ALIGN_Drift_002",
        subsystem="spm",
        module="WPOS",
        status="failed",
        assertion="expect AlignStatus.LOCKED, real AlignStatus.DRIFT",
        call_chain=(
            ("/data/atlog/align_case.py", 88, "run_align"),
            ("/data/atlog/align_check.py", 203, "verify_lock"),
        ),
        xytest_message=(
            "[align_check.py:203] [AssertionError] expect: AlignStatus.LOCKED, "
            "real: AlignStatus.DRIFT, caller: verify_lock"
        ),
        event_message="wafer stage alignment mark drift detected",
        lower_ip="192.3.4.42",
        debug_layout="nested",
    ),
    CaseSpec(
        case_id="SN_MECORE_CPCORE_Thermal_OK_003",
        subsystem="mecore",
        module="CPCORE",
        status="passed",
        assertion="",
        call_chain=(),
        xytest_message="",
        event_message="thermal loop settled within specification",
        lower_ip="192.3.4.43",
        debug_layout="flat",
    ),
)


# --------------------------------------------------------------------------- 时间线


@dataclass(frozen=True)
class CaseTimeline:
    start: datetime
    stop: datetime
    failure: datetime


def _stamp(moment: datetime) -> str:
    return f"{moment:%Y-%m-%d %H:%M:%S}.{moment.microsecond // 1000:03d}"


def _timeline(spec: CaseSpec, now: datetime) -> CaseTimeline:
    stop = (now - timedelta(minutes=3)).replace(microsecond=0)
    start = stop - timedelta(minutes=CASE_SPAN_MINUTES)
    # 失败点靠近用例结束，与 xytest 行、event 行保持一致
    return CaseTimeline(start=start, stop=stop, failure=stop - timedelta(seconds=40))


# --------------------------------------------------------------------------- 内容构造


def _summary_xml(spec: CaseSpec, line: CaseTimeline) -> bytes:
    """JUnit 风格汇总报告。testcase 的 classname 必须含 case_id。"""
    case_lower = spec.case_id.lower()
    failures = 1 if spec.status == "failed" else 0
    rows: list[str] = []

    if spec.status == "failed":
        body = "\n".join(
            f"{path}:{number}: in {function}" for path, number, function in spec.call_chain
        )
        body += f"\nE   AssertionError: {spec.assertion}"
        rows.append(
            f'    <testcase classname={quoteattr(spec.case_id)} name={quoteattr("test_" + case_lower)} '
            f'time="742.310" result="failed">\n'
            f'      <failure message={quoteattr("AssertionError: " + spec.assertion)}>'
            f"{xml_escape(body)}</failure>\n"
            f"    </testcase>"
        )
    else:
        rows.append(
            f'    <testcase classname={quoteattr(spec.case_id)} name={quoteattr("test_" + case_lower)} '
            f'time="318.420" result="passed"/>'
        )
    # 同一次跑批里别人的用例：用来验证 case_id 过滤不会串台
    rows.append(
        f'    <testcase classname="SN_OTHER_case" name="test_sn_other_case" time="12.500" '
        f'result="{"passed" if spec.status == "failed" else "failed"}"/>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<testsuite name={quoteattr(spec.case_id)} tests="2" failures="{failures}" errors="0" '
        f'skipped="0" starttime="{line.start:%Y-%m-%d %H:%M:%S}" endtime="{line.stop:%Y-%m-%d %H:%M:%S}">\n'
        + "\n".join(rows)
        + "\n</testsuite>\n"
    ).encode("utf-8")


def _pytest_xml(spec: CaseSpec) -> bytes:
    """pytest 详情。traceback 要写成 ``文件:行号: in 函数``，后端才能解析出调用栈。"""
    case_lower = spec.case_id.lower()
    body = "\n".join(f"{path}:{number}: in {function}" for path, number, function in spec.call_chain)
    body += f"\nE       AssertionError: {spec.assertion}"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<testsuite name="pytest" tests="2" failures="1" errors="0" skipped="0">\n'
        f'  <testcase classname={quoteattr(spec.case_id)} name={quoteattr("test_" + case_lower)} time="742.310">\n'
        f'    <failure message={quoteattr("AssertionError: " + spec.assertion)}>'
        f"{xml_escape(body)}</failure>\n"
        "  </testcase>\n"
        '  <testcase classname="SN_OTHER_case" name="test_sn_other_case" time="12.500"/>\n'
        "</testsuite>\n"
    ).encode("utf-8")


def _test_html(spec: CaseSpec, line: CaseTimeline) -> bytes:
    """pytest-html 报告。必须有 ``<table id="results-table">``，行属性/文本含 case_id。"""
    case_lower = spec.case_id.lower()
    if spec.status == "failed":
        rows = (
            f'<tr class="result case-{case_lower}"><td>FAILED</td>'
            f"<td>test_{case_lower}</td><td>742.31s</td></tr>\n"
            '<tr class="detail"><td colspan="3"><div>'
            f"AssertionError: {html.escape(spec.assertion)}<br/>"
            f"at {spec.call_chain[-1][0]}:{spec.call_chain[-1][1]}: in {spec.call_chain[-1][2]}"
            "</div></td></tr>\n"
        )
    else:
        rows = (
            f'<tr class="result case-{case_lower}"><td>PASSED</td>'
            f"<td>test_{case_lower}</td><td>318.42s</td></tr>\n"
        )
    return (
        "<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\">"
        f"<title>pytest report - {html.escape(spec.case_id)}</title></head><body>\n"
        "<h1>Test Report</h1>\n"
        f'<p>Started {line.start:%Y-%m-%d %H:%M:%S} · Finished {line.stop:%Y-%m-%d %H:%M:%S}</p>\n'
        '<table id="results-table">\n'
        "<thead><tr><th>Result</th><th>Test</th><th>Duration</th></tr></thead>\n"
        "<tbody>\n"
        f"{rows}"
        '<tr class="result case-sn_other_case"><td>PASSED</td>'
        "<td>test_sn_other_case</td><td>12.50s</td></tr>\n"
        "</tbody>\n</table>\n</body></html>\n"
    ).encode("utf-8")


def _xytest_log(spec: CaseSpec, line: CaseTimeline) -> bytes:
    """xytest 日志。行首 ``[时间] [级别]`` 且级别必须是 ERROR/FATAL/CRITICAL/FAIL/FAILED。"""
    rows = [
        (line.start + timedelta(seconds=5), "INFO", "[T-1] case bootstrap finished"),
        (line.start + timedelta(seconds=35), "INFO", "[T-2] device session established"),
        (
            (line.failure - timedelta(minutes=1)) if spec.status == "failed" else (line.start + timedelta(minutes=1)),
            "INFO",
            "[T-3] running assertion batch",
        ),
    ]
    if spec.status == "failed":
        rows.append((line.failure, "ERROR", spec.xytest_message))
        rows.append((line.failure + timedelta(seconds=1), "FATAL", "[T-9] case aborted on assertion failure"))
    else:
        rows.append((line.failure + timedelta(seconds=20), "INFO", "[T-3] all assertions passed"))
    rows.sort(key=lambda item: item[0])
    return ("\n".join(f"[{_stamp(m)}] [{level}] {text}" for m, level, text in rows) + "\n").encode("utf-8")


def _event_line(moment: datetime, *, level: str, code: int, error_id: str, message: str) -> str:
    """十三字段运行事件日志（与 loggen.run_line 同布局，但可指定 ERROR 级）。"""
    linked = code - 10
    linked_error = error_id.replace("ERR", "ERR") and f"ERR{int(error_id[3:]) - 10}" if error_id else ""
    return (
        f"[{_stamp(moment)}] [RUNCTRL] [40001] [event] [process] [{level}] "
        f"[{code}] [{linked}] [{error_id}] [{linked_error}] "
        f"[DP{code}] [DP{linked}] [processing] {message}"
    )


def _event_log(spec: CaseSpec, line: CaseTimeline) -> bytes:
    """用例根 event.log：十三字段格式，每行带时间戳且单调递增。"""
    rows: list[tuple[datetime, str]] = []
    moment = line.start
    index = 0
    while moment <= line.stop:
        rows.append((moment, _event_line(
            moment,
            level="INFO",
            code=1000 + (index % 9) * 10,
            error_id="",
            message=("lot started, process sequence initialised", "recipe step advanced to next stage",
                     "process checkpoint reported nominal")[index % 3],
        )))
        index += 1
        moment = line.start + timedelta(seconds=index * EVENT_STEP_SECONDS)
    if spec.status == "failed":
        rows.append((line.failure - timedelta(seconds=5), _event_line(
            line.failure - timedelta(seconds=5), level="ERROR", code=1060,
            error_id="ERR660", message=spec.event_message,
        )))
    rows.sort(key=lambda item: item[0])
    return ("\n".join(text for _, text in rows) + "\n").encode("utf-8")


def _environment_html(spec: CaseSpec) -> bytes:
    """详细日志链接.html：环境报告。

    上位机用 ``192.*`` 拓扑 IP —— 会被正常解析，但 ``ensure_atlog_environment``
    会判定为小网并跳过自动录入，避免给环境资源塞一堆连不上的机器。
    """
    return (
        "<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\">"
        "<title>详细日志链接</title></head><body>\n"
        "<h2>环境信息[9GPB]</h2>\n"
        "<p>环境TOPO: TB1.0_SIM_GPB, SIM模式: sim0_sil</p>\n"
        "<h3>上位机SCH: 192.4.4.48</h3>\n"
        "<p>TICC-Agent: 192.4.4.48</p>\n"
        f"<p>root@192.4.4.48 {html.escape(spec.case_id)}-sim-pass</p>\n"
        "<h3>下位机</h3>\n"
        f"<h4>L3-SPUR_SUBRACK1_SLOT1_GPB: {spec.lower_ip}</h4>\n"
        f"<p><b>业务IP</b>: root@{spec.lower_ip}, sim-pass</p>\n"
        "</body></html>\n"
    ).encode("utf-8")


def _debug_lines(spec: CaseSpec, line: CaseTimeline) -> tuple[bytes, int]:
    """用例的调试日志夹具。行按时间戳单调递增（调用链规则下同样成立）。"""
    rows: list[tuple[datetime, str]] = []
    moment = line.start
    index = 0
    while moment <= line.stop:
        rows.append((moment, loggen.debug_line(moment, spec.subsystem, spec.module, index)))
        index += 1
        moment = line.start + timedelta(seconds=index * DEBUG_STEP_SECONDS)
    if spec.status == "failed":
        # 失败用例的调试日志必须在故障时刻留下错误行：一轮调用链程序里 ERROR 级别的
        # 正文就那么一两行，时间线短的时候整段都可能覆盖不到，"异常行不误报"那条
        # 校验也就失去了样本。这里直接把程序里的 ERROR 正文钉到故障时刻。
        rows.append((
            line.failure,
            loggen.debug_line(line.failure, spec.subsystem, spec.module, loggen.ERROR_STEP_INDEX),
        ))
    rows.sort(key=lambda item: item[0])
    return ("\n".join(text for _, text in rows) + "\n").encode("utf-8"), len(rows)


def _executor_lines(spec: CaseSpec, line: CaseTimeline) -> tuple[bytes, int]:
    rows = []
    moment = line.start
    index = 0
    while moment <= line.stop:
        rows.append(loggen.executor_line(
            moment, spec.subsystem, spec.module, index, inner=(index % 2 == 0)
        ))
        index += 1
        moment = line.start + timedelta(seconds=index * DEBUG_STEP_SECONDS)
    return ("\n".join(rows) + "\n").encode("utf-8"), len(rows)


# --------------------------------------------------------------------------- 目录树


def _case_relative_dir(spec: CaseSpec) -> Path:
    return Path(fleet.ATLOG_TASK_NAME) / fleet.ATLOG_BLOCK_NAME / spec.case_id


def case_files(spec: CaseSpec, line: CaseTimeline) -> dict[Path, bytes]:
    """一个用例目录下的全部文件（相对用例根）。"""
    files: dict[Path, bytes] = {
        Path("summary_report.xml"): _summary_xml(spec, line),
        Path("xytest.log"): _xytest_log(spec, line),
        Path("event.log"): _event_log(spec, line),
        Path(f"test_{spec.case_id.lower()}.html"): _test_html(spec, line),
        Path("详细日志链接.html"): _environment_html(spec),
        Path("summary.ini"): (
            f"[case]\nid = {spec.case_id}\nstatus = {spec.status}\n"
            f"start = {line.start:%Y-%m-%d %H:%M:%S}\nstop = {line.stop:%Y-%m-%d %H:%M:%S}\n"
        ).encode("utf-8"),
    }
    if spec.status == "failed":
        files[Path("result") / "pytest-01.xml"] = _pytest_xml(spec)
    debug_name = f"{spec.module}.log"
    debug_relative = (
        Path("full_logs/log/debug") / spec.subsystem / debug_name
        if spec.debug_layout == "flat"
        else Path("full_logs/log/debug") / spec.subsystem / spec.module / debug_name
    )
    files[debug_relative] = _debug_lines(spec, line)[0]
    # 执行器树：full_logs/log/debug/elog/<IP>/<子系统>/<模块>_cp_01.log
    files[Path("full_logs/log/debug/elog") / spec.lower_ip / spec.subsystem / f"{spec.module}_cp_01.log"] = (
        _executor_lines(spec, line)[0]
    )
    # 兼容 legacy 镜像根 full_logs/log/elog/<IP>/...
    files[Path("full_logs/log/elog") / spec.lower_ip / spec.subsystem / f"{spec.module}_cp_02.log"] = (
        _executor_lines(spec, line)[0]
    )
    return files


def generate(*, now: datetime | None = None, report: loggen.GenerationReport | None = None, prune: bool = True) -> list[dict]:
    """生成全部用例目录，返回可直接粘贴的 URL 列表。"""
    moment = (now or datetime.now()).replace(microsecond=0)
    sink = report if report is not None else loggen.GenerationReport(anchor=moment)
    keep: set[Path] = set()

    for spec in CASES:
        line = _timeline(spec, moment)
        base = SITE_ROOT / _case_relative_dir(spec)
        for relative, payload in case_files(spec, line).items():
            target = base / relative
            keep.add(target.resolve())
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            sink.files += 1
            sink.bytes_written += len(payload)

    if prune:
        loggen.prune_tree(SITE_ROOT, keep, sink)
    return case_urls()


def case_urls(host: str | None = None, port: int | None = None) -> list[dict]:
    """用例 URL 列表（含本地文件路径，便于核对生成结果）。

    默认 host 用**局域网 IP**（``fleet.public_host()``），因为这份 URL 是要
    展示给用户、粘进「用例 URL 分析」的；本机探测仍走 127.0.0.1。
    """
    host = host or fleet.public_host()
    port = port or fleet.ATLOG_SITE_PORT
    values: list[dict] = []
    for spec in CASES:
        relative = _case_relative_dir(spec)
        quoted = urllib.parse.quote(str(relative).replace("\\", "/"), safe="/")
        values.append({
            "case_id": spec.case_id,
            "subsystem": spec.subsystem,
            "module": spec.module,
            "status": spec.status,
            "url": f"http://{host}:{port}/{quoted}/",
            "local_path": str(SITE_ROOT / relative),
        })
    return values


def reset() -> None:
    """删除报告站的全部文件。仅用于 --fresh。"""
    import shutil

    if SITE_ROOT.exists():
        shutil.rmtree(SITE_ROOT, ignore_errors=True)


# --------------------------------------------------------------------------- HTTP 服务


class _ReportSiteHandler(BaseHTTPRequestHandler):
    """最小 nginx：目录出 autoindex，文件支持 HEAD 与 Range。"""

    server_version = "TraceLensSimReportSite/1.0"
    protocol_version = "HTTP/1.1"

    # 继承自 BaseHTTPRequestHandler；由 serve() 注入
    root: Path = SITE_ROOT
    # 虚拟挂载：URL 前缀 → 本地目录。用于把"只走 SSH 的"Cpd 报告/数据也搬到一个 HTTP URL 上，
    # 这样"报告文件 URL 分析"对 .rpt / .xlsx 同样可用。
    mounts: dict[str, Path] = {}

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - 基类签名
        # 静音：报告站的访问日志不该混进 sim 的输出
        return

    # ------------------------------------------------------------------ 路由

    def _resolve(self) -> tuple[str, Path]:
        parsed = urllib.parse.urlsplit(self.path)
        raw = urllib.parse.unquote(parsed.path)
        # posixpath.normpath 会把 "/a/b/" 收敛成 "/a/b"，尾斜杠信息要单独记住：
        # 丢掉它的话目录会被我们自己的 301 反复重定向（死循环）。
        wants_dir = raw.endswith("/")
        clean = posixpath.normpath(raw)
        if not clean.startswith("/"):
            clean = "/" + clean
        if clean.startswith("/../") or clean.endswith("/..") or clean == "/..":
            raise ValueError("路径越界")
        # 先看虚拟挂载：/cpd/report/<子系统>/<模块>/<文件>.rpt → 远端 CPD 报告根
        for prefix, base in sorted(self.mounts.items(), key=lambda item: -len(item[0])):
            if clean == prefix or clean.startswith(prefix + "/"):
                rest = clean[len(prefix):].strip("/")
                if {item for item in rest.split("/") if item} & {".."}:
                    raise ValueError("路径越界")
                display = prefix + ("/" + rest if rest else "/")
                if wants_dir and not display.endswith("/"):
                    display += "/"
                return display, (base / rest if rest else base)
        display = clean if (clean == "/" or not wants_dir) else clean + "/"
        return display, self.root / clean.lstrip("/")

    def do_GET(self) -> None:  # noqa: N802 - 基类命名
        self._respond(with_body=True)

    def do_HEAD(self) -> None:  # noqa: N802 - 基类命名
        self._respond(with_body=False)

    def _respond(self, *, with_body: bool) -> None:
        try:
            display, local = self._resolve()
        except ValueError:
            return self._send_error(403, with_body)
        if self._is_mount_point(display):
            # 挂载根本身不是真目录，合成一层索引把可访问的挂载列出来
            if not display.endswith("/"):
                return self._redirect(display + "/")
            entries = [
                (prefix.strip("/").rsplit("/", 1)[-1], 0, 0.0, True)
                for prefix in sorted(self.mounts)
            ]
            return self._send_listing(display, entries, with_body)
        if local.is_dir():
            if not display.endswith("/"):
                # 与 nginx 一致：目录不带尾斜杠就重定向，保证后端能拿到相对路径
                return self._redirect(display + "/")
            return self._send_index(display, local, with_body)
        if local.is_file():
            return self._send_file(local, with_body)
        return self._send_error(404, with_body)

    def _is_mount_point(self, display: str) -> bool:
        path = display.rstrip("/")
        if path == CPD_MOUNT:
            return True
        return any(
            path == prefix.rsplit("/", 1)[0] and prefix.startswith(CPD_MOUNT)
            for prefix in self.mounts
        )

    def _redirect(self, location: str) -> None:
        self.send_response(301)
        self.send_header("Location", urllib.parse.quote(location, safe="/._-()[]"))
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ------------------------------------------------------------------ 目录

    def _send_index(self, clean: str, local: Path, with_body: bool) -> None:
        entries: list[tuple[str, int, float, bool]] = []
        for item in sorted(local.iterdir(), key=lambda p: p.name):
            try:
                info = item.stat()
            except OSError:
                continue
            entries.append((item.name, info.st_size, info.st_mtime, item.is_dir()))
        if clean.rstrip("/") in {"", "/"}:
            # 站根目录额外挂一个 cpd/ 入口，方便一眼看到测校报告与数据表格
            known = {name for name, *_ in entries}
            for prefix in self.mounts:
                head = prefix.strip("/").split("/", 1)[0]
                if head and head not in known:
                    entries.append((head, 0, 0.0, True))
                    known.add(head)
        return self._send_listing(clean, sorted(entries, key=lambda item: item[0]), with_body)

    def _send_listing(
        self, clean: str, entries: list[tuple[str, int, float, bool]], with_body: bool
    ) -> None:
        rows = [
            "<html>",
            f"<head><title>Index of {html.escape(clean)}</title></head>",
            "<body>",
            f"<h1>Index of {html.escape(clean)}</h1>",
            '<hr><pre><a href="../">../</a>',
        ]
        for name, size, mtime, is_dir in entries:
            # 目录链接必须带尾斜杠，否则后端不会当目录；非 ASCII 名按 nginx 习惯百分号编码
            href = urllib.parse.quote(name + ("/" if is_dir else ""), safe="/._-()[]")
            label = html.escape(name + ("/" if is_dir else ""))
            when = datetime.fromtimestamp(mtime).strftime("%d-%b-%Y %H:%M") if mtime else " " * 17
            size_text = "-" if is_dir else str(size)
            rows.append(
                f'<a href="{href}">{label}</a>{" " * max(1, 44 - len(name))}{when} {size_text:>10}'
            )
        rows.append("</pre><hr></body></html>")
        payload = ("\n".join(rows) + "\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if with_body:
            self.wfile.write(payload)

    # ------------------------------------------------------------------ 文件

    def _send_file(self, local: Path, with_body: bool) -> None:
        try:
            total = local.stat().st_size
        except OSError:
            return self._send_error(404, with_body)
        content_type = _CONTENT_TYPES.get(local.suffix.lower(), "application/octet-stream")
        start, end = 0, max(0, total - 1)
        status = 200
        range_header = self.headers.get("Range", "").strip()
        if range_header.startswith("bytes="):
            raw = range_header[len("bytes="):].split(",")[0].strip()
            head, _, tail = raw.partition("-")
            try:
                if head:
                    start = int(head)
                    end = int(tail) if tail else total - 1
                else:
                    # bytes=-N：最后 N 字节
                    start = max(0, total - int(tail or 0))
                end = min(end, total - 1)
                if start < 0 or start > end:
                    raise ValueError
                status = 206
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        length = max(0, end - start + 1) if total else 0
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()
        if not with_body or not length:
            return
        with local.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _send_error(self, code: int, with_body: bool) -> None:
        text = {403: "Forbidden", 404: "Not Found"}.get(code, "Error")
        payload = f"<html><body><h1>{code} {text}</h1></body></html>\n".encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if with_body:
            self.wfile.write(payload)


class ReportSiteServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler_cls, root: Path, mounts: dict[str, Path] | None = None) -> None:
        handler = type(
            "BoundHandler",
            (handler_cls,),
            {"root": root, "mounts": dict(mounts or {})},
        )
        super().__init__(address, handler)


class ReportSiteGroup:
    """报告站在多个地址上的监听集合（网络 IP + 回环）。

    给用户粘贴的 URL 用的是局域网 IP，本机探测用 127.0.0.1，两个都要通，
    所以同一端口起多个 server，各自在独立线程里 serve_forever。
    """

    def __init__(self, servers: list[ReportSiteServer]) -> None:
        self.servers = servers
        self._threads: list[threading.Thread] = []

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self._threads = [
            threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": poll_interval},
                daemon=True,
                name=f"site-{server.server_address[0]}",
            )
            for server in self.servers
        ]
        for thread in self._threads:
            thread.start()
        try:
            while any(thread.is_alive() for thread in self._threads):
                time.sleep(0.5)
        except KeyboardInterrupt:
            self.shutdown()
            raise

    def shutdown(self) -> None:
        for server in self.servers:
            try:
                server.shutdown()
            except Exception:  # noqa: BLE001
                pass

    def server_close(self) -> None:
        for server in self.servers:
            try:
                server.server_close()
            except Exception:  # noqa: BLE001
                pass


def cpd_mounts() -> dict[str, Path]:
    """CPD 报告 / 测校数据在报告站上的虚拟挂载。

    注意 ``fleet.CPD_REPORT_ROOT`` 是**格式模板**（含 ``{username}``），
    必须用 ``MachineSpec`` 上展开过的属性，否则会映射出一个不存在的目录。
    """
    upper = fleet.UPPER
    return {
        CPD_REPORT_MOUNT: fleet.remote_to_local(upper, upper.cpd_report_root),
        CPD_DATA_MOUNT: fleet.remote_to_local(upper, upper.cpd_data_root),
    }


def cpd_urls(host: str | None = None, port: int | None = None) -> list[dict]:
    """每个 CPD 模块挑一份报告 + 一份测校数据表格，给出可直接粘贴的 URL。"""
    host = host or fleet.public_host()
    port = port or fleet.ATLOG_SITE_PORT
    report_root = fleet.remote_to_local(fleet.UPPER, fleet.UPPER.cpd_report_root)
    data_root = fleet.remote_to_local(fleet.UPPER, fleet.UPPER.cpd_data_root)
    values: list[dict] = []
    for subsystem, modules in sorted(fleet.CPD_MODULES.items()):
        for module in modules:
            reports = sorted((report_root / subsystem / module).glob("*.rpt"))
            sheets = sorted((data_root / subsystem.lower() / module.lower()).glob("*.xlsx"))
            values.append({
                "subsystem": subsystem,
                "module": module,
                "report_name": reports[-1].name if reports else "",
                "report_url": (
                    f"http://{host}:{port}{CPD_REPORT_MOUNT}/{subsystem}/{module}/{reports[-1].name}"
                    if reports else ""
                ),
                "data_name": sheets[-1].name if sheets else "",
                "data_url": (
                    f"http://{host}:{port}{CPD_DATA_MOUNT}/{subsystem.lower()}/{module.lower()}/{sheets[-1].name}"
                    if sheets else ""
                ),
                "report_count": len(reports),
                "data_count": len(sheets),
            })
    return values


def serve(
    host: str | None = None,
    port: int | None = None,
    hosts: tuple[str, ...] | None = None,
) -> ReportSiteGroup:
    """启动报告站（调用方负责 serve_forever / 关闭）。

    默认同时绑 ``fleet.bind_hosts()``（局域网 IP + 回环）。传 ``host`` 则只绑那一个。
    """
    port = int(port or fleet.ATLOG_SITE_PORT)
    if not SITE_ROOT.exists():
        raise RuntimeError(f"报告站尚未生成：{SITE_ROOT}，先跑 `python -m simremote.cli init`")
    if hosts is None:
        hosts = (host,) if host else fleet.bind_hosts()
    servers: list[ReportSiteServer] = []
    try:
        for bind_host in hosts:
            servers.append(
                ReportSiteServer((bind_host, port), _ReportSiteHandler, SITE_ROOT, cpd_mounts())
            )
    except OSError:
        for server in servers:
            server.server_close()
        raise
    return ReportSiteGroup(servers)


def serve_forever(
    host: str | None = None,
    port: int | None = None,
    hosts: tuple[str, ...] | None = None,
) -> None:
    group = serve(host, port, hosts)
    group.serve_forever()


def serve_forever(host: str | None = None, port: int | None = None) -> None:
    server = serve(host, port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
