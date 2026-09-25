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
# 上位机与下位机是**两台机器**，所以各要一个**互不相同、且真的能连上**的地址：
#
#   真实部署里两台机器本来就是两个 IP，「互信」（SSH 免密）说的正是 A ⇄ B 这
#   两个地址之间的事。如果两台模拟机共用一个 IP、只靠端口区分，语义上就变成
#   "自己跟自己互信"，既不像真机台，也验证不了公钥分发是否真的跨主机生效。
#
# 地址来源按优先级：
#   1. 环境变量 ``SIM_UPPER_HOST`` / ``SIM_LOWER_HOST``（要对齐真实机台时用）
#   2. lo0 上已存在的别名地址（``127.0.0.2`` / ``127.0.0.3``）
#      一次性准备（需要管理员权限，本机回环网段只预置了 127.0.0.1）：
#          sudo ifconfig lo0 alias 127.0.0.2 up
#          sudo ifconfig lo0 alias 127.0.0.3 up
#   3. 兜底：局域网 IP 与回环各占一个 —— 这两个地址本机一定有，无需任何准备
#
# 每台机器都额外绑 ``127.0.0.1``，这样网卡状态变化不会把本机健康检查一起搞挂。
# ---------------------------------------------------------------------------

#: 环境变量名（可强制指定某一台机器的地址）
UPPER_HOST_ENV = "SIM_UPPER_HOST"
LOWER_HOST_ENV = "SIM_LOWER_HOST"
#: 旧名兼容：单机地址时代只有一个 MACHINE_HOST，现在它等价于上位机地址
LEGACY_HOST_ENV = "SIM_MACHINE_HOST"


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


def _host_pool() -> list[str]:
    """按优先级排好的候选地址池（去重）。

    lo0 别名排最前 —— 它们是"干净"的模拟机地址，不掺 VPN / 无线网段，
    也不会跟真实设备抢地址；其次是内网网卡地址。

    **刻意不含裸 ``127.0.0.1``**：它作为"宣告地址"写进数据库后，跟网卡地址
    不是一个值，排查链路时会把"服务没起来"和"地址选错了"混在一起。
    只有本机一个可用地址都没有时（网卡全关、也没加别名）才退回它。
    """
    addresses = _local_ipv4_addresses()
    aliases = [ip for ip in addresses if ip.startswith("127.") and ip != "127.0.0.1"]
    others = [
        ip for ip in addresses if not ip.startswith("127.") and _address_rank(ip)[0] <= 2
    ]
    ordered = sorted(aliases) + sorted(others, key=_address_rank)
    if not ordered:
        ordered = ["127.0.0.1"]
    return list(dict.fromkeys(ordered))


def resolve_machine_hosts() -> tuple[str, str]:
    """给上下位机各挑一个地址，返回 ``(上位机, 下位机)``。

    显式给了环境变量就照用；没给的按 :func:`_host_pool()` 的顺序补，
    并且**跳过已经被另一个占用的地址** —— 两台机器拿到同一个地址会让
    "两个 IP 的互信"退化回自连自，失去意义。
    """
    picks = [
        os.environ.get(UPPER_HOST_ENV, "").strip(),
        os.environ.get(LOWER_HOST_ENV, "").strip(),
    ]
    legacy = os.environ.get(LEGACY_HOST_ENV, "").strip()
    if legacy and not picks[0]:
        # 老写法只给一个地址：上位机用它，下位机自动挑一个不同的
        picks[0] = legacy

    pool = _host_pool()
    taken = {item for item in picks if item}
    # 候选顺序 = 候选池顺序：**回环别名在最前**（见 _host_pool），其次是网卡地址。
    #
    # 于是两种情形都对：
    #   * 加过别名（127.0.0.2 / 127.0.0.3）→ 两台各拿一个别名，真正是两个 IP；
    #   * 没加别名 → 候选池只有网卡地址，两台共用它、靠端口 2222/2223 区分。
    #
    # 不回退到 127.0.0.1：它作为"宣告地址"写进数据库后，后端按它拼出的地址与
    # 网卡地址不是同一个，链路排查时极易被误判成服务没起来。宁可用端口区分。
    spare = [ip for ip in pool if ip not in taken]
    for index, item in enumerate(picks):
        if item:
            continue
        if spare:
            picks[index] = spare.pop(0)
        else:
            # 只有一个可用地址时，两台机器共用它（靠端口区分）
            picks[index] = picks[0] or "127.0.0.1"
    return picks[0], picks[1]


def loopback_aliases() -> list[str]:
    """lo0 上已有的额外别名地址（用户执行过 ``ifconfig lo0 alias …`` 才有）。"""
    return [
        ip
        for ip in _local_ipv4_addresses()
        if ip.startswith("127.") and ip != "127.0.0.1"
    ]


#: 上位机 / 下位机各自宣告的地址（两个不同的 IP）。
UPPER_HOST, LOWER_HOST = resolve_machine_hosts()

#: 兼容旧名：单机地址时代两台机器共用本机 IP，``MACHINE_HOST`` 就是它。
MACHINE_HOST = UPPER_HOST


def bind_hosts() -> tuple[str, ...]:
    """**一个服务监听多个地址**时用的集合（报告站这种单端口服务）。

    机群不走这里：每台机器只该绑自己的地址，见 :func:`bind_hosts_for` ——
    否则下位机也会去绑上位机的地址，地址一多就分不清谁是谁。
    """
    hosts = [UPPER_HOST, LOWER_HOST, "127.0.0.1"]
    return tuple(dict.fromkeys(item for item in hosts if item))


def bind_hosts_for(spec: MachineSpec) -> tuple[str, ...]:
    """一台模拟机自己的绑定地址：宣告地址 + 回环兜底。"""
    return tuple(dict.fromkeys([spec.host, "127.0.0.1"]))


def public_host() -> str:
    """对外展示的地址（用户会照着它 ssh、粘贴报告站 URL）。

    局域网 IP 优先：回环地址给不了别的设备，只用在本机自测时有意义。
    """
    addresses = _local_ipv4_addresses()
    lan = [
        ip for ip in addresses if not ip.startswith("127.") and _address_rank(ip)[0] <= 2
    ]
    if lan:
        return min(lan, key=_address_rank)
    return UPPER_HOST


def detect_machine_host() -> str:
    """兼容旧调用：返回上位机地址。新代码请直接用 ``UPPER.host`` / ``LOWER1.host``。"""
    return UPPER_HOST


def loopback_alias_hint() -> str:
    """两台机器没能拿到两个不同地址时，给用户的一条可选增强命令。"""
    return (
        "sudo ifconfig lo0 alias 127.0.0.2 up && "
        "sudo ifconfig lo0 alias 127.0.0.3 up"
    )


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
    # wsp = 工件台点位子系统。它的 fm 日志专门记**绝对移动点位**
    # （``move absolute { x:…, y:… }``），内容规则见 loggen.WSP_MOVE_POINTS。
    "wsp": ("wsp",),
    "mecore": ("mecore", "cpcore", "metrl"),
    "cpfr": ("cpfr", "frhyd"),
    "sil": ("sil", "silrt"),
    "hmi": ("hmi", "hmidsp"),
    "swlib": ("swlib", "adflib"),
}

#: 有**专属日志内容规则**的子系统：它们的 fm 日志不走通用扫片调用链程序
#: （``loggen._DEBUG_PROGRAM``），而是用各自的程序。见 ``loggen.program_for``。
CONTENT_SPECIFIC_SUBSYSTEMS: tuple[str, ...] = ("wsp",)

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
    def log_root(self) -> str:
        return LOG_ROOT.format(username=SIM_USERNAME)

    @property
    def debug_root(self) -> str:
        return DEBUG_ROOT.format(username=SIM_USERNAME)

    @property
    def deploy_root(self) -> str:
        """部署日志目录（与 device 日志分开，避免被 run 目录的归档规则误认）。"""
        return f"{self.log_root}/deploy"

    @property
    def ssh_dir(self) -> str:
        return f"{self.home}/.ssh"

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
    host=UPPER_HOST,
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
    host=LOWER_HOST,
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

    两台机器各有独立地址，所以 host 是唯一的；要按 ``host:port`` 精确定位
    请用 :func:`machine_by_endpoint`。
    """
    text = str(host or "").strip().lower()
    for item in FLEET:
        if item.host.lower() == text or item.name.lower() == text:
            return item
    return None


def peer_of(spec: MachineSpec) -> MachineSpec:
    """另一台机器 —— 部署互信时它就是对端。"""
    for item in FLEET:
        if item.key != spec.key:
            return item
    raise KeyError(f"机群里只有一台机器，找不到 {spec.key} 的对端")


def hosts_are_distinct() -> bool:
    """两台机器是否拿到了**两个不同的地址**（互信模拟的前提）。"""
    return len({spec.host for spec in FLEET}) == len(FLEET)


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
