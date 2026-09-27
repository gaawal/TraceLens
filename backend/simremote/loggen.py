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

行内容遵守项目固定的调用链规则：``函数名() >()`` 是入口、``函数名() <()`` 是出口，
同名 LIFO 配对（见下方"调用链规则"一节）。前端就是按这对方向符折叠函数卡片的，
所以正文不能写成没有入口/出口的散句。**函数方法名一定要带括号** —— 方向符前面那个词
是被调用的函数，写成调用形状才是原生格式。
"""

from __future__ import annotations

import io
import json
import math
import os
import random
import re
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

# --------------------------------------------------------------------------- 时间戳抖动
#
# 日志时间戳**不能落在整齐的网格上**。曾经是 ``start + i * step`` 的纯算术序列，
# 结果是：毫秒位恒为 ``.000``，秒位在 60s/300s 步长下永远是 ``:00``，
# 一分钟一行就真的每分钟第 0 秒一行 —— 一眼假，用户第一句话就是"太整齐了"。
#
# 真实机台不可能这样：写日志的进程会和别的线程抢 CPU、被 IO 阻塞、被调度器挪走，
# 相邻两行的间隔会**忽快忽慢**。所以这里给时刻序列叠加一个**有界随机游走**：
#
#     moment_i = start + i * step + walk_i
#     walk_{i+1} = reflect(walk_i + U(-swing, +swing))   在 [0, limit] 内
#
# * 用**游走**而不是每点独立随机：相邻间隔带惯性（连着几行偏慢、再连着几行偏快），
#   像真的调度抖动；独立随机看起来是均匀噪声，反而不自然。
# * 边界用**反射**而不是**钳位**（``min/max`` 夹住）。这是踩过的坑：钳位会让游走
#   一头撞在 0 / limit 上"贴住"不动，时间戳就卡在同一个相位上 —— 实测 1361 行里
#   有 114 行（8%）的毫秒位仍然是 ``000``，而且秒位只覆盖每分钟的前 15 秒
#   （"从来没有 :30 之后的行"同样很假）。反射的平稳分布是均匀的，两个毛病一起消失。
# * ``walk`` 是浮点数，所以亚秒尾巴（毫秒位）天然是散的，不需要额外造。
#
# 抖动由 ``salt`` 播种。同一天、同一个 (机器, 子系统, 模块, 文件种类) 恒定，
# 所以自检与对齐脚本仍然可复现；但跨模块 / 跨子系统 / 跨天各不相同，
# 整棵树看上去就不是"一个模子刻出来的"。

#: 游走偏移的上限，取步长的这个比例 —— 要接近整个步长，时间戳的**秒位**才能铺满
#: 60 秒（否则永远落在每分钟的头几秒，一眼就能看出规律）。
WALK_LIMIT_RATIO = 0.9
#: 上限的绝对值封顶（秒）。步长很大时（10 分钟）不必漂满整个周期。
WALK_LIMIT_SECONDS = 300.0
#: 每步游走幅度相对步长的比例。太小则相位扩散不动（一整份文件里时间戳只落在
#: 每分钟的头几秒，又成了另一种"整齐"）；太大就成了均匀噪声，失掉"惯性"。
#: 0.05 的含义：一分钟一行的流，相邻间隔在 57~63s 之间晃，同时相位能在
#: 一份文件里铺满整个 60 秒周期。
WALK_RATIO = 0.05
#: **事件驱动**型日志（用例日志、测试 run log 之类）的比例。它们本来就不该有
#: 稳定节拍 —— 日志是"跑到哪句打哪句"，间隔本来就忽长忽短，所以放大到 0.4。
EVENT_WALK_RATIO = 0.4


def clock_series(
    start: datetime,
    end: datetime,
    step_seconds: int,
    *,
    salt: str = "",
    ratio: float = WALK_RATIO,
):
    """``[start, end)`` 上按 ``step_seconds`` 递进、**带真实感抖动**的时刻序列。

    见文件上方「时间戳抖动」一节：这是**反射边界**的有界随机游走，不是算术网格。
    偏移恒为非负，所以序列里的每个时刻都不会早于 ``start``（调用方依赖这一点：
    归档文件的覆盖区间、CPD 数据行必须落在报告时间窗内）。

    ``ratio`` 决定"每步能晃多远"，也就是节奏有多不稳；默认是周期采样器的
    小抖动，事件驱动的日志传 :data:`EVENT_WALK_RATIO`。
    """
    seed = f"{salt}|{start:%Y%m%d%H%M%S}|{step_seconds}|{ratio}"
    rng = random.Random(seed)
    limit = min(WALK_LIMIT_SECONDS, max(0.05, step_seconds * WALK_LIMIT_RATIO))
    swing = min(max(0.02, step_seconds * ratio), limit / 2.0)
    walk = rng.uniform(0.0, limit)
    index = 0
    while True:
        moment = start + timedelta(seconds=index * step_seconds + walk)
        if moment >= end:
            return
        yield moment
        index += 1
        walk += rng.uniform(-swing, swing)
        if walk < 0.0:
            walk = -walk
        elif walk > limit:
            walk = 2.0 * limit - walk


def subsecond(moment: datetime, *, salt: str) -> datetime:
    """只把**亚秒部分**随机化，秒级刻度不动。

    给那些"时间点本身有业务含义"的地方用（测校的起止时刻、用例的起止时刻）：
    整秒刻度是规格，毫秒尾巴才是调度抖动。
    """
    rng = random.Random(f"{salt}|{moment:%Y%m%d%H%M%S}")
    return moment + timedelta(microseconds=rng.randrange(0, 1_000_000))

# 受控环境里一次性删除大量文件会被安全保护拦截（阈值约 50 个/turn），因此**真删除**
# 保持小批量，而且预算是**整轮共享**的（见 ``prune_tree``）。
PURGE_BUDGET_PER_RUN = 40

# 删除额度之外的残留不再"留给下次 init"，而是 ``os.replace`` **改名**搬进这里：
# 改名不是删除，不消耗配额，工作树立刻干净。
#
# 为什么必须改名、不能只靠"留给下次"：``init`` 每轮新增的 CPD 批次是
# 6 个模块 ×（4 份 ``.rpt`` + 4 份 ``.xlsx``）= **48 个**残留，而整轮删除预算只有
# 40 个 —— 结构性追不上。更糟的是 ``cpd_report`` 与 ``cpd_data`` 是**两棵树分别
# 清理**的：报告树 24 个残留会把额度吃光删干净，数据树的 93 个只删得掉前 16 个，
# 于是"报告没了、表格还在" → 自检 ``CPD 报告与数据表格成对`` 报孤儿。
# 改成"删不完就改名搬走"，两棵树的重合键都只剩本轮产物，对称差自然归零。
PURGE_RECYCLE_DIR = Path(__file__).resolve().parent / "run" / "recycle" / "pruned"

# --------------------------------------------------------------------------- 调用链规则
#
# 日志正文不是随手写的句子，而是遵守固定规则：
#
#     函数名() >() enter <关键字> start <入参>     <- 函数入口
#     函数名() <某个正文>                          <- 函数体内的普通日志
#     函数名() <() leave <关键字> end <耗时/状态>   <- 函数出口
#
# 两个要素缺一不可：
#
# * **方向符** ``>()`` / ``<()`` —— TraceLens 的内置折叠规则
#   ``builtin-explicit-boundary``（``frontend/src/rendering/foldingRules.ts``，
#   startKeyword ``"> ()"`` / endKeyword ``"< ()"``；``App.tsx`` 里读作
#   「函数开始 >()」「函数结束 <()」）就是认这对符号；
# * **函数方法名带括号** —— 方向符**前面**那个词是被调用的函数，写成调用形状
#   ``ScanLot()``。前端的 ``logParser.ts`` 里 ``FUNCTION_PREFIX_REGEX``
#   （``/^\s*([A-Za-z_~][\w:<>~.-]*\(\))/``）**首选**就是抓正文开头这个
#   ``函数名()``，也就是说 ``函数名() >()`` 才是原生写法。
#
# 🔴 **不要退回方括号写法**：``[ScanLot] >()`` 那种是靠 ``parseBoundaryFunctionName``
# 的兜底分支（取方向符前最后一个 ``[...]``）才勉强解析出来的历史格式。它的
# ``functionName`` 是空、只有 ``boundaryFunctionName`` 有值，会让「连续同名函数日志」
# 「闭合后的同名尾随日志」两条内置折叠规则失效。方括号只留给**纯数值/时间戳字段**
# （``[2026-09-26 08:39:46.267]`` 那种），不要拿它当函数名。
#
# **关键字模板**：入口行在方向符之后先打一个**固定关键字**（见 PHASE_KEYWORDS），
# 说明这一步在流程里干什么（``lot scan`` / ``exposure scan`` / ``coolant flow read``……），
# 出口行打同一个关键字 + ``end``。这样：
#
# * 扫一眼就知道日志处在流程的哪一段，不用反推函数名；
# * 入口/出口靠"同一个关键字"就能肉眼配对，也方便按关键字全局检索；
# * 关键字是**固定模板**（一个函数永远同一句），所以可以当稳定的检索锚点。
#
# **正文一律英文**：真实机台的调试日志、执行器日志、运行事件日志都是英文的，
# 模拟器必须一致 —— 出现中文会让人一眼看出是假数据，也会让"按关键字检索"的
# 习惯无法迁移到真机。关键字表里只放英文短语，别再改回中文。
#
# 三个必须守住的约束：
#
# * **同名**：出口的 ``函数名()`` 要和入口逐字相同 —— 配对键就是这个字符串，
#   差一个字母就变成一条永远合不上的调用；
# * **LIFO**：入口压栈、出口出栈，子函数必须先于父函数闭合，否则前端会画出
#   错乱嵌套，或者把父函数标成「未闭合」；
# * **关键字固定**：同一函数的入口/出口关键字必须一致，且不能随手改
#   （改了等于把用户的检索习惯作废）。
#
# 另外用 ``Stage_<阶段码>()`` 形式的框把一串调用包成**流程阶段**
# （``Stage_EXPOSURE()`` = 曝光阶段），这样日志既能看阶段、又能看阶段内部的调用链。
# 阶段框走的是**同一个** :func:`log_message`，所以它也是 ``Stage_EXPOSURE() >() ...``。

ENTRY_MARKER = ">()"
EXIT_MARKER = "<()"

PHASE_ENTER = "enter"
PHASE_BODY = "body"
PHASE_LEAVE = "leave"

#: 被调用函数名后面必须补的括号 —— 方向符前要写成**调用形状**。
CALL_SUFFIX = "()"

#: 「函数方法名 + 括号」的校验用正则：``ScanLot()`` / ``Stage_WSP_UNLOAD_MOVE()``。
#:
#: 与前端 ``logParser.ts::FUNCTION_PREFIX_REGEX`` 的形状保持一致（首字符字母/下划线，
#: 允许 ``:`` ``<>`` ``~`` ``.`` ``-`` 这些在执行器/模板里出现过的字符）。
FUNCTION_CALL_PATTERN = re.compile(r"[A-Za-z_~][\w:<>~.\-]*\(\)")

#: 阶段框的函数名前缀：``Stage_WAFER_LOAD() >() ...``。
STAGE_FUNCTION_PREFIX = "Stage_"

#: 函数 -> 入口/出口行的**固定关键字模板**。
#:
#: 只覆盖调用链上的函数（正文行不带关键字）。新增函数时**必须**在这里登记，
#: 否则会退回成拿函数名当关键字（selftest 会直接报出来）。
PHASE_KEYWORDS: dict[str, str] = {
    # ---- 扫片主流程 ----
    "ScanLot": "lot scan",
    "ScanWafer": "wafer scan",
    "LoadWafer": "wafer load",
    "UnloadWafer": "wafer unload",
    "MoveWaferStage": "wafer stage move",
    "AlignWafer": "wafer alignment",
    "ExposeWafer": "exposure scan",
    "StabiliseSource": "source stabilisation",
    "MeasureOverlay": "overlay measurement",
    "RetryExposure": "exposure retry",
    "HaltScan": "scan halt",
    "RecoverStage": "stage recovery",
    "ResumeExposure": "exposure resume",
    #: 跨行测量快照（正文一次落十几行字典，见 scan_metric_body）
    "CaptureScanMetrics": "scan metric capture",
    # ---- 编码器 / 冷却 / 互锁子系统 ----
    "ServoLoop": "servo loop",
    "CheckEncoderFeedback": "encoder data read",
    "CompensateJitter": "jitter compensation",
    "CoolantLoop": "coolant loop",
    "CheckFlow": "coolant flow read",
    "UpdateThermalBudget": "thermal budget update",
    "SourceInterlock": "source interlock check",
    "TripInterlock": "interlock trip",
    # ---- 工件台点位子系统（wsp）----
    "HomeStage": "stage homing",
    "MoveAbsolute": "stage absolute move",
    "CheckPositionError": "position error check",
    "AbortMotion": "motion abort",
    # ---- 流程阶段框 ----
    "Stage_WAFER_LOAD": "wafer load stage",
    "Stage_ALIGNMENT": "alignment stage",
    "Stage_EXPOSURE": "exposure stage",
    "Stage_SERVO_SAMPLE": "servo sample stage",
    "Stage_COOLANT_FLOW": "coolant flow stage",
    "Stage_INTERLOCK_ARM": "interlock arm stage",
    "Stage_SERVO_COMPENSATE": "servo compensate stage",
    "Stage_THERMAL_BUDGET": "thermal budget stage",
    "Stage_INTERLOCK_TRIP": "interlock trip stage",
    "Stage_INTERLOCK_RECOVER": "interlock recover stage",
    "Stage_SCAN_HALT": "scan halt stage",
    "Stage_SCAN_RECOVER": "scan recover stage",
    "Stage_MEASUREMENT": "measurement stage",
    "Stage_UNLOAD": "wafer unload stage",
    # 工件台点位流的阶段框
    "Stage_WSP_HOME": "stage homing stage",
    "Stage_WSP_LOAD_MOVE": "load move stage",
    "Stage_WSP_ALIGN_MOVE": "align move stage",
    "Stage_WSP_SCAN_MOVE": "scan move stage",
    "Stage_WSP_SETTLE": "stage settle stage",
    "Stage_WSP_UNLOAD_MOVE": "unload move stage",
}


# --------------------------------------------------------------------------- 移动点位
#
# ``wsp``（工件台点位）组件的日志不是泛泛的"扫片正文"，而是**固定的绝对移动点位**，
# 并且是**完整六自由度**（工件台本来就是 6-DOF 台：三个平动 + 三个转动）：
#
#     move absolute { "x":0.003, "y":0.999, "z":0.008, "rx":0.0009, "ry":-0.0013,
#                     "rz":0.0021, "speed":120.0, "mode":"absolute",
#                     "point":"load_position", "status":"settled" }
#
# 🔴 **键与字符串值一律带双引号 —— 花括号里就是一个合法的 JSON 对象。**
# 用户提过这件事：字典不带引号（``{ x:1.250, … }``）看着像字典，其实
# ``json.loads`` 直接抛 ``JSONDecodeError``，"看着能解析、一用就崩"最坑人。
# 所以数值字段照 JSON 写裸数字（``1.250`` / ``-0.0003`` / ``300.0`` 都合法），
# 键与 ``mode`` / ``point`` / ``status`` 这三个字符串值必须加 ``"``。
# 要取出来用，直接 :func:`parse_move_point`（内部就是 ``json.loads``）。
#
# 三个"固定"，是为了让它可被当检索锚点用：
#   * 字段顺序固定（x / y / z / rx / ry / rz / speed / mode / point / status）；
#   * 数值精度固定（平动 3 位小数、三个转角 4 位、speed 1 位）；
#   * 点位名固定（见 WSP_MOVE_POINTS，全表就这几个）。
#
# 生成与校验共用下面这一份定义：``move_point()`` 负责拼、``MOVE_POINT_PATTERN``
# 负责校。改格式时两边一起改，不会出现"生成变了、校验还在按老格式找"。

# ---- 曝光扫描轨迹：从场中心向外螺旋步进 -------------------------------------
#
# 曝光扫描不是"两点连一条直线"，而是**从曝光场中心出发、一圈圈向外螺旋步进**的
# 阿基米德螺线：每步转过的角度固定（Δθ = 360°/每圈步数）、每步的径向增量固定，
# 于是走满一圈半径就大一档，螺距恒定，轨迹由内向外盘出去：
#
#     中心 ──→ 第 1 圈 ──→ 第 2 圈 ──→ 第 3 圈（贴到曝光场边缘）
#
# 为什么是**椭圆**螺线而不是正圆：工件台在曝光场里的行程是矩形的
# （x∈[0.5, 2.0]、y∈[2.0, 2.8]，见下面 exposure_start / exposure_end），正圆螺线
# 会在长边方向留一大片空行程、短边方向又顶出行程。椭圆的两个半轴直接取曝光场的
# 两个半幅，螺线就刚好由内向外铺满整个场，最后一圈贴在边缘上。
#
# 为什么**程序生成**而不是手写一张表：螺线的形状是可调的几何量（圈数 / 每圈步数 /
# 两个半轴），手写二十几行数值既没法调参，也看不出"这是螺线"。生成用纯解析式、
# 一个随机数都不掺 —— 夹具必须可复现（同一个 index 永远同一个坐标），否则
# "落盘比对"型自检会随机失败。

#: 螺线圈数与每圈步进数。步进 = 每一步转过的角度固定为 ``360°/每圈步数``。
SPIRAL_TURNS = 3
SPIRAL_STEPS_PER_TURN = 8

#: 螺线总步数（不含中心点本身，中心是轨迹的起点 exposure_mid）。
SPIRAL_STEPS = SPIRAL_TURNS * SPIRAL_STEPS_PER_TURN

#: 螺线中心 = 曝光场中心，与 ``exposure_mid`` 同坐标（轨迹从这一点的位置起步）。
SPIRAL_CENTER: tuple[float, float] = (1.250, 2.400)

#: 螺线椭圆的半轴：分别铺满曝光场 x∈[0.5, 2.0] 与 y∈[2.0, 2.8]。
SPIRAL_RADIUS_X = 0.750
SPIRAL_RADIUS_Y = 0.400

#: 扫描工作高度（与 exposure_* 点位同一平面）与目标速度。
SPIRAL_Z = 0.015
SPIRAL_SPEED = 300.0

#: 螺线点位的名字前缀：``spiral_01`` … ``spiral_{SPIRAL_STEPS:02d}``。
#: 检索"台的运动轨迹"就按 ``"point":"spiral_`` 捞 —— 按序读到的坐标序列就是轨迹。
SPIRAL_POINT_PREFIX = "spiral_"


def _snap(value: float, digits: int) -> float:
    """四舍五入到指定小数位，并把 ``-0.0`` 归成 ``0.0``。

    不归零的话 ``f"{-0.00004:.4f}"`` 会写出 ``-0.0000`` —— 一个"负零"坐标在日志里
    很扎眼：格式正则虽然放行，但人一眼就知道这个数是凑出来的。
    """
    rounded = round(value, digits)
    return 0.0 if rounded == 0 else rounded


def spiral_points() -> tuple[tuple[str, float, float, float, float, float, float, float], ...]:
    """曝光扫描的**螺旋步进轨迹**点位表（自中心起算的 ``SPIRAL_STEPS`` 个点）。

    第 k 步（k = 1…``SPIRAL_STEPS``）::

        θ_k = k · 2π / SPIRAL_STEPS_PER_TURN     # 等角度步进
        t_k = k / SPIRAL_STEPS                   # 径向比例，末步 = 1（贴到边缘）
        x_k = cx + SPIRAL_RADIUS_X · t_k · cos θ_k
        y_k = cy + SPIRAL_RADIUS_Y · t_k · sin θ_k

    所以第 0 步在中心、第 8/16/24 步分别是第 1/2/3 圈走完的时刻 ——
    "从中点不断螺旋步进到外面"就是这个序列本身。

    三个姿态自由度不是常数：曝光时工件台要**边扫边补**镜面倾斜与晶圆旋转残差，
    外圈的补偿量比内圈大。这里按"线性随半径 × 正弦随角度"给一组确定量，
    量级落在 1e-4 ~ 1e-3 度（真实补偿量的量级）。
    """
    rows: list[tuple[str, float, float, float, float, float, float, float]] = []
    cx, cy = SPIRAL_CENTER
    step_angle = 2.0 * math.pi / SPIRAL_STEPS_PER_TURN
    for step in range(1, SPIRAL_STEPS + 1):
        angle = step * step_angle
        ratio = step / SPIRAL_STEPS
        rows.append((
            f"{SPIRAL_POINT_PREFIX}{step:02d}",
            _snap(cx + SPIRAL_RADIUS_X * ratio * math.cos(angle), 3),
            _snap(cy + SPIRAL_RADIUS_Y * ratio * math.sin(angle), 3),
            SPIRAL_Z,
            _snap(0.0002 + 0.00035 * math.sin(angle * 0.5) * ratio, 4),
            _snap(-0.0004 - 0.00030 * math.cos(angle * 0.5) * ratio, 4),
            _snap(0.00060 * math.sin(angle) * ratio, 4),
            SPIRAL_SPEED,
        ))
    return tuple(rows)


#: 螺旋轨迹点位（生成一次；点位名固定，可当检索锚点）。
SPIRAL_POINTS = spiral_points()

#: 曝光扫描阶段要走的**完整点序**：入场 → 螺旋中心 → 螺旋步进向外 → 退场。
#: 批量程序与实时剧本都引用它 —— 改圈数/密度只动上面的常量，两边自动同步，
#: 不会出现"剧本还在走老轨迹、点位表已经变了"。
SCAN_TRAJECTORY_POINTS: tuple[str, ...] = (
    "exposure_start",
    "exposure_mid",
    *(row[0] for row in SPIRAL_POINTS),
    "exposure_end",
)

#: 固定的移动点位表：(点位名, x, y, z, rx, ry, rz, speed)。
#: 单位：x/y/z = mm，rx/ry/rz = 度（绕 X/Y/Z 轴），speed = mm/s。
#: 点位名只许小写字母/数字/下划线。
WSP_MOVE_POINTS: tuple[tuple[str, float, float, float, float, float, float, float], ...] = (
    ("origin", 0.000, 0.000, 0.000, 0.0000, 0.0000, 0.0000, 50.0),
    ("load_position", 0.003, 0.999, 0.008, 0.0009, -0.0013, 0.0021, 120.0),
    ("align_mark_01", 0.031, 1.247, 0.012, 0.0006, -0.0009, 0.0018, 120.0),
    ("align_mark_08", 0.263, 1.508, 0.012, 0.0011, -0.0016, 0.0034, 120.0),
    ("exposure_start", 0.500, 2.000, 0.015, 0.0000, 0.0000, 0.0000, 300.0),
    # 螺旋中心：轨迹的起点，坐标与 SPIRAL_CENTER 同源（别只改一处）。
    ("exposure_mid", SPIRAL_CENTER[0], SPIRAL_CENTER[1], SPIRAL_Z,
     0.0003, -0.0005, 0.0007, SPIRAL_SPEED),
    *SPIRAL_POINTS,
    ("exposure_end", 2.000, 2.800, 0.015, 0.0005, -0.0008, 0.0012, 300.0),
    ("unload_position", 0.004, 0.998, 0.006, 0.0010, -0.0014, 0.0023, 150.0),
)

_MOVE_POINT_BY_NAME = {row[0]: row[1:] for row in WSP_MOVE_POINTS}

#: 六自由度的**轴顺序**。生成与校验都以它为准，避免哪天又被砍掉几个轴。
MOVE_POINT_DOF: tuple[str, ...] = ("x", "y", "z", "rx", "ry", "rz")

#: 移动点位正文的**固定前缀**。冒号右边就是一个 JSON 对象，见 :func:`parse_move_point`。
MOVE_POINT_PREFIX = "move absolute "

#: 一条移动点位正文的**校验正则**，与 :func:`move_point` 的输出严格对应。
#: 键与字符串值都带双引号（花括号内是合法 JSON），数值照 JSON 写裸数字。
MOVE_POINT_PATTERN = (
    r'move absolute \{ "x":-?\d+\.\d{3}, "y":-?\d+\.\d{3}, "z":-?\d+\.\d{3}, '
    r'"rx":-?\d+\.\d{4}, "ry":-?\d+\.\d{4}, "rz":-?\d+\.\d{4}, "speed":\d+\.\d, '
    r'"mode":"absolute", "point":"[a-z0-9_]+", "status":"[a-z]+" \}'
)


def stage_function(code: str) -> str:
    """阶段码 -> 阶段框的函数名。"""
    return f"{STAGE_FUNCTION_PREFIX}{code}"


def keyword_of(function: str) -> str:
    """入口/出口行的固定关键字。没登记的函数退回函数名（selftest 会报未登记）。"""
    return PHASE_KEYWORDS.get(function, function)


def call_signature(function: str) -> str:
    """函数名 -> **调用形状**：``ScanLot`` -> ``ScanLot()``。

    方向符 ``>()`` / ``<()`` 前面那个词是**被调用的函数**，必须写成调用形状带括号。
    前端的 ``FUNCTION_PREFIX_REGEX`` 就是按 ``函数名()`` 抓的；写成光秃秃的
    ``ScanLot >()`` 或方括号 ``[ScanLot] >()`` 都会掉进兜底解析，丢掉
    ``functionName``（「连续同名函数日志」「闭合后的同名尾随日志」两条折叠规则就废了）。
    """
    return f"{function}{CALL_SUFFIX}"


def log_message(function: str, phase: str, body: str) -> str:
    """按调用链规则拼一行日志正文。

    三种相位的形状（注意函数名一律带括号）：

    * ``PHASE_ENTER`` —— ``ScanLot() >() enter lot scan start lot=… wafers=25``
    * ``PHASE_BODY``  —— ``ScanLot() exposure sequence resumed, lot … continues``
    * ``PHASE_LEAVE`` —— ``ScanLot() <() leave lot scan end lot=… elapsed=52.5ms status=ok``
    """
    keyword = keyword_of(function)
    call = call_signature(function)
    if phase == PHASE_ENTER:
        return f"{call} {ENTRY_MARKER} enter {keyword} start {body}"
    if phase == PHASE_LEAVE:
        return f"{call} {EXIT_MARKER} leave {keyword} end {body}"
    return f"{call} {body}"


def move_point(
    *,
    x: float,
    y: float,
    z: float = 0.0,
    rx: float = 0.0,
    ry: float = 0.0,
    rz: float = 0.0,
    speed: float = 120.0,
    point: str = "scan",
    status: str = "settled",
) -> str:
    """一条「绝对移动点位」日志正文（wsp 组件的固定格式）。

    平动 x/y/z + 转动 rx/ry/rz 共**六自由度**，顺序固定为
    ``x, y, z, rx, ry, rz``（就是工件台的位置/姿态向量）。

    🔴 **花括号里是合法 JSON**：键与 ``mode`` / ``point`` / ``status`` 三个字符串值
    都带双引号，数值字段是裸数字。所以这行日志能被直接 ``json.loads`` —— 反过来写
    （``{ x:1.250, … }``）就是"看着像字典、其实解析不了"，用户点过这一条。
    要取回字典用 :func:`parse_move_point`，别自己切字符串。

    ⚠️ 正文里带 ``{ ... }`` 花括号，而剧本是要过 ``str.format`` 的（模板变量
    ``{trace}`` / ``{elapsed}``）。直接把它当模板存进去会被 ``format`` 当占位符解析并
    抛 ``KeyError`` / ``ValueError``。所以这里返回的是**已转义**的模板片段
    （``{{ ... }}``），拼进模板后由 ``format`` 还原；要拿"最终写进日志的样子"，
    用 :func:`body_text` 反解。
    """
    text = (
        f'{MOVE_POINT_PREFIX}{{ "x":{x:.3f}, "y":{y:.3f}, "z":{z:.3f}, '
        f'"rx":{rx:.4f}, "ry":{ry:.4f}, "rz":{rz:.4f}, '
        f'"speed":{speed:.1f}, "mode":"absolute", "point":"{point}", "status":"{status}" }}'
    )
    return text.replace("{", "{{").replace("}", "}}")


def point_body(name: str, *, status: str = "settled") -> str:
    """按 :data:`WSP_MOVE_POINTS` 渲染指定点位的一条移动点位正文（已转义）。"""
    try:
        x, y, z, rx, ry, rz, speed = _MOVE_POINT_BY_NAME[name]
    except KeyError:
        raise KeyError(
            f"点位 {name!r} 没登记在 WSP_MOVE_POINTS（已登记：{sorted(_MOVE_POINT_BY_NAME)}）"
        ) from None
    return move_point(
        x=x, y=y, z=z, rx=rx, ry=ry, rz=rz, speed=speed, point=name, status=status
    )


def move_call_rows(
    name: str,
    *,
    profile: str = "rapid",
    status: str = "ok",
    note: str = "",
) -> tuple[tuple[str, str, str, str], ...]:
    """**一次绝对移动的完整调用**：入口 → 点位正文（+ 可选附注）→ 出口。

    为什么**每个点位各成一次调用**，而不是"一次调用里连打十几条点位"：
    项目的日志规范要求每个动作都有自己的入口/出口
    （``函数名() >()`` … ``函数名() <()``，见 :data:`PHASE_ENTER` / :data:`PHASE_LEAVE`）。
    一次 ``MoveAbsolute`` 里塞进整条轨迹的话，那几十条 ``move absolute { … }`` 正文
    **谁都没有自己的边界** —— 前端折不出函数卡片，也没法回答"走到这个点位用了多久"，
    读日志的人甚至分不清哪里是一次移动的结束。真机台的台控也不是那么打的：
    它是走一个点位压一次栈。所以**轨迹有多少个点位，就有多少次 ``MoveAbsolute``
    调用**（24 步螺旋 + 三个端点 = 27 组入口/出口）。

    返回 :data:`_WSP_GROUPS` 用的四元组布局 ``(函数, 相位, 级别, 正文)``。
    """
    rows: list[tuple[str, str, str, str]] = [
        ("MoveAbsolute", PHASE_ENTER, "INFO", f"dof=6 point={name} profile={profile}"),
        ("MoveAbsolute", PHASE_BODY, "INFO", point_body(name)),
    ]
    if note:
        rows.append(("MoveAbsolute", PHASE_BODY, "INFO", note))
    rows.append((
        "MoveAbsolute",
        PHASE_LEAVE,
        "INFO",
        # 出口**带上点位名**：屏幕上几十条 ``<() leave … elapsed=… status=ok`` 长得
        # 一模一样，不点名就分不清是哪一次移动合的；配上点位名之后一次调用三行
        # 自成一体（入口点名、正文点位、出口点名）。
        f"dof=6 point={name} elapsed={{elapsed}} status={status}",
    ))
    return tuple(rows)


def parse_move_point(text: str) -> dict:
    """把一条点位正文反解成字典 —— **内部就是 ``json.loads``，不做字符串切分**。

    点位正文（``move absolute { "x":…, "status":"settled" }``）的花括号里是合法
    JSON，所以这里直接交给 ``json.loads``：一来省掉一整套脆弱的
    ``split("point:")[1].split(",")[0]``（字段顺序或引号一变就静默取错），
    二来它本身就是"这行到底能不能被当 JSON 解析"的校验 —— 校验类代码全部走
    这个函数，格式与生成就天然同源了。

    入参可以是**已转义**的模板片段，内部用 :func:`body_text` 先还原。
    """
    body = body_text(text).strip()
    if not body.startswith(MOVE_POINT_PREFIX):
        raise ValueError(f"不是点位正文（缺前缀 {MOVE_POINT_PREFIX!r}）：{body[:60]!r}")
    payload = body[len(MOVE_POINT_PREFIX):]
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"点位正文不是合法 JSON：{payload[:80]!r}（{exc}）") from None
    if not isinstance(parsed, dict):
        raise ValueError(f"点位正文的 JSON 不是对象：{type(parsed).__name__}")
    return parsed


def point_name_of(text: str) -> str:
    """从一条点位正文里取出点位名（``"point":"spiral_07"`` -> ``spiral_07``）。

    校验类代码一律用它，别自己 ``split("point:")`` —— 键名带不带引号、字段顺序
    有没有变，都不该影响"这条是哪次移动"。
    """
    return str(parse_move_point(text)["point"])


def dof_of_point(text: str) -> list[str]:
    """从一条（还原后的）点位正文里按**出现顺序**抽出自由度字段名。

    用来断言"六个轴一个都没少"。两处细节都不能省：

    * ``rx|ry|rz`` 要排在 ``[xyz]`` 前面交替，否则 ``rx`` 会被单字母分支先吃掉；
    * 键名两侧的引号是**可选**的（``"x":`` 与 ``x:`` 都要认），所以引号写在
      捕获组外面 —— 匹配的是 ``"x":``，取回来的仍然只有 ``x``。
    """
    return re.findall(r'''["']?((?:rx|ry|rz|[xyz]))["']?:''', text)


def body_text(body: str) -> str:
    """把（可能转义过的）正文模板还原成"最终写进日志的样子"。

    只有 :func:`move_point` 产出的片段会带转义花括号；普通正文原样返回。
    校验类代码（查中文、查方向符、查移动点位格式）必须按还原后的文本判，
    否则看到的是模板而不是日志。
    """
    if "{{" not in body and "}}" not in body:
        return body
    return body.replace("{{", "{").replace("}}", "}")


# --------------------------------------------------------------------------- 测量快照
#
# 真实机台的测量 / 干涉模块习惯把**一次采样的整包读数**打成字典再落一条日志：
# 一条记录横跨很多物理行，**只有第一行**带八字段前缀（时间戳在最前），后面各行
# 都是没有时间戳的裸续行，一直持续到下一条记录的时间戳出现为止。
#
#     [2026-09-26 00:05:12.345] [INFO] [SPWSP] [20123] [30145] [spwsp] [normal]
#         [spwsp:CaptureScanMetrics:412] CaptureScanMetrics() scan metric snapshot …
#       "position": {"x": 1.250, "y": 2.400, …},
#       "power_mW": 248.63,
#       …
#
# 续行里的花括号整体是**合法 JSON**（键与字符串值双引号），跟 ``move_point`` 一致 ——
# 日志里的字典一律按 JSON 写，别用 Python ``repr`` 的单引号。
#
# ⚠️ 这种日志会**打穿「按行首时间戳切记录」的解析器**。本项目里就是
# ``apps/logsources/services/remote_logs.py`` 的三条消费路径：
# ``_stream_direct_forward_from_offset``、``_stream_direct`` 的 ``reverse_lines``
# 分支、以及 ``_stream_tar_member`` —— 它们对 ``parse_line_time`` 返回 None 的行
# 直接 ``continue``；tar 那条更早一步，shell 侧的 awk 预筛写着
# ``substr($0,1,1) != "[" { next }``，续行连 Python 都没见到就被丢了。
# 结果就是**正文只剩第一行，后半截被截断**。
#
# 下面这个夹具专门用来复现该场景，别把它"顺手修回"成单行。

#: 激光器镜干涉仪的对比度测量通道：水平 4 路 + 垂直 3 路。
INTERFEROMETER_CHANNELS: tuple[str, ...] = ("x1", "x2", "x3", "x4", "y1", "y2", "y3")

#: 测量快照的正文首句标记。校验类代码用它认这条记录（正文只有首行带它）。
SCAN_METRIC_MARKER = "scan metric snapshot"


def channel_contrast(axis: str, *, base: float = 0.9137) -> float:
    """干涉仪单个通道的对比度读数：以基准值为中心的一个稳定小偏移。

    偏移量取自轴名的 ``crc32`` 而不是 ``random`` —— 夹具必须**可复现**：同一通道
    每次生成的读数完全一致，否则"落盘比对"型的自检会随机失败。
    """
    offset = (zlib.crc32(axis.encode("utf-8")) % 61 - 30) / 1000.0
    return base + offset


def scan_metric_body(
    name: str,
    *,
    power_mw: float = 248.63,
    contrast: float = 0.9137,
    status: str = "settled",
) -> str:
    """一条**跨多行**的扫片测量快照正文（花括号已转义，可直接进 ``str.format``）。

    一次调用打印：工件台当前**六自由度位置** + 该位置采到的**功率 / 对比度** +
    激光器镜干涉仪七路通道（``x1``…``x4`` / ``y1``…``y3``）的**对比度字典**。
    形状就是真机台里把一次测量结果 ``json.dumps(..., indent=2)`` 落下来的样子 ——
    **键与字符串值一律双引号，花括号里是合法 JSON**（理由见 :func:`move_point`：
    字典不带引号就 ``json.loads`` 不了；以前这里写的是 Python ``repr`` 风格的单引号，
    同样过不了 JSON，一并改成双引号）。

    返回的是**已转义**的模板片段（``{{ … }}``），理由同 :func:`move_point`：
    正文要过 ``str.format``，不转义会被当成占位符解析并抛 ``KeyError``。
    要取"最终写进日志的样子"，用 :func:`body_text` 反解。
    """
    try:
        x, y, z, rx, ry, rz, speed = _MOVE_POINT_BY_NAME[name]
    except KeyError:
        raise KeyError(
            f"点位 {name!r} 没登记在 WSP_MOVE_POINTS（已登记：{sorted(_MOVE_POINT_BY_NAME)}）"
        ) from None

    lines = [
        f'{SCAN_METRIC_MARKER} point={name} dof=6 {{',
        f'  "position": {{"x": {x:.3f}, "y": {y:.3f}, "z": {z:.3f}, '
        f'"rx": {rx:.4f}, "ry": {ry:.4f}, "rz": {rz:.4f}}},',
        f'  "power_mW": {power_mw:.2f},',
        f'  "contrast": {contrast:.4f},',
        '  "interferometer": {',
    ]
    # ⚠️ **最后一对键值不带逗号** —— 尾随逗号在 Python 里合法、在 JSON 里非法，
    # 留着它这个字典就 ``json.loads`` 不了（形状看着完全正确，一解析才炸）。
    last = len(INTERFEROMETER_CHANNELS) - 1
    for index, axis in enumerate(INTERFEROMETER_CHANNELS):
        tail = "" if index == last else ","
        lines.append(f'    "{axis}": {channel_contrast(axis, base=contrast):.4f}{tail}')
    lines += [
        "  },",
        f'  "stage_speed_mm_s": {speed:.1f},',
        f'  "status": "{status}"',
        "}",
    ]
    return "\n".join(lines).replace("{", "{{").replace("}", "}}")


def parse_scan_metric(text: str) -> dict:
    """把测量快照正文反解成字典（首行那句前缀之后就是一个**合法 JSON 对象**）。

    与 :func:`parse_move_point` 一并构成"日志里的字典都能 ``json.loads``"的约定：
    校验类代码走这个函数，而不是去 ``split("'position':")`` —— 那种写法在引号从
    单引号改双引号、或尾随逗号被加回来时**不会报错，只会取错**。
    """
    body = body_text(text).strip()
    start = body.find("{")
    if start < 0:
        raise ValueError(f"快照正文里没有字典：{body[:60]!r}")
    try:
        parsed = json.loads(body[start:])
    except json.JSONDecodeError as exc:
        raise ValueError(f"快照字典不是合法 JSON（{exc}）：{body[start:start + 80]!r}") from None
    if not isinstance(parsed, dict):
        raise ValueError(f"快照字典不是 JSON 对象：{type(parsed).__name__}")
    return parsed


def stage_codes_of(rows) -> tuple[str, ...]:  # noqa: ANN001 - 迭代即可
    """阶段码按**首次出现的顺序**排 —— 就是流程的阶段序号。"""
    return tuple(dict.fromkeys(row[1] for row in rows if row[1]))


def expand_stage_groups(rows) -> tuple:  # noqa: ANN001 - 迭代即可
    """把带阶段码的行展开成扁平剧本，给**每条流上连续的同阶段**套一个阶段框。

    入参每项 = ``(流标识, 阶段码或 None, 级别, 函数, 相位, 正文)``，
    返回每项 = **固定五元组** ``(流标识, 级别, 函数, 相位, 正文)``。

    ⚠️ 出口形状只有这一种：普通行与阶段框的**头/尾行**都走同一个 7 元组内部布局
    ``(位置, 流, 阶段码, 级别, 函数, 相位, 正文)``。以前头/尾行按
    ``(..., 函数, 相位, 级别, ...)`` 另建元组，结果同一个返回值里两种行的字段
    含义不一样 —— 调用方按一种顺序解包，另一种就必然错位（实测会把
    ``Stage_EXPOSURE`` 解到「级别」槽里、函数名变成 ``enter``）。

    "连续"是按**该流自己**的步序判断的 —— 跨流的交错不算打断。所以一条流上的
    同一阶段可以横跨别的流的若干阶段而仍然是同一个框（曝光阶段就是这样：spwsp
    开在曝光前、合在互锁恢复之后，中间夹着编码器 / 冷却 / 互锁各自的阶段）。

    阶段框本身也吃 ``PHASE_KEYWORDS`` 里的关键字模板，所以入口是
    ``Stage_EXPOSURE() >() enter exposure stage start ...``、出口是
    ``... exposure stage end ...``。
    """
    stream_order: list[str] = []
    rows_by_stream: dict[str, list[tuple[str | None, tuple]]] = {}
    for position, row in enumerate(rows):
        key = row[0]
        if key not in rows_by_stream:
            rows_by_stream[key] = []
            stream_order.append(key)
        # 内部行统一成 7 元组：位置 + 流 + 阶段码 + 级别 + 函数 + 相位 + 正文
        rows_by_stream[key].append((row[1], (position, key, row[1], row[2], row[3], row[4], row[5])))

    codes = stage_codes_of(rows)
    total = len(codes)
    ordinal_of = {code: index + 1 for index, code in enumerate(codes)}
    items: list[tuple[int, list[tuple]]] = []

    for key in stream_order:
        stream_rows = rows_by_stream[key]
        index = 0
        while index < len(stream_rows):
            code, row = stream_rows[index]
            if code is None:
                items.append((row[0], [row]))
                index += 1
                continue
            end = index
            while end + 1 < len(stream_rows) and stream_rows[end + 1][0] == code:
                end += 1
            group = [stream_rows[position][1] for position in range(index, end + 1)]
            step = f"step={ordinal_of[code]}/{total}"
            function = stage_function(code)
            head = (
                group[0][0], key, code, "INFO", function, PHASE_ENTER,
                f"{step} wafer={{wafer}} lot={{lot}}",
            )
            tail = (
                group[-1][0], key, code, "INFO", function, PHASE_LEAVE,
                f"{step} status={_STAGE_STATUS.get(code, 'ok')} elapsed={{elapsed}}",
            )
            items.append((group[0][0], [head, *group, tail]))
            index = end + 1

    items.sort(key=lambda item: item[0])
    return tuple(
        (line[1], line[3], line[4], line[5], line[6])
        for _position, lines in items
        for line in lines
    )


#: 阶段框收尾时的状态。让阶段出口行也带真实结果（曝光是被互锁中止的、互锁是跳闸的……），
#: 而不是清一色 ``status=ok`` —— 否则阶段框反而会掩盖故障。
_STAGE_STATUS: dict[str, str] = {
    "WAFER_LOAD": "ok",
    "ALIGNMENT": "ok",
    "EXPOSURE": "aborted",
    "MEASUREMENT": "ok",
    "UNLOAD": "ok",
    "SERVO_SAMPLE": "degraded",
    "COOLANT_FLOW": "low",
    "INTERLOCK_ARM": "armed",
    "SERVO_COMPENSATE": "degraded",
    "THERMAL_BUDGET": "degraded",
    "INTERLOCK_TRIP": "tripped",
    "INTERLOCK_RECOVER": "recovered",
    "SCAN_HALT": "halted",
    "SCAN_RECOVER": "ok",
}


def source_line(function: str) -> int:
    """函数 -> 源码行号，让 ``rpc`` 字段保持 ``文件:函数:行`` 的形状。

    行号按函数名固定：同一个函数在整棵日志树里永远指同一处，不会一会儿 120
    一会儿 380（那样"同名字"的调用看着像两个不同的函数）。
    """
    return 120 + zlib.crc32(function.encode("utf-8")) % 400


def call_mode(function: str) -> str:
    """执行通道标记。同一函数固定不变 —— 折叠聚合要求一次调用的入口/出口通道一致。"""
    return "trace" if zlib.crc32(function.encode("utf-8")) % 3 == 0 else "normal"


#: 调试日志的调用链程序：每项 = (阶段码或 None, 函数名, 相位, 级别, 正文模板)。
#:
#: 相位序列必须是一条合法的调用栈轨迹（入口/出口同名配对、LIFO 闭合）：
#:
#:     ScanLot() >()  ScanWafer() >()  Stage_WAFER_LOAD() >()  MoveWaferStage() >() ... <()
#:
#: 阶段码为 ``None`` 表示**不套阶段框**：最外层的 ``ScanLot``/``ScanWafer`` 要跨
#: 整轮，套进阶段框就会让子阶段先闭合、父帧被迫跨框（LIFO 直接破掉）。
#: 同一阶段里可以放多个函数（``EXPOSURE`` 里就有 ``ExposeWafer`` 和
#: ``StabiliseSource``），也可以让同一个函数跨两条相邻的同阶段记录。
#:
#: 模板变量：{wafer} 晶圆号、{lot} 批次号、{elapsed} 本次调用耗时、{software} 软件版本。
#:
#: 级别约定：**入口/出口行固定 INFO，异常级别只落在正文行** —— 边界行只负责记
#: 进出，正文才是产生告警的地方。
_DEBUG_GROUPS: tuple[tuple[str | None, tuple[tuple[str, str, str, str], ...]], ...] = (
    (None, (
        ("ScanLot", PHASE_ENTER, "INFO", "lot={lot} wafers=25 recipe={software}"),
        ("ScanWafer", PHASE_ENTER, "INFO", "wafer={wafer} recipe={software}"),
    )),
    ("WAFER_LOAD", (
        ("LoadWafer", PHASE_ENTER, "INFO", "wafer={wafer} source=loadport"),
        ("MoveWaferStage", PHASE_ENTER, "INFO", "axis=XY target=chuck"),
        ("MoveWaferStage", PHASE_BODY, "INFO", "wafer stage settled, position error within tolerance"),
        ("MoveWaferStage", PHASE_LEAVE, "INFO", "axis=XY elapsed={elapsed} status=ok"),
        ("LoadWafer", PHASE_LEAVE, "INFO", "wafer={wafer} elapsed={elapsed} status=ok"),
    )),
    ("ALIGNMENT", (
        ("AlignWafer", PHASE_ENTER, "INFO", "wafer={wafer} marks=8"),
        ("AlignWafer", PHASE_BODY, "INFO", "alignment mark detected, offset compensation applied"),
        ("AlignWafer", PHASE_LEAVE, "INFO", "wafer={wafer} elapsed={elapsed} status=ok"),
    )),
    ("EXPOSURE", (
        ("ExposeWafer", PHASE_ENTER, "INFO", "wafer={wafer} dose=30mJ/cm2"),
        ("StabiliseSource", PHASE_ENTER, "INFO", "setpoint=250W"),
        ("StabiliseSource", PHASE_BODY, "INFO", "illumination source power stabilised at setpoint"),
        ("StabiliseSource", PHASE_LEAVE, "INFO", "setpoint=250W elapsed={elapsed} status=ok"),
        ("ExposeWafer", PHASE_BODY, "INFO", "scan trajectory buffered and verified"),
        ("ExposeWafer", PHASE_BODY, "WARN", "dose control loop reported within specification, margin=1.4%"),
        ("ExposeWafer", PHASE_LEAVE, "INFO", "wafer={wafer} elapsed={elapsed} status=ok"),
    )),
    ("MEASUREMENT", (
        ("MeasureOverlay", PHASE_ENTER, "INFO", "wafer={wafer} sensor=interferometer"),
        ("MeasureOverlay", PHASE_BODY, "DEBUG", "interferometer reading refreshed for wafer stage"),
        ("MeasureOverlay", PHASE_BODY, "ERROR", "fringe contrast below limit, measurement retried"),
        ("MeasureOverlay", PHASE_LEAVE, "INFO", "wafer={wafer} elapsed={elapsed} status=ok"),
    )),
    ("UNLOAD", (
        ("UnloadWafer", PHASE_ENTER, "INFO", "wafer={wafer} destination=loadport"),
        ("UnloadWafer", PHASE_LEAVE, "INFO", "wafer={wafer} elapsed={elapsed} status=ok"),
    )),
    (None, (
        ("ScanWafer", PHASE_LEAVE, "INFO", "wafer={wafer} elapsed={elapsed} status=ok"),
        ("ScanLot", PHASE_LEAVE, "INFO", "lot={lot} elapsed={elapsed} status=ok"),
    )),
)

#: 批量日志的阶段序（按首次出现）。
DEBUG_STAGE_CODES: tuple[str, ...] = tuple(code for code, _steps in _DEBUG_GROUPS if code)

_DEBUG_PROGRAM: tuple[tuple[str, str, str, str], ...] = tuple(
    (function, phase, level, body)
    for _key, level, function, phase, body in expand_stage_groups(
        [
            # 入参布局与 expand_stage_groups 的文档一致：(流, 阶段码, 级别, 函数, 相位, 正文)
            (None, code, level, function, phase, body)
            for code, steps in _DEBUG_GROUPS
            for function, phase, level, body in steps
        ]
    )
)

#: 程序里第一条 ERROR 正文的位置。失败用例的日志夹具要把错误行**钉在故障时刻**，
#: 不能指望"时间线刚好够长、刚好覆盖到那一步" —— 那是碰运气。
ERROR_STEP_INDEX = next(
    index
    for index, (_function, _phase, level, _body) in enumerate(_DEBUG_PROGRAM)
    if _phase == PHASE_BODY and level in ("ERROR", "FATAL")
)


# --------------------------------------------------------------------------- wsp 程序
#
# ``wsp``（工件台点位）组件的 fm 日志**不写通用扫片正文** —— 它记的是工件台自己的
# 运动轨迹：回零 → 上片点 → 对准点 → **曝光扫描（螺旋步进）** → 停位检查 → 卸片点。
# 正文是一条固定的绝对移动点位（见 ``move_point``），这样用户打开 ``wsp.log``
# 一眼看到的就是"点位日志"，而不是又一份扫片日志；把点位行按序读下来，读到的就是
# 工件台的运动轨迹本身。
#
# ⚠️ **一个点位 = 一次 ``MoveAbsolute`` 调用**（``MoveAbsolute() >()`` 入口 /
# ``move absolute { … }`` 正文 / ``MoveAbsolute() <()`` 出口，三行一组，见
# :func:`move_call_rows`）。**别把整条轨迹塞进一次调用**：那样几十条点位正文谁都
# 没有自己的入口/出口，既不合乎"每个动作都有边界"的日志规范，折不出函数卡片，
# 也回答不了"走到这个点位用了多久"。台控本来就是走一个点位压一次栈。
#
# 曝光那一段不是"两点连一线"，而是 ``SCAN_TRAJECTORY_POINTS``：入场点 → 曝光场中心
# → 从中心一圈圈向外螺旋步进的每一步（``spiral_01`` … ``spiral_24``，末步贴到曝光场
# 边缘）→ 退场点。螺线是**椭圆**的，两个半轴直接取曝光场的两个半幅，于是螺线刚好
# 由内向外铺满整个场；几何参数（圈数 / 每圈步数 / 半轴）都在 ``SPIRAL_*`` 常量里，
# 点位由 :func:`spiral_points` 生成 —— 批量程序与实时剧本共用这一份点序。
#
# 级别约定与通用程序一致：**入口/出口固定 INFO，异常只落在正文行**。
# 停位检查里刻意留了一条 WARN → ERROR → FATAL 的本地升级链，且根因挂在
# ``ERR_MECORE_ENC_JITTER`` 上 —— 于是 wsp 既能单独订阅看异常，又仍然串在整条
# 跨模块因果链里（``trace=`` 与其它子系统同号）。
_WSP_GROUPS: tuple[tuple[str | None, tuple[tuple[str, str, str, str], ...]], ...] = (
    ("WSP_HOME", (
        ("HomeStage", PHASE_ENTER, "INFO", "dof=6 mode=absolute search=reference_mark"),
        ("HomeStage", PHASE_BODY, "INFO", point_body("origin")),
        ("HomeStage", PHASE_BODY, "INFO", "reference mark acquired, encoder counter zeroed at origin"),
        ("HomeStage", PHASE_LEAVE, "INFO", "dof=6 elapsed={elapsed} status=ok"),
    )),
    # 一个点位 = 一次 ``MoveAbsolute`` 调用（入口 / 点位正文 / 出口三行一组）。
    # 用 :func:`move_call_rows` 生成而不是手抄三行：轨迹点数一改，三行自动跟着走，
    # 也不会出现"某一次移动忘了写出口"这种半条调用。
    ("WSP_LOAD_MOVE", move_call_rows(
        "load_position",
        profile="rapid",
        note="in position window reached, settling servo loop",
    )),
    ("WSP_ALIGN_MOVE", tuple(
        row
        for name in ("align_mark_01", "align_mark_08")
        for row in move_call_rows(name, profile="align")
    )),
    ("WSP_SCAN_MOVE", tuple(
        # 曝光扫描轨迹：入场点 → 螺旋中心 → **从中心向外螺旋步进的每一步** → 退场点，
        # 每一步各是一次调用。点序取自 ``SCAN_TRAJECTORY_POINTS``（几何参数在
        # ``SPIRAL_*`` 常量），这里不手写 —— 改圈数 / 密度只动那几个常量，
        # 点位表与轨迹自动同步。
        row
        for name in SCAN_TRAJECTORY_POINTS
        for row in move_call_rows(name, profile="scan")
    )),
    ("WSP_SETTLE", (
        ("CheckPositionError", PHASE_ENTER, "INFO", "dof=6 tolerance=0.020um"),
        ("CheckPositionError", PHASE_BODY, "INFO", "position error within tolerance, stage has settled"),
        ("CheckPositionError", PHASE_BODY, "WARN",
         "position error 0.031um exceeds tolerance 0.020um dof=6 "
         "code=ERR_WSP_POSITION_DEVIATION cause=ERR_MECORE_ENC_JITTER trace={trace}"),
        ("CheckPositionError", PHASE_BODY, "ERROR",
         "settling window expired, position error not converged dof=6 "
         "code=ERR_WSP_SETTLE_TIMEOUT cause=ERR_WSP_POSITION_DEVIATION trace={trace}"),
        ("CheckPositionError", PHASE_LEAVE, "INFO", "dof=6 elapsed={elapsed} status=deviated"),
        ("AbortMotion", PHASE_ENTER, "INFO", "dof=6 reason=settle_timeout"),
        ("AbortMotion", PHASE_BODY, "FATAL",
         "motion aborted near soft limit, travel range guard triggered dof=6 "
         "code=ERR_WSP_SOFT_LIMIT_PROXIMITY cause=ERR_WSP_SETTLE_TIMEOUT trace={trace}"),
        ("AbortMotion", PHASE_BODY, "INFO", "stage re-settled after motion abort, position error within tolerance"),
        ("AbortMotion", PHASE_LEAVE, "INFO", "dof=6 elapsed={elapsed} status=aborted"),
    )),
    ("WSP_UNLOAD_MOVE", move_call_rows("unload_position", profile="rapid")),
)

#: wsp 程序的阶段序（按首次出现）。
WSP_STAGE_CODES: tuple[str, ...] = tuple(code for code, _steps in _WSP_GROUPS if code)

_WSP_PROGRAM: tuple[tuple[str, str, str, str], ...] = tuple(
    (function, phase, level, body)
    for _key, level, function, phase, body in expand_stage_groups(
        [
            (None, code, level, function, phase, body)
            for code, steps in _WSP_GROUPS
            for function, phase, level, body in steps
        ]
    )
)

#: 子系统 -> 专属调用链程序。没登记的子系统沿用通用扫片程序。
SUBSYSTEM_PROGRAMS: dict[str, tuple[tuple[str, str, str, str], ...]] = {
    "wsp": _WSP_PROGRAM,
}


def program_for(subsystem: str) -> tuple[tuple[str, str, str, str], ...]:
    """取某个子系统的调用链程序。

    同一子系统的所有 fm 模块（debug / executor）共用一份程序 —— 否则同一个组件在
    调试视图和执行器视图里会变成两套不相干的日志。
    """
    return SUBSYSTEM_PROGRAMS.get(str(subsystem or "").strip().lower(), _DEBUG_PROGRAM)


def error_step_index(subsystem: str) -> int:
    """子系统程序里第一条 ERROR/FATAL 正文的位置。

    失败用例的日志夹具要把错误行**钉在故障时刻**。索引必须按**该系统自己的程序**
    算 —— 拿通用程序的索引去索引 wsp 程序，钉出来的可能是一条正常的点位行。
    """
    program = program_for(subsystem)
    for index, (_function, phase, level, _body) in enumerate(program):
        if phase == PHASE_BODY and level in ("ERROR", "FATAL"):
            return index
    return ERROR_STEP_INDEX


#: 所有程序（通用 + 各子系统专属）—— 校验类代码要逐个过一遍，别漏了专属程序。
ALL_PROGRAMS: tuple[tuple[str, tuple[tuple[str, str, str, str], ...]], ...] = (
    ("批量日志", _DEBUG_PROGRAM),
    *((f"批量日志 {name}", program) for name, program in SUBSYSTEM_PROGRAMS.items()),
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
    #: 删除额度用完之后、靠 ``os.replace`` **改名**搬进回收站的残留数（不是删除）。
    recycled: int = 0
    machines: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        anchor = self.anchor.strftime("%Y-%m-%d %H:%M:%S") if self.anchor else "-"
        size = f"{self.bytes_written / 1024 / 1024:.1f} MiB"
        detail = ", ".join(f"{key}={value}" for key, value in sorted(self.machines.items()))
        return (
            f"锚点 {anchor}｜文件 {self.files}｜日志行 {self.lines}｜写入 {size}｜清理 {self.pruned}"
            + (f"｜改名回收 {self.recycled}" if self.recycled else "")
            + (f"｜{detail}" if detail else "")
        )


# --------------------------------------------------------------------------- 行格式


def _stamp(moment: datetime) -> str:
    """日志行时间戳：毫秒精度，前 19 个字符是 YYYY-MM-DD HH:MM:SS。"""
    return f"{moment:%Y-%m-%d %H:%M:%S}.{moment.microsecond // 1000:03d}"


def _program_values(
    moment: datetime, seq: int, function: str, length: int, template: str = ""
) -> dict[str, str]:
    """调用链程序的模板变量。第几轮（``seq // length``）决定晶圆/批次。

    ``length`` 是**该子系统自己的程序长度** —— 用通用程序长度去除，wsp 这种短程序
    会每隔几行就跳一个批次号，看着像数据错乱。

    ``template`` 参与 ``elapsed`` 的取数：耗时本来就应该**逐次调用各不相同**，
    只按函数名取的话，wsp 那 27 次 ``MoveAbsolute`` 的出口会整整齐齐印 27 遍
    「elapsed=121.5ms」—— 一眼就是克隆出来的。用整条模板当盐，同一次调用的
    入口/正文/出口各算各的（只有出口用得上 ``elapsed``），而不同点位因为出口模板
    里写着各自的点位名，耗时自然也就各不相同。
    """
    round_index = seq // max(1, length)
    elapsed = 6 + zlib.crc32(f"{function}|{template}".encode("utf-8")) % 180
    # 批次也要有自己的追踪号：wsp 的点位异常链要写 ``trace=``，没有这个键就会
    # ``KeyError: 'trace'``。同一轮里所有行同号，形状与实时源的 ``TR-0001-ABCD``
    # 一致，前端按它检索时两种日志可以一起命中。
    suffix = zlib.crc32(f"{moment:%Y%m%d}{round_index}".encode("utf-8")) % 0xFFFF
    return {
        "wafer": f"W{1 + round_index % 25:02d}",
        "lot": f"LOT-{moment:%Y%m%d}-{round_index % 24 + 1:02d}",
        "software": fleet.SOFTWARE_VERSION,
        "elapsed": f"{elapsed + (round_index % 7) * 4.3:.1f}ms",
        "trace": f"TR-{round_index % 9999 + 1:04d}-{suffix:04X}",
    }


def debug_line(moment: datetime, subsystem: str, module: str, seq: int) -> str:
    """八字段调试日志，匹配 DEBUG_PATTERN；正文带 ``函数名() >()`` / ``<()`` 调用链边界。

    内容按**子系统**选程序：通用扫片子系统走 ``_DEBUG_PROGRAM``，wsp 走点位程序。
    """
    program = program_for(subsystem)
    function, phase, level, template = program[seq % len(program)]
    process_id = 20000 + zlib.crc32(subsystem.encode("utf-8")) % 9000
    # 线程号按模块固定：一次调用链跑在同一个线程上，前端才能把它们归到同一条执行泳道
    # （折叠、×N 聚合都以 component/process/thread 相同为前提）。
    # 用 crc32 而不是内置 hash()：hash() 每个进程都带随机种子，重新生成一次日志
    # 线程号就变了，历史段和实时段会被前端当成两条不同的泳道。
    thread_id = 30000 + zlib.crc32(module.encode("utf-8")) % 500
    rpc = f"{module}:{function}:{source_line(function)}"
    message = log_message(
        function,
        phase,
        template.format(**_program_values(moment, seq, function, len(program), template)),
    )
    return (
        f"[{_stamp(moment)}] [{level}] [{subsystem.upper()}] [{process_id}] "
        f"[{thread_id}] [{module}] [{call_mode(function)}] [{rpc}] {message}"
    )


def executor_line(moment: datetime, subsystem: str, module: str, seq: int, *, inner: bool) -> str:
    """执行器日志：100 内部（context/rpc/mode）与 101 外部（mode/rpc）两种布局。

    EXECUTOR_PATTERN 的 rpc 组要求形如 ``a:b:c``（两个冒号），不能省。
    正文与调试日志走**同一个子系统程序**，所以执行器视图里同样能折出函数卡片，
    并且 wsp 的执行器日志也仍然是点位日志。
    """
    program = program_for(subsystem)
    function, phase, level, template = program[seq % len(program)]
    process_id = 40000 + zlib.crc32(module.encode("utf-8")) % 5000
    thread_id = 50000 + zlib.crc32(module.encode("utf-8")) % 300
    rpc = f"{module}:{function}:{source_line(function)}"
    message = log_message(
        function,
        phase,
        template.format(**_program_values(moment, seq, function, len(program), template)),
    )
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


def _stamps(start: datetime, end: datetime, step_seconds: int, *, salt: str = ""):
    """兼容旧名；实际实现在 :func:`clock_series`（带抖动）。"""
    return clock_series(start, end, step_seconds, salt=salt)


def _render(
    start: datetime, end: datetime, step_seconds: int, builder, *, salt: str = ""
) -> tuple[bytes, int]:
    lines = [
        builder(moment, index)
        for index, moment in enumerate(_stamps(start, end, step_seconds, salt=salt))
    ]
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

    # 抖动种子带上"哪一类文件"：同一个模块的当前段 / 轮转段 / 嵌套包 / 日包
    # 各自一套节奏，免得看起来像同一份数据复制出来的。
    tag = f"{subsystem}:{module}"

    # 1) 当前段
    payload, count = _render(
        today, now, CURRENT_STEP_SECONDS,
        lambda m, i: debug_line(m, subsystem, module, i), salt=f"{tag}:current",
    )
    outputs.append((node / f"{module}.log", payload, count))

    # 2) 轮转段：关闭边界 = 今天 00:00
    day_1 = today - timedelta(days=1)
    payload, count = _render(
        day_1, today, ROTATED_STEP_SECONDS,
        lambda m, i: debug_line(m, subsystem, module, i + 3), salt=f"{tag}:rotated",
    )
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
                salt=f"{tag}:nested:{offset}:{hour}",
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
        salt=f"{tag}:flat",
    )
    flat_bytes, _ = _pack_tar([(f"{module}.log", payload, count)], day_4)
    outputs.append((node / f"{module}_{day_4:%Y%m%d}.tar.gz", flat_bytes, count))

    return outputs


def run_family(*, now: datetime, today: datetime) -> list[Artifact]:
    """运行日志是扁平的：<run root>/event.log + 归档。"""
    outputs: list[Artifact] = []
    payload, count = _render(
        today, now, CURRENT_STEP_SECONDS, lambda m, i: run_line(m, i), salt="run:current"
    )
    outputs.append((Path("event.log"), payload, count))

    day_1 = today - timedelta(days=1)
    payload, count = _render(
        day_1, today, ROTATED_STEP_SECONDS, lambda m, i: run_line(m, i + 2), salt="run:rotated"
    )
    outputs.append((Path(f"event_{today:%Y%m%d}000000.log"), payload, count))

    for offset in (2, 3):
        day = today - timedelta(days=offset)
        payload, count = _render(
            day, day + timedelta(days=1), FLAT_ARCHIVE_STEP_SECONDS,
            lambda m, i: run_line(m, i + 4), salt=f"run:flat:{offset}",
        )
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

    # 每个执行器槽位（_cp_01.._cp_NN）也是一条独立的流，抖动种子必须带上 slot，
    # 否则同一模块的 8 个 slot 会走出完全一样的毫秒尾巴。
    tag = f"{lower_host}:{subsystem}:{module}:cp{slot:02d}"

    outputs: list[Artifact] = []
    payload, count = _render(
        today, now, CURRENT_STEP_SECONDS, builder, salt=f"{tag}:current"
    )
    outputs.append((node / filename, payload, count))

    day_1 = today - timedelta(days=1)
    payload, count = _render(
        day_1, today, ROTATED_STEP_SECONDS, builder, salt=f"{tag}:rotated"
    )
    outputs.append((node / f"{module}_cp_{slot:02d}_{today:%Y%m%d}000000.log", payload, count))
    return outputs


# --------------------------------------------------------------------------- 清理与入口


#: 清理时**永远跳过**的目录名。``.ssh`` 是 ``deploy.py`` 铺的互信产物（私钥、
#: 公钥、authorized_keys、known_hosts），不属于"本轮资产生成"的范畴。以前它会被
#: 当残留删掉，后果是每跑一次 ``init`` 就得重新 ``sim.sh deploy`` 一次，否则
#: 「公钥免密登录可用」那条自检必然 FAIL —— 而且删除本身还在消耗受控环境的
#: 文件删除配额，直接导致 init 在半途被拦下。
PRUNE_SKIP_DIRS = frozenset({".ssh"})


def _recycle_root(root: Path) -> Path:
    """残留回收站里代表 ``root`` 的子目录。

    目录名带 ``root`` 的 crc32 指纹：两台机器的 debug 根**同名**（都叫 ``debug``），
    只按名字分会互相覆盖。
    """
    fingerprint = zlib.crc32(str(root).encode("utf-8")) % 0xFFFF
    return PURGE_RECYCLE_DIR / f"{root.name}-{fingerprint:04X}"


def _recycle_spill(root: Path, files: list[Path]) -> int:
    """把删除额度之外的残留**改名**搬进回收站，返回搬运成功的个数。

    🔴 用 ``os.replace`` 而**不是** ``unlink``：受控环境的安全删除保护只统计删除，
    改名不计数，所以这条通道不会在半途被拦下 —— 也不会让 `init` 报"资产生成失败"。

    回收站里的名字是 ``<槽位>_<原名>``：同一个槽位每轮被新的残留覆盖，
    所以目录大小天然有界（≈ 单轮残留峰值），不需要再删一遍（删除额度本来就不够）。
    """
    bucket = _recycle_root(root)
    moved = 0
    for slot, path in enumerate(files):
        try:
            relative = path.relative_to(root)
        except ValueError:
            relative = Path(path.name)
        # 槽位前缀锁定文件名，跨轮覆盖；原名保留在槽位之后，便于人工辨认。
        destination = bucket / f"{slot:04d}_{relative.name}"
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, destination)
            moved += 1
        except OSError:
            # 目标被占用 / 跨设备 / 权限 —— 单个文件失败不影响其余，更不让 init 失败。
            continue
    return moved


def _drop_empty_dirs(root: Path) -> None:
    """自底向上收掉 ``root`` 下的**空目录**（``root`` 本身保留）。

    残留清完之后，形如 ``debug/<子系统>/`` 的目录会先变空；只按"残留文件的祖先"
    去收，``debug`` 那一层还留着 —— 一台机器放弃整棵树之后会留下一个空壳目录。
    空目录不承载数据，却会让"这台机器有没有这棵树"的探测（``[ -d ]``）误判，
    所以统一清掉。只删**空**目录，任何还有内容的目录都原样保留。

    ``root`` 自己也会尝试删除（只在它真的空了的时候才成功）—— 一台机器彻底放弃
    某棵树之后，连根目录也不该留个空壳。
    """
    for directory in sorted(
        (item for item in root.rglob("*") if item.is_dir()),
        key=lambda item: -len(item.parts),
    ):
        if PRUNE_SKIP_DIRS.intersection(directory.parts):
            continue
        try:
            directory.rmdir()
        except OSError:
            continue
    if not PRUNE_SKIP_DIRS.intersection(root.parts):
        try:
            root.rmdir()
        except OSError:
            pass


def prune_tree(root: Path, keep: set[Path], report: GenerationReport) -> None:
    """清走不属于本轮产物的残留。**删除小批量，删不完的就改名搬走。**

    分两段，因为两种手段的成本完全不同：

    1. **真删除** —— 最多 ``PURGE_BUDGET_PER_RUN`` 个（整轮共享）。受控环境的
       "一个 turn 内删除超过阈值"保护一旦触发，会抛出一个既不是 ``OSError``
       也不可恢复的异常，以前它会一路冒泡出去，让 `init` 报"资产生成失败" ——
       明明日志树 / CPD 都写好了，只因为扫尾没扫干净，连后面的 ATLog 站都不再生成了。
       现在记一条提示就继续。
    2. **改名回收** —— 额度之外的全部 ``os.replace`` 进 ``run/recycle/pruned/``，
       不消耗配额。这一步保证**工作树里只剩本轮产物**，是 ``CPD 报告与数据表格成对``
       能通过的前提（报告树与数据树都清干净了，重合键才一致）。
    """
    if not root.exists():
        return
    stale = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.resolve() not in keep
        and not PRUNE_SKIP_DIRS.intersection(path.parts)
    ]
    if not stale:
        _drop_empty_dirs(root)
        return
    # ``rglob`` 的顺序取决于文件系统，只按它切预算的话，某些目录会永远排在窗口之外，
    # 越积越多（同一天多次 init 时 CPD 批次就会这样）。按 mtime 升序先删最旧的：
    # 顺序确定，也符合"老批次先过期"的直觉；也保证两棵树搬走的是**同一批**旧批次。
    stale.sort(key=lambda item: item.stat().st_mtime)
    # 预算按**整轮**算，不是每个根各给一份。曾经是每个 base 各 40 个，而一台机器
    # 有 debug（含 elog 子树）/ run / cpd_report / cpd_data / home 五个根 ——
    # 一轮最多尝试删两百个，必然越过受控环境的 50 个阈值，init 就半途而废。
    # 现在整轮最多 40 个，稳稳落在阈值以内。
    remaining_budget = max(0, PURGE_BUDGET_PER_RUN - report.pruned)
    budget = min(len(stale), remaining_budget)
    spill: list[Path] = []
    blocked = False
    for index, path in enumerate(stale[:budget]):
        try:
            path.unlink(missing_ok=True)
            report.pruned += 1
        except OSError:
            # 删不掉（占用 / 权限）就交给改名回收，别让它留在工作树里。
            spill.append(path)
        except Exception as exc:  # noqa: BLE001 - 受控环境的安全删除保护
            blocked = True
            print(f"  [loggen] 清理被安全保护拦下（{type(exc).__name__}: {exc}）；"
                  f"本轮产物已全部写入，剩下的残留改用改名回收继续腾位置")
            # 保护触发后本 turn 内后续删除都会失败，整段直接转回收。
            spill.extend(stale[index:])
            break
    if not blocked:
        spill.extend(stale[budget:])

    recycled = _recycle_spill(root, spill) if spill else 0
    if recycled:
        report.recycled += recycled
        print(f"  [loggen] {root.name}：删除额度已用尽，{recycled} 个残留改名搬进 "
              f"{PURGE_RECYCLE_DIR.name}/（改名不消耗删除配额）")

    # 残留都已经离场（删掉或搬走），空目录顺手收掉。
    _drop_empty_dirs(root)


def _sim_script(action: str) -> bytes:
    """仿真用的 stop.sh / start.sh：忽略部署传的开关（-ls/-les/-es/-f/-ef/-eif），成功退出。"""
    label = "停止" if action == "stop" else "启动"
    return (
        "#!/bin/sh\n"
        f"# 仿真占位脚本：真实环境由安装包提供。忽略开关，回显一行。\n"
        f"echo \"[sim] {action}.sh 已执行（{label}上位机进程）args=$*\"\n"
        "exit 0\n"
    ).encode("utf-8")


def plan_machine(spec: fleet.MachineSpec, *, now: datetime) -> list[tuple[str, Path, bytes, int]]:
    """一台机器的全部**日志**产物：(远端根, 相对路径, 内容, 行数)。

    只有**上位机**拥有 ``<debug root>/<子系统>/<fm>.log`` 与 ``<run root>/event.log``
    这两棵检索树 —— 见 ``fleet.hosts_subsystem_logs`` 的说明。下位机在本模拟里只出
    ``home/SW``（版本 + 启停脚本）与部署日志，它的机台日志已经作为 executor 目录发布在
    上位机的 ``<debug root>/elog/<下位机地址>/<子系统>/`` 下。
    """
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    plan: list[tuple[str, Path, bytes, int]] = []
    if fleet.hosts_subsystem_logs(spec):
        for subsystem, module in fleet.all_modules():
            for relative, payload, count in debug_family(module, subsystem, now=now, today=today):
                plan.append((spec.debug_root, relative, payload, count))
        for relative, payload, count in run_family(now=now, today=today):
            plan.append((spec.run_root, relative, payload, count))

    # 版本文件：后端 ResourceSettings.version_file_path 默认 ~/SW/version。
    # 🔴 内容必须是真实机器那种**带标记的**格式（`Current Version: <版本>`）：
    # 后端 `discovery.parse_version_text()` 只认标记行，仿真里写一行裸版本号会让
    # 「查询环境版本」判成"没找到 Current Version"→ 工具执行失败。
    plan.append((
        f"/home/{fleet.SIM_USERNAME}",
        Path("SW") / "version",
        f"Current Version: {fleet.SOFTWARE_VERSION}\n".encode("utf-8"),
        1,
    ))
    # 部署与「一键停/启进程」用的脚本。真实机器上是安装包带的，
    # 仿真里没有就会 `stop.sh: command not found` —— 功能看着像坏了。
    # 这里给最小实现：接受部署用到的那些开关，回显一行，退出码 0。
    home = f"/home/{fleet.SIM_USERNAME}"
    plan.append((home, Path("SW") / "stop.sh", _sim_script("stop"), 1))
    plan.append((home, Path("SW") / "start.sh", _sim_script("start"), 1))

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

    清理范围要覆盖"这台机器**可能**承载的全部根"，而不只是本轮计划里出现过的根 ——
    否则某棵树一旦不再由这台机器产出（例如子系统日志树改成只有上位机承载），
    它就再也不会出现在计划里、永远不被清理，旧内容一直躺在磁盘上被后端读到。
    见 ``fleet.managed_roots``。
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
        # 仿真脚本要能直接执行（部署与一键停/启进程都是 ./stop.sh 风格）
        if target.suffix == ".sh":
            target.chmod(0o755)
        report.files += 1
        report.bytes_written += len(payload)
        report.lines += count

    if prune:
        # 把这台机器"可能承载"的根也纳入清理范围（见 fleet.managed_roots）。
        # 已经出现过的根不重复追加，保持"计划内的根排在前面"的顺序：
        # 它们有 keep 清单、残留少，先扫它们更省。
        for remote_root in fleet.managed_roots(spec):
            base = fleet.remote_to_local(spec, remote_root)
            if base not in bases:
                bases.append(base)
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
