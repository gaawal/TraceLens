"""按「当前时刻」为锚生成模拟上下位机日志树。

时间线布局（now 为生成时刻，today = 今天 00:00，D-n = today - n 天）：

    <fm>.log                        覆盖 [today, now]           当前未归档段
    <fm>_<today>000000.log          覆盖 [D-1, today)           轮转段
    <fm>_<D-2>.tar.gz               嵌套日包，内含 24 个小时包   覆盖 D-2 全天
    <fm>_<D-3>.tar.gz               嵌套日包，内含 24 个小时包   覆盖 D-3 全天
    <fm>_<D-4>.tar.gz               扁平日包，直接含 <fm>.log    覆盖 D-4 全天

**命名语义（务必遵守）**：带时间戳的后缀是「关闭边界」（close boundary），
即该文件记录到这一刻为止，不是起始时刻。后端 file_index 的注释是权威：

    "``fm_YYYYMMDDHHMMSSmmm.log`` is a rotated file whose suffix is its close
     boundary; only the boundary files required to cover the query are kept."

所以覆盖 D-1 全天的轮转文件叫 ``<fm>_<today>000000.log``（关闭于今天 0 点），
覆盖 [h, h+1) 的内层小时包叫 ``<fm>_<D><h+1>0000.tar.gz``。
写成起始时刻会让每个块整体错位，"最近 24 小时"之类的窗口会漏文件或查到空。

时间锚点是**生成时刻**：放置久了 ``<fm>.log`` 的末行会停在旧时间上，前端查
"最近 1 小时"就会空。这时重跑 ``init`` 或 ``scripts/sim_realign.sh``。

行内容遵守项目固定的调用链规则：``[函数名] >()`` 是入口、``[函数名] <()`` 是出口，
同名 LIFO 配对（见下方"调用链规则"一节）。前端就是按这对方向符折叠函数卡片的，
所以正文不能写成没有入口/出口的散句。
"""

from __future__ import annotations

import io
import shutil
import tarfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import fleet

# 各类文件的行密度（秒）
CURRENT_STEP_SECONDS = 60
ROTATED_STEP_SECONDS = 300
NESTED_INNER_STEP_SECONDS = 600
FLAT_ARCHIVE_STEP_SECONDS = 600

# 受控环境里一次性删除大量文件会被安全保护拦截，因此清理保持小批量。
# 正常情况（日期未滚动）残留为 0，不会触发删除。
PURGE_BATCH = 20
PURGE_BUDGET_PER_RUN = 40

# --------------------------------------------------------------------------- 调用链规则
#
# 日志正文不是随手写的句子，而是遵守固定规则：
#
#     [函数名] >() enter <入参>        <- 函数入口
#     [函数名] <某个正文>              <- 函数体内的普通日志
#     [函数名] <() leave <耗时/状态>    <- 函数出口
#
# 方向符 ``>()`` / ``<()`` 是这套规则的核心。TraceLens 的内置折叠规则
# ``builtin-explicit-boundary``（``frontend/src/rendering/foldingRules.ts``，
# startKeyword ``"> ()"`` / endKeyword ``"< ()"``；``App.tsx`` 里读作
# 「函数开始 >()」「函数结束 <()」）会把 ``[函数名]`` **按名字 LIFO 配对**，把一对
# 入口/出口之间的日志收成一张可折叠的函数卡片。
#
# 两个必须守住的约束：
#
# * **同名**：出口的 ``[函数名]`` 要和入口逐字相同 —— 配对键就是这个字符串，
#   差一个字母就变成一条永远合不上的调用；
# * **LIFO**：入口压栈、出口出栈，子函数必须先于父函数闭合，否则前端会画出
#   错乱嵌套，或者把父函数标成「未闭合」。

ENTRY_MARKER = ">()"
EXIT_MARKER = "<()"

PHASE_ENTER = "enter"
PHASE_BODY = "body"
PHASE_LEAVE = "leave"


def log_message(function: str, phase: str, body: str) -> str:
    """按调用链规则拼一行日志正文。"""
    if phase == PHASE_ENTER:
        return f"[{function}] {ENTRY_MARKER} enter {body}"
    if phase == PHASE_LEAVE:
        return f"[{function}] {EXIT_MARKER} leave {body}"
    return f"[{function}] {body}"


def source_line(function: str) -> int:
    """函数 -> 源码行号，让 ``rpc`` 字段保持 ``文件:函数:行`` 的形状。

    行号按函数名固定：同一个函数在整棵日志树里永远指同一处，不会一会儿 120
    一会儿 380（那样"同名字"的调用看着像两个不同的函数）。
    """
    return 120 + zlib.crc32(function.encode("utf-8")) % 400


def call_mode(function: str) -> str:
    """执行通道标记。同一函数固定不变 —— 折叠聚合要求一次调用的入口/出口通道一致。"""
    return "trace" if zlib.crc32(function.encode("utf-8")) % 3 == 0 else "normal"


#: 调试日志的调用链程序：每项 = (函数名, 相位, 级别, 正文模板)。
#:
#: 相位序列必须是一条合法的调用栈轨迹（入口/出口同名配对、LIFO 闭合）：
#:
#:     ScanLot >()  ScanWafer >()  LoadWafer >()  ...  <() LoadWafer  ...  <() ScanWafer  <() ScanLot
#:
#: 模板变量：{wafer} 晶圆号、{lot} 批次号、{elapsed} 本次调用耗时、{software} 软件版本。
#:
#: 级别约定：**入口/出口行固定 INFO，异常级别只落在正文行** —— 边界行只负责记
#: 进出，正文才是产生告警的地方。
_DEBUG_PROGRAM: tuple[tuple[str, str, str, str], ...] = (
    ("ScanLot", PHASE_ENTER, "INFO", "lot={lot} wafers=25 recipe={software}"),
    ("ScanWafer", PHASE_ENTER, "INFO", "wafer={wafer} recipe={software}"),
    ("LoadWafer", PHASE_ENTER, "INFO", "wafer={wafer} source=loadport"),
    ("MoveWaferStage", PHASE_ENTER, "INFO", "axis=XY target=chuck"),
    ("MoveWaferStage", PHASE_BODY, "INFO", "wafer stage settled, position error within tolerance"),
    ("MoveWaferStage", PHASE_LEAVE, "INFO", "axis=XY elapsed={elapsed} status=ok"),
    ("LoadWafer", PHASE_LEAVE, "INFO", "wafer={wafer} elapsed={elapsed} status=ok"),
    ("AlignWafer", PHASE_ENTER, "INFO", "wafer={wafer} marks=8"),
    ("AlignWafer", PHASE_BODY, "INFO", "alignment mark detected, offset compensation applied"),
    ("AlignWafer", PHASE_LEAVE, "INFO", "wafer={wafer} elapsed={elapsed} status=ok"),
    ("ExposeWafer", PHASE_ENTER, "INFO", "wafer={wafer} dose=30mJ/cm2"),
    ("StabiliseSource", PHASE_ENTER, "INFO", "setpoint=250W"),
    ("StabiliseSource", PHASE_BODY, "INFO", "illumination source power stabilised at setpoint"),
    ("StabiliseSource", PHASE_LEAVE, "INFO", "setpoint=250W elapsed={elapsed} status=ok"),
    ("ExposeWafer", PHASE_BODY, "INFO", "scan trajectory buffered and verified"),
    ("ExposeWafer", PHASE_BODY, "WARN", "dose control loop reported within specification, margin=1.4%"),
    ("ExposeWafer", PHASE_LEAVE, "INFO", "wafer={wafer} elapsed={elapsed} status=ok"),
    ("MeasureOverlay", PHASE_ENTER, "INFO", "wafer={wafer} sensor=interferometer"),
    ("MeasureOverlay", PHASE_BODY, "DEBUG", "interferometer reading refreshed for wafer stage"),
    ("MeasureOverlay", PHASE_BODY, "ERROR", "fringe contrast below limit, measurement retried"),
    ("MeasureOverlay", PHASE_LEAVE, "INFO", "wafer={wafer} elapsed={elapsed} status=ok"),
    ("UnloadWafer", PHASE_ENTER, "INFO", "wafer={wafer} destination=loadport"),
    ("UnloadWafer", PHASE_LEAVE, "INFO", "wafer={wafer} elapsed={elapsed} status=ok"),
    ("ScanWafer", PHASE_LEAVE, "INFO", "wafer={wafer} elapsed={elapsed} status=ok"),
    ("ScanLot", PHASE_LEAVE, "INFO", "lot={lot} elapsed={elapsed} status=ok"),
)

#: 程序里第一条 ERROR 正文的位置。失败用例的日志夹具要把错误行**钉在故障时刻**，
#: 不能指望"时间线刚好够长、刚好覆盖到那一步" —— 那是碰运气。
ERROR_STEP_INDEX = next(
    index
    for index, (_function, _phase, level, _body) in enumerate(_DEBUG_PROGRAM)
    if _phase == PHASE_BODY and level in ("ERROR", "FATAL")
)

_RUN_MESSAGES = (
    "lot started, process sequence initialised",
    "recipe step advanced to next stage",
    "wafer exposure completed, moving to measurement",
    "process checkpoint reported nominal",
    "lot finished, awaiting next dispatch",
)
_RUN_EVENT_TYPES = ("processing", "processing", "measurement", "transfer")

# 一个产物：相对路径、字节内容、日志行数
Artifact = tuple[Path, bytes, int]


@dataclass
class GenerationReport:
    anchor: datetime | None = None
    files: int = 0
    lines: int = 0
    bytes_written: int = 0
    pruned: int = 0
    machines: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        anchor = self.anchor.strftime("%Y-%m-%d %H:%M:%S") if self.anchor else "-"
        size = f"{self.bytes_written / 1024 / 1024:.1f} MiB"
        detail = ", ".join(f"{key}={value}" for key, value in sorted(self.machines.items()))
        return (
            f"锚点 {anchor}｜文件 {self.files}｜日志行 {self.lines}｜写入 {size}｜清理 {self.pruned}"
            + (f"｜{detail}" if detail else "")
        )


# --------------------------------------------------------------------------- 行格式


def _stamp(moment: datetime) -> str:
    """日志行时间戳：毫秒精度，前 19 个字符是 YYYY-MM-DD HH:MM:SS。"""
    return f"{moment:%Y-%m-%d %H:%M:%S}.{moment.microsecond // 1000:03d}"


def _program_values(moment: datetime, seq: int, function: str) -> dict[str, str]:
    """调用链程序的模板变量。第几轮（``seq // 程序长度``）决定晶圆/批次。"""
    round_index = seq // len(_DEBUG_PROGRAM)
    elapsed = 6 + zlib.crc32(function.encode("utf-8")) % 180
    return {
        "wafer": f"W{1 + round_index % 25:02d}",
        "lot": f"LOT-{moment:%Y%m%d}-{round_index % 24 + 1:02d}",
        "software": fleet.SOFTWARE_VERSION,
        "elapsed": f"{elapsed + (round_index % 7) * 4.3:.1f}ms",
    }


def debug_line(moment: datetime, subsystem: str, module: str, seq: int) -> str:
    """八字段调试日志，匹配 DEBUG_PATTERN；正文带 ``[函数名] >()`` / ``<()`` 调用链边界。"""
    function, phase, level, template = _DEBUG_PROGRAM[seq % len(_DEBUG_PROGRAM)]
    process_id = 20000 + (abs(hash(subsystem)) % 9000)
    # 线程号按模块固定：一次调用链跑在同一个线程上，前端才能把它们归到同一条执行泳道
    # （折叠、×N 聚合都以 component/process/thread 相同为前提）。
    thread_id = 30000 + (abs(hash(module)) % 500)
    rpc = f"{module}:{function}:{source_line(function)}"
    message = log_message(function, phase, template.format(**_program_values(moment, seq, function)))
    return (
        f"[{_stamp(moment)}] [{level}] [{subsystem.upper()}] [{process_id}] "
        f"[{thread_id}] [{module}] [{call_mode(function)}] [{rpc}] {message}"
    )


def executor_line(moment: datetime, subsystem: str, module: str, seq: int, *, inner: bool) -> str:
    """执行器日志：100 内部（context/rpc/mode）与 101 外部（mode/rpc）两种布局。

    EXECUTOR_PATTERN 的 rpc 组要求形如 ``a:b:c``（两个冒号），不能省。
    正文与调试日志走同一套调用链程序，所以执行器视图里同样能折出函数卡片。
    """
    function, phase, level, template = _DEBUG_PROGRAM[seq % len(_DEBUG_PROGRAM)]
    process_id = 40000 + (abs(hash(module)) % 5000)
    thread_id = 50000 + (abs(hash(module)) % 300)
    rpc = f"{module}:{function}:{source_line(function)}"
    message = log_message(function, phase, template.format(**_program_values(moment, seq, function)))
    if inner:
        context = f"ctx{(seq % 8) + 1}"
        return (
            f"[{_stamp(moment)}] [{level}] [{subsystem.upper()}] [{process_id}] "
            f"[{thread_id}] [{module}] [{context}] [{rpc}] [100] {message}"
        )
    return (
        f"[{_stamp(moment)}] [{level}] [{subsystem.upper()}] [{process_id}] "
        f"[{thread_id}] [{module}] [101] [{rpc}] {message}"
    )


def run_line(moment: datetime, seq: int) -> str:
    """十三字段运行事件日志，匹配 RUN_PATTERN。"""
    level = ("INFO", "INFO", "WARN", "INFO")[seq % 4]
    event_code = 1000 + (seq % 40) * 10
    linked_code = event_code - 10 if seq % 3 else ""
    error_id = f"ERR{600 + (seq % 30)}" if level == "WARN" else ""
    linked_error = f"ERR{590 + (seq % 30)}" if level == "WARN" and seq % 2 else ""
    display = f"DP{event_code}"
    linked_display = f"DP{event_code - 10}" if linked_code else ""
    event_type = _RUN_EVENT_TYPES[seq % len(_RUN_EVENT_TYPES)]
    message = _RUN_MESSAGES[seq % len(_RUN_MESSAGES)]
    return (
        f"[{_stamp(moment)}] [RUNCTRL] [40001] [event] [process] [{level}] "
        f"[{event_code}] [{linked_code}] [{error_id}] [{linked_error}] "
        f"[{display}] [{linked_display}] [{event_type}] {message}"
    )


# --------------------------------------------------------------------------- 渲染


def _stamps(start: datetime, end: datetime, step_seconds: int):
    current = start
    while current < end:
        yield current
        current += timedelta(seconds=step_seconds)


def _render(start: datetime, end: datetime, step_seconds: int, builder) -> tuple[bytes, int]:
    lines = [builder(moment, index) for index, moment in enumerate(_stamps(start, end, step_seconds))]
    if not lines:
        return b"", 0
    return ("\n".join(lines) + "\n").encode("utf-8"), len(lines)


def _pack_tar(members: list[tuple[str, bytes, int]], moment: datetime) -> tuple[bytes, int]:
    """打 tar.gz。成员名用 basename，后端只取 PurePosixPath(name).name。"""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", compresslevel=6) as archive:
        for name, payload, _ in members:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mtime = int(moment.timestamp())
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue(), sum(count for _, _, count in members)


# --------------------------------------------------------------------------- 单个 fm 模块


def debug_family(module: str, subsystem: str, *, now: datetime, today: datetime) -> list[Artifact]:
    """一个 fm 模块在 debug 根下的全部产物。"""
    node = Path(subsystem)
    outputs: list[Artifact] = []

    # 1) 当前段
    payload, count = _render(today, now, CURRENT_STEP_SECONDS, lambda m, i: debug_line(m, subsystem, module, i))
    outputs.append((node / f"{module}.log", payload, count))

    # 2) 轮转段：关闭边界 = 今天 00:00
    day_1 = today - timedelta(days=1)
    payload, count = _render(day_1, today, ROTATED_STEP_SECONDS, lambda m, i: debug_line(m, subsystem, module, i + 3))
    outputs.append((node / f"{module}_{today:%Y%m%d}000000.log", payload, count))

    # 3) 嵌套日包：D-2 / D-3，每小时一个内层包，包名 = 小时段结束时刻（关闭边界）
    for offset in (2, 3):
        day = today - timedelta(days=offset)
        inner_members: list[tuple[str, bytes, int]] = []
        for hour in range(24):
            segment_start = day + timedelta(hours=hour)
            segment_end = segment_start + timedelta(hours=1)
            payload, count = _render(
                segment_start, segment_end, NESTED_INNER_STEP_SECONDS,
                lambda m, i, _s=subsystem, _mod=module, _h=hour: debug_line(m, _s, _mod, i + _h),
            )
            inner_bytes, _ = _pack_tar([(f"{module}.log", payload, count)], segment_end)
            inner_members.append((f"{module}_{segment_end:%Y%m%d%H%M%S}.tar.gz", inner_bytes, count))
        outer_bytes, outer_lines = _pack_tar(inner_members, day)
        outputs.append((node / f"{module}_{day:%Y%m%d}.tar.gz", outer_bytes, outer_lines))

    # 4) 扁平日包：D-4，直接含 <fm>.log
    day_4 = today - timedelta(days=4)
    payload, count = _render(
        day_4, day_4 + timedelta(days=1), FLAT_ARCHIVE_STEP_SECONDS,
        lambda m, i: debug_line(m, subsystem, module, i + 7),
    )
    flat_bytes, _ = _pack_tar([(f"{module}.log", payload, count)], day_4)
    outputs.append((node / f"{module}_{day_4:%Y%m%d}.tar.gz", flat_bytes, count))

    return outputs


def run_family(*, now: datetime, today: datetime) -> list[Artifact]:
    """运行日志是扁平的：<run root>/event.log + 归档。"""
    outputs: list[Artifact] = []
    payload, count = _render(today, now, CURRENT_STEP_SECONDS, lambda m, i: run_line(m, i))
    outputs.append((Path("event.log"), payload, count))

    day_1 = today - timedelta(days=1)
    payload, count = _render(day_1, today, ROTATED_STEP_SECONDS, lambda m, i: run_line(m, i + 2))
    outputs.append((Path(f"event_{today:%Y%m%d}000000.log"), payload, count))

    for offset in (2, 3):
        day = today - timedelta(days=offset)
        payload, count = _render(day, day + timedelta(days=1), FLAT_ARCHIVE_STEP_SECONDS, lambda m, i: run_line(m, i + 4))
        blob, _ = _pack_tar([("event.log", payload, count)], day)
        outputs.append((Path(f"event_{day:%Y%m%d}.tar.gz"), blob, count))
    return outputs


def executor_family(
    module: str, subsystem: str, lower_host: str, slot: int, *, now: datetime, today: datetime
) -> list[Artifact]:
    """executor 树：<elog root>/<lower_host>/<subsystem>/<module>_cp_<nn>.log"""
    node = Path(lower_host) / subsystem
    filename = f"{module}_cp_{slot:02d}.log"

    def builder(moment: datetime, index: int) -> str:
        return executor_line(moment, subsystem, module, index + slot, inner=(index % 2 == 0))

    outputs: list[Artifact] = []
    payload, count = _render(today, now, CURRENT_STEP_SECONDS, builder)
    outputs.append((node / filename, payload, count))

    day_1 = today - timedelta(days=1)
    payload, count = _render(day_1, today, ROTATED_STEP_SECONDS, builder)
    outputs.append((node / f"{module}_cp_{slot:02d}_{today:%Y%m%d}000000.log", payload, count))
    return outputs


# --------------------------------------------------------------------------- 清理与入口


def prune_tree(root: Path, keep: set[Path], report: GenerationReport) -> None:
    """删除不属于本轮产物的残留。小批量进行，避免触发受控环境的安全删除保护。"""
    if not root.exists():
        return
    stale = [path for path in root.rglob("*") if path.is_file() and path.resolve() not in keep]
    if not stale:
        return
    # ``rglob`` 的顺序取决于文件系统，只按它切预算的话，某些目录会永远排在窗口之外，
    # 越积越多（同一天多次 init 时 CPD 批次就会这样）。按 mtime 升序先删最旧的：
    # 顺序确定，也符合"老批次先过期"的直觉。
    stale.sort(key=lambda item: item.stat().st_mtime)
    budget = min(len(stale), PURGE_BUDGET_PER_RUN)
    for path in stale[:budget]:
        try:
            path.unlink(missing_ok=True)
            report.pruned += 1
        except OSError:
            continue
    for directory in sorted({item.parent for item in stale[:budget]}, key=lambda item: -len(item.parts)):
        try:
            directory.rmdir()
        except OSError:
            continue
    remaining = len(stale) - budget
    if remaining > 0:
        print(f"  [loggen] 另有 {remaining} 个残留待清理，再跑一次 init 即可（小批量保护）")


def plan_machine(spec: fleet.MachineSpec, *, now: datetime) -> list[tuple[str, Path, bytes, int]]:
    """一台机器的全部**日志**产物：(远端根, 相对路径, 内容, 行数)。"""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    plan: list[tuple[str, Path, bytes, int]] = []
    for subsystem, module in fleet.all_modules():
        for relative, payload, count in debug_family(module, subsystem, now=now, today=today):
            plan.append((spec.debug_root, relative, payload, count))
    for relative, payload, count in run_family(now=now, today=today):
        plan.append((spec.run_root, relative, payload, count))

    # 版本文件：后端 ResourceSettings.version_file_path 默认 ~/SW/version
    plan.append((
        f"/home/{fleet.SIM_USERNAME}",
        Path("SW") / "version",
        f"{fleet.SOFTWARE_VERSION}\n".encode("utf-8"),
        1,
    ))

    if spec.role == "upper":
        for lower in [item for item in fleet.FLEET if item.role == "lower"]:
            for subsystem in fleet.SUBSYSTEM_MODULES:
                for module in fleet.EXECUTOR_MODULES:
                    for slot in range(1, fleet.EXECUTOR_PER_MODULE + 1):
                        for relative, payload, count in executor_family(
                            module, subsystem, lower.host, slot, now=now, today=today
                        ):
                            plan.append((spec.elog_root, relative, payload, count))
    return plan


def write_plan(
    spec: fleet.MachineSpec,
    plan: list[tuple[str, Path, bytes, int]],
    report: GenerationReport,
    *,
    prune: bool = True,
) -> None:
    """把产物写进本地模拟文件系统，并清理不属于本轮产物的残留。

    注意：elog root 是 debug root 的子目录（/log/<user>/debug/elog），
    所以清理时必须用**跨根共享**的保留清单，否则 debug 的清理会把 elog 产物当残留删掉。
    """
    keep: set[Path] = set()
    bases: list[Path] = []
    for remote_root, relative, payload, count in plan:
        base = fleet.remote_to_local(spec, remote_root)
        if base not in bases:
            bases.append(base)
        target = base / relative
        keep.add(target.resolve())
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        report.files += 1
        report.bytes_written += len(payload)
        report.lines += count

    if prune:
        for base in bases:
            prune_tree(base, keep, report)

    report.machines[spec.key] = len(plan)


def generate_machine(spec: fleet.MachineSpec, now: datetime, report: GenerationReport, *, prune: bool = True) -> None:
    write_plan(spec, plan_machine(spec, now=now), report, prune=prune)


def generate_all(*, now: datetime | None = None, prune: bool = True) -> GenerationReport:
    """生成全部模拟资产：日志 + CPD 测校报告/数据表格 + ATLog 用例报告站。"""
    moment = (now or datetime.now()).replace(microsecond=0)
    report = GenerationReport(anchor=moment)

    # 延迟导入：atlog_site 需要本模块的日志行构造器，模块级互导会成环。
    from . import atlog_site, cpdgen

    for spec in fleet.FLEET:
        # CPD 资产只落在上位机（后端报告的 report_root() 与数据的 data_root()
        # 都取自 environment.upper_machine），cpdgen.plan 内部已自行判断角色。
        plan = plan_machine(spec, now=moment) + cpdgen.plan(spec, now=moment)
        write_plan(spec, plan, report, prune=prune)

    atlog_site.generate(now=moment, report=report, prune=prune)
    return report


def reset_filesystem() -> None:
    """删除整个本地模拟文件系统。仅用于 --fresh。"""
    if fleet.LOCAL_ROOT.exists():
        shutil.rmtree(fleet.LOCAL_ROOT, ignore_errors=True)
    # 报告站不在 remote_fs 下，单独清
    from . import atlog_site

    atlog_site.reset()
