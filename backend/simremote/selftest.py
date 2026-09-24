"""模拟机群端到端自检。

覆盖「后端真实会发的命令」：SFTP 只读访问、find（含 -printf NUL 协议）、
tar 列表与嵌套解压管道、awk 时间窗过滤、长驻 tail 转发，以及日志行是否
匹配后端的三套内置正则。

用法：``python -m simremote.cli selftest``（需要机群已在运行）
"""

from __future__ import annotations

import fnmatch
import os
import re
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import paramiko

from . import atlog_site, fleet, livesim

_RESULTS: list[tuple[str, bool, str]] = []

# 调用链规则的两个方向符。前端 ``logParser.ts`` 用的是同一条正则
# （``/>\s*\(\s*\)/`` 入口、``/<\s*\(\s*\)/`` 出口）。
ENTRY_MARKER_REGEX = re.compile(r">\s*\(\s*\)")
EXIT_MARKER_REGEX = re.compile(r"<\s*\(\s*\)")
#: 与前端 ``logParser.ts::parseBoundaryFunctionName`` 同一条规则：取方向符之前
#: 最后一个 ``[函数名]``。名字必须以字母/下划线开头，所以 ``[2026-09-24 ...]``
#: 这类时间戳方括号不会被误认成函数名。
BOUNDARY_NAME_REGEX = re.compile(r"\[([A-Za-z_~][\w:<>~.\-]*)]")


def _record(name: str, ok: bool, detail: str = "") -> bool:
    _RESULTS.append((name, bool(ok), detail))
    return bool(ok)


def _connect(spec: fleet.MachineSpec) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        spec.host,
        port=spec.ssh_port,
        username=fleet.SIM_USERNAME,
        password=fleet.SIM_PASSWORD,
        timeout=6,
        allow_agent=False,
        look_for_keys=False,
    )
    return client


def _exec(client: paramiko.SSHClient, command: str, timeout: int = 30) -> tuple[bytes, bytes, int]:
    _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read()
    err = stderr.read()
    return out, err, stdout.channel.recv_exit_status()


# --------------------------------------------------------------------------- 各项检查


def check_ports() -> None:
    for spec in fleet.FLEET:
        try:
            client = _connect(spec)
            client.close()
            _record(f"SSH 可达 {spec.host}:{spec.ssh_port}", True)
        except Exception as exc:  # noqa: BLE001
            _record(f"SSH 可达 {spec.host}:{spec.ssh_port}", False, str(exc))


def check_auth_reject() -> None:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            fleet.UPPER.host, port=fleet.UPPER.ssh_port, username="intruder",
            password="nope", timeout=5, allow_agent=False, look_for_keys=False,
        )
        client.close()
        _record("错误口令被拒绝", False, "竟然登录成功了")
    except paramiko.AuthenticationException:
        _record("错误口令被拒绝", True)
    except Exception as exc:  # noqa: BLE001
        _record("错误口令被拒绝", False, str(exc))


def check_sftp_readonly(client: paramiko.SSHClient) -> None:
    sftp = client.open_sftp()
    try:
        entries = sftp.listdir_attr(f"{fleet.UPPER.debug_root}")
        names = sorted(item.filename for item in entries)
        _record("SFTP 列出调试根目录", len(names) >= 5, f"{len(names)} 项：{names[:4]}")

        module_dir = f"{fleet.UPPER.debug_root}/spwsp"
        files = sorted(item.filename for item in sftp.listdir_attr(module_dir))
        _record(
            "SFTP 列出子系统目录",
            any(item.endswith(".log") for item in files),
            f"{len(files)} 项",
        )

        target = f"{module_dir}/spwsp.log"
        info = sftp.stat(target)
        _record("SFTP stat 返回真实大小", info.st_size > 0, f"size={info.st_size}")

        with sftp.open(target, "rb") as handle:
            head = handle.read(64)
            handle.seek(0)
            first = handle.readline()
        _record(
            "SFTP 随机读 + readline",
            head.startswith(b"[") and first.startswith(b"["),
            first.decode("utf-8", "replace")[:48],
        )

        try:
            sftp.open(f"{module_dir}/readonly-probe.log", "w")
            _record("SFTP 拒绝写操作", False, "竟然允许写入")
        except OSError:
            _record("SFTP 拒绝写操作", True)
    finally:
        sftp.close()


def check_find(client: paramiko.SSHClient) -> None:
    out, _err, code = _exec(
        client,
        r"""find /log/tracepilot/debug -mindepth 2 -maxdepth 2 -type f \( -name '*.log' -o -name '*.tar.gz' \) -print""",
    )
    lines = [item for item in out.decode().splitlines() if item]
    # 实时日志源会在同一目录里留下 <fm>_<时刻>.log 这种滚动归档（真实机台也一样），
    # 所以这里校验的是"14 个模块都被发现、且文件名都是合法形态"，而不是总数恰好相等。
    modules = {module for _subsystem, module in fleet.all_modules()}
    artifact_re = re.compile(r"^(?P<module>[a-z0-9]+)(?:_\d{8}|_\d{14})?\.(?:log|tar\.gz)$")
    parsed: list[tuple[str, str]] = []
    illegal: list[str] = []
    for item in lines:
        name = Path(item).name
        match = artifact_re.match(name)
        if match and match.group("module") in modules:
            parsed.append((item, match.group("module")))
        else:
            illegal.append(item)
    covered = {module for _item, module in parsed}
    _record(
        "find 模块发现（-print）",
        code == 0 and not illegal and covered == modules,
        f"{len(lines)} 行 · 覆盖 {len(covered)}/{len(modules)} 个模块"
        + (f" · 非法文件名 {illegal[:3]}" if illegal else ""),
    )
    archive_counts: dict[str, int] = {}
    for item, module in parsed:
        if item.endswith(".tar.gz"):
            archive_counts[module] = archive_counts.get(module, 0) + 1
    thin = sorted(module for module in modules if archive_counts.get(module, 0) < 3)
    _record(
        "find 发现每日归档包",
        not thin,
        f"每模块归档数最少 {min(archive_counts.values(), default=0)}（应 ≥3）"
        + (f" · 不足 {thin[:4]}" if thin else ""),
    )
    _record(
        "find 输出为远端路径",
        all(item.startswith("/log/") for item in lines),
        lines[0] if lines else "(空)",
    )

    out, _err, code = _exec(
        client,
        r"""find /log/tracepilot/debug/spwsp -maxdepth 1 -type f \( -iname 'spwsp.log' -o -iname 'spwsp_*.log' \) -printf '%i\t%s\t%T@\t%p\0'""",
    )
    records = [item for item in out.split(b"\0") if item]
    shape_ok = bool(records)
    for record in records:
        parts = record.decode("utf-8", "replace").split("\t")
        if len(parts) != 4:
            shape_ok = False
            break
        try:
            int(parts[0])
            int(parts[1])
            float(parts[2])
        except ValueError:
            shape_ok = False
            break
    _record(
        "find -printf NUL 协议",
        code == 0 and shape_ok,
        f"{len(records)} 条记录，字段 %i/%s/%T@/%p",
    )


def check_tar(client: paramiko.SSHClient) -> None:
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    nested = today - timedelta(days=2)
    archive = f"{fleet.UPPER.debug_root}/spwsp/spwsp_{nested:%Y%m%d}.tar.gz"

    out, err, code = _exec(client, f"tar -tzf {archive}")
    members = [item for item in out.decode().splitlines() if item]
    _record("tar -tzf 列出嵌套日包", code == 0 and len(members) == 24, f"{len(members)} 个成员（期望 24）")

    inner = members[0] if members else ""
    _record(
        "内层包名 = 小时段结束时刻",
        bool(inner) and inner.endswith(".tar.gz") and inner.startswith("spwsp_"),
        inner,
    )

    out, err, code = _exec(client, f"tar -xOzf {archive} {inner} | tar -tzf -")
    _record("嵌套管道逐级解压", code == 0 and out.decode().strip() == "spwsp.log", out.decode().strip()[:40])

    out, _err, code = _exec(client, f"tar -xOzf {archive} {inner} | tar -xOzf - spwsp.log | head -1")
    _record("解出内层成员正文", code == 0 and out.startswith(b"["), out.decode("utf-8", "replace")[:56])


def check_close_boundary() -> None:
    """轮转文件名后缀必须是「关闭边界」：今天 0 点关闭的文件覆盖昨天。"""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    rotated = f"{fleet.UPPER.debug_root}/spwsp/spwsp_{today:%Y%m%d}000000.log"
    local = fleet.remote_to_local(fleet.UPPER, rotated)
    if not local.exists():
        _record("轮转文件按关闭边界命名", False, f"缺少 {rotated}")
        return
    with local.open(encoding="utf-8") as handle:
        stamps = [line[1:20] for line in handle if line.startswith("[")]
    first, last = stamps[0], stamps[-1]
    yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    _record(
        "轮转文件按关闭边界命名",
        first.startswith(yesterday) and last.startswith(yesterday),
        f"{first} → {last}（应落在 {yesterday}）",
    )


def check_awk_window(client: paramiko.SSHClient) -> None:
    now = datetime.now()
    start = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    end = now.strftime("%Y-%m-%d %H:%M:%S")
    command = (
        f"tar -xOzf {fleet.UPPER.debug_root}/spwsp/spwsp_20260919.tar.gz spwsp.log | "
        f"LC_ALL=C awk -v s='{start}' -v e='{end}' "
        """'{ if (substr($0,1,1) != "[") next; ts=substr($0,2,19); if (ts < s) next; if (ts > e) exit; print $0 }'"""
    )
    out, err, code = _exec(client, command)
    _record("awk 窗口过滤：窗口外返回 0 行", code == 0 and out.strip() == b"", f"{len(out.splitlines())} 行")

    current = f"{fleet.UPPER.debug_root}/spwsp/spwsp.log"
    command = f"cat -- {current} | head -3"
    out, err, code = _exec(client, command)
    _record("cat 读取当前日志", code == 0 and out.count(b"\n") == 3, f"{out.count(b'\n')} 行")

    window_start = (now - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    # 实时日志源的活动文件写满 1000 行就轮转，刚轮转过的那一小会儿当前段只有
    # 几行 —— 只查当前段会误报"窗口没命中"。真实查询路径本来就是
    # 「当前段 + 覆盖窗口的归档」一起读，这里跟着一起读。
    command = (
        "{ f=$(ls -1t "
        f"{fleet.UPPER.debug_root}/spwsp/spwsp_*.log 2>/dev/null | head -1); "
        f'[ -n "$f" ] && cat -- "$f"; cat -- {current}; }} | '
        f"LC_ALL=C awk -v s='{window_start}' -v e='{end}' "
        """'{ if (substr($0,1,1) != "[") next; ts=substr($0,2,19); if (ts < s) next; if (ts > e) exit; print $0 }'"""
    )
    out, _err, code = _exec(client, command)
    _record(
        "awk 窗口过滤：最近 30 分钟命中",
        code == 0 and len(out.splitlines()) >= 20,
        f"{len(out.splitlines())} 行（当前段 spwsp.log + 最新归档）",
    )


def check_version_file(client: paramiko.SSHClient) -> None:
    out, err, code = _exec(client, "cat -- /home/tracepilot/SW/version")
    _record("cat 版本文件", code == 0 and out.decode().strip() == fleet.SOFTWARE_VERSION, out.decode().strip())

    out, _err, code = _exec(client, """printf '%s\n' "$(hostname)"; uname -srm""")
    _record("连接探测命令", code == 0 and b"\n" in out, out.decode().strip().replace("\n", " | ")[:48])


def check_line_formats() -> None:
    from apps.logsources.services.log_format_parser import (
        BUILTIN_RULE_DEFAULTS,
        _compile,
    )

    rules = {
        "debug": BUILTIN_RULE_DEFAULTS[("debug", "标准调试日志")],
        "run": BUILTIN_RULE_DEFAULTS[("run", "运行事件日志")],
    }
    for category, relative in (("debug", "spwsp/spwsp.log"), ("run", "event.log")):
        root = fleet.UPPER.debug_root if category == "debug" else fleet.UPPER.run_root
        local = fleet.remote_to_local(fleet.UPPER, f"{root}/{relative}")
        pattern = _compile(rules[category]["pattern"])
        matched = 0
        total = 0
        with local.open(encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if index >= 200:
                    break
                total += 1
                if pattern.match(line.rstrip("\n")):
                    matched += 1
        _record(f"{category} 日志行匹配内置正则", total > 0 and matched == total, f"{matched}/{total}")

    executor_root = fleet.UPPER.elog_root
    executor_files = sorted(
        Path(fleet.remote_to_local(fleet.UPPER, executor_root)).glob(f"{fleet.LOWER1.host}/*/*.log")
    )
    if not executor_files:
        _record("executor 日志行匹配内置正则", False, "未找到 executor 文件")
        return
    pattern = _compile(BUILTIN_RULE_DEFAULTS[("executor", "标准执行器日志")]["pattern"])
    matched = total = 0
    with executor_files[0].open(encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            if index >= 200:
                break
            total += 1
            if pattern.match(line.rstrip("\n")):
                matched += 1
    _record("executor 日志行匹配内置正则", total > 0 and matched == total, f"{matched}/{total}")


def _stream_pid() -> int | None:
    pid_file = Path(__file__).resolve().parent / "run" / "stream.pid"
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _stream_running() -> bool:
    pid = _stream_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def check_live_tail() -> None:
    """实时监听：订阅点之后新产出的行应经 SSH ``tail -F`` 通道推过来。

    实时日志源在跑时直接用它产出的行验证，既不写探针也不回滚 —— 并发写入下把
    文件截断回原长度，会把日志源这期间写的行一起削掉，还会让远端 tail 以为文件
    被截断而重读。日志源没跑时才退回"自己写一行探针、事后回滚"的老办法。
    """
    from . import livesim

    probe = livesim.TARGETS[0]
    target = probe.local_path
    running = _stream_running()
    original_size = target.stat().st_size
    marker = f"selftest probe {time.time():.3f}"
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    channel = None
    try:
        client.connect(
            fleet.UPPER.host, port=fleet.UPPER.ssh_port, username=fleet.SIM_USERNAME,
            password=fleet.SIM_PASSWORD, timeout=6, allow_agent=False, look_for_keys=False,
        )
        transport = client.get_transport()
        assert transport is not None
        channel = transport.open_session()
        channel.exec_command(f"LC_ALL=C exec tail -n 0 -F -- {probe.remote_path}")
        time.sleep(0.6)
        if not running:
            line = (
                f"[{datetime.now():%Y-%m-%d %H:%M:%S}.000] [INFO] [SPWSP] [1] [2] [spwsp] "
                f"[normal] [spwsp:selftest:1] {marker}\n"
            )
            with target.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()

        received = b""
        # 窗口要按**产出速率**算，不能按 tail 的响应速度算：远端 ``tail -F`` 的 stdout
        # 是管道而不是 tty，stdio 走全缓冲，一行约 150B，要攒满 ~4KB（≈27 行）才会
        # flush 一次。日志源 0.5s/行时第一行要等 ~14s 才冒出来，窗口开太短会隔几次
        # 就假失败一次 —— 真实机台也是这个行为，不是模拟器的问题。
        deadline = time.time() + (28 if running else 5)
        while time.time() < deadline:
            if channel.recv_ready():
                received += channel.recv(65536)
                if not running and marker.encode() in received:
                    break
                if running and received.count(b"\n") >= 1:
                    break
            time.sleep(0.1)
        if running:
            forwarded = [item for item in received.decode("utf-8", "replace").splitlines() if item.strip()]
            _record("实时 tail 转发新增行", bool(forwarded), f"由实时日志源产出 {len(forwarded)} 行")
        else:
            _record("实时 tail 转发新增行", marker.encode() in received, f"探针模式，收到 {len(received)} 字节")
    except Exception as exc:  # noqa: BLE001
        _record("实时 tail 转发新增行", False, str(exc))
    finally:
        if channel is not None:
            try:
                channel.close()
            except Exception:  # noqa: BLE001
                pass
        client.close()
        if not running:
            # 只有探针模式才回滚：这时没人并发写，截断是安全的
            with target.open("r+b") as handle:
                handle.truncate(original_size)


def check_live_stream() -> None:
    """实时日志源：3 类互相关联的异常 + 正常节拍（单模块视角也齐备），活动文件不超过 1000 行。"""
    from . import livesim
    from apps.logsources.services.log_format_parser import DEBUG_PATTERN

    pattern = re.compile(DEBUG_PATTERN)
    state = livesim.load_state()

    # 1) 容量：活动文件永远不超过上限；历史采样取自当前段 + 归档段
    contents: dict[str, str] = {}
    history: dict[str, str] = {}
    oversize: list[str] = []
    for target in livesim.TARGETS:
        path = target.local_path
        if not path.exists():
            oversize.append(f"{target.module}: 缺失")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        contents[target.key] = text
        count = text.count("\n")
        if count > livesim.MAX_LIVE_LINES:
            oversize.append(f"{target.module}={count}")
        slot = state.target(target.key)
        if slot.archived and Path(slot.archived).exists():
            try:
                archived_text = Path(slot.archived).read_text(encoding="utf-8", errors="replace")
                history[target.key] = archived_text
                # 轮转出来的归档也必须卡在上限内（写满即收档，不允许 1001 行越界）
                archived_count = archived_text.count("\n")
                if archived_count > livesim.MAX_LIVE_LINES:
                    oversize.append(f"{Path(slot.archived).name}={archived_count}")
            except OSError:
                pass
    _record(
        "实时日志源活动文件与归档 ≤ 1000 行",
        not oversize,
        "、".join(oversize) or f"{len(contents)} 条流均在上限 {livesim.MAX_LIVE_LINES} 内",
    )
    if not contents:
        return

    # 2) 行格式必须匹配后端八字段调试日志（否则前端解析不出 level/module）
    pool = [line for text in contents.values() for line in text.splitlines() if line.strip()]
    bad = [line for line in pool if not pattern.match(line)]
    _record("实时日志源行格式合规", bool(pool) and not bad, f"{len(pool)} 行，不合规 {len(bad)}")

    # 采样窗口：**整段**活动文件（上限 1000 行，本来就装得下一轮）+ 整段归档。
    # 不能只取尾部几十行：一轮剧本会在 4 条流上各写十几行，窗口太小会恰好卡在
    # 两轮之间，"同一 trace 横跨几条流"这种跨轮判断就取不到完整样本；
    # 某个流刚轮转时活动段可能只有几行，靠归档段把这一轮剧本补齐。
    samples: dict[str, str] = {}
    for key, text in contents.items():
        samples[key] = text + "\n" + history.get(key, "")
    joined = "\n".join(samples.values())
    sample_lines = [line for text in samples.values() for line in text.splitlines() if line.strip()]

    # 一轮剧本里带 trace= 的行数（从剧本自身算出来，不写死数字）。日志源刚重启或
    # 刚跑完 init 时，实时内容还不足一轮，跨模块关联必然取不到样本 —— 这种情况
    # 报「跳过」而不是假失败。
    trace_lines = [line for line in sample_lines if "trace=TR-" in line]
    round_trace_lines = sum(1 for _k, _l, _f, _p, body in livesim._SCRIPT if "trace={trace}" in body)
    if len(sample_lines) < 40 or len(trace_lines) < round_trace_lines:
        _record(
            "实时日志源内容样本",
            True,
            f"实时内容 {len(trace_lines)} 行（一轮剧本约 {round_trace_lines} 行），日志源刚启动，内容与关联检查跳过",
        )
    else:
        # 3) 至少 3 类异常 + 1 类正常
        codes = sorted(set(re.findall(r"code=(ERR_[A-Z_]+)", joined)))
        causes = sorted(set(re.findall(r"cause=(ERR_[A-Z_]+)", joined)))
        levels = sorted({m.group("level") for line in sample_lines if (m := pattern.match(line))})
        normal = sum(1 for line in sample_lines if "position error within tolerance" in line)
        _record("实时日志源含正常节拍日志", normal > 0, f"{normal} 行 · 级别 {levels}")
        _record("实时日志源含 ≥3 类异常", len(codes) >= 3, "、".join(codes) or "未找到 code=ERR_*")

        # 4) 关联：同一 trace 横跨多条流，且 cause 指向上游故障码
        traces: dict[str, set[str]] = {}
        for key, text in samples.items():
            for value in set(re.findall(r"trace=(TR-[\w-]+)", text)):
                traces.setdefault(value, set()).add(key)
        best_trace, best_files = max(traces.items(), key=lambda item: len(item[1]), default=("", set()))
        _record(
            "实时日志源异常跨模块关联",
            len(best_files) >= 3,
            f"{best_trace} 横跨 {len(best_files)} 条流：{'、'.join(sorted(best_files))}",
        )
        _record(
            "实时日志源 cause 因果链完整",
            {"ERR_MECORE_ENC_JITTER", "ERR_CPFR_FLOW_LOW", "ERR_CPFR_THERM_OVERLOAD"} <= set(causes),
            " → ".join(causes) or "未找到 cause=",
        )

        # 4b) 单模块可验证：前端「实时监听」一次只盯一个模块，所以被观察的 spwsp
        #     流自己就得凑齐「≥3 类异常 + 1 类正常节拍」，否则订阅单模块时凑不齐。
        observer_lines = [line for line in samples.get("spwsp", "").splitlines() if line.strip()]
        observer_codes = sorted(set(re.findall(r"code=(ERR_[A-Z_]+)", samples.get("spwsp", ""))))
        observer_levels = sorted({
            m.group("level") for line in observer_lines if (m := pattern.match(line))
        })
        observer_normal = sum(1 for line in observer_lines if "position error within tolerance" in line)
        _record(
            "实时日志源单模块含 ≥3 类异常 + 正常节拍",
            len(observer_codes) >= 3 and observer_normal > 0,
            f"spwsp 异常 {len(observer_codes)} 类 [{'、'.join(observer_codes)}] · 级别 {observer_levels} · 正常 {observer_normal} 行",
        )

        # 4c) 日志内容规则：正文必须带 [函数名] >() / <() 调用链边界，
        #     前端折叠函数卡片认的就是这对方向符。
        marker_in = sum(1 for line in sample_lines if ENTRY_MARKER_REGEX.search(line))
        marker_out = sum(1 for line in sample_lines if EXIT_MARKER_REGEX.search(line))
        _record(
            "实时日志源正文含入口/出口边界",
            marker_in > 0 and marker_out > 0,
            f"入口 >() {marker_in} 行 · 出口 <() {marker_out} 行",
        )

    # 5) 轮转原子性：改名成归档的同时必须留下一个空的当前段
    with tempfile.TemporaryDirectory() as folder:
        probe = Path(folder) / "cpcore.log"
        probe.write_text("x\n" * 5, encoding="utf-8")
        archived = livesim.archive_current(probe, "cpcore", datetime.now())
        ok = (
            probe.exists()
            and probe.stat().st_size == 0
            and archived.exists()
            and archived.stat().st_size == 10
        )
        _record("实时日志源轮转原子性", ok, f"{probe.name} 归零 / 归档 {archived.name} 保留原内容")


def check_live_rotation_cycle() -> None:
    """高速驱动一份缩小容量的副本，验证「写满即轮转、只留最新归档、活动文件永不越界」。

    在真实日志树上等 spwsp 写到 1000 行要十几分钟，所以这里把日志根指向临时目录、
    容量压到 50 行，用零间隔把同一套 ``run_forever`` / ``emit_once`` / ``rotate``
    代码路径跑满若干轮 —— 验的是行为本身，不是被压小的那个数字。
    """
    from . import livesim

    capacity = 50
    ticks = capacity * 6
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        real_root_of = livesim.fleet.local_root_of
        real_state_file = livesim.STATE_FILE
        real_capacity = livesim.MAX_LIVE_LINES
        real_recycle = livesim.RECYCLE_DIR
        livesim.fleet.local_root_of = lambda _machine: root
        livesim.STATE_FILE = root / "stream_state.json"
        livesim.MAX_LIVE_LINES = capacity
        # 回收站也要指到临时目录，否则测试产物会落到真实 run/recycle 里
        livesim.RECYCLE_DIR = root / "recycle"
        try:
            state = livesim.run_forever(interval=0.0, limit=ticks, verbose=False)

            # 必须趁日志根还指向临时目录时核对，否则读到的是真实日志树。
            oversize: list[str] = []
            leftover: list[str] = []
            rotated: list[str] = []
            archived_sizes: list[int] = []
            reclaimed: list[str] = []
            for target in livesim.TARGETS:
                slot = state.target(target.key)
                current = livesim.count_lines(target.local_path)
                if current > capacity:
                    oversize.append(f"{target.module}(当前段)={current}")
                archives = sorted(target.local_path.parent.glob(f"{target.module}_*.log"))
                if slot.rotations:
                    rotated.append(f"{target.module}×{slot.rotations}")
                    for item in archives:
                        archived_sizes.append(livesim.count_lines(item))
                    if len(archives) != 1:
                        leftover.append(f"{target.module} 留下 {len(archives)} 份归档")
                if slot.archived and not Path(slot.archived).exists():
                    leftover.append(f"{target.module}: 归档记录悬空")
                # 回收用的是改名不是删除，所以日志目录里不会有残余 ——
                # 断言"除最新一份外全部搬走"，而不是"删掉了"。
                expected = [slot.archived] if slot.archived else []
                if slot.owned != expected:
                    leftover.append(f"{target.module} 未回收 {len(slot.owned)} 份（应为 {len(expected)}）")
                reclaimed.append(f"{target.module}×{slot.recycled}")
        finally:
            livesim.fleet.local_root_of = real_root_of
            livesim.STATE_FILE = real_state_file
            livesim.MAX_LIVE_LINES = real_capacity
            livesim.RECYCLE_DIR = real_recycle

        _record(
            "实时日志源写满即轮转（缩小容量实测）",
            bool(rotated) and not oversize,
            f"上限 {capacity} 行 · {len(rotated)} 条流轮转过 [{'、'.join(rotated)}]"
            + (f" · 越界 {'、'.join(oversize)}" if oversize else ""),
        )
        _record(
            "实时日志源归档正好卡在上限",
            bool(archived_sizes) and all(size == capacity for size in archived_sizes),
            f"归档行数 {sorted(set(archived_sizes))}（应为 [{capacity}]）",
        )
        _record(
            "实时日志源只保留最新归档",
            not leftover,
            "、".join(leftover) or f"日志目录只留最新一份归档，旧归档已搬进回收站 [{'、'.join(reclaimed)}]",
        )


def _validate_call_chain(label: str, steps) -> None:  # noqa: ANN001 - 迭代器即可
    """校验一串 (函数, 相位, 正文) 是合法的调用栈轨迹，并回报结果。"""
    from .loggen import PHASE_BODY, PHASE_ENTER, PHASE_LEAVE, keyword_of, log_message

    stack: list[str] = []
    problems: list[str] = []
    pairs = 0
    boundary_levels: set[str] = set()
    # 入口/出口行必须带**固定关键字模板**：
    #   ``[ScanWafer] >() enter 晶圆扫片 开始 ...``  /  ``... 晶圆扫片 end ...``
    # 关键字让人一眼看出这一步在做什么；出口复用同一个关键字，不用回头翻入口
    # 就能配对上。缺失就说明 PHASE_KEYWORDS 漏登记（会退回英文函数名）。
    template_problems: list[str] = []

    for item in steps:
        function, phase, body = item[0], item[1], item[2]
        level = item[3] if len(item) > 3 else "INFO"
        rendered = log_message(function, phase, body)
        keyword = keyword_of(function)
        if phase == PHASE_ENTER:
            match = ENTRY_MARKER_REGEX.search(rendered)
            if not match:
                problems.append(f"{function} 入口缺 >()")
            elif BOUNDARY_NAME_REGEX.findall(rendered[: match.start()])[-1:] != [function]:
                problems.append(f"{function} 入口的函数名没紧邻 >()")
            else:
                boundary_levels.add(level)
                stack.append(function)
                if f"enter {keyword} 开始" not in rendered:
                    template_problems.append(function)
        elif phase == PHASE_LEAVE:
            match = EXIT_MARKER_REGEX.search(rendered)
            if not match:
                problems.append(f"{function} 出口缺 <()")
            elif BOUNDARY_NAME_REGEX.findall(rendered[: match.start()])[-1:] != [function]:
                problems.append(f"{function} 出口的函数名没紧邻 <()")
            if not stack or stack[-1] != function:
                problems.append(f"{function} 出口对不上（栈顶 {stack[-1] if stack else '空'}）")
            else:
                boundary_levels.add(level)
                stack.pop()
                pairs += 1
                if f"leave {keyword} end" not in rendered:
                    template_problems.append(function)
        else:
            if ENTRY_MARKER_REGEX.search(rendered) or EXIT_MARKER_REGEX.search(rendered):
                problems.append(f"{function} 正文行带了方向符")

    if stack:
        problems.append("未闭合：" + "、".join(stack))

    _record(
        f"{label} 调用链 LIFO 闭合",
        not problems and pairs > 0,
        "；".join(problems) if problems else f"{pairs} 组入口/出口同名配对",
    )
    # 边界行只负责记进出，异常级别必须落在正文行 —— 否则会出现
    # 「<() leave status=ok」却标着 FATAL 这种自相矛盾的行。
    _record(
        f"{label} 边界行级别为 INFO",
        boundary_levels <= {"INFO"},
        f"边界行级别 {sorted(boundary_levels) or '无'}",
    )
    _record(
        f"{label} 边界行带关键字模板",
        not template_problems,
        f"缺关键字 {sorted(set(template_problems))}" if template_problems
        else f"{pairs} 组「关键字 开始 / 关键字 end」",
    )


def _validate_stage_frames(label: str, steps, codes) -> None:  # noqa: ANN001 - 迭代器即可
    """阶段框：``[Stage_XXX] >() enter <阶段名> 开始 step=n/N ...`` 成对且序号递增。

    这是"日志体现不同流程阶段"的落点：每到一个新阶段就开一个框，阶段内所有
    子调用都嵌在框里。两条硬约束：

    * **成对且 LIFO** —— 阶段框也是调用链，出口名字要逐字对上；
    * **序号递增** —— 同一条流上的阶段顺序就是流程顺序，回退说明剧本写反了。
    """
    from .loggen import PHASE_ENTER, PHASE_LEAVE, STAGE_FUNCTION_PREFIX

    order = {code: index for index, code in enumerate(codes)}
    opened: list[str] = []
    sequence: list[int] = []
    problems: list[str] = []

    for item in steps:
        function, phase = item[0], item[1]
        if not function.startswith(STAGE_FUNCTION_PREFIX):
            continue
        code = function[len(STAGE_FUNCTION_PREFIX):]
        if code not in order:
            problems.append(f"{function} 不在阶段序里")
            continue
        if phase == PHASE_ENTER:
            opened.append(function)
            sequence.append(order[code])
        elif phase == PHASE_LEAVE:
            if not opened or opened[-1] != function:
                problems.append(f"{function} 阶段出口对不上（栈顶 {opened[-1] if opened else '空'}）")
            else:
                opened.pop()

    if opened:
        problems.append("阶段框未闭合：" + "、".join(opened))
    if any(sequence[index] >= sequence[index + 1] for index in range(len(sequence) - 1)):
        problems.append(f"阶段序号非递增 {sequence}")

    _record(
        f"{label} 阶段框成对且顺序递增",
        not problems and len(sequence) > 1,
        "；".join(problems) if problems
        else f"{len(sequence)} 个阶段，序号 {[item + 1 for item in sequence]}",
    )


def check_log_call_chain() -> None:
    """日志内容规则：``[函数名] >()`` 入口 / ``[函数名] <()`` 出口，同名 LIFO 配对。

    这是前端折叠函数卡片的唯一依据（``foldingRules.ts`` 的内置规则
    ``builtin-explicit-boundary``，关键字就是 ``> ()`` / ``< ()``）。名字不一致
    或嵌套顺序写反，前端就会画出错乱嵌套、把父函数标成「未闭合」。

    这里直接校验两份剧本本身（批量 ``loggen._DEBUG_PROGRAM``、实时
    ``livesim._SCRIPT`` 的每条流），不用等日志落盘，也不用等 1000 行轮转。
    """
    from . import livesim, loggen

    _validate_call_chain(
        "批量日志",
        [(fn, phase, body, level) for fn, phase, level, body in loggen._DEBUG_PROGRAM],
    )
    _validate_stage_frames(
        "批量日志",
        [(fn, phase) for fn, phase, _level, _body in loggen._DEBUG_PROGRAM],
        loggen.DEBUG_STAGE_CODES,
    )
    for target in livesim.TARGETS:
        steps = [
            (function, phase, body, level)
            for key, level, function, phase, body in livesim._SCRIPT
            if key == target.key
        ]
        _validate_call_chain(f"实时日志 {target.module}", steps)
        _validate_stage_frames(
            f"实时日志 {target.module}",
            [(function, phase) for function, phase, _body, _level in steps],
            livesim.STAGE_CODES,
        )

    # 关键字模板必须**全覆盖**：任何边界行函数没登记在 PHASE_KEYWORDS 里，就会
    # 退回英文函数名当关键字，用户看到的就不是「晶圆扫片 开始」而是「ScanWafer 开始」。
    from .loggen import PHASE_BODY

    boundary_functions = {
        function
        for function, phase, _level, _body in loggen._DEBUG_PROGRAM
        if phase != PHASE_BODY
    } | {
        function
        for _key, _level, function, phase, _body in livesim._SCRIPT
        if phase != PHASE_BODY
    }
    unregistered = sorted(boundary_functions - set(loggen.PHASE_KEYWORDS))
    _record(
        "调用链函数都有关键字模板",
        not unregistered,
        f"未登记 {unregistered}" if unregistered
        else f"{len(boundary_functions)} 个函数全部登记在 PHASE_KEYWORDS",
    )

    # 落盘抽查：活动文件里真的能看到方向符（剧本对不代表写出来的对）
    sample = Path(fleet.remote_to_local(fleet.UPPER, f"{fleet.UPPER.debug_root}/spwsp/spwsp.log"))
    if not sample.exists():
        _record("日志正文含调用链边界", False, f"{sample} 不存在")
        return
    lines = [line for line in sample.read_text(encoding="utf-8", errors="replace").splitlines()[-400:] if line.strip()]
    entries = sum(1 for line in lines if ENTRY_MARKER_REGEX.search(line))
    exits = sum(1 for line in lines if EXIT_MARKER_REGEX.search(line))
    # 第 9 段才是正文（前 8 个 [...] 是固定字段），所以从正文里找 [函数名]
    named = sum(1 for line in lines if BOUNDARY_NAME_REGEX.search(line.split("] ", 8)[-1]))
    _record(
        "日志正文含调用链边界",
        entries > 0 and exits > 0 and named == len(lines),
        f"最近 {len(lines)} 行：入口 {entries} / 出口 {exits} / 带函数名 {named}",
    )


def check_cpd_assets() -> None:
    """CPD 测校资产：报告字段能被后端解析，数据表格时间列 + 行时间落在报告窗口内。"""
    from apps.reports.parser import parse_report_summary

    report_root = Path(fleet.remote_to_local(fleet.UPPER, fleet.UPPER.cpd_report_root))
    data_root = Path(fleet.remote_to_local(fleet.UPPER, fleet.UPPER.cpd_data_root))
    reports = sorted(report_root.glob("*/*/*.rpt"))
    books = sorted(data_root.glob("*/*/*.xlsx"))
    _record("CPD 测校报告已生成", len(reports) > 0, f"{len(reports)} 份 · {fleet.UPPER.cpd_report_root}")
    # 同一天多次 init 会留下更早的批次（清理受小批量删除保护限制），所以这里比的是
    # "每一批报告都有同名数据表格"，而不是两边总数相等。
    report_keys = {item.relative_to(report_root).with_suffix("").as_posix() for item in reports}
    book_keys = {item.relative_to(data_root).with_suffix("").as_posix() for item in books}
    orphans = sorted(report_keys ^ book_keys)
    _record(
        "CPD 报告与数据表格成对",
        not orphans,
        f"{len(report_keys & book_keys)} 对配对" + (f"，孤儿 {orphans[:4]}" if orphans else ""),
    )
    if not reports:
        return

    # 目录层级必须是 <子系统>/<模块>/<文件>，否则后端 find -mindepth 3 扫不到
    depths = {len(item.relative_to(report_root).parts) for item in reports}
    _record("CPD 报告目录层级正确", depths == {3}, f"层级 {sorted(depths)}（应为 [3]）")

    # 两棵树的子系统/模块目录必须同名（后端数据根会转小写）
    report_dirs = {item.parent.relative_to(report_root).as_posix() for item in reports}
    data_dirs = {item.parent.relative_to(data_root).as_posix() for item in books}
    _record("报告与数据目录同构", report_dirs == data_dirs, f"报告 {len(report_dirs)} / 数据 {len(data_dirs)}")

    sample = reports[-1]
    relative = sample.relative_to(report_root).as_posix()
    summary = parse_report_summary(
        sample.read_text(encoding="utf-8"),
        file_name=sample.name,
        full_path=f"{fleet.UPPER.cpd_report_root}/{relative}",
        modified_at=sample.stat().st_mtime,
    )
    required = ("start_time", "stop_time", "test_run_result", "results_validation",
                "measurement_quality", "mcs_status", "execution_time", "cpd_name")
    missing = [key for key in required if not summary.get(key)]
    _record(
        "CPD 报告字段可解析",
        not missing,
        f"缺 {missing}" if missing else f"{summary['test_run_result']} · {summary['start_time']} → {summary['stop_time']}",
    )

    # 数据表格要有一列时间，且行时间落在同名报告的 [Start, Stop] 内
    twin = data_root / sample.relative_to(report_root).with_suffix(".xlsx")
    if not twin.exists():
        _record("CPD 报告有同名数据表格", False, f"缺少 {twin.name}")
        return
    from openpyxl import load_workbook

    workbook = load_workbook(twin, read_only=True, data_only=True)
    try:
        sheet = workbook.active
        rows = list(sheet.iter_rows(values_only=True))
    finally:
        workbook.close()
    headers = [str(cell or "").strip().casefold() for cell in (rows[0] if rows else ())]
    time_column = next((name for name in ("timestamp", "time", "date", "时间", "时间戳") if name in headers), None)
    _record("数据表格含时间列", time_column is not None, f"表头 {headers[:4]}")
    if time_column is None or len(rows) < 2:
        return
    index = headers.index(time_column)
    start = datetime.fromisoformat(summary["start_time"])
    stop = datetime.fromisoformat(summary["stop_time"])
    stamps = [row[index] for row in rows[1:] if isinstance(row[index], datetime)]
    inside = [item for item in stamps if start <= item <= stop]
    _record(
        "表格行时间落在报告窗口内",
        bool(stamps) and len(inside) == len(stamps),
        f"{len(inside)}/{len(stamps)} 行 ∈ [{start:%H:%M:%S}, {stop:%H:%M:%S}]",
    )


def check_cpd_backend_read() -> None:
    """走一遍后端真正的读取路径：SFTP 拉工作簿 → 解析 → 按报告时间窗过滤。"""
    from apps.environments.models import Environment
    from apps.reports import cpd_data_service as cpd
    from apps.reports.parser import parse_report_summary

    environment = Environment.objects.filter(upper_machine__host=fleet.UPPER.host).first()
    if environment is None:
        _record("CPD 数据表格（后端读取路径）", False, "尚未 seed，先跑 `python -m simremote.cli seed`")
        return

    report_root = Path(fleet.remote_to_local(fleet.UPPER, fleet.UPPER.cpd_report_root))
    reports = sorted(report_root.glob("*/*/*.rpt"))
    if not reports:
        _record("CPD 数据表格（后端读取路径）", False, "未生成报告")
        return
    sample = reports[-1].relative_to(report_root)
    subsystem, module, filename = sample.parts[0], sample.parts[1], sample.parts[2]
    remote_report = f"{fleet.UPPER.cpd_report_root}/{sample.as_posix()}"
    summary = parse_report_summary(
        (report_root / sample).read_text(encoding="utf-8"),
        file_name=filename, full_path=remote_report, modified_at=(report_root / sample).stat().st_mtime,
    )
    start_time, stop_time = summary["start_time"], summary["stop_time"]
    try:
        found = cpd.find_cpd_excel_files(environment, subsystem, module, start_time, stop_time)
        names = [item["path"].rsplit("/", 1)[-1] for item in found["files"]]
        _record(
            "后端按报告窗口筛出数据表格",
            names == [sample.with_suffix(".xlsx").name],
            f"命中 {names}",
        )
        if not names:
            return
        preview = cpd.preview_cpd_excel(
            environment, subsystem, module, found["files"][0]["path"],
            found["files"][0]["sheets"][0]["name"], start_time, stop_time,
        )
        _record(
            "后端可预览表格内容",
            preview.get("total", 0) > 0 and preview.get("filter_status") == "filtered",
            f"{preview.get('total')} 行 · 时间列 {preview.get('time_column')}",
        )
    except Exception as exc:  # noqa: BLE001
        _record("后端按报告窗口筛出数据表格", False, str(exc))


def check_atlog_report_site() -> None:
    """模拟 ATLog 用例报告站：目录页、HEAD/Range、以及真实的 URL 分析链路。"""
    import urllib.error
    import urllib.request

    from apps.atlog import services as atlog

    cases = [item for item in atlog_site.case_urls() if item["status"] == "failed"]
    if not cases:
        _record("报告站用例 URL", False, "未生成用例")
        return
    base = cases[0]["url"]
    _record("报告站用例 URL", True, base)

    # 1) 目录页必须是 nginx autoindex 风格（后端靠 HTMLParser 抓 <a href>）
    try:
        with urllib.request.urlopen(base, timeout=6) as response:
            page = response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        _record("报告站目录页可达", False, f"{exc}（先跑 `python -m simremote.cli site-serve`）")
        return
    required = ["summary_report.xml", "event.log", "xytest.log", "full_logs/", "result/"]
    _record(
        "报告站目录页可达",
        bool(page) and all(f'"{name}"' in page for name in required),
        f"{len(page)} 字节",
    )

    # 2) 后端真正用 HTTP HEAD + Range 读尾部，这里直接验证这两个动作
    target = base + "summary_report.xml"
    request = urllib.request.Request(target, method="HEAD")
    with urllib.request.urlopen(request, timeout=6) as response:
        size = int(response.headers.get("Content-Length") or 0)
    _record("报告站支持 HEAD", size > 0, f"Content-Length={size}")
    ranged = urllib.request.Request(target, headers={"Range": "bytes=0-63"})
    with urllib.request.urlopen(ranged, timeout=6) as response:
        chunk = response.read()
        status = getattr(response, "status", 200)
    _record("报告站支持 Range", status == 206 and bytes(chunk).startswith(b"<?xml"), f"status={status} {bytes(chunk)[:24]!r}")

    # 3) 真正的分析：粘贴这个 URL 应该直接得到失败根因
    try:
        result = atlog.analyze_case(base)
    except Exception as exc:  # noqa: BLE001
        _record("ATLog URL 分析返回失败根因", False, str(exc))
        return
    conclusion = str(result.get("conclusion") or "")
    assertion = str(result.get("assertion") or "")
    _record(
        "ATLog URL 分析返回失败根因",
        result.get("status") == "failed" and "ResultStatus.FAILED" in f"{conclusion} {assertion}",
        f"status={result.get('status')} · {conclusion[:40]}",
    )
    _record(
        "ATLog 解析出调用栈",
        len(result.get("call_chain") or []) >= 3 and bool(result.get("failure_location")),
        f"{len(result.get('call_chain') or [])} 帧 · 末帧 {(result.get('failure_location') or {}).get('function')}",
    )
    sources = {item.get("source") for item in (result.get("evidence") or [])}
    _record(
        "ATLog 汇总多路证据",
        {"summary_report.xml", "pytest XML", "pytest HTML", "xytest.log"} <= sources,
        f"证据来源 {sorted(sources)}",
    )
    catalog = result.get("log_catalog") or []
    _record(
        "ATLog 发现用例日志目录",
        bool(catalog) and bool((catalog[0] or {}).get("modules")),
        f"{[(item.get('subsystem'), item.get('modules')) for item in catalog]}",
    )
    _record("ATLog 分析无告警", not (result.get("warnings") or []), f"warnings={result.get('warnings')}")

    # 4) 用例日志检索（按组件选目标，full_logs 是可检索的）
    modules = ((catalog[0] or {}).get("modules") or []) if catalog else []
    if not modules:
        return
    try:
        logs = atlog.query_case_logs(base, components=[modules[0]], max_lines=200)
    except Exception as exc:  # noqa: BLE001
        _record("ATLog 用例日志可检索", False, str(exc))
        return
    _record(
        "ATLog 用例日志可检索",
        logs.get("matched_file_count", 0) > 0 and logs.get("count", 0) > 0,
        f"{logs.get('matched_file_count')} 个文件 / {logs.get('count')} 行",
    )


def check_cpd_report_site() -> None:
    """CPD 资产同样发布在报告站上：.rpt / .xlsx 都能用「报告文件 URL 分析」直接解析。"""
    import urllib.request

    from apps.atlog import services as atlog

    host, port = fleet.ATLOG_SITE_HOST, fleet.ATLOG_SITE_PORT
    report_root = Path(fleet.remote_to_local(fleet.UPPER, fleet.UPPER.cpd_report_root))
    data_root = Path(fleet.remote_to_local(fleet.UPPER, fleet.UPPER.cpd_data_root))
    reports = sorted(report_root.glob("*/*/*.rpt"))
    if not reports:
        _record("报告站发布 CPD 报告 URL", False, "未生成 CPD 报告")
        return

    def data_root_of(path: Path) -> Path:
        # 数据树与报告树不同根，且目录名是小写
        item = path.relative_to(report_root)
        return data_root / item.parts[0].lower() / item.parts[1].lower() / f"{path.stem}.xlsx"

    def report_url_of(path: Path) -> str:
        item = path.relative_to(report_root)
        return f"http://{host}:{port}{atlog_site.CPD_REPORT_MOUNT}/{item.parts[0]}/{item.parts[1]}/{path.name}"

    def data_url_of(path: Path) -> str:
        item = path.relative_to(report_root)
        return (
            f"http://{host}:{port}{atlog_site.CPD_DATA_MOUNT}/"
            f"{item.parts[0].lower()}/{item.parts[1].lower()}/{path.with_suffix('.xlsx').name}"
        )

    mounts = atlog_site.cpd_urls()
    _record(
        "报告站发布 CPD 报告 URL",
        bool(mounts) and all(item["report_url"] for item in mounts),
        f"{len(mounts)} 个模块 · {atlog_site.CPD_REPORT_MOUNT}",
    )

    sample = reports[-1]
    try:
        with urllib.request.urlopen(report_url_of(sample), timeout=8) as response:
            head = response.read(64).decode("utf-8", "replace")
        _record("报告站可访问 .rpt", head.startswith("CPD Measurement Report"), head[:36])
    except Exception as exc:  # noqa: BLE001
        _record("报告站可访问 .rpt", False, f"{exc}（先跑 `python -m simremote.cli site-serve`）")
        return

    twin = data_root_of(sample)
    if not twin.exists():
        _record("报告站可访问 .xlsx", False, f"缺少同名表格 {twin.name}")
        return
    request = urllib.request.Request(data_url_of(sample), method="HEAD")
    with urllib.request.urlopen(request, timeout=8) as response:
        content_type = response.headers.get("Content-Type") or ""
        size = int(response.headers.get("Content-Length") or 0)
    _record(
        "报告站可访问 .xlsx",
        "spreadsheetml" in content_type and size == twin.stat().st_size,
        f"{content_type} · {size} 字节",
    )

    report = atlog.analyze_report_file(report_url_of(sample))
    _record(
        "报告文件 URL 分析（.rpt）",
        report.get("kind") == "cpd_report" and bool((report.get("cpd_report") or {}).get("test_run_result")),
        f"kind={report.get('kind')} result={(report.get('cpd_report') or {}).get('test_run_result')}",
    )
    sheet = atlog.analyze_report_file(data_url_of(sample))
    sections = sheet.get("sections") or []
    _record(
        "报告文件 URL 分析（.xlsx）",
        sheet.get("kind") == "spreadsheet" and bool(sections) and bool(sections[0].get("table")),
        f"{len(sections[0].get('table') or []) if sections else 0} 行 · {str(sheet.get('conclusion'))[:52]}",
    )

    # 同一批测校：报告判 FAILED，表格里就该有非 OK 行；两边数量必须一一对应
    failed_reports = failed_sheets = 0
    for path in reports:
        failed_reports += atlog.analyze_report_file(report_url_of(path)).get("status") == "failed"
        failed_sheets += atlog.analyze_report_file(data_url_of(path)).get("status") == "failed"
    _record(
        "CPD 报告与数据表格判定一致",
        failed_reports == failed_sheets and failed_reports > 0,
        f"{len(reports)} 批中：失败报告 {failed_reports} · 含非 OK 行的表格 {failed_sheets}",
    )


def check_report_file_analysis() -> None:
    """报告文件 URL 分析：一个链接进去，按类型分派到对应解析器。"""
    import re

    from apps.atlog import services as atlog

    cases = [item for item in atlog_site.case_urls() if item["status"] == "failed"]
    if not cases:
        _record("报告文件 URL 分析", False, "未生成失败用例")
        return
    base = cases[0]["url"]

    expectations = (
        ("summary_report.xml", "junit_xml"),
        ("result/pytest-01.xml", "junit_xml"),
        ("xytest.log", "xytest_log"),
        ("event.log", "event_log"),
        ("full_logs/log/debug/spm/RSPMCPD.log", "debug_log"),
    )
    for relative, kind in expectations:
        try:
            result = atlog.analyze_report_file(base + relative)
        except Exception as exc:  # noqa: BLE001
            _record(f"报告文件 URL 分析 {relative}", False, str(exc))
            continue
        _record(
            f"报告文件 URL 分析 {relative}",
            result.get("kind") == kind,
            f"kind={result.get('kind')} status={result.get('status')} case={result.get('case_id')}",
        )

    deep = atlog.analyze_report_file(base + "full_logs/log/debug/spm/RSPMCPD.log")
    _record(
        "报告文件能上溯出用例编号",
        deep.get("case_id") == cases[0]["case_id"],
        f"case_id={deep.get('case_id')}（期望 {cases[0]['case_id']}）",
    )

    # 调试日志只认行首 [..] 里的级别字段：正文里的 "position error" 不算错误行
    local = Path(cases[0]["local_path"]) / "full_logs/log/debug/spm/RSPMCPD.log"
    raw = local.read_text(encoding="utf-8") if local.exists() else ""
    expected = sum(
        1 for line in raw.splitlines()
        if re.match(r"^\[[^\]]+\]\s*\[(?:ERROR|FATAL|CRITICAL|FAIL|FAILED)\]", line)
    )
    findings = deep.get("findings") or []
    _record(
        "调试日志异常行不误报",
        len(findings) == expected and expected > 0,
        f"抽取 {len(findings)} 条（文件内真错误行 {expected} 条）",
    )

    try:
        atlog.analyze_report_file("http://example.com/summary_report.xml")
        _record("报告文件 URL 拒绝公网地址", False, "竟然放行了公网主机")
    except atlog.AtLogError:
        _record("报告文件 URL 拒绝公网地址", True)


def check_backend_catalog() -> None:
    from apps.environments.models import Environment
    from apps.logsources.services.remote_logs import scan_environment_logs

    environment = Environment.objects.filter(upper_machine__host=fleet.UPPER.host).first()
    if environment is None:
        _record("后端能发现模拟环境", False, "尚未 seed，先跑 `python -m simremote.cli seed`")
        return
    result = scan_environment_logs(environment, refresh=True)
    subsystems = result.get("subsystems") or []
    catalog = result.get("global_catalog") or []
    fms = sum(len(item.get("fms") or []) for item in catalog if isinstance(item, dict))
    _record("后端能发现子系统", len(subsystems) > 0, f"{len(subsystems)} 个：{', '.join(subsystems[:6])}")
    _record("后端能发现模块", fms >= len(fleet.all_modules()), f"{fms} 个模块")


# --------------------------------------------------------------------------- 入口


def _bootstrap_django() -> bool:
    """配置 Django。

    这里必须先 ``django.setup()``：``check_backend_catalog`` 等检查会 import 模型，
    只 ``import django`` 是不够的，否则访问 settings 时抛 ImproperlyConfigured。
    """
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    try:
        import django

        django.setup()
    except Exception as exc:  # noqa: BLE001
        _record("Django 初始化", False, f"{type(exc).__name__}: {exc}")
        return False
    _record("Django 初始化", True, os.environ["DJANGO_SETTINGS_MODULE"])
    return True


def run_all() -> int:
    _RESULTS.clear()
    for spec in fleet.FLEET:
        assert isinstance(spec, fleet.MachineSpec)

    try:
        check_ports()
    except Exception as exc:  # noqa: BLE001
        _record("机群可达", False, str(exc))

    try:
        client = _connect(fleet.UPPER)
    except Exception as exc:  # noqa: BLE001
        print(f"无法连接模拟上位机：{exc}")
        print("请先启动机群：cd backend && .venv/bin/python -m simremote.cli serve")
        return 1

    try:
        check_auth_reject()
        check_sftp_readonly(client)
        check_find(client)
        check_tar(client)
        check_awk_window(client)
        check_version_file(client)
        check_close_boundary()
        check_live_tail()
    finally:
        client.close()

    if _bootstrap_django():
        check_line_formats()
        check_backend_catalog()
        check_cpd_assets()
        check_cpd_backend_read()
        check_atlog_report_site()
        check_cpd_report_site()
        check_report_file_analysis()
        check_live_stream()
        check_live_rotation_cycle()
        check_log_call_chain()

    passed = sum(1 for _, ok, _detail in _RESULTS if ok)
    print("=" * 64)
    for name, ok, detail in _RESULTS:
        mark = "PASS" if ok else "FAIL"
        suffix = f"  — {detail}" if detail else ""
        print(f"  [{mark}] {name}{suffix}")
    print("=" * 64)
    print(f"  {passed}/{len(_RESULTS)} 项通过")
    return 0 if passed == len(_RESULTS) else 1
