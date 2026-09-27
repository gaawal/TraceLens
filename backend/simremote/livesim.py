"""实时日志源模拟：让远端 ``<fm>.log`` 像被 ``tail -f`` 跟着一样源源不断增长。

真实机台上，``<debug 根>/<子系统>/<模块>.log`` 是守护进程持续追加的**当前段**；
后端「实时监听」正是对这个文件开一条保活的 SSH 通道跑 ``tail -n 0 -F``
（见 ``apps/logsources/services/remote_logs.py::_open_live_tail_runtime``）。
本模块在模拟机上扮演那个守护进程：

* 往 5 条 ``<root>/debug/<子系统>/<模块>.log`` 持续追加**合规的八字段调试日志**
  （匹配 ``DEBUG_PATTERN``，否则前端解析不出 level/module）；
* **每条流 0.5 秒落一行**：一个 tick 里 5 条流**各写一行**（不是共用一条扁平
  游标、5 条轮流写），所以不管你订阅哪一条，都是稳定的 0.5s 一行；
  落笔时刻每条流各抖 ``LIVE_STAMP_SPREAD_SECONDS``，各流看起来有自己的时钟；
* **每条流有自己的剧本游标**：走完自己那一遍就回到开头，不跟别人的长度对齐。
  于是短剧本的流（cpfr 12 行 / mecore 13 行 / sil 15 行）一轮里会把自己的剧本
  走二三遍 —— 真机台的"周期采样 / 周期检查"日志本来就是这样反复出现的；
  spwsp（39 行）一轮正好走一遍，wsp（116 行）因为**每个点位都是一次调用**
  （三行一组）长得多，一轮里走不完，跨 3 轮才走完自己的剧本。一轮 ≈ 20s 换一次
  ``trace`` / ``lot`` / ``wafer``（轮长 ``ROUND_TICKS`` 由扫片流 spwsp 定义）；
* 正文遵守项目的调用链规则：``函数名() >()`` 入口、``函数名() <()`` 出口，
  同名 LIFO 配对（拼装逻辑复用 ``loggen.log_message``）。前端就是靠这对方向符
  把一段调用折成函数卡片的，所以不能只写没有边界的散句；函数方法名必须带括号，
  写成 ``ScanWafer() >()`` 而不是 ``[ScanWafer] >()``；
* 内容是一段可循环的故障剧本：**1 类正常节拍 + 3 类异常**，异常之间靠
  ``trace=`` / ``cause=`` 与同一个 lot / wafer 编号显式关联 ——
  编码器抖动 → 伺服补偿发热 → 冷却流量不足 → 光源互锁跳闸 → 扫片侧
  WARN/ERROR/FATAL 三级升级 → 复位重试；
* 其中 ``wsp`` 这条流是**工件台点位**日志：正文是一条固定格式的
  ``move absolute { "x":…, "y":…, "z":…, "rx":…, "ry":…, "rz":… }``
  （**完整六自由度**，花括号里是**合法 JSON**，键与字符串值双引号；
  点位表见 ``loggen.WSP_MOVE_POINTS``）。**每个点位各是一次 ``MoveAbsolute``
  调用** —— 入口 / 点位正文 / 出口三行一组（见 :func:`_wsp_call`），
  所以每条点位正文都落在自己的边界里，前端能折出"这一次移动"、并读出它花了多久。
  曝光那一段走的是**螺旋步进轨迹** —— 从曝光场中心起步、一圈圈向外盘到边缘
  （``"point":"spiral_01"`` … ``"spiral_24"``，几何参数见 ``loggen.SPIRAL_*``），
  所以把 wsp 的点位正文按序读下来，读到的就是工件台的运动轨迹本身。
  它自己也带一条 WARN → ERROR → FATAL 的停位异常链，所以单独订阅它同样有料；
* 因为前端「实时监听」一次只盯一个模块，需要被单独盯住的流（``spwsp`` / ``wsp``，
  见 :data:`OBSERVER_KEYS`）自己也会把整条链的后果按 WARN / ERROR / FATAL
  三个级别记一遍，只订阅一个模块就能同时看到「≥3 类异常 + 1 类正常节拍」；
* **滑动窗口**：活动文件写满 ``MAX_LIVE_LINES`` 行就**轮转**（改名成带关闭边界的
  归档、重建空文件），日志目录里只留最新一份归档，旧的搬进 ``run/recycle/``
  （用改名而不是删除，见 ``reclaim()``）。于是每条流的实时数据量**恒定有界**：
  当前段 ≤ ``MAX_LIVE_LINES`` + 最新归档 ≤ ``MAX_LIVE_LINES``，回收站的槽位是
  固定文件名、下一轮直接覆盖 —— 日志既不会无限追加，也不会把磁盘/内存堆爆。

于是前端点开「实时监听」就能看到日志从订阅点开始一行行滚出来（0.5s 一行），
写满 ``MAX_LIVE_LINES`` 行就滑动一格接着写，不需要人工造数据，也不会越挂越大。
"""

from __future__ import annotations

import json
import random
import signal
import time
import zlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import fleet
from .loggen import (
    PHASE_BODY,
    PHASE_ENTER,
    PHASE_LEAVE,
    SCAN_TRAJECTORY_POINTS,
    expand_stage_groups,
    log_message,
    move_call_rows,
    point_body,
    scan_metric_body,
    source_line,
    stage_codes_of,
)

# --------------------------------------------------------------------------- 常量

#: **滑动窗口宽度**：一条流的活动文件最多放这么多行，写满就整段收档、重建空文件。
#: 于是实时数据量恒定有界（当前段 ≤ 2000 行 + 最新归档 ≤ 2000 行），日志源可以
#: 7×24 挂着跑：每 2000 行挪一次，旧段进回收站固定槽位被下一轮覆盖，
#: 不会像"只追加、从不清理"的写法那样把磁盘（以及读它的进程内存）慢慢吃满。
MAX_LIVE_LINES = 2000

#: 每条流的落笔间隔（秒）。``emit_tick`` 一个 tick 里 5 条流**各写一行**，
#: 所以这就是"任何一条日志"的追加间隔 —— 0.5s 一行，看着像活的。
#: 2000 行 ÷ 0.5s ≈ 16.7 分钟写满一轮活动文件，然后滑动一格。
DEFAULT_INTERVAL_SECONDS = 0.5

#: 轮转后留给远端 ``tail -F`` 的检测窗口：BSD/GNU tail 都是每秒 stat 一次文件名，
#: 太急着往新文件写，切换瞬间的那几行会被漏掉。
ROTATE_SETTLE_SECONDS = 1.05

#: 实时节奏的抖动比例。
#:
#: 不抖的话，``time.sleep(interval)`` 会让相邻两行的时间戳**匀速平移** ——
#: 毫秒位从 .045 一路 .083 / .135 / .181 / .232 地往上爬，看着像节拍器，
#: 一行行的"活气"就没了（用户原话：「有时间不断变化的感觉」）。
#: 真机台写日志不可能匀速：别的线程抢 CPU、日志缓冲 flush、磁盘 IO 都会插队，
#: 间隔会忽快忽慢。±35% 足够看出节奏在呼吸，又不会把"0.5s 一行"的观感
#: 拉成"有时候半天不动"（下限仍有 0.325s）。
INTERVAL_JITTER_RATIO = 0.35

#: 每条流的**落笔时刻抖动**（秒）。
#:
#: 一个 tick 里 5 条流各写一行，如果都用同一个 ``datetime.now()``，那么跨模块
#: 查询（把几条流摆在一起看）里 5 条流的时间戳会**逐列对齐到同一毫秒** ——
#: 真机台里五个子系统各有各的时钟，不可能同时落笔，一眼就能看出是同进程写出来的。
#: ±120ms 让每条流看起来有自己的时钟，又不会打乱任何一条文件内部的时间顺序
#: （间隔 0.5s，来回最多吃掉 0.24s，仍是递增的）。
LIVE_STAMP_SPREAD_SECONDS = 0.12

#: 每 N 个 tick 用磁盘真实行数校准一次内存计数，防止外部改动导致计数漂移。
RECOUNT_EVERY = 60

STATE_FILE = Path(__file__).resolve().parent / "run" / "stream_state.json"

#: 回收站：被替换下来的旧归档先搬到这里，靠 ``os.replace`` 覆盖同名文件，
#: **不用 unlink**。理由见 ``reclaim()`` 的说明。
RECYCLE_DIR = Path(__file__).resolve().parent / "run" / "recycle"


@dataclass(frozen=True)
class StreamTarget:
    """一条被模拟的日志流：一台机器上的一个 fm 模块。"""

    key: str
    machine_key: str
    subsystem: str
    module: str
    role: str

    @property
    def machine(self) -> fleet.MachineSpec:
        return _MACHINES[self.machine_key]

    @property
    def remote_path(self) -> str:
        return f"{self.machine.debug_root}/{self.subsystem}/{self.module}.log"

    @property
    def local_path(self) -> Path:
        relative = self.remote_path.lstrip("/")
        return fleet.local_root_of(self.machine) / relative


_MACHINES: dict[str, fleet.MachineSpec] = {item.key: item for item in fleet.FLEET}

#: 剧本里的 5 条流。都放在上位机：后端「实时监听」默认盯的就是调试日志根，
#: 上位机调试根下各子系统的 fm 都在（``loggen.plan_machine`` 每台机器都生成全套）。
#:
#: ``role`` 只是给人读的标签：normal = 正常节拍流、motion = 工件台点位流，
#: 其余三条各自对应故障链上的一环。
TARGETS: tuple[StreamTarget, ...] = (
    StreamTarget("spwsp", "upper", "spwsp", "spwsp", "normal"),
    StreamTarget("wsp", "upper", "wsp", "wsp", "motion"),
    StreamTarget("encoder", "upper", "mecore", "cpcore", "encoder"),
    StreamTarget("coolant", "upper", "cpfr", "cpfr", "coolant"),
    StreamTarget("interlock", "upper", "sil", "sil", "interlock"),
)
_TARGET_BY_KEY = {item.key: item for item in TARGETS}

#: 会被用户**单独订阅**的流。前端「实时监听」一次只盯一个模块，所以这些流自己
#: 就必须凑齐「≥3 类异常 + 1 类正常节拍」，否则单模块视图里只有 1 类异常，
#: 过滤/告警效果就没法验证。新增"让人专门去看"的流时，把 key 加进来 ——
#: ``selftest`` 会挨个查。
OBSERVER_KEYS: tuple[str, ...] = ("spwsp", "wsp")

#: "正常节拍"的判定标记：在这条流里找得到任意一个，就说明它不只是异常刷屏。
NORMAL_BEAT_MARKERS: tuple[str, ...] = (
    "position error within tolerance",
    # 点位正文的 JSON 收尾字段。字典带引号之后这里必须跟着写成 ``"status":"settled"``，
    # 否则 wsp 这条流会被判成"只有异常、没有正常节拍"。
    '"status":"settled"',
)


# --------------------------------------------------------------------------- 剧本
#
# 一轮剧本 = 一段**流程阶段**推进：上片 → 对准 → 曝光（期间下位机各子系统在跑
# 自己的阶段）→ 互锁跳闸 → 扫片停线 → 恢复，然后回到新的批次 / 晶圆。
#
# 每项 = (目标流, 阶段码, 级别, 函数名, 相位, 正文模板)。模板变量：
#   {trace} 本轮故障追踪号   {lot} 批次号   {wafer} 晶圆号
#   {rms} {rms2} 编码器抖动读数   {flow} 冷却流量   {elapsed} 本次调用耗时
#
# 相位决定正文长什么样（见 ``loggen.log_message``）：
#   enter -> ``ScanWafer() >() enter wafer scan start wafer=W07 ...``
#   body  -> ``ScanWafer() 普通正文``
#   leave -> ``ScanWafer() <() leave wafer scan end wafer=W07 elapsed=86.4ms status=ok``
# 方向符**前面**那个词是被调用的函数，写成调用形状 ``函数名()``（前端
# ``logParser.ts::FUNCTION_PREFIX_REGEX`` 首选就是按这个抓的）。
# 入口/出口那句**固定关键字**（``wafer scan`` / ``exposure scan`` / ``coolant flow read``……）
# 来自 ``loggen.PHASE_KEYWORDS``，**一律英文**；出口复用同一个关键字 + ``end``，肉眼就能配对。
#
# 阶段码把一串调用包成 ``Stage_<阶段码>()`` 阶段框（展开逻辑在
# ``loggen.expand_stage_groups``）。码为 ``None`` 表示不套框 —— ``ScanLot()`` /
# ``ScanWafer()`` 要跨整轮，套进阶段框就会让子阶段先闭合、父帧被迫跨框。
#
# 三条必须守住的约束：
#
# * **每条流内部按 LIFO 闭合**：同一条文件里的入口/出口要能压栈配对，
#   子函数先于父函数闭合。跨流交错是允许的（不同文件本来就不同进程），
#   但同一条流内的出现顺序就是它的调用栈轨迹。
#   ⚠️ 推论：一个函数**不能跨两个阶段框**（例如把服务循环的开头放阶段 A、
#   收尾放阶段 B）—— 阶段框比它晚开却要比它先合，栈立刻乱。
# * **边界行只记进出**：入口/出口固定 INFO，异常级别只落在正文行 ——
#   否则会出现 ``ScanWafer() <() leave status=ok`` 却标着 FATAL 这种自相矛盾的行。
# * **阶段框的顺序就是流程顺序**：同一条流上阶段序号必须递增（selftest 会查）。
#
# ``cause=`` 里写的是**上游故障码**，这样三条异常在文本层面就能串成一条链，
# 前端按关键字搜索任意一环都能找到整条因果链。

def _wsp_call(
    stream: str, code: str, name: str, profile: str
) -> tuple[tuple[str, str, str, str, str, str], ...]:
    """把一个点位展开成实时剧本里的**一整次** ``MoveAbsolute`` 调用（三行一组）。

    三行的形状由 ``loggen.move_call_rows`` 定义 —— 批量程序与实时剧本共用同一份，
    所以不会出现"批量补了出口、实时的还是半条调用"这种两边漂移。

    ⚠️ **一个点位一次调用**：整条扫描轨迹（27 个点）是 27 次 ``MoveAbsolute``，
    不是"一次调用里连打 27 条点位"。后者的点位正文没有自己的入口/出口，
    既不合日志规范，也没法回答"走到这个点位用了多久"。
    """
    return tuple(
        (stream, code, level, function, phase, body)
        for function, phase, level, body in move_call_rows(name, profile=profile)
    )


_ROUND_ROWS: tuple[tuple[str, str | None, str, str, str, str], ...] = (
    # ---- 引子：扫片主流程的最外层帧（跨整轮，不套阶段框） ----
    ("spwsp", None, "INFO", "ScanLot", PHASE_ENTER, "lot={lot} wafers=25"),
    ("spwsp", None, "INFO", "ScanWafer", PHASE_ENTER, "wafer={wafer} recipe=SPM-V2026.09.21"),
    # ---- 阶段⓪：工件台回零（wsp 点位流）----
    # wsp 这条流记的是**工件台运动轨迹**，所以它跟着主流程一路走：回零 → 上片点 →
    # 对准点 → 扫描点 → 停位检查 → 卸片点。正文是一条固定格式的移动点位
    # （``move absolute { "x":…, "y":…, … "status":"settled" }``，六自由度，
    # 花括号里是合法 JSON），点位表在 ``loggen.WSP_MOVE_POINTS``。
    #
    # ⚠️ **一个点位 = 一次 ``MoveAbsolute`` 调用**（入口 / 点位正文 / 出口，
    # 由 ``_wsp_call`` 展开）：点位正文必须落在**它自己那一次调用**的边界里，
    # 而不是几十条点位共用一对入口/出口。所以下面每个点位都是三行。
    #
    # 之所以**插在主流程各个节点之间**而不是整块放在末尾：真实机台的点位日志是
    # 跟着动作持续刷的，整块放末尾会让"实时监听 wsp"变成几十秒静默 + 一串爆发。
    # 回零不是"绝对移动一个点位"，它有自己的函数（``HomeStage``），所以它照旧是
    # 三行一组、正文里报的是参考点（origin）的坐标。
    ("wsp", "WSP_HOME", "INFO", "HomeStage", PHASE_ENTER, "dof=6 mode=absolute search=reference_mark"),
    ("wsp", "WSP_HOME", "INFO", "HomeStage", PHASE_BODY, point_body("origin")),
    ("wsp", "WSP_HOME", "INFO", "HomeStage", PHASE_LEAVE, "dof=6 elapsed={elapsed} status=ok"),
    # ---- 阶段①：上片 ----
    ("spwsp", "WAFER_LOAD", "INFO", "MoveWaferStage", PHASE_ENTER, "axis=XY target=chuck"),
    ("spwsp", "WAFER_LOAD", "INFO", "MoveWaferStage", PHASE_BODY, "wafer stage settled, position error within tolerance"),
    ("spwsp", "WAFER_LOAD", "INFO", "MoveWaferStage", PHASE_LEAVE, "axis=XY elapsed={elapsed} status=ok"),
    # ---- 阶段①b：工件台走上片点（wsp）----
    *_wsp_call("wsp", "WSP_LOAD_MOVE", "load_position", "rapid"),
    # ---- 阶段②：对准 ----
    ("spwsp", "ALIGNMENT", "INFO", "AlignWafer", PHASE_ENTER, "wafer={wafer} marks=8"),
    ("spwsp", "ALIGNMENT", "INFO", "AlignWafer", PHASE_BODY, "alignment mark detected, offset compensation applied for wafer {wafer}"),
    ("spwsp", "ALIGNMENT", "INFO", "AlignWafer", PHASE_LEAVE, "wafer={wafer} elapsed={elapsed} status=ok"),
    # ---- 阶段②b：工件台逐个对准标记对位（wsp）—— 两个标记 = 两次移动 ----
    *_wsp_call("wsp", "WSP_ALIGN_MOVE", "align_mark_01", "align"),
    *_wsp_call("wsp", "WSP_ALIGN_MOVE", "align_mark_08", "align"),
    # ---- 阶段③：曝光开始。这个框在 spwsp 上要一直开到互锁恢复之后才合
    #      （中间夹着编码器 / 冷却 / 互锁各自的阶段，但那些是别的文件，不打断本流）----
    ("spwsp", "EXPOSURE", "INFO", "ExposeWafer", PHASE_ENTER, "wafer={wafer} dose=30mJ/cm2"),
    ("spwsp", "EXPOSURE", "INFO", "ExposeWafer", PHASE_BODY, "illumination source power stabilised at setpoint, dose within specification"),
    # ---- 阶段③a：曝光过程中的测量快照（spwsp）----
    # 一次采样就把整包读数打成**跨多行**的字典：六自由度位置 + 功率 + 对比度 +
    # 激光器镜干涉仪 x1..x4 / y1..y3 七路对比度通道。正文构造见
    # ``loggen.scan_metric_body``。
    #
    # ⚠️ 这是**故意**保留的多行形态：只有首行带时间戳，后面十几行都是无时间戳的
    # 续行 —— 正是"按行首时间戳切记录"的解析器会**截断正文**的场景，用来复现
    # 并盯住那个 bug。别把它改成单行，改单了截断场景就复现不出来了。
    ("spwsp", "EXPOSURE", "INFO", "CaptureScanMetrics", PHASE_ENTER, "wafer={wafer} point=exposure_mid channels=7"),
    ("spwsp", "EXPOSURE", "INFO", "CaptureScanMetrics", PHASE_BODY, scan_metric_body("exposure_mid")),
    ("spwsp", "EXPOSURE", "INFO", "CaptureScanMetrics", PHASE_LEAVE, "wafer={wafer} elapsed={elapsed} status=ok"),
    # ---- 阶段③b：工件台走扫描轨迹（wsp）—— 曝光期间真正在动的就是它 ----
    # 轨迹是**螺旋步进**的：入场到曝光场 → 走到场中心 → 从中心一圈圈向外螺旋
    # 步进（``spiral_01`` … 末圈贴边缘）→ 退场。点序取自
    # ``loggen.SCAN_TRAJECTORY_POINTS``（几何参数在 ``loggen.SPIRAL_*``），
    # 与批量程序共用同一份 —— 改圈数/密度只动常量，两边自动同步。
    # **每一个点位各是一次 ``MoveAbsolute`` 调用**（三行），所以这段是
    # ``len(SCAN_TRAJECTORY_POINTS)`` 次调用，不是一次调用连打几十条点位。
    *(
        row
        for name in SCAN_TRAJECTORY_POINTS
        for row in _wsp_call("wsp", "WSP_SCAN_MOVE", name, "scan")
    ),
    # ---- 阶段④：伺服采样（编码器流）→ 异常① 故障链起点 ----
    ("encoder", "SERVO_SAMPLE", "INFO", "ServoLoop", PHASE_ENTER, "axis=Rz loop=position"),
    ("encoder", "SERVO_SAMPLE", "INFO", "CheckEncoderFeedback", PHASE_ENTER, "axis=Rz threshold=0.50um"),
    ("encoder", "SERVO_SAMPLE", "WARN", "CheckEncoderFeedback", PHASE_BODY, "encoder feedback jitter rms={rms}um exceeds threshold 0.50um axis=Rz code=ERR_MECORE_ENC_JITTER trace={trace}"),
    ("encoder", "SERVO_SAMPLE", "WARN", "CheckEncoderFeedback", PHASE_BODY, "encoder feedback jitter persists after filter update rms={rms2}um code=ERR_MECORE_ENC_JITTER trace={trace}"),
    ("encoder", "SERVO_SAMPLE", "INFO", "CheckEncoderFeedback", PHASE_LEAVE, "axis=Rz elapsed={elapsed} status=degraded"),
    ("encoder", "SERVO_SAMPLE", "INFO", "ServoLoop", PHASE_LEAVE, "axis=Rz status=degraded"),
    # ---- 阶段⑤：冷却流量（冷却流）→ 异常②，起因是 ① 的伺服补偿持续发热 ----
    ("coolant", "COOLANT_FLOW", "INFO", "CoolantLoop", PHASE_ENTER, "loop=cpfr-mecore setpoint=5.0L/min"),
    ("coolant", "COOLANT_FLOW", "INFO", "CheckFlow", PHASE_ENTER, "loop=cpfr-mecore"),
    ("coolant", "COOLANT_FLOW", "WARN", "CheckFlow", PHASE_BODY, "coolant flow {flow}L/min below threshold 5.0L/min loop=cpfr-mecore code=ERR_CPFR_FLOW_LOW cause=ERR_MECORE_ENC_JITTER trace={trace}"),
    ("coolant", "COOLANT_FLOW", "INFO", "CheckFlow", PHASE_LEAVE, "loop=cpfr-mecore elapsed={elapsed} status=low"),
    ("coolant", "COOLANT_FLOW", "INFO", "CoolantLoop", PHASE_LEAVE, "loop=cpfr-mecore status=degraded"),
    # ---- 阶段⑥：互锁布防（互锁流）→ 异常③ 的前置条件 ----
    ("interlock", "INTERLOCK_ARM", "INFO", "SourceInterlock", PHASE_ENTER, "loop=sil state=armed"),
    ("interlock", "INTERLOCK_ARM", "ERROR", "SourceInterlock", PHASE_BODY, "source interlock armed, coolant loop degraded code=ERR_SIL_INTERLOCK_ARMED cause=ERR_CPFR_THERM_OVERLOAD trace={trace}"),
    ("interlock", "INTERLOCK_ARM", "INFO", "SourceInterlock", PHASE_LEAVE, "loop=sil elapsed={elapsed} status=armed"),
    # ---- 阶段⑦：伺服补偿（编码器流）→ 对异常① 的处置 ----
    ("encoder", "SERVO_COMPENSATE", "INFO", "CompensateJitter", PHASE_ENTER, "axis=Rz"),
    ("encoder", "SERVO_COMPENSATE", "ERROR", "CompensateJitter", PHASE_BODY, "servo loop gain reduced to compensate jitter, position stability degraded code=ERR_MECORE_ENC_JITTER trace={trace}"),
    ("encoder", "SERVO_COMPENSATE", "INFO", "CompensateJitter", PHASE_LEAVE, "axis=Rz elapsed={elapsed} status=degraded"),
    # ---- 阶段⑧：热预算核算（冷却流）→ 异常② 升级 ----
    ("coolant", "THERMAL_BUDGET", "INFO", "UpdateThermalBudget", PHASE_ENTER, "loop=cpfr-mecore"),
    ("coolant", "THERMAL_BUDGET", "ERROR", "UpdateThermalBudget", PHASE_BODY, "thermal load from mecore servo compensation exceeds budget code=ERR_CPFR_THERM_OVERLOAD cause=ERR_CPFR_FLOW_LOW trace={trace}"),
    ("coolant", "THERMAL_BUDGET", "INFO", "UpdateThermalBudget", PHASE_LEAVE, "loop=cpfr-mecore elapsed={elapsed} status=degraded"),
    # ---- 阶段⑨：互锁跳闸（互锁流）→ 异常③ 落地 ----
    ("interlock", "INTERLOCK_TRIP", "INFO", "TripInterlock", PHASE_ENTER, "loop=sil reason=coolant_degraded"),
    ("interlock", "INTERLOCK_TRIP", "FATAL", "TripInterlock", PHASE_BODY, "exposure aborted by interlock, wafer {wafer} held on stage code=ERR_SIL_INTERLOCK_TRIP cause=ERR_SIL_INTERLOCK_ARMED trace={trace}"),
    ("interlock", "INTERLOCK_TRIP", "INFO", "TripInterlock", PHASE_LEAVE, "loop=sil elapsed={elapsed} status=tripped"),
    # ---- 阶段⑩：互锁恢复（互锁流）----
    ("interlock", "INTERLOCK_RECOVER", "INFO", "SourceInterlock", PHASE_ENTER, "loop=sil state=recovering"),
    ("interlock", "INTERLOCK_RECOVER", "INFO", "SourceInterlock", PHASE_BODY, "interlock cleared after coolant flow restored to 5.4L/min code=ERR_SIL_INTERLOCK_TRIP trace={trace}"),
    ("interlock", "INTERLOCK_RECOVER", "INFO", "SourceInterlock", PHASE_LEAVE, "loop=sil elapsed={elapsed} status=recovered"),
    # ---- 阶段③ 收尾：曝光确实被互锁中止了（spwsp 上的同一个曝光阶段框到此闭合）----
    ("spwsp", "EXPOSURE", "INFO", "ExposeWafer", PHASE_LEAVE, "wafer={wafer} elapsed={elapsed} status=aborted"),
    # ---- 阶段⑩b：工件台停位检查（wsp 点位流）——曝光被中止后工件台停不住 ----
    # 异常出处：编码器抖动（上游 ERR_MECORE_ENC_JITTER）→ 位置偏差超差 → 停位超时
    # → 逼近软限位、动作中止。这条 WARN → ERROR → FATAL 的本地链让 **wsp 单独订阅
    # 也有 3 类异常**；根因仍挂在编码器上，所以 trace 与其它子系统同号，跨模块关联
    # 照样成立（wsp 就是第 5 条流）。
    ("wsp", "WSP_SETTLE", "INFO", "CheckPositionError", PHASE_ENTER, "dof=6 tolerance=0.020um"),
    ("wsp", "WSP_SETTLE", "WARN", "CheckPositionError", PHASE_BODY, "position error 0.031um exceeds tolerance 0.020um dof=6 code=ERR_WSP_POSITION_DEVIATION cause=ERR_MECORE_ENC_JITTER trace={trace}"),
    ("wsp", "WSP_SETTLE", "ERROR", "CheckPositionError", PHASE_BODY, "settling window expired, position error not converged dof=6 code=ERR_WSP_SETTLE_TIMEOUT cause=ERR_WSP_POSITION_DEVIATION trace={trace}"),
    ("wsp", "WSP_SETTLE", "INFO", "CheckPositionError", PHASE_LEAVE, "dof=6 elapsed={elapsed} status=deviated"),
    ("wsp", "WSP_SETTLE", "INFO", "AbortMotion", PHASE_ENTER, "dof=6 reason=settle_timeout"),
    ("wsp", "WSP_SETTLE", "FATAL", "AbortMotion", PHASE_BODY, "motion aborted near soft limit, travel range guard triggered dof=6 code=ERR_WSP_SOFT_LIMIT_PROXIMITY cause=ERR_WSP_SETTLE_TIMEOUT trace={trace}"),
    ("wsp", "WSP_SETTLE", "INFO", "AbortMotion", PHASE_BODY, "stage re-settled after motion abort, position error within tolerance"),
    ("wsp", "WSP_SETTLE", "INFO", "AbortMotion", PHASE_LEAVE, "dof=6 elapsed={elapsed} status=aborted"),
    # ---- 阶段⑪：扫片停线（spwsp）WARN → ERROR → FATAL，三级升级 ----
    # 前端「实时监听」一次只盯一个模块，所以被观察的那条流自己也必须把整条链的
    # 后果按三个级别记下来 —— 否则只订阅一个模块时只能看到 1 类异常，凑不齐
    # 「至少 3 类异常 + 1 类正常」，也就不方便验证实时监听的过滤/告警效果。
    ("spwsp", "SCAN_HALT", "INFO", "RetryExposure", PHASE_ENTER, "wafer={wafer} attempt=1"),
    ("spwsp", "SCAN_HALT", "WARN", "RetryExposure", PHASE_BODY, "exposure retry scheduled after interlock recovery code=ERR_SPWSP_RETRY_SCHEDULED cause=ERR_SIL_INTERLOCK_TRIP trace={trace}"),
    ("spwsp", "SCAN_HALT", "ERROR", "RetryExposure", PHASE_BODY, "wafer stage handshake lost, retry aborted code=ERR_SPWSP_STAGE_HANDSHAKE cause=ERR_SPWSP_RETRY_SCHEDULED trace={trace}"),
    ("spwsp", "SCAN_HALT", "INFO", "RetryExposure", PHASE_LEAVE, "wafer={wafer} elapsed={elapsed} status=failed"),
    ("spwsp", "SCAN_HALT", "INFO", "HaltScan", PHASE_ENTER, "wafer={wafer} reason=stage_handshake"),
    ("spwsp", "SCAN_HALT", "FATAL", "HaltScan", PHASE_BODY, "scan sequence halted for wafer {wafer}, manual recovery required code=ERR_SPWSP_SCAN_HALTED cause=ERR_SPWSP_STAGE_HANDSHAKE trace={trace}"),
    ("spwsp", "SCAN_HALT", "INFO", "HaltScan", PHASE_LEAVE, "wafer={wafer} elapsed={elapsed} status=halted"),
    # ---- 阶段⑫：扫片恢复（spwsp）----
    ("spwsp", "SCAN_RECOVER", "INFO", "RecoverStage", PHASE_ENTER, "wafer={wafer} loop=servo"),
    ("spwsp", "SCAN_RECOVER", "INFO", "RecoverStage", PHASE_BODY, "recovery sequence completed, stage handshake restored, servo loop re-locked"),
    ("spwsp", "SCAN_RECOVER", "INFO", "RecoverStage", PHASE_LEAVE, "wafer={wafer} elapsed={elapsed} status=ok"),
    ("spwsp", "SCAN_RECOVER", "INFO", "ResumeExposure", PHASE_ENTER, "wafer={wafer} from=checkpoint"),
    ("spwsp", "SCAN_RECOVER", "INFO", "ResumeExposure", PHASE_BODY, "exposure sequence resumed, lot {lot} continues from checkpoint"),
    ("spwsp", "SCAN_RECOVER", "INFO", "ResumeExposure", PHASE_LEAVE, "wafer={wafer} elapsed={elapsed} status=ok"),
    # ---- 阶段⑫b：工件台退回卸片位（wsp 点位流）----
    *_wsp_call("wsp", "WSP_UNLOAD_MOVE", "unload_position", "rapid"),
    # ---- 收尾：最外层帧闭合（不套阶段框）----
    ("spwsp", None, "INFO", "ScanWafer", PHASE_LEAVE, "wafer={wafer} elapsed={elapsed} status=recovered"),
    ("spwsp", None, "INFO", "ScanLot", PHASE_LEAVE, "lot={lot} elapsed={elapsed} status=ok"),
)

#: 实时流的阶段序（按首次出现）—— 就是流程的阶段序号。
STAGE_CODES: tuple[str, ...] = stage_codes_of(_ROUND_ROWS)

#: 扁平剧本：(目标流, 级别, 函数名, 相位, 正文模板)。阶段框已由展开器插好。
#: ``expand_stage_groups`` 的输出形状与 ``_ROUND_ROWS`` 扁平化后**一致**，
#: 所以这里直接转 tuple 即可 —— 再手动换位会把 level/function 拧反。
_SCRIPT: tuple[tuple[str, str, str, str, str], ...] = tuple(expand_stage_groups(_ROUND_ROWS))

SCRIPT_LENGTH = len(_SCRIPT)

#: **每条流自己的剧本行**（只含本流的行，顺序与 ``_SCRIPT`` 一致）。
#:
#: 为什么要按流切片：``_SCRIPT`` 是一条扁平序列，一个 tick 只落一行的话，
#: 5 条流要轮流等 —— 任何一条都得 5 个 tick 才轮到自己，用户看到的就不是
#: "0.5 秒一行"了。切片之后 ``emit_tick`` 一个 tick 让**每条流各落一行**，
#: 于是每条日志都是稳定的 0.5s 一行。
#:
#: 切片不改变任何顺序：每条流的行仍按剧本先后出现，所以 LIFO 闭合、
#: 阶段序号递增这些调用链约束照样成立（selftest 对每条流单独查）。
_FLOW_SCRIPTS: dict[str, tuple[tuple[str, str, str, str], ...]] = {
    target.key: tuple(
        (level, function, phase, body)
        for key, level, function, phase, body in _SCRIPT
        if key == target.key
    )
    for target in TARGETS
}

#: 一轮剧本由**扫片流**（spwsp）定义：它走完自己一遍 = 一片晶圆走完
#: （``ScanLot() >()`` … ``ScanLot() <()``）。轮末换新的追踪号 / 批次 / 晶圆，
#: 所以一轮 ≈ 39 × 0.5s ≈ 20s。
#:
#: 为什么不用"最长那条流"的长度：追踪号是给**这一片晶圆**打的相关性标记，
#: 换轮时刻必须落在 spwsp 的 pass 边界上，否则新的一片晶圆的前几行会顶着
#: 上一片的 trace。其它流各有各的周期（wsp 116 行 ≈ 58s，cpfr 12 行 ≈ 6s），
#: 各走各的游标，互不干扰 —— 这正是真机台的形态：每个子系统按自己的节拍记日志。
ROUND_DRIVER = "spwsp"
ROUND_TICKS = len(_FLOW_SCRIPTS[ROUND_DRIVER])


# --------------------------------------------------------------------------- 状态


@dataclass
class TargetState:
    lines: int = 0
    rotations: int = 0
    #: 最新一份归档，留在日志目录里供查询（最新关闭的那一段仍然可读）
    archived: str = ""
    #: 我产出的、还没回收掉的归档路径。轮转时除 ``archived`` 之外都要搬走，
    #: 搬不动的留在这里等下轮再试 —— 不按 glob 批量删，避免误伤 loggen 的合法归档。
    owned: list[str] = field(default_factory=list)
    #: 成功回收（搬进回收站）的归档个数
    recycled: int = 0


@dataclass
class StreamState:
    #: 轮次时钟：0..ROUND_TICKS-1。站满一轮就换追踪号 / 批次 / 晶圆。
    cursor: int = 0
    #: **每条流**各自的行游标（走完自己的剧本回到 0）。见 ``emit_tick``。
    cursors: dict[str, int] = field(default_factory=dict)
    round: int = 0
    trace: str = ""
    lot: str = ""
    wafer: int = 0
    ticks: int = 0
    last_line: str = ""
    targets: dict[str, TargetState] = field(default_factory=dict)

    def target(self, key: str) -> TargetState:
        return self.targets.setdefault(key, TargetState())


def new_round(state: StreamState, moment: datetime) -> None:
    """开启新一轮故障剧本：新的追踪号、批次号与晶圆号。"""
    state.round += 1
    suffix = zlib.crc32(f"{moment:%Y%m%d%H%M%S}{state.round}".encode("utf-8")) % 0xFFFF
    state.trace = f"TR-{state.round:04d}-{suffix:04X}"
    state.lot = f"LOT-{moment:%Y%m%d}-{((state.round - 1) % 24) + 1:02d}"
    state.wafer = (state.wafer % 25) + 1


def render_values(state: StreamState, function: str, template: str = "") -> dict[str, object]:
    """剧本模板变量。抖动、流量与耗时随轮次变化，避免逐轮完全雷同。

    ``template`` 参与 ``elapsed`` 的取数（与 ``loggen._program_values`` 同一套算法）：
    耗时应当**逐次调用各不相同**，只按函数名取的话，wsp 那 27 次 ``MoveAbsolute``
    的出口会把同一个毫秒数连印 27 遍 —— 一眼就是克隆的。
    """
    if not state.trace:
        new_round(state, datetime.now())
    elapsed = 6 + zlib.crc32(f"{function}|{template}".encode("utf-8")) % 180
    return {
        "trace": state.trace,
        "lot": state.lot,
        "wafer": f"W{state.wafer:02d}",
        "rms": f"{0.86 + (state.round % 5) * 0.03:.2f}",
        "rms2": f"{0.94 + (state.round % 4) * 0.04:.2f}",
        "flow": f"{3.8 - (state.round % 3) * 0.2:.1f}",
        "elapsed": f"{elapsed + (state.round % 7) * 4.3:.1f}ms",
    }


# --------------------------------------------------------------------------- 行格式


def make_line(
    target: StreamTarget,
    level: str,
    function: str,
    phase: str,
    message: str,
    moment: datetime,
) -> str:
    """八字段调试日志，字段布局与 ``loggen.debug_line`` 完全一致。

    正文用 ``loggen.log_message`` 拼装，所以每一行都以 ``函数名()`` 开头，
    入口/出口行紧跟着 ``>()`` / ``<()`` 方向符 —— 前端折叠就是认这对符号，
    而函数名靠 ``函数名()`` 这个调用形状识别（必须带括号）。

    后端 ``DEBUG_PATTERN`` 要求 8 个 ``[...]`` 分组；正文是第 9 组的 ``.*``，
    里面的方括号不参与分字段（不过现在正文里已经不再放方括号了）。
    level 位置允许任意词，所以这里可以放心用 WARN / ERROR / FATAL。
    """
    stamp = f"{moment:%Y-%m-%d %H:%M:%S}.{moment.microsecond // 1000:03d}"
    process_id = 20000 + zlib.crc32(target.subsystem.encode("utf-8")) % 9000
    # 线程号按模块固定：一条调用链跑在同一线程上，前端才能把入口/出口和正文
    # 归到同一条执行泳道（折叠与 ×N 聚合都以 component/process/thread 相同为前提）。
    thread_id = 30000 + zlib.crc32(target.module.encode("utf-8")) % 500
    rpc = f"{target.module}:{function}:{source_line(function)}"
    return (
        f"[{stamp}] [{level}] [{target.subsystem.upper()}] [{process_id}] "
        f"[{thread_id}] [{target.module}] [normal] [{rpc}] {log_message(function, phase, message)}"
    )


# --------------------------------------------------------------------------- 文件


def count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def archive_current(path: Path, module: str, moment: datetime) -> Path:
    """把活动文件改名成带关闭边界的归档，并重建一个空的当前段。

    归档名沿用 ``<模块>_<关闭边界>.log`` 的既有约定，看上去和 loggen 产出的历史段
    完全是同一类东西。调用方负责决定这个归档是"我的、可回收"还是"接管的、保留"。
    """
    archived = path.with_name(f"{module}_{moment:%Y%m%d%H%M%S}.log")
    index = 1
    while archived.exists():
        archived = path.with_name(f"{module}_{moment:%Y%m%d%H%M%S}_{index}.log")
        index += 1
    path.rename(archived)
    path.touch()
    return archived


def reclaim(state: TargetState, module: str) -> list[str]:
    """把 ``state.owned`` 里除最新一份之外的归档搬进回收站，返回搬不动的路径。

    为什么是 ``os.replace`` 而不是 ``unlink``：受控运行环境对"一个 turn 内删除
    超过阈值（约 50 个）文件"有安全拦截，触发后删除会**静默失败**并打一行
    ``SAFE_DELETE_BULK_CONFIRM_REQUIRED``。日志源是长跑进程，几千次轮转必然撞上，
    残余归档就会一直堆在日志目录里（实测 spwsp 目录里留下了两份 1000 行归档）。
    改名不受该拦截影响，而且回收站用的是**固定文件名**，下一轮直接覆盖 ——
    既不需要删除权限，也不会无限增长。
    """
    keep = state.archived
    if keep and not Path(keep).exists():
        # 记录里的"最新归档"已经被 loggen 的清理收走了，那就没有要保留的对象
        keep = ""
    stuck: list[str] = []
    for path in list(state.owned):
        if path == keep:
            continue
        source = Path(path)
        if not source.exists():
            state.owned.remove(path)
            continue
        try:
            RECYCLE_DIR.mkdir(parents=True, exist_ok=True)
            source.replace(RECYCLE_DIR / f"{module}_prev.log")
        except OSError:
            stuck.append(path)
            continue
        state.owned.remove(path)
        state.recycled += 1
    return stuck


def rotate(target: StreamTarget, state: TargetState, moment: datetime) -> Path:
    """轮转：整段收档 + 重建空当前段，然后把旧归档搬进回收站。

    这就是**滑动窗口的那一格**：窗口写满 → 关掉当前段 → 开一段空的接着写，
    同时把上上段挪出日志目录。清理只认 state 里记下的路径（``owned``），
    绝不按 glob 批量删 —— 否则会误伤 loggen 生成的合法日包与轮转段。
    """
    archived = archive_current(target.local_path, target.module, moment)
    state.archived = str(archived)
    state.owned.append(str(archived))
    state.rotations += 1
    state.lines = 0
    reclaim(state, target.module)
    return archived


def trim_to_capacity(path: Path, limit: int = MAX_LIVE_LINES) -> int:
    """当前段超限时只保留最后 ``limit`` 行，返回保留后的行数。"""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        return 0
    if len(raw) <= limit:
        return len(raw)
    path.write_text("".join(raw[-limit:]), encoding="utf-8")
    return limit


def prepare(state: StreamState, *, report: bool = False) -> list[str]:
    """确保每条流的目录/文件存在，并把当前段裁到 ``MAX_LIVE_LINES`` 行以内。

    启动时 ``<fm>.log`` 往往带着 loggen 生成的当天历史（00:00 → 现在，上千行）。
    这里**只截掉超出的部分**，不整段收档归零，理由有两个：

    * 订阅一开始就要有上下文 —— 历史的"最近 30 分钟"过滤、时间窗查询都得有行可读；
    * 归零会让当前段在最初一两分钟里几乎为空，看着像日志源没工作。

    被截掉的只是模拟数据，``scripts/sim_realign.sh`` 随时能重建。
    """
    notes: list[str] = []
    for target in TARGETS:
        path = target.local_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.touch()
        slot = state.target(target.key)
        # 启动时再试一次回收：新进程通常落在新的 turn 上，删除配额是新的，
        # 上一轮因为撞上拦截而没搬走的旧归档可以在这里清掉。
        stuck = reclaim(slot, target.module)
        if report and stuck:
            notes.append(f"{target.subsystem}/{target.module} 有 {len(stuck)} 份旧归档暂未回收")
        lines = count_lines(path)
        if lines > MAX_LIVE_LINES:
            kept = trim_to_capacity(path)
            slot.lines = kept
            if report:
                notes.append(f"{target.subsystem}/{target.module}.log 历史 {lines} 行，裁到最近 {kept} 行")
            continue
        slot.lines = lines
        if report:
            notes.append(f"{target.subsystem}/{target.module}.log 起始 {lines} 行")
    return notes


# --------------------------------------------------------------------------- 推进

#: 一个 tick 里单条流的产物：(哪条流, 写进去的行, 这一笔之前轮转出来的归档)。
EmitEvent = tuple[StreamTarget, str, Path | None]

#: 给每条流抖落笔时刻用。没有可复现性要求（自检查的是"散不散"，不是"等于几"），
#: 所以不播种，进程启动时从系统熵源起算。
_STAMP_RNG = random.Random()


def emit_tick(state: StreamState, moment: datetime | None = None) -> list[EmitEvent]:
    """推进一个 tick：**每条流各落一条记录**，写满就先轮转再落笔。

    返回 ``[(目标, 行内容, 本次轮转出来的归档), ...]``，一个元素对应一条流。
    轮转发生在**落笔之前**：当前段已经写满就先整段收档、重建空文件，再写新的
    记录。这样活动文件任何时刻都不会超过 ``MAX_LIVE_LINES``，不会出现 2001 行
    这种越界。

    ⚠️ "一条记录"**不等于**"一个物理行"：测量快照一次落十几行、只有首行带时间戳
    （``loggen.scan_metric_body``）。容量与轮转都以**物理行**为准，所以下面是按
    ``line.count("\\n") + 1`` 的宽度累加、并把这个宽度带进轮转判断的。

    每条流按**自己的**游标往前走一步（``state.cursors[模块]``，走完自己的剧本回到 0），
    所以一个 tick 之后每条流都恰好多**一条记录** —— 任何一条日志的追加间隔都等于
    tick 间隔（0.5s），且每条流内部的记录顺序（也就是它的调用栈轨迹）永远按剧本走，
    不跳行。
    """
    moment = moment or datetime.now()
    events: list[EmitEvent] = []
    for target in TARGETS:
        script = _FLOW_SCRIPTS[target.key]
        # 每条流**自己的**游标：走完自己的剧本就回到第 0 行，不跟别人的长度对齐。
        # 用全局游标取模是错的 —— 剧本长度（36 / 13 / 12 …）与一轮的 tick 数
        # （``ROUND_TICKS``）不整除时，每次换轮都会把这条流拽回第 0 行，
        # 于是 ``ScanLot() >()`` 这种入口会连着出现两遍、出口却少一个，
        # 前端折叠出来的调用栈就断了。
        index = state.cursors.get(target.key, 0) % len(script)
        level, function, phase, template = script[index]
        slot = state.target(target.key)

        message = template.format(**render_values(state, function, template))
        # 每条流各抖一点落笔时刻，别让 5 条流的时间戳逐列对齐（见常量说明）
        stamp = moment + timedelta(
            seconds=_STAMP_RNG.uniform(-LIVE_STAMP_SPREAD_SECONDS, LIVE_STAMP_SPREAD_SECONDS)
        )
        line = make_line(target, level, function, phase, message, stamp)

        # ``MAX_LIVE_LINES`` / ``trim_to_capacity`` / 前端读的都是**物理行**，
        # 而一条记录**不一定只占一行**：测量快照那种字典日志一次落十几行、
        # 只有首行带时间戳（``loggen.scan_metric_body``）。所以这里必须按实际
        # 落盘宽度累加，不能恒加 1 —— 否则多行记录会让活动文件悄悄越过上限。
        width = line.count("\n") + 1

        # 轮转在落笔**之前**判，而且要把这一笔的宽度一起算进去：只判
        # ``slot.lines >= MAX_LIVE_LINES`` 时，一条 16 行的记录能把当前段从
        # 1999 行顶到 2015 行，容量上限与滑动窗口就都失效了。
        archived = (
            rotate(target, slot, moment)
            if slot.lines + width > MAX_LIVE_LINES
            else None
        )

        path = target.local_path
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

        slot.lines += width
        state.last_line = line
        state.cursors[target.key] = (index + 1) % len(script)
        events.append((target, line, archived))

    state.ticks += 1
    state.cursor = (state.cursor + 1) % ROUND_TICKS
    if state.cursor == 0:
        # 一轮剧本站满 -> 下一 tick 开始就是新的追踪号/批次/晶圆
        new_round(state, moment)
    return events


# --------------------------------------------------------------------------- 状态文件


def load_state() -> StreamState:
    if not STATE_FILE.exists():
        return StreamState()
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return StreamState()
    state = StreamState(
        cursor=int(raw.get("cursor", 0)) % ROUND_TICKS,
        round=int(raw.get("round", 0)),
        trace=str(raw.get("trace", "")),
        lot=str(raw.get("lot", "")),
        wafer=int(raw.get("wafer", 0)),
        ticks=int(raw.get("ticks", 0)),
        last_line=str(raw.get("last_line", "")),
    )
    for key, value in dict(raw.get("cursors") or {}).items():
        script = _FLOW_SCRIPTS.get(str(key))
        if script:
            state.cursors[str(key)] = int(value) % len(script)
    for key, value in dict(raw.get("targets") or {}).items():
        state.targets[str(key)] = TargetState(
            lines=int(value.get("lines", 0)),
            rotations=int(value.get("rotations", 0)),
            archived=str(value.get("archived", "")),
            owned=[str(item) for item in (value.get("owned") or [])],
            recycled=int(value.get("recycled", 0)),
        )
    return state


def save_state(state: StreamState) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(state)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(STATE_FILE)


def snapshot() -> dict:
    """给 ``stream-status`` 用的只读快照。"""
    state = load_state()
    rows = []
    for target in TARGETS:
        slot = state.target(target.key)
        # 归档可能已被 loggen 的清理或下一次轮转收走，只在文件真的还在时才报，
        # 免得状态里挂着一个已经不存在的名字。
        archived = slot.archived if slot.archived and Path(slot.archived).exists() else ""
        rows.append({
            "key": target.key,
            "subsystem": target.subsystem,
            "module": target.module,
            "machine": target.machine.host,
            "remote_path": target.remote_path,
            "lines": count_lines(target.local_path),
            "recorded_lines": slot.lines,
            "rotations": slot.rotations,
            "archived": archived,
            "recycled": slot.recycled,
            # 只报"该搬走却还没搬走"的（``owned`` 里留着的是最新那份，不算欠账）
            "pending_reclaim": len([item for item in slot.owned if item != slot.archived]),
            # 滑动窗口的实际占用：当前段 + 最新归档（两者都 ≤ MAX_LIVE_LINES）
            "window_lines": count_lines(target.local_path)
            + (count_lines(Path(archived)) if archived else 0),
        })
    return {
        "round": state.round,
        "cursor": state.cursor,
        "trace": state.trace,
        "lot": state.lot,
        "wafer": state.wafer,
        "ticks": state.ticks,
        "max_lines": MAX_LIVE_LINES,
        "interval_seconds": DEFAULT_INTERVAL_SECONDS,
        "round_ticks": ROUND_TICKS,
        "cursor_of_round": f"{state.cursor}/{ROUND_TICKS}",
        "targets": rows,
    }


# --------------------------------------------------------------------------- 主循环


class _Stopper:
    def __init__(self) -> None:
        self.flag = False

    def install(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self._handle)
            except (ValueError, OSError):
                continue

    def _handle(self, _signum, _frame) -> None:  # noqa: ANN001 - 信号签名
        self.flag = True


def _breath(interval: float, rng: random.Random) -> float:
    """下一行的间隔：围绕 ``interval`` 抖动，让实时流的节奏有呼吸感。

    见 :data:`INTERVAL_JITTER_RATIO`。**均值等于 interval**，所以平均速率不变
    （自检里那条"等 ~27 行把管道缓冲填满"的窗口不用跟着改），
    变的只是"每两行之间到底是快了还是慢了"。
    """
    return interval * rng.uniform(1.0 - INTERVAL_JITTER_RATIO, 1.0 + INTERVAL_JITTER_RATIO)


def run_forever(
    *,
    interval: float = DEFAULT_INTERVAL_SECONDS,
    limit: int | None = None,
    verbose: bool = True,
) -> StreamState:
    """持续产出日志，直到收到 SIGINT/SIGTERM（或达到 ``limit`` 个 tick，调试用）。

    ``interval`` 是**每条流的追加间隔**：一个 tick 里 5 条流各落一行，所以
    ``interval=0.5`` 就是"每条日志 0.5 秒加一条"、全局 10 行/秒。
    """
    state = load_state()
    # 每次启动都从剧本第 0 行重新起算（``cursors`` 不跨进程复用），并把轮次时钟归零。
    #
    # 理由不是"连续性"，恰恰相反：**落盘文件是拼接出来的** —— 前面是 ``init``
    # 用 ``loggen`` 生成的历史段，后面是本进程追加的实时段。若沿用上一进程的游标，
    # 本进程的第一笔就落在剧本中间，可能正好是某次调用的 ``<() leave``、甚至是
    # ``move absolute`` 正文；它和历史段尾部那个没来得及收口的 ``>()``
    # 在同一个调用栈上配成一对 —— 折出来的调用"有边界却没有正文"。
    # 实测：``wsp.log`` 里一次 ``MoveAbsolute`` 被配成 0 条点位正文，
    # 被 ``selftest.check_wsp_point_call_boundaries()`` 的落盘层抓了个正着。
    #
    # 从第 0 行起算就不一样了：每条流剧本的第 0 行都是**最外层函数的入口**
    # （``ScanLot()`` / ``Stage_WSP_HOME()`` / …），所以拼接缝在调用栈上是干净的。
    # 历史段尾部那个没闭合的 ``>()`` 会一直留在栈底、到文件尾也不会被配上，
    # 既不会凑成"缺正文的调用"，也不会让正文变成孤儿。
    state.cursors.clear()
    state.cursor = 0
    if not state.trace:
        new_round(state, datetime.now())
    notes = prepare(state)
    if verbose:
        print(
            f"实时日志源启动：{len(TARGETS)} 条流，每条 {interval:g}s 一行"
            f"（活动文件写满 {MAX_LIVE_LINES} 行即滑动一格）"
        )
        for note in notes:
            print(f"  {note}")
        for target in TARGETS:
            print(f"  监听地址 {target.machine.host}:{target.remote_path}")

    stopper = _Stopper()
    stopper.install()
    rng = random.Random()
    sleep_for = interval
    # 轮转后的那一觉**不能**抖动：它的下限 ROTATE_SETTLE_SECONDS 是给远端
    # ``tail -F`` 发现新 inode 用的，抖低了就会在切换瞬间丢行。
    settle = False
    try:
        while not stopper.flag:
            started = time.monotonic()
            moment = datetime.now()
            for target, _line, rotated in emit_tick(state, moment):
                if rotated is None:
                    continue
                if verbose:
                    print(f"  [轮转] {target.module}: 满 {MAX_LIVE_LINES} 行 -> {rotated.name}")
                # 给远端 tail -F 留出发现新 inode 的窗口，避免切换瞬间丢行
                sleep_for = max(interval, ROTATE_SETTLE_SECONDS)
                settle = True
            if state.ticks % RECOUNT_EVERY == 0:
                for target in TARGETS:
                    state.target(target.key).lines = count_lines(target.local_path)
            save_state(state)
            if limit is not None and state.ticks >= limit:
                break
            # 扣掉本 tick 真正花掉的时间（5 次落盘 + 状态写盘 + 计数校准）：
            # 不扣的话实际间隔会漂成 0.5s + 开销（实测 ~0.57s），
            # "0.5 秒一条"就名不副实了。开销超过间隔时退化为不睡。
            budget = sleep_for if settle else _breath(interval, rng)
            time.sleep(max(0.0, budget - (time.monotonic() - started)))
            sleep_for = interval
            settle = False
    except KeyboardInterrupt:
        pass
    finally:
        save_state(state)
        if verbose:
            print(f"实时日志源已停止（累计 {state.ticks} 轮 tick，第 {state.round} 轮）")
    return state
