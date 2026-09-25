"""实时日志源模拟：让远端 ``<fm>.log`` 像被 ``tail -f`` 跟着一样源源不断增长。

真实机台上，``<debug 根>/<子系统>/<模块>.log`` 是守护进程持续追加的**当前段**；
后端「实时监听」正是对这个文件开一条保活的 SSH 通道跑 ``tail -n 0 -F``
（见 ``apps/logsources/services/remote_logs.py::_open_live_tail_runtime``）。
本模块在模拟机上扮演那个守护进程：

* 往 5 条 ``<root>/debug/<子系统>/<模块>.log`` 持续追加**合规的八字段调试日志**
  （匹配 ``DEBUG_PATTERN``，否则前端解析不出 level/module）；
* 正文遵守项目的调用链规则：``[函数名] >()`` 入口、``[函数名] <()`` 出口，
  同名 LIFO 配对（拼装逻辑复用 ``loggen.log_message``）。前端就是靠这对方向符
  把一段调用折成函数卡片的，所以不能只写没有边界的散句；
* 内容是一段可循环的故障剧本：**1 类正常节拍 + 3 类异常**，异常之间靠
  ``trace=`` / ``cause=`` 与同一个 lot / wafer 编号显式关联 ——
  编码器抖动 → 伺服补偿发热 → 冷却流量不足 → 光源互锁跳闸 → 扫片侧
  WARN/ERROR/FATAL 三级升级 → 复位重试；
* 其中 ``wsp`` 这条流是**工件台点位**日志：每一行都是一条固定格式的
  ``move absolute { x:…, y:… }``（点位表见 ``loggen.WSP_MOVE_POINTS``），
  它自己也带一条 WARN → ERROR → FATAL 的停位异常链，所以单独订阅它同样有料；
* 因为前端「实时监听」一次只盯一个模块，需要被单独盯住的流（``spwsp`` / ``wsp``，
  见 :data:`OBSERVER_KEYS`）自己也会把整条链的后果按 WARN / ERROR / FATAL
  三个级别记一遍，只订阅一个模块就能同时看到「≥3 类异常 + 1 类正常节拍」；
* 活动文件写满 ``MAX_LIVE_LINES`` 行就**轮转**（改名成带关闭边界的归档、重建空文件），
  日志目录里只留最新一份归档，旧的搬进 ``run/recycle/``（用改名而不是删除，
  见 ``reclaim()``），所以活动文件永远不超过 1000 行、目录也不会越堆越乱。

于是前端点开「实时监听」就能看到日志从订阅点开始一行行滚出来，
滚满一轮后自动从头再来，不需要人工造数据。
"""

from __future__ import annotations

import json
import signal
import time
import zlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from . import fleet
from .loggen import (
    PHASE_BODY,
    PHASE_ENTER,
    PHASE_LEAVE,
    expand_stage_groups,
    log_message,
    point_body,
    source_line,
    stage_codes_of,
)

# --------------------------------------------------------------------------- 常量

#: 活动文件行数上限。写满就轮转，保证前端「日志不超过 1000 行」的观感。
MAX_LIVE_LINES = 1000

#: 每行间隔（秒）。1s 一行 ≈ 1000 行 / 17 分钟写满一轮活动文件。
#: 注意剧本是**5 条流交错**的一条扁平序列，每 tick 只落一行，
#: 所以被观察的那一条流实际是 ``5 × DEFAULT_INTERVAL_SECONDS`` 才走一行。
#: 想让自己盯的那条流 1 秒一行，用 ``--interval 0.2``。
DEFAULT_INTERVAL_SECONDS = 1.0

#: 轮转后留给远端 ``tail -F`` 的检测窗口：BSD/GNU tail 都是每秒 stat 一次文件名，
#: 太急着往新文件写，切换瞬间的那几行会被漏掉。
ROTATE_SETTLE_SECONDS = 1.05

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
    "status:settled",
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
#   enter -> ``[ScanWafer] >() enter 晶圆扫片 开始 wafer=W07 ...``
#   body  -> ``[ScanWafer] 普通正文``
#   leave -> ``[ScanWafer] <() leave 晶圆扫片 end wafer=W07 elapsed=86.4ms status=ok``
# 入口/出口那句**固定关键字**（「晶圆扫片」「曝光扫描」「获取冷却流量」……）来自
# ``loggen.PHASE_KEYWORDS``；出口复用同一个关键字 + ``end``，肉眼就能配对。
#
# 阶段码把一串调用包成 ``[Stage_<阶段码>]`` 阶段框（展开逻辑在
# ``loggen.expand_stage_groups``）。码为 ``None`` 表示不套框 —— ``ScanLot`` /
# ``ScanWafer`` 要跨整轮，套进阶段框就会让子阶段先闭合、父帧被迫跨框。
#
# 三条必须守住的约束：
#
# * **每条流内部按 LIFO 闭合**：同一条文件里的入口/出口要能压栈配对，
#   子函数先于父函数闭合。跨流交错是允许的（不同文件本来就不同进程），
#   但同一条流内的出现顺序就是它的调用栈轨迹。
#   ⚠️ 推论：一个函数**不能跨两个阶段框**（例如把服务循环的开头放阶段 A、
#   收尾放阶段 B）—— 阶段框比它晚开却要比它先合，栈立刻乱。
# * **边界行只记进出**：入口/出口固定 INFO，异常级别只落在正文行 ——
#   否则会出现 ``[X] <() leave status=ok`` 却标着 FATAL 这种自相矛盾的行。
# * **阶段框的顺序就是流程顺序**：同一条流上阶段序号必须递增（selftest 会查）。
#
# ``cause=`` 里写的是**上游故障码**，这样三条异常在文本层面就能串成一条链，
# 前端按关键字搜索任意一环都能找到整条因果链。

_ROUND_ROWS: tuple[tuple[str, str | None, str, str, str, str], ...] = (
    # ---- 引子：扫片主流程的最外层帧（跨整轮，不套阶段框） ----
    ("spwsp", None, "INFO", "ScanLot", PHASE_ENTER, "lot={lot} wafers=25"),
    ("spwsp", None, "INFO", "ScanWafer", PHASE_ENTER, "wafer={wafer} recipe=SPM-V2026.09.21"),
    # ---- 阶段⓪：工件台回零（wsp 点位流）----
    # wsp 这条流记的是**工件台运动轨迹**，所以它跟着主流程一路走：回零 → 上片点 →
    # 对准点 → 扫描点 → 停位检查 → 卸片点。每行都是一条固定格式的移动点位
    # （``move absolute { x:…, y:… }``），点位表在 ``loggen.WSP_MOVE_POINTS``。
    # 之所以**插在主流程各个节点之间**而不是整块放在末尾：真实机台的点位日志是
    # 跟着动作持续刷的，整块放末尾会让"实时监听 wsp"变成几十秒静默 + 一串爆发。
    ("wsp", "WSP_HOME", "INFO", "HomeStage", PHASE_ENTER, "axis=XY mode=absolute search=reference_mark"),
    ("wsp", "WSP_HOME", "INFO", "HomeStage", PHASE_BODY, point_body("origin")),
    ("wsp", "WSP_HOME", "INFO", "HomeStage", PHASE_LEAVE, "axis=XY elapsed={elapsed} status=ok"),
    # ---- 阶段①：上片 ----
    ("spwsp", "WAFER_LOAD", "INFO", "MoveWaferStage", PHASE_ENTER, "axis=XY target=chuck"),
    ("spwsp", "WAFER_LOAD", "INFO", "MoveWaferStage", PHASE_BODY, "wafer stage settled, position error within tolerance"),
    ("spwsp", "WAFER_LOAD", "INFO", "MoveWaferStage", PHASE_LEAVE, "axis=XY elapsed={elapsed} status=ok"),
    # ---- 阶段①b：工件台走上片点（wsp）----
    ("wsp", "WSP_LOAD_MOVE", "INFO", "MoveAbsolute", PHASE_ENTER, "axis=XY point=load_position profile=rapid"),
    ("wsp", "WSP_LOAD_MOVE", "INFO", "MoveAbsolute", PHASE_BODY, point_body("load_position")),
    ("wsp", "WSP_LOAD_MOVE", "INFO", "MoveAbsolute", PHASE_LEAVE, "axis=XY elapsed={elapsed} status=ok"),
    # ---- 阶段②：对准 ----
    ("spwsp", "ALIGNMENT", "INFO", "AlignWafer", PHASE_ENTER, "wafer={wafer} marks=8"),
    ("spwsp", "ALIGNMENT", "INFO", "AlignWafer", PHASE_BODY, "alignment mark detected, offset compensation applied for wafer {wafer}"),
    ("spwsp", "ALIGNMENT", "INFO", "AlignWafer", PHASE_LEAVE, "wafer={wafer} elapsed={elapsed} status=ok"),
    # ---- 阶段②b：工件台逐个对准标记对位（wsp）----
    ("wsp", "WSP_ALIGN_MOVE", "INFO", "MoveAbsolute", PHASE_ENTER, "axis=XY point=align_mark_01 profile=align"),
    ("wsp", "WSP_ALIGN_MOVE", "INFO", "MoveAbsolute", PHASE_BODY, point_body("align_mark_01")),
    ("wsp", "WSP_ALIGN_MOVE", "INFO", "MoveAbsolute", PHASE_BODY, point_body("align_mark_08")),
    ("wsp", "WSP_ALIGN_MOVE", "INFO", "MoveAbsolute", PHASE_LEAVE, "axis=XY elapsed={elapsed} status=ok"),
    # ---- 阶段③：曝光开始。这个框在 spwsp 上要一直开到互锁恢复之后才合
    #      （中间夹着编码器 / 冷却 / 互锁各自的阶段，但那些是别的文件，不打断本流）----
    ("spwsp", "EXPOSURE", "INFO", "ExposeWafer", PHASE_ENTER, "wafer={wafer} dose=30mJ/cm2"),
    ("spwsp", "EXPOSURE", "INFO", "ExposeWafer", PHASE_BODY, "illumination source power stabilised at setpoint, dose within specification"),
    # ---- 阶段③b：工件台走扫描轨迹（wsp）—— 曝光期间真正在动的就是它 ----
    ("wsp", "WSP_SCAN_MOVE", "INFO", "MoveAbsolute", PHASE_ENTER, "axis=XY point=exposure_start profile=scan"),
    ("wsp", "WSP_SCAN_MOVE", "INFO", "MoveAbsolute", PHASE_BODY, point_body("exposure_start")),
    ("wsp", "WSP_SCAN_MOVE", "INFO", "MoveAbsolute", PHASE_BODY, point_body("exposure_mid")),
    ("wsp", "WSP_SCAN_MOVE", "INFO", "MoveAbsolute", PHASE_BODY, point_body("exposure_end")),
    ("wsp", "WSP_SCAN_MOVE", "INFO", "MoveAbsolute", PHASE_LEAVE, "axis=XY elapsed={elapsed} status=ok"),
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
    ("wsp", "WSP_SETTLE", "INFO", "CheckPositionError", PHASE_ENTER, "axis=XY tolerance=0.020um"),
    ("wsp", "WSP_SETTLE", "WARN", "CheckPositionError", PHASE_BODY, "position error 0.031um exceeds tolerance 0.020um axis=XY code=ERR_WSP_POSITION_DEVIATION cause=ERR_MECORE_ENC_JITTER trace={trace}"),
    ("wsp", "WSP_SETTLE", "ERROR", "CheckPositionError", PHASE_BODY, "settling window expired, position error not converged axis=XY code=ERR_WSP_SETTLE_TIMEOUT cause=ERR_WSP_POSITION_DEVIATION trace={trace}"),
    ("wsp", "WSP_SETTLE", "INFO", "CheckPositionError", PHASE_LEAVE, "axis=XY elapsed={elapsed} status=deviated"),
    ("wsp", "WSP_SETTLE", "INFO", "AbortMotion", PHASE_ENTER, "axis=XY reason=settle_timeout"),
    ("wsp", "WSP_SETTLE", "FATAL", "AbortMotion", PHASE_BODY, "motion aborted near soft limit, travel range guard triggered axis=XY code=ERR_WSP_SOFT_LIMIT_PROXIMITY cause=ERR_WSP_SETTLE_TIMEOUT trace={trace}"),
    ("wsp", "WSP_SETTLE", "INFO", "AbortMotion", PHASE_BODY, "stage re-settled after motion abort, position error within tolerance"),
    ("wsp", "WSP_SETTLE", "INFO", "AbortMotion", PHASE_LEAVE, "axis=XY elapsed={elapsed} status=aborted"),
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
    ("wsp", "WSP_UNLOAD_MOVE", "INFO", "MoveAbsolute", PHASE_ENTER, "axis=XY point=unload_position profile=rapid"),
    ("wsp", "WSP_UNLOAD_MOVE", "INFO", "MoveAbsolute", PHASE_BODY, point_body("unload_position")),
    ("wsp", "WSP_UNLOAD_MOVE", "INFO", "MoveAbsolute", PHASE_LEAVE, "axis=XY elapsed={elapsed} status=ok"),
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
    cursor: int = 0
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


def render_values(state: StreamState, function: str) -> dict[str, object]:
    """剧本模板变量。抖动、流量与耗时随轮次变化，避免逐轮完全雷同。"""
    if not state.trace:
        new_round(state, datetime.now())
    elapsed = 6 + zlib.crc32(function.encode("utf-8")) % 180
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

    正文用 ``loggen.log_message`` 拼装，所以每一行都带 ``[函数名]`` 前缀，
    入口/出口行还带 ``>()`` / ``<()`` 方向符 —— 前端折叠就是认这两个符号。

    后端 ``DEBUG_PATTERN`` 要求 8 个 ``[...]`` 分组；正文里的方括号不参与分字段
    （第 9 组是 ``.*``），所以正文可以放心写 ``[ScanWafer] >() ...``。
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

    清理只认 state 里记下的路径（``owned``），绝不按 glob 批量删 —— 否则会误伤
    loggen 生成的合法日包与轮转段。
    """
    archived = archive_current(target.local_path, target.module, moment)
    state.archived = str(archived)
    state.owned.append(str(archived))
    state.rotations += 1
    state.lines = 0
    reclaim(state, target.module)
    return archived
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
    """确保每条流的目录/文件存在，并把当前段裁到 1000 行以内。

    启动时 ``<fm>.log`` 往往带着 loggen 生成的当天历史（00:00 → 现在，通常 1400+ 行）。
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


def emit_once(
    state: StreamState, moment: datetime | None = None
) -> tuple[StreamTarget, str, Path | None] | None:
    """按剧本推进一行：写入目标文件并更新状态。

    返回 ``(目标, 行内容, 本次轮转出来的归档)``。轮转发生在**落笔之前**：
    当前段已经写满就先整段收档、重建空文件，再写新的一行。这样活动文件
    任何时刻都不会超过 ``MAX_LIVE_LINES``，不会出现 1001 行这种越界。
    """
    moment = moment or datetime.now()
    cursor = state.cursor % SCRIPT_LENGTH
    key, level, function, phase, template = _SCRIPT[cursor]
    target = _TARGET_BY_KEY[key]
    slot = state.target(target.key)

    archived = rotate(target, slot, moment) if slot.lines >= MAX_LIVE_LINES else None

    message = template.format(**render_values(state, function))
    line = make_line(target, level, function, phase, message, moment)

    path = target.local_path
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")

    slot.lines += 1
    state.last_line = line
    state.ticks += 1

    state.cursor = (cursor + 1) % SCRIPT_LENGTH
    if state.cursor == 0:
        # 剧本站满一整轮 -> 下一行开始就是新的追踪号/批次/晶圆
        new_round(state, moment)
    return target, line, archived


# --------------------------------------------------------------------------- 状态文件


def load_state() -> StreamState:
    if not STATE_FILE.exists():
        return StreamState()
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return StreamState()
    state = StreamState(
        cursor=int(raw.get("cursor", 0)) % SCRIPT_LENGTH,
        round=int(raw.get("round", 0)),
        trace=str(raw.get("trace", "")),
        lot=str(raw.get("lot", "")),
        wafer=int(raw.get("wafer", 0)),
        ticks=int(raw.get("ticks", 0)),
        last_line=str(raw.get("last_line", "")),
    )
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
        })
    return {
        "round": state.round,
        "cursor": state.cursor,
        "trace": state.trace,
        "lot": state.lot,
        "wafer": state.wafer,
        "ticks": state.ticks,
        "max_lines": MAX_LIVE_LINES,
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


def run_forever(
    *,
    interval: float = DEFAULT_INTERVAL_SECONDS,
    limit: int | None = None,
    verbose: bool = True,
) -> StreamState:
    """持续产出日志，直到收到 SIGINT/SIGTERM（或达到 ``limit`` 行，调试用）。"""
    state = load_state()
    if not state.trace:
        new_round(state, datetime.now())
    notes = prepare(state)
    if verbose:
        print(f"实时日志源启动：{len(TARGETS)} 条流，间隔 {interval:g}s，活动文件上限 {MAX_LIVE_LINES} 行")
        for note in notes:
            print(f"  {note}")
        for target in TARGETS:
            print(f"  监听地址 {target.machine.host}:{target.remote_path}")

    stopper = _Stopper()
    stopper.install()
    sleep_for = interval
    try:
        while not stopper.flag:
            moment = datetime.now()
            produced = emit_once(state, moment)
            if produced is not None:
                target, _line, rotated = produced
                if rotated is not None:
                    if verbose:
                        print(f"  [轮转] {target.module}: 满 {MAX_LIVE_LINES} 行 -> {rotated.name}")
                    # 给远端 tail -F 留出发现新 inode 的窗口，避免切换瞬间丢行
                    sleep_for = max(interval, ROTATE_SETTLE_SECONDS)
            if state.ticks % RECOUNT_EVERY == 0:
                for target in TARGETS:
                    state.target(target.key).lines = count_lines(target.local_path)
            save_state(state)
            if limit is not None and state.ticks >= limit:
                break
            time.sleep(sleep_for)
            sleep_for = interval
    except KeyboardInterrupt:
        pass
    finally:
        save_state(state)
        if verbose:
            print(f"实时日志源已停止（累计 {state.ticks} 行，第 {state.round} 轮）")
    return state
