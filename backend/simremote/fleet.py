"""模拟上下位机机群的静态定义。

这里集中放"远端"的拓扑、账号、日志路径与目录布局，供 loggen / sshd / seed
共享，避免同一个事实在多处各写一份。
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

# 本地模拟文件系统的根。每台机器一个子目录，充当这台机器的 "/"。
LOCAL_ROOT = Path(__file__).resolve().parent / "remote_fs"

SIM_USERNAME = "tracepilot"
SIM_PASSWORD = "tracelens"
ENVIRONMENT_NAME = "SIM-EUV-01"
SOFTWARE_VERSION = "SPM-V2026.09.23"
STATION_USER_ID = "1"


# ---------------------------------------------------------------------------
# 机器地址
#
# 模拟机对外的 host 必须是**真的能连上的地址**，不能是 "sim-upper.localhost"
# 这种只在字面上像主机的字符串 —— 后端是拿这个值直接开 SSH 的，用户也会照着它
# 手工 ssh 上去复核。所以这里取本机的局域网出口 IP（就是真实网卡地址），
# 并且监听时同时绑这个地址和回环：
#
#   网络 IP:2222   给"另一台机器/另一台设备"访问，路径与真实机台一致
#   127.0.0.1:2222 本机探测（sim.sh 健康检查、selftest）走这条，不依赖网卡状态
#
# 需要固定成某个地址（例如要跟真实机台对齐）时用环境变量覆盖：
#   SIM_MACHINE_HOST=10.20.30.40 scripts/sim.sh restart fleet
# ---------------------------------------------------------------------------


def _local_ipv4_addresses() -> list[str]:
    """枚举本机 IPv4 地址（含回环）。取不到就退回 UDP 出口探测。"""
    found: list[str] = []
    try:
        output = subprocess.run(
            ["ifconfig"], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        output = ""
    for ip in re.findall(r"\binet (?:addr:)?(\d{1,3}(?:\.\d{1,3}){3})", output):
        if ip not in found:
            found.append(ip)
    if not found:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # 连外部地址只为让内核选出出口网卡，UDP 不会真的发包。
            probe.connect(("8.8.8.8", 53))
            found.append(probe.getsockname()[0])
        except OSError:
            pass
        finally:
            probe.close()
    return found


def _address_rank(ip: str) -> tuple[int, str]:
    """越小越优先。

    刻意把**内网段**排在前面：公网/VPN 地址（例如全隧道 VPN 的 utun 地址）
    虽然"能通"，但 ``apps.atlog`` 的 _is_private_host 会把它当公网拒掉，
    拿它当模拟机地址会让报告站 URL 直接失效。
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return (9, ip)
    if addr.is_loopback:
        return (3, ip)
    if not addr.is_private:
        return (4, ip)
    if ip.startswith("192.168."):
        return (0, ip)
    if ip.startswith("10."):
        return (1, ip)
    return (2, ip)


def detect_machine_host() -> str:
    """本机局域网 IP；优先级 192.168 → 10 → 其它内网 → 回环。"""
    override = os.environ.get("SIM_MACHINE_HOST", "").strip()
    if override:
        return override
    candidates = _local_ipv4_addresses()
    if not candidates:
        return "127.0.0.1"
    return min(candidates, key=_address_rank)


#: 模拟机对外宣告的地址（两台机器共用本机 IP，靠端口区分）。
MACHINE_HOST = detect_machine_host()


def bind_hosts() -> tuple[str, ...]:
    """机群/报告站实际绑定的地址：网络 IP 优先，回环兜底（去重）。"""
    hosts = [MACHINE_HOST, "127.0.0.1"]
    return tuple(dict.fromkeys(host for host in hosts if host))


def public_host() -> str:
    """对外展示用的地址（与 MACHINE_HOST 一致；回环时也照实显示）。"""
    return MACHINE_HOST


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
# 且 normalize_base_url 要求 host 是内网/回环地址。ATLOG_SITE_HOST 只用来
# **探测 / 本机抓取**（回环最稳，不受网卡状态影响）；对外展示、给用户粘贴的
# URL 走 ``public_host()``（局域网 IP），监听时两个地址都绑。
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
    host=MACHINE_HOST,
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
    host=MACHINE_HOST,
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
    """按 host 或机器名找机器。

    两台机器现在共用本机 IP（靠端口区分），所以只给 host 时返回第一台；
    要精确匹配请用 :func:`machine_by_endpoint`。
    """
    text = str(host or "").strip().lower()
    for item in FLEET:
        if item.host.lower() == text or item.name.lower() == text:
            return item
    return None


def machine_by_endpoint(host: str, port: int | str | None = None) -> MachineSpec | None:
    """按 host:port 精确定位机器（同 IP 多端口时不会认错）。"""
    text = str(host or "").strip().lower()
    try:
        number = int(port) if port not in (None, "") else None
    except (TypeError, ValueError):
        number = None
    for item in FLEET:
        if item.host.lower() != text:
            continue
        if number is None or item.ssh_port == number:
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
