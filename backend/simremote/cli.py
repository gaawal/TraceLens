"""simremote 命令行入口。

    python -m simremote.cli init [--no-prune] [--fresh]   生成/对齐模拟资产
                                                          （日志 + CPD 测校报告/数据表格 + ATLog 用例报告站）
    python -m simremote.cli serve                          启动假 SSH/SFTP 机群（前台）
    python -m simremote.cli seed                           写入环境资源并刷新日志索引
    python -m simremote.cli status                         查看机群、报告站与日志时间线状态
    python -m simremote.cli site-serve [--port 8901]       启动模拟 ATLog 用例报告站（前台）
    python -m simremote.cli site-url                       打印用例 URL（可直接粘到 ATLog 页面）
    python -m simremote.cli site-stop                      停止报告站
    python -m simremote.cli selftest                       端到端自检
    python -m simremote.cli stop                           停止机群
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

from . import fleet, loggen

RUN_DIR = Path(__file__).resolve().parent / "run"
PID_FILE = RUN_DIR / "fleet.pid"
SITE_PID_FILE = RUN_DIR / "site.pid"
STREAM_PID_FILE = RUN_DIR / "stream.pid"


def _django_setup() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
    except OSError:
        return False
    finally:
        sock.close()
    return True


def _read_pid(path: Path = PID_FILE) -> int | None:
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    return True


# --------------------------------------------------------------------------- 命令


def cmd_init(args: argparse.Namespace) -> int:
    if args.fresh:
        print("清空本地模拟文件系统…")
        loggen.reset_filesystem()
    started = time.time()
    report = loggen.generate_all(prune=not args.no_prune)
    print(f"生成完成（{time.time() - started:.1f}s）")
    print(f"  {report.summary()}")
    print(f"  本地根：{fleet.LOCAL_ROOT}")
    return 0


def cmd_serve(_args: argparse.Namespace) -> int:
    from .sshd import FleetServer

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    server = FleetServer()
    try:
        server.start()
    except OSError as exc:
        print(f"机群启动失败：{exc}", file=sys.stderr)
        PID_FILE.unlink(missing_ok=True)
        return 1
    for spec in fleet.FLEET:
        print(f"  {spec.role:5s} {spec.name:12s} ssh {fleet.SIM_USERNAME}@{spec.host}:{spec.ssh_port}")
    print(f"机群已就绪（pid {os.getpid()}），Ctrl-C 退出")
    sys.stdout.flush()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n停止机群")
    finally:
        server.stop()
        PID_FILE.unlink(missing_ok=True)
    return 0


def cmd_stop(_args: argparse.Namespace) -> int:
    pid = _read_pid()
    if pid is None:
        print("机群没有 pidfile（不是由本脚本启动，或已退出）")
    else:
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                if not _port_open("127.0.0.1", fleet.UPPER.ssh_port):
                    break
                time.sleep(0.2)
            print(f"机群已停止（pid {pid}）")
        except ProcessLookupError:
            # pidfile 可能是被回收的旧进程留下的，别急着说"已停止"，以端口为准
            print(f"pidfile 中的进程 {pid} 不存在")
        except OSError as exc:
            print(f"停止失败：{exc}", file=sys.stderr)
            return 1
        finally:
            PID_FILE.unlink(missing_ok=True)

    still_up = [spec.host for spec in fleet.FLEET if _port_open("127.0.0.1", spec.ssh_port)]
    if still_up:
        print(f"注意：{', '.join(still_up)} 仍在监听，可能还有另一个机群进程。", file=sys.stderr)
        return 1
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    print("模拟机群状态")
    for spec in fleet.FLEET:
        state = "运行中" if _port_open("127.0.0.1", spec.ssh_port) else "未运行"
        print(f"  {spec.host}:{spec.ssh_port}  {state}")

    pid = _read_pid()
    print(f"  pidfile：{pid if pid else '（无）'}")

    from . import atlog_site

    site_state = "运行中" if _port_open(fleet.ATLOG_SITE_HOST, fleet.ATLOG_SITE_PORT) else "未运行"
    site_pid = _read_pid(SITE_PID_FILE)
    print(f"  ATLog 报告站 {fleet.ATLOG_SITE_HOST}:{fleet.ATLOG_SITE_PORT}  {site_state}"
          + (f"（pid {site_pid}）" if site_pid else ""))
    for item in atlog_site.case_urls():
        print(f"    [{item['status']:6s}] {item['url']}")
    print(f"  CPD 报告根：{fleet.UPPER.cpd_report_root}")
    print(f"  CPD 数据根：{fleet.UPPER.cpd_data_root}")
    for item in atlog_site.cpd_urls():
        print(f"    [CPD  rpt] {item['report_url']}")
        print(f"    [CPD xlsx] {item['data_url']}")

    from . import livesim

    stream_pid = _read_pid(STREAM_PID_FILE)
    stream_state = "运行中" if _pid_alive(stream_pid) else "未运行"
    print(f"  实时日志源（tail -f 效果）  {stream_state}"
          + (f"（pid {stream_pid}）" if stream_pid else ""))
    if stream_state == "运行中":
        info = livesim.snapshot()
        rows = ", ".join(f"{row['subsystem']}/{row['module']}={row['lines']}行" for row in info["targets"])
        print(f"    第 {info['round']} 轮｜trace={info['trace'] or '-'}｜{rows}")
        print(f"    监听：{info['targets'][0]['machine']}:{info['targets'][0]['remote_path']}")

    sample = fleet.remote_to_local(fleet.UPPER, f"{fleet.UPPER.debug_root}/spwsp/spwsp.log")
    if sample.exists():
        stamps = []
        with sample.open(encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("["):
                    stamps.append(line[1:20])
        if stamps:
            first, last = stamps[0], stamps[-1]
            lag = datetime.now() - datetime.fromisoformat(last)
            minutes = int(lag.total_seconds() // 60)
            print(f"  当前日志段：{first} → {last}（滞后 {minutes} 分钟）")
            if minutes > 360:
                print("  提示：时间线已陈旧，跑 `python -m simremote.cli init` 或 scripts/sim_realign.sh 对齐")
    else:
        print("  提示：尚未生成日志树，先跑 `python -m simremote.cli init`")
    return 0


def cmd_seed(_args: argparse.Namespace) -> int:
    _django_setup()
    from . import seed as seed_module

    payload = seed_module.seed(refresh=True)
    print(f"  环境      : {payload['environment']} (#{payload['environment_id']}) 状态={payload['status_label']}")
    print(f"  软件版本  : {payload['software_version']}")
    print(f"  上位机    : {payload['upper']}")
    for item in payload["lowers"]:
        print(f"  下位机    : {item}")
    print(f"  日志配置  : {', '.join(payload['profiles'])}")
    if payload.get("catalog_error"):
        print(f"  目录扫描  : 失败 —— {payload['catalog_error']}")
        return 1
    print(f"  子系统    : {', '.join(payload['subsystems']) or '（无）'}")
    print(f"  全局目录  : {len(payload['catalog_global'])} 个子系统 / {payload.get('catalog_fm_count', 0)} 个模块")
    return 0


def cmd_site_serve(args: argparse.Namespace) -> int:
    from . import atlog_site

    if not atlog_site.SITE_ROOT.exists():
        print("报告站尚未生成，先跑 `python -m simremote.cli init`", file=sys.stderr)
        return 1
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        server = atlog_site.serve(args.host, args.port)
    except OSError as exc:
        print(f"报告站启动失败：{exc}", file=sys.stderr)
        return 1
    SITE_PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    print(f"ATLog 用例报告站 http://{args.host}:{args.port}/")
    for item in atlog_site.case_urls(args.host, args.port):
        print(f"  [{item['status']:6s}] {item['url']}")
    cpd = atlog_site.cpd_urls(args.host, args.port)
    if cpd:
        print("  CPD 测校报告 / 数据表格：")
        for item in cpd:
            print(f"    [{item['subsystem']}/{item['module']}] {item['report_url']}")
            print(f"    [{item['subsystem']}/{item['module']}] {item['data_url']}")
    print(f"报告站已就绪（pid {os.getpid()}），Ctrl-C 退出")
    sys.stdout.flush()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n停止报告站")
    finally:
        server.shutdown()
        server.server_close()
        SITE_PID_FILE.unlink(missing_ok=True)
    return 0


def cmd_site_url(args: argparse.Namespace) -> int:
    from . import atlog_site

    for item in atlog_site.case_urls(args.host, args.port):
        print(f"[{item['status']:6s}] {item['url']}")
    for item in atlog_site.cpd_urls(args.host, args.port):
        print(f"[CPD  {item['report_count']}rpt] {item['report_url']}")
        print(f"[CPD  {item['data_count']}xlsx] {item['data_url']}")
    return 0


def cmd_site_stop(_args: argparse.Namespace) -> int:
    pid = _read_pid(SITE_PID_FILE)
    if pid is None:
        print("报告站没有 pidfile（不是由本脚本启动，或已退出）")
    else:
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                if not _port_open(fleet.ATLOG_SITE_HOST, fleet.ATLOG_SITE_PORT):
                    break
                time.sleep(0.2)
            print(f"报告站已停止（pid {pid}）")
        except ProcessLookupError:
            # pidfile 可能是被回收的旧进程留下的，别急着说"已停止"，以端口为准
            print(f"pidfile 中的进程 {pid} 不存在")
        except OSError as exc:
            print(f"停止失败：{exc}", file=sys.stderr)
            return 1
        finally:
            SITE_PID_FILE.unlink(missing_ok=True)

    if _port_open(fleet.ATLOG_SITE_HOST, fleet.ATLOG_SITE_PORT):
        print(
            f"注意：{fleet.ATLOG_SITE_HOST}:{fleet.ATLOG_SITE_PORT} 仍在监听，"
            "可能还有另一个报告站进程占着端口。",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_stream(args: argparse.Namespace) -> int:
    """前台运行实时日志源；由 scripts/sim_up.sh 用 nohup 放到后台。"""
    from . import livesim

    STREAM_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    STREAM_PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    sys.stdout.flush()
    try:
        livesim.run_forever(interval=float(args.interval), limit=args.lines)
    finally:
        STREAM_PID_FILE.unlink(missing_ok=True)
    return 0


def cmd_stream_status(_args: argparse.Namespace) -> int:
    from . import livesim

    info = livesim.snapshot()
    pid = _read_pid(STREAM_PID_FILE)
    alive = "运行中" if _pid_alive(pid) else "未运行"
    print(f"实时日志源  {alive}" + (f"（pid {pid}）" if pid else "（无 pidfile）"))
    print(f"  剧本进度：第 {info['round']} 轮，行内游标 {info['cursor']}，累计 {info['ticks']} 行")
    print(f"  当前追踪：trace={info['trace'] or '-'} lot={info['lot'] or '-'} wafer=W{info['wafer']:02d}")
    print(f"  活动文件上限：{info['max_lines']} 行（超出即轮转）")
    for row in info["targets"]:
        marker = "●" if row["lines"] else "○"
        print(
            f"  {marker} {row['subsystem']}/{row['module']}.log  {row['lines']} 行"
            f"｜轮转 {row['rotations']} 次"
            + (f"｜归档 {Path(row['archived']).name}" if row["archived"] else "")
            + (f"｜已回收 {row['recycled']}" if row["recycled"] else "")
            + (f"｜待回收 {row['pending_reclaim']}" if row["pending_reclaim"] else "")
        )
    print(f"  监听路径示例：{info['targets'][0]['machine']}:{info['targets'][0]['remote_path']}")
    return 0


def cmd_stream_stop(_args: argparse.Namespace) -> int:
    pid = _read_pid(STREAM_PID_FILE)
    if pid is None:
        print("实时日志源没有 pidfile（不是由本脚本启动，或已退出）")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print(f"pidfile 中的进程 {pid} 不存在")
    except OSError as exc:
        print(f"停止失败：{exc}", file=sys.stderr)
        return 1
    finally:
        STREAM_PID_FILE.unlink(missing_ok=True)

    for _ in range(20):
        if not _pid_alive(pid):
            break
        time.sleep(0.2)
    if _pid_alive(pid):
        print(f"注意：进程 {pid} 仍在运行。", file=sys.stderr)
        return 1
    print(f"实时日志源已停止（pid {pid}）")
    return 0


def cmd_selftest(_args: argparse.Namespace) -> int:
    from .selftest import run_all

    return run_all()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="simremote", description="TraceLens 本地模拟上下位机机群")
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init", help="生成/对齐本地模拟日志树")
    init_parser.add_argument("--no-prune", action="store_true", help="不清理非本轮产物")
    init_parser.add_argument("--fresh", action="store_true", help="先清空本地模拟文件系统")
    init_parser.set_defaults(func=cmd_init)

    for name, func, help_text in (
        ("serve", cmd_serve, "启动假 SSH/SFTP 机群（前台）"),
        ("stop", cmd_stop, "停止机群"),
        ("status", cmd_status, "查看机群、报告站与日志时间线状态"),
        ("seed", cmd_seed, "写入环境资源并刷新日志索引"),
        ("selftest", cmd_selftest, "端到端自检"),
        ("site-stop", cmd_site_stop, "停止模拟 ATLog 用例报告站"),
        ("stream-stop", cmd_stream_stop, "停止实时日志源"),
        ("stream-status", cmd_stream_status, "查看实时日志源进度"),
    ):
        item = sub.add_parser(name, help=help_text)
        item.set_defaults(func=func)

    stream_parser = sub.add_parser("stream", help="实时日志源（前台，持续追加 <fm>.log）")
    stream_parser.add_argument("--interval", type=float, default=0.5, help="每行间隔秒数（默认 0.5）")
    stream_parser.add_argument("--lines", type=int, default=None, help="产够 N 行后退出（默认一直跑）")
    stream_parser.set_defaults(func=cmd_stream)

    for name, func, help_text in (
        ("site-serve", cmd_site_serve, "启动模拟 ATLog 用例报告站（前台）"),
        ("site-url", cmd_site_url, "打印用例 URL"),
    ):
        item = sub.add_parser(name, help=help_text)
        item.add_argument("--host", default=fleet.ATLOG_SITE_HOST, help="监听地址（默认 127.0.0.1）")
        item.add_argument("--port", type=int, default=fleet.ATLOG_SITE_PORT, help="监听端口（默认 8901）")
        item.set_defaults(func=func)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
