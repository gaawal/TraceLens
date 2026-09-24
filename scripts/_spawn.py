#!/usr/bin/env python3
"""把长跑进程放进**独立会话**里启动。

为什么需要它：macOS 没有 setsid(1)，所以 `nohup cmd &` 启动的进程仍然留在调用方的
进程组 / 会话里。调用方（终端、上层脚本、CI）一退出，长跑进程就被一起回收 ——
表现是"服务刚起来就静默死掉"，而 pidfile 却留在原地变成陈旧 pid。

这里用 subprocess.Popen(..., start_new_session=True) 让子进程自成一个会话，
实测存活服务的 pgid == sid == pid，与调用方彻底解耦。

用法：
    python _spawn.py --cwd DIR --log FILE [--pidfile FILE] [--env K=V ...] -- cmd args...
子进程 pid 打到 stdout（唯一一行），日志以追加方式写入 --log 指向的文件。

注意：--log 是普通文件，Python 子进程的 stdout 会变成块缓冲，不落盘就看不到进度。
所以调用方要传 `python -u`，否则排查问题时日志是空的。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _usage(message: str) -> "SystemExit":
    return SystemExit(f"_spawn.py: {message}\n用法：_spawn.py --cwd DIR --log FILE [--pidfile F] [--env K=V] -- cmd args...")


def parse(argv: list[str]) -> tuple[str | None, str, str | None, dict, list[str]]:
    cwd: str | None = None
    log: str | None = None
    pidfile: str | None = None
    envs: dict[str, str] = {}

    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            index += 1
            break
        if token in ("--cwd", "--log", "--pidfile", "--env"):
            if index + 1 >= len(argv):
                raise _usage(f"{token} 后面缺少取值")
            value = argv[index + 1]
            if token == "--cwd":
                cwd = value
            elif token == "--log":
                log = value
            elif token == "--pidfile":
                pidfile = value
            else:
                key, sep, val = value.partition("=")
                if not sep:
                    raise _usage(f"--env 需要 K=V 形式，收到 {value!r}")
                envs[key] = val
            index += 2
            continue
        raise _usage(f"未知参数 {token!r}（要传给子进程的命令请放在 `--` 之后）")

    cmd = argv[index:]
    if not cmd:
        raise _usage("缺少要启动的命令")
    if not log:
        raise _usage("缺少 --log")
    return cwd, log, pidfile, envs, cmd


def main(argv: list[str]) -> int:
    cwd, log, pidfile, envs, cmd = parse(argv)

    if cwd and not Path(cwd).is_dir():
        raise _usage(f"--cwd 不存在：{cwd}")

    log_path = Path(log)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env.update(envs)

    # 追加写、不带缓冲：句柄只作为 fd 交给子进程，子进程自己决定缓冲策略。
    handle = open(log_path, "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        handle.close()

    if pidfile:
        Path(pidfile).parent.mkdir(parents=True, exist_ok=True)
        Path(pidfile).write_text(str(proc.pid), encoding="utf-8")

    # 立刻回收一次，尽早暴露 exec 失败（比如命令不存在），避免"看起来启动了"。
    if proc.poll() is not None:
        print(f"_spawn.py: 子进程立即退出（rc={proc.returncode}），见 {log_path}", file=sys.stderr)
        return 1

    print(proc.pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
