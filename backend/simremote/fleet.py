"""模拟上下位机机群的静态定义。

这里集中放"远端"的拓扑、账号、日志路径与目录布局，供 loggen / sshd / seed
共享，避免同一个事实在多处各写一份。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# 本地模拟文件系统的根。每台机器一个子目录，充当这台机器的 "/"。
LOCAL_ROOT = Path(__file__).resolve().parent / "remote_fs"

SIM_USERNAME = "tracepilot"
SIM_PASSWORD = "tracelens"
ENVIRONMENT_NAME = "SIM-EUV-01"
SOFTWARE_VERSION = "SPM-V2026.09.23"
STATION_USER_ID = "1"

# 远端日志根模板。后端 ResourceSettings 默认模板是 "/log/{username}/debug"。
LOG_ROOT = "/log/{username}"
DEBUG_ROOT = LOG_ROOT + "/debug"
ELOG_ROOT = DEBUG_ROOT + "/elog"
RUN_ROOT = LOG_ROOT + "/run"

# ---------------------------------------------------------------------------
# 目录布局
#
# 后端按 <root>/<subsystem>/<fm>.log 的层级发现「子系统」与「fm 模块」，
# 并且要求这两级名字里 **不含数字和下划线**（_is_discovery_catalog_name_allowed），
# 否则会被当成备份/快照目录忽略。命名时务必遵守。
# ---------------------------------------------------------------------------
SUBSYSTEM_MODULES: dict[str, tuple[str, ...]] = {
    "spwsp": ("spwsp", "lgsw", "wtrm"),
    "mecore": ("mecore", "cpcore", "metrl"),
    "cpfr": ("cpfr", "frhyd"),
    "sil": ("sil", "silrt"),
    "hmi": ("hmi", "hmidsp"),
    "swlib": ("swlib", "adflib"),
}

# executor 树：<elog root>/<lower_host>/<subsystem>/<executor>_cp_<nn>.log
# 后端用 r"^(?P<module>.+?)_cp(?:_|$)" 提取模块名。
EXECUTOR_MODULES: tuple[str, ...] = ("cpdisp", "cpfeed", "cptherm")
EXECUTOR_PER_MODULE = 2

# run 日志是扁平的：<run root>/event.log
RUN_MODULE = "event"

# ---------------------------------------------------------------------------
# CPD 测校资产（走后端 SFTP/SSH 侧）
#
# 后端 apps/reports 有两条独立的远端根：
#   报告  _REPORT_ROOT = /data/{username}/report/cpd_report/<子系统>/<模块>/<名字>.rpt
#   数据  data_root()  = /data/{username}/cpd_data/<子系统.lower()>/<模块.lower()>/*.xlsx
# 注意数据根会把子系统/模块名 **转小写**，所以两棵树都必须用小写目录名，
# 否则前端「从报告行打开测校数据表格」会落进一个空目录。
# ---------------------------------------------------------------------------
DATA_ROOT = "/data/{username}"
CPD_REPORT_ROOT = DATA_ROOT + "/report/cpd_report"
CPD_DATA_ROOT = DATA_ROOT + "/cpd_data"

# 测校只覆盖部分子系统；报告与数据表格成对生成
CPD_MODULES: dict[str, tuple[str, ...]] = {
    "spwsp": ("spwsp", "lgsw"),
    "mecore": ("mecore", "cpcore"),
    "cpfr": ("cpfr",),
    "sil": ("sil",),
}
CPD_REPORTS_PER_MODULE = 4
CPD_SAMPLE_STEP_SECONDS = 15

# ---------------------------------------------------------------------------
# ATLog 用例报告站（走后端 HTTP 侧）
#
# 模拟发布 ATLog 用例的 nginx 报告站。后端 apps/atlog 用 urllib 抓取，
# 且 normalize_base_url 要求 host 是内网/回环地址，所以绑定 127.0.0.1。
# ---------------------------------------------------------------------------
ATLOG_SITE_HOST = "127.0.0.1"
ATLOG_SITE_PORT = 8901
ATLOG_TASK_NAME = "task_L4SIM_20260923"
ATLOG_BLOCK_NAME = "block_SIM_GPB_01"


@dataclass(frozen=True)
class MachineSpec:
    key: str
    name: str
    host: str
    ssh_port: int
    role: str  # upper / lower
    station_id: str
    station_type: str
    station_name: str
    description: str = ""

    @property
    def debug_root(self) -> str:
        return DEBUG_ROOT.format(username=SIM_USERNAME)

    @property
    def elog_root(self) -> str:
        return ELOG_ROOT.format(username=SIM_USERNAME)

    @property
    def run_root(self) -> str:
        return RUN_ROOT.format(username=SIM_USERNAME)

    @property
    def home(self) -> str:
        return f"/home/{SIM_USERNAME}"

    @property
    def cpd_report_root(self) -> str:
        return CPD_REPORT_ROOT.format(username=SIM_USERNAME)

    @property
    def cpd_data_root(self) -> str:
        return CPD_DATA_ROOT.format(username=SIM_USERNAME)


UPPER = MachineSpec(
    key="upper",
    name="SIM-SCH-01",
    host="sim-upper.localhost",
    ssh_port=2222,
    role="upper",
    station_id="1",
    station_type="SCH",
    station_name="SCH",
    description="模拟上位机（调度机 SCH）",
)

LOWER1 = MachineSpec(
    key="lower1",
    name="SIM-LCH1-01",
    host="sim-lower1.localhost",
    ssh_port=2223,
    role="lower",
    station_id="2",
    station_type="LCH1",
    station_name="LCH1",
    description="模拟下位机 1（光刻控制机 LCH1）",
)

FLEET: tuple[MachineSpec, ...] = (UPPER, LOWER1)


def machine_by_key(key: str) -> MachineSpec:
    for item in FLEET:
        if item.key == key:
            return item
    raise KeyError(f"未知机器：{key}")


def machine_by_host(host: str) -> MachineSpec | None:
    text = str(host or "").strip().lower()
    for item in FLEET:
        if item.host.lower() == text or item.name.lower() == text:
            return item
    return None


def remote_to_local(machine: MachineSpec, remote_path: str) -> Path:
    """把远端绝对路径映射成本地模拟文件系统里的路径。"""
    text = str(remote_path or "").strip()
    if not text:
        return LOCAL_ROOT / machine.key
    relative = text.lstrip("/")
    if relative.startswith("~/"):
        relative = relative[2:]
    return LOCAL_ROOT / machine.key / relative


def local_root_of(machine: MachineSpec) -> Path:
    return LOCAL_ROOT / machine.key


def all_modules() -> list[tuple[str, str]]:
    """返回 [(subsystem, module), ...]。"""
    return [
        (subsystem, module)
        for subsystem, modules in SUBSYSTEM_MODULES.items()
        for module in modules
    ]


def executor_count() -> int:
    return len(EXECUTOR_MODULES) * EXECUTOR_PER_MODULE


def all_cpd_modules() -> list[tuple[str, str]]:
    """返回测校覆盖的 [(子系统, 模块), ...]。"""
    return [
        (subsystem, module)
        for subsystem, modules in CPD_MODULES.items()
        for module in modules
    ]
