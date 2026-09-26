"""版本查询链路的**契约**：仿真机必须生成真实机器那种版本文件。

背景（用户报的「查询环境版本 → 工具执行失败 / 没有搜到」）：
1. 仿真假 shell 把 ``$HOME``（后端 ``read_text`` 用它读 ``~/SW/version``）映射到了机器根，
   于是永远 "No such file or directory" —— 这是**仿真自己的 bug**，修在仿真里；
2. 仿真写的版本文件是**一行裸版本号**，而后端 ``parse_version_text()`` 只认
   ``Current Version: <版本>`` 标记行 —— 同样是仿真没对齐真实机器，所以仿真改成带标记的格式，
   后端的查询逻辑**一行都不动**。

这里守住两件事：仿真 shell 的 ``$HOME`` 语义、以及仿真版本文件能被后端原始解析器读出来。
"""

from __future__ import annotations

from datetime import datetime, timezone

from apps.environments.services.discovery import parse_version_text


def _shell(role: str):
    from simremote import fleet
    from simremote.shell import RemoteShell

    spec = [machine for machine in fleet.FLEET if machine.role == role][0]
    return RemoteShell(spec)


def _version_plan(role: str) -> bytes:
    """跑一遍 simremote 的计划生成，取出它准备写进 ``~/SW/version`` 的字节。"""
    from simremote import fleet, loggen

    spec = [machine for machine in fleet.FLEET if machine.role == role][0]
    plan = loggen.plan_machine(spec, now=datetime.now(timezone.utc))
    for _root, relative, payload, _count in plan:
        if str(relative) == "SW/version":
            return payload
    raise AssertionError("plan_machine 没有生成 SW/version")


# --- 仿真 shell：$HOME 必须等于 ~ -------------------------------------------

def test_sim_shell_maps_dollar_home_to_the_user_home():
    """``$HOME`` 与 ``~`` 指向同一个目录（后端 read_text 用的正是 ``"$HOME"/…``）。"""
    for role in ("upper", "lower"):
        shell = _shell(role)
        home = str(shell.home)
        assert shell.rewrite_command('cat -- "$HOME"/SW/version').endswith(f"{home}/SW/version")
        assert shell.rewrite_command("cat -- $HOME/SW/version").endswith(f"{home}/SW/version")
        assert shell.rewrite_command("cat -- ~/SW/version").endswith(f"{home}/SW/version")
        # 远端绝对路径仍然映射到机器根（映射结果里 `<root>//log/...` 多一个斜杠是既有写法）。
        mapped = shell.rewrite_command("ls /log/tracepilot/debug").replace("//", "/")
        assert mapped.endswith(f"{shell.root}/log/tracepilot/debug")


def test_sim_shell_can_actually_read_the_version_file():
    """真读一遍：这条曾经是 No such file（用户看到的"工具执行失败"）。"""
    from simremote import fleet

    for role in ("upper", "lower"):
        result = _shell(role).execute('cat -- "$HOME"/SW/version')
        assert result.status == 0, f"{role} 读不到版本文件：{result.stderr.decode().strip()}"
        assert parse_version_text(result.stdout.decode()) == fleet.SOFTWARE_VERSION


# --- 版本文件格式：仿真写的内容必须能被后端**原始**解析器读出来 ---------------

def test_sim_writes_a_version_file_the_backend_parser_accepts():
    """仿真生成的版本文件带 ``Current Version:`` 标记（真实机器就是这种格式）。"""
    from simremote import fleet

    payload = _version_plan("upper").decode("utf-8")
    assert "Current Version:" in payload, f"仿真版本文件缺少标记行：{payload!r}"
    assert parse_version_text(payload) == fleet.SOFTWARE_VERSION


def test_backend_parser_stays_strict():
    """后端解析逻辑保持原样：只认标记行，裸版本号/说明文字/日期都不算（仿真去对齐它）。"""
    assert parse_version_text("Current Version: SPM-V2026.09.23") == "SPM-V2026.09.23"
    assert parse_version_text("Current XY Version: XY V100.2") == "XY V100.2"
    assert parse_version_text("SPM-V2026.09.23") == ""
    assert parse_version_text("Release Notes\nREADME") == ""
