"""删除环境资源时**只属于它的机器也要一起删掉**（静态契约）。

背景：以前 `delete_environment_resource` 只 `environment.delete()`，机器会变成孤儿 ——
界面上看不见，但再建同名机器会报「机器名已存在」，而且会一直堆在库里（实测：删完环境
`Machine.objects.filter(name=...)` 还在）。这里守住修复后的行为与"共用机器不许删"的护栏。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source() -> str:
    return (ROOT / "backend/apps/tooling/plugins/environment_ops.py").read_text(encoding="utf-8")


def test_delete_environment_also_removes_its_own_machines():
    source = _source()
    assert "removed_machines" in source
    # 删环境**之前**先把机器 id 记下来（关系是 CASCADE，环境一删就查不到）
    assert 'environment.machine_relations.values_list("source_machine_id", "target_machine_id")' in source
    assert "machine.delete()" in source


def test_shared_machines_are_never_deleted():
    """还被别的环境/关系引用的机器不能删（upper_machine 是 PROTECT，删了会直接抛错）。"""
    source = _source()
    assert "Environment.objects.filter(upper_machine=machine).exists()" in source
    assert "MachineRelation.objects.filter(source_machine=machine).exists()" in source
    assert "MachineRelation.objects.filter(target_machine=machine).exists()" in source


def test_tool_description_promises_machine_cleanup():
    source = _source()
    assert "只属于它的机器" in source
