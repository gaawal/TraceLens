"""版本查询链路：解析、容错、给模型的文案。

背景（用户报的 "查询环境版本 → 工具执行失败 / 没有搜到"）：两个原因叠在一起 ——
1. 仿真机的假 shell 把 ``$HOME`` 映射到了**机器根**而不是用户家目录，于是后端
   ``cat -- "$HOME"/SW/version`` 永远 "No such file or directory"；
2. 后端解析器只认 ``Current Version:`` 标记行，而仿真机的版本文件就是一行裸版本号；
   再加上上位机读取失败会让整个工具抛异常 → AI 只看到"工具执行失败"。

这里守住修复后的行为：仿真 shell 的 ``$HOME`` 映射、裸版本号兜底、读取失败不再是异常、
以及失败原因必须出现在给模型的文案里。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.environments.services.discovery import parse_version_text, read_environment_versions
from apps.tooling.assistant_runtime.results import compact_environment_versions


# --- 解析 -------------------------------------------------------------------

def test_marker_lines_still_win():
    assert parse_version_text("Current Version: SPM-V2026.09.23") == "SPM-V2026.09.23"
    assert parse_version_text("Current XY Version: XY V100.2") == "XY V100.2"
    # 多行时优先有标记的那行，别被其它行抢走。
    assert parse_version_text("build 2026-09-26\nCurrent Version: SPM-V2026.09.23\n") == "SPM-V2026.09.23"


def test_bare_version_file_is_accepted():
    """仿真机 `~/SW/version` 就是一行裸版本号，以前会被判成"没找到标记"。"""
    assert parse_version_text("SPM-V2026.09.23\n") == "SPM-V2026.09.23"
    assert parse_version_text("  SPM-V2026.09.23  ") == "SPM-V2026.09.23"


def test_bare_fallback_does_not_grab_junk():
    """兜底必须保守：说明文字、日期、多词行都不能当成版本。"""
    for content in ("Release Notes\nREADME", "2026-09-26", "", "build failed see log\n", "no version here"):
        assert parse_version_text(content) == "", f"{content!r} 不该被解析出主版本"


# --- 上/下位机查询容错 -------------------------------------------------------

class _EmptyRelations:
    def select_related(self, *args, **kwargs):
        return self

    def filter(self, **kwargs):
        return []


def _environment(**overrides):
    base = {
        "id": 2,
        "name": "SIM-EUV-01",
        "software_version": "SPM-V2026.09.23",
        "version_checked_at": None,
        "machine_relations": _EmptyRelations(),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _settings():
    return SimpleNamespace(version_file_path="~/SW/version")


def test_upper_read_failure_is_reported_not_raised(monkeypatch):
    """上位机读不到版本文件时：保留上次已知版本 + 带出原因，而不是抛异常。"""

    def boom(*args, **kwargs):
        raise RuntimeError("cat: .../SW/version: No such file or directory")

    monkeypatch.setattr("apps.environments.services.discovery.read_software_version", boom)
    data = read_environment_versions(_environment(), _settings())

    assert data["version"] == "SPM-V2026.09.23", "读不到就退回上一次已知版本"
    assert data["upper_error"], "必须带出失败原因"
    assert "No such file" in data["upper_error"]
    assert data["message"].startswith("上位机版本文件读取失败")
    assert data["lower_versions"] == []


def test_successful_upper_read_has_no_error(monkeypatch):
    monkeypatch.setattr(
        "apps.environments.services.discovery.read_software_version",
        lambda environment, settings_obj: "SPM-V2026.09.23",
    )
    data = read_environment_versions(_environment(), _settings())
    assert data["version"] == "SPM-V2026.09.23"
    assert data["upper_error"] == "" and data["message"] == ""


# --- 给模型的文案 -----------------------------------------------------------

def test_compact_text_tells_the_model_why_it_failed():
    """空值 ≠ 不存在：读不到时必须把原因交给模型，否则它会答"环境没有版本"。"""
    text = compact_environment_versions({
        "version": "",
        "version_mismatch": False,
        "checked_at": None,
        "lower_versions": [],
        "upper_error": "读取远程文件失败 ~/SW/version：No such file or directory",
    })
    assert "读取问题=" in text
    assert "No such file" in text


def test_compact_text_has_no_problem_line_on_success():
    text = compact_environment_versions({
        "version": "SPM-V2026.09.23",
        "version_mismatch": False,
        "checked_at": "2026-09-26T03:10:44Z",
        "lower_versions": [],
    })
    assert text.startswith("上位机版本=SPM-V2026.09.23")
    assert "读取问题" not in text


# --- 仿真 shell 的 $HOME 映射 -------------------------------------------------

def _shell(role: str):
    from simremote import fleet
    from simremote.shell import RemoteShell

    spec = [machine for machine in fleet.FLEET if machine.role == role][0]
    return RemoteShell(spec)


@pytest.mark.parametrize("role", ["upper", "lower"])
def test_sim_shell_maps_dollar_home_to_the_user_home(role):
    """``$HOME`` 必须等于 ``~``：后端的 read_text 正是用 ``"$HOME"/SW/version`` 读版本文件的。"""
    shell = _shell(role)
    home = str(shell.home)
    assert shell.rewrite_command('cat -- "$HOME"/SW/version').endswith(f"{home}/SW/version")
    assert shell.rewrite_command("cat -- $HOME/SW/version").endswith(f"{home}/SW/version")
    assert shell.rewrite_command("cat -- ~/SW/version").endswith(f"{home}/SW/version")
    # 远端绝对路径仍然映射到机器根，别被家目录替换带偏。
    # （映射结果是 `<root>//log/...`，多一个斜杠是既有写法，路径语义不变，这里一并归一后比较。）
    mapped = shell.rewrite_command("ls /log/tracepilot/debug").replace("//", "/")
    assert mapped.endswith(f"{shell.root}/log/tracepilot/debug")


def test_sim_shell_can_actually_read_the_version_file():
    """真读一遍：这条曾经是 No such file（就是用户看到的"工具执行失败"）。"""
    from simremote import fleet

    for role in ("upper", "lower"):
        result = _shell(role).execute('cat -- "$HOME"/SW/version')
        assert result.status == 0, f"{role} 读不到版本文件：{result.stderr.decode().strip()}"
        assert result.stdout.decode().strip() == fleet.SOFTWARE_VERSION
