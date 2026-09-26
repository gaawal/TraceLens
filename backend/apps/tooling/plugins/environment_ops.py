"""Environment / resource operation tools for TracePilot.

Adds the three capabilities the assistant was missing around environment work:

* opening the deployment page for an environment (navigation),
* a readable snapshot of a deployment step's process log,
* creating and deleting environment resources (writes, confirmation-gated).

The read-only two are freely callable; the two writes declare ``risk_level`` so the existing
policy layer decides whether a confirmation card is required.
"""
from __future__ import annotations

import logging
from typing import Any

from apps.tooling.plugins.catalog import register_function_tool
from apps.tooling.registry import ToolDefinition

logger = logging.getLogger("tracelens.tooling.plugins.environment_ops")

_ENV_ID = {"type": ["integer", "string"], "description": "TraceLens 环境 ID"}


def _environment(environment_id: Any):
    from apps.tooling.services import _environment as resolve

    return resolve(environment_id)


# ------------------------------------------------------------------ navigation
@register_function_tool(ToolDefinition(
    id="open_environment_deployment",
    name="打开环境部署页面",
    description=(
        "把界面切到指定环境的部署页面（部署参数、预览、历史与执行入口）。"
        "只改变界面位置，不启动部署。"
    ),
    category="界面操作",
    handler=None,
    agent_exposed=True,
    read_only=True,
    risk_level="ui_navigation",
    transport="function",
    domain="navigation",
    kind="navigation",
    skills=("deployment", "environment"),
    input_schema={
        "type": "object",
        "properties": {
            "environment_id": _ENV_ID,
            "open_deployment_dialog": {
                "type": "boolean",
                "default": False,
                "description": "是否顺带打开部署参数对话框（仍不会自动执行）",
            },
        },
        "required": ["environment_id"],
    },
    use_when="用户说“打开部署页面 / 去部署那里 / 看一下部署配置”，但还没有要求实际开始部署时。",
    do_not_use_when="用户要求的是开始或停止部署；那要用 start_environment_deployment / stop_environment_deployment。",
    implementation="apps.tooling.plugins.environment_ops.open_environment_deployment",
))
def open_environment_deployment(payload: dict) -> dict[str, Any]:
    environment = _environment(payload.get("environment_id"))
    open_dialog = bool(payload.get("open_deployment_dialog", False))
    # Mirrors what the resource page already listens for; the page switches to 环境资源 and
    # opens the deployment view from this action.
    return {
        "ok": True,
        "environment_id": environment.id,
        "environment_name": environment.name,
        "ui_action": {
            "type": "open_environment_page",
            "environment_id": environment.id,
            "open_deployment": True,
            "workspace_page": "resources",
        },
        "note": (
            "已打开部署页面。"
            + ("部署参数对话框已展开，但不会自动开始部署，仍需你确认。" if open_dialog else "未开始任何部署。")
        ),
    }


# -------------------------------------------------------------- process log
@register_function_tool(ToolDefinition(
    id="get_deployment_process_log",
    name="查看部署过程日志快照",
    description=(
        "读取一次部署（或其中某一步）的过程日志快照：步骤、状态、命令、stdout/stderr 与过程日志尾部。"
        "用于回答“这次部署卡在哪一步”“报了什么错”。"
    ),
    category="部署操作",
    handler=None,
    agent_exposed=True,
    read_only=True,
    transport="function",
    domain="deployment",
    kind="query",
    skills=("deployment",),
    input_schema={
        "type": "object",
        "properties": {
            "environment_id": _ENV_ID,
            "deployment_id": {"type": ["integer", "string"], "description": "部署任务 ID；不传则取该环境最近一次部署"},
            "step_key": {"type": "string", "default": "", "description": "只看某一步的日志；不传取当前步骤"},
            "max_chars": {"type": "integer", "default": 8000, "minimum": 500, "maximum": 40000},
        },
        "required": ["environment_id"],
    },
    use_when="用户问部署进度、失败步骤、部署报错内容，或需要把部署现场作为证据时。",
    do_not_use_when="用户要查看的是环境运行日志（debug/run/event.log），不是部署过程。",
    implementation="apps.tooling.plugins.environment_ops.get_deployment_process_log",
))
def get_deployment_process_log(payload: dict) -> dict[str, Any]:
    from apps.tooling.services import get_deployment_detail, get_latest_deployment

    environment = _environment(payload.get("environment_id"))
    deployment_id = payload.get("deployment_id")
    resolved_from = "explicit"

    if deployment_id in (None, ""):
        latest = get_latest_deployment({"environment_id": environment.id}) or {}
        deployment = latest.get("deployment") if isinstance(latest.get("deployment"), dict) else latest
        deployment_id = (deployment or {}).get("id") or latest.get("id")
        resolved_from = "latest"
    if deployment_id in (None, ""):
        return {"ok": False, "error": "该环境还没有部署记录。"}

    detail = get_deployment_detail({
        "environment_id": environment.id,
        "deployment_id": deployment_id,
        "step_key": str(payload.get("step_key") or "").strip(),
    }) or {}

    limit = max(500, min(40000, int(payload.get("max_chars") or 8000)))
    steps = detail.get("steps") if isinstance(detail.get("steps"), list) else []
    failed: list[dict[str, Any]] = []
    process_log = ""
    for step in steps:
        if not isinstance(step, dict):
            continue
        entry = {
            "key": str(step.get("key") or ""),
            "name": str(step.get("name") or ""),
            "status": str(step.get("status") or ""),
            "status_label": str(step.get("status_label") or ""),
            "exit_status": step.get("exit_status"),
            "started_at": step.get("started_at") or "",
            "finished_at": step.get("finished_at") or "",
            "message": str(step.get("message") or "")[:400],
        }
        if str(step.get("status") or "") not in {"success", ""}:
            failed.append(entry)
        if step.get("process_log") and len(str(step["process_log"])) > len(process_log):
            process_log = str(step["process_log"])

    # The serializer exposes the selected step's log under one of these keys depending on
    # version; prefer an explicit field and fall back to the longest step log.
    for key in ("process_log", "step_process_log", "log"):
        value = detail.get(key)
        if isinstance(value, str) and len(value) > len(process_log):
            process_log = value

    truncated = len(process_log) > limit
    return {
        "ok": True,
        "environment_id": environment.id,
        "environment_name": environment.name,
        "deployment_id": detail.get("id") or deployment_id,
        "resolved_from": resolved_from,
        "status": str(detail.get("status") or ""),
        "status_label": str(detail.get("status_label") or ""),
        "current_step": str(detail.get("current_step") or ""),
        "target_version": str(detail.get("target_version") or ""),
        "created_at": detail.get("created_at") or "",
        "finished_at": detail.get("finished_at") or "",
        "step_count": len(steps),
        "unfinished_or_failed_steps": failed[:12],
        "process_log_tail": (process_log[-limit:] if truncated else process_log),
        "process_log_chars": len(process_log),
        "truncated": truncated,
    }


# ------------------------------------------------------------ resource writes
@register_function_tool(ToolDefinition(
    id="create_environment_resource",
    name="创建环境资源",
    description=(
        "登记一台上位机并（可选）为其建立 TraceLens 环境。写操作，必须经用户确认。"
    ),
    category="环境操作",
    handler=None,
    agent_exposed=True,
    read_only=False,
    risk_level="low_write",
    transport="function",
    domain="environment",
    kind="mutation",
    skills=("environment", "deployment"),
    input_schema={
        "type": "object",
        "properties": {
            "machine_name": {"type": "string", "description": "上位机显示名"},
            "host": {"type": "string", "description": "IP 或主机名"},
            "ssh_port": {"type": "integer", "default": 22},
            "username": {"type": "string", "default": ""},
            "environment_name": {"type": "string", "default": "", "description": "留空则只建机器、不建环境"},
            "description": {"type": "string", "default": ""},
        },
        "required": ["machine_name", "host"],
    },
    use_when="用户明确要“新增/登记一台环境/机器”，并给出了名称与地址时。",
    do_not_use_when=(
        "用户只是查询或修改已有环境；或者还没确认主机名/IP。"
        "同一台机器只能属于一个环境（upper_machine 是 OneToOne），已绑定时会直接报错而不是覆盖。"
    ),
    validation_rules=(
        "写操作，必须先生成确认卡并取得用户明确确认",
        "host 必须是用户给出的真实地址，不要猜测或用示例值",
    ),
    implementation="apps.tooling.plugins.environment_ops.create_environment_resource",
))
def create_environment_resource(payload: dict) -> dict[str, Any]:
    from apps.environments.models import Environment
    from apps.machines.models import Machine, MachineRole

    machine_name = str(payload.get("machine_name") or "").strip()[:128]
    host = str(payload.get("host") or "").strip()[:255]
    if not machine_name or not host:
        raise ValueError("machine_name / host 不能为空。")
    if Machine.objects.filter(name=machine_name).exists():
        raise ValueError(f"机器名「{machine_name}」已存在，请换一个名字或改用已有机器。")

    try:
        ssh_port = max(1, min(65535, int(payload.get("ssh_port") or 22)))
    except (TypeError, ValueError):
        ssh_port = 22

    machine = Machine.objects.create(
        name=machine_name,
        host=host,
        ssh_port=ssh_port,
        username=str(payload.get("username") or "").strip()[:128],
        role=MachineRole.UPPER,
        description=str(payload.get("description") or "")[:2000],
    )

    environment_name = str(payload.get("environment_name") or "").strip()[:128]
    # `apps.machines.signals.ensure_upper_machine_environment` already creates an Environment
    # for every upper machine (named after it), and `upper_machine` is a OneToOneField — so an
    # explicit create here hits `UNIQUE constraint failed: ...upper_machine_id`.
    # Adopt what the signal made and rename it to what the user asked for.
    environment = Environment.objects.filter(upper_machine=machine).first()
    if environment is None:  # signal disabled or replaced: stay correct anyway
        environment = Environment.objects.create(name=environment_name or machine_name, upper_machine=machine)
    if environment_name and environment.name != environment_name:
        environment.name = environment_name
        environment.save(update_fields=["name", "updated_at"])
    if payload.get("description"):
        environment.description = str(payload["description"])[:2000]
        environment.save(update_fields=["description", "updated_at"])
    logger.info(
        "tooling.environment.created machine=%s host=%s environment=%s",
        machine.pk, host, environment.pk if environment else None,
    )
    return {
        "ok": True,
        "machine_id": machine.pk,
        "machine_name": machine.name,
        "host": machine.host,
        "ssh_port": machine.ssh_port,
        "environment_id": environment.pk if environment else None,
        "environment_name": environment.name if environment else "",
        "note": "已登记机器" + ("并创建环境。" if environment else "（未创建环境）。")
        + " 建议接着做一次 refresh_environment 拉取拓扑，或 query_environment_version 验证连通性。",
    }


@register_function_tool(ToolDefinition(
    id="delete_environment_resource",
    name="删除环境资源",
    description=(
        "删除一个 TraceLens 环境：部署记录、上下位机绑定关系，以及**只属于它的机器**一并移除"
        "（还被别的环境引用的机器不动）。高风险写操作，必须经用户确认。"
    ),
    category="环境操作",
    handler=None,
    agent_exposed=True,
    read_only=False,
    risk_level="high",
    transport="function",
    domain="environment",
    kind="mutation",
    skills=("environment",),
    input_schema={
        "type": "object",
        "properties": {
            "environment_id": _ENV_ID,
            "confirm_name": {
                "type": "string",
                "description": "必须原样填写要删除的环境名，作为二次确认，防止删错",
            },
        },
        "required": ["environment_id", "confirm_name"],
    },
    use_when="用户明确要求删除某个环境，并且已经说出/确认了它的名字。",
    do_not_use_when=(
        "用户只是要停止或删除某次部署（那用 stop_environment_deployment）；"
        "或者环境名对不上时——不要替用户猜。"
    ),
    validation_rules=(
        "高风险写操作，必须先生成确认卡并取得用户明确确认",
        "confirm_name 必须与环境当前名称完全一致，否则拒绝执行",
    ),
    implementation="apps.tooling.plugins.environment_ops.delete_environment_resource",
))
def delete_environment_resource(payload: dict) -> dict[str, Any]:
    from apps.environments.models import Environment, MachineRelation
    from apps.machines.models import Machine

    environment = _environment(payload.get("environment_id"))
    confirm_name = str(payload.get("confirm_name") or "").strip()
    if confirm_name != environment.name:
        raise ValueError(
            f"confirm_name 必须与环境名称完全一致（当前为「{environment.name}」），已拒绝删除。"
        )

    # 这台环境用到的机器：上位机（OneToOne）+ 拓扑关系里的上/下位机。
    # 删环境之前先记下来 —— 关系是 CASCADE，环境一删就查不到了。
    machine_ids = {environment.upper_machine_id} if environment.upper_machine_id else set()
    relations = list(environment.machine_relations.values_list("source_machine_id", "target_machine_id"))
    for source_id, target_id in relations:
        machine_ids.update({source_id, target_id})
    machine_ids.discard(None)

    snapshot = {
        "environment_id": environment.pk,
        "name": environment.name,
        "upper_machine": getattr(environment.upper_machine, "name", ""),
        "deployment_count": environment.deployments.count(),
    }
    environment.delete()

    # 🔴 顺手把**只属于这台环境**的机器也删掉：
    # 以前只删环境，机器会变成孤儿 —— 界面上看不见，但再建同名机器会报「机器名已存在」，
    # 而且会一直堆在库里。还被别的环境/关系引用的机器不动（upper_machine 是 PROTECT）。
    removed_machines: list[dict[str, Any]] = []
    for machine in Machine.objects.filter(pk__in=machine_ids):
        if Environment.objects.filter(upper_machine=machine).exists():
            continue
        if MachineRelation.objects.filter(source_machine=machine).exists() or MachineRelation.objects.filter(target_machine=machine).exists():
            continue
        removed_machines.append({"machine_id": machine.pk, "name": machine.name, "host": machine.host})
        machine.delete()

    logger.warning("tooling.environment.deleted %s machines=%s", snapshot, [item["name"] for item in removed_machines])
    note = "环境及其部署记录已删除。"
    if removed_machines:
        note += f"同时删除了 {len(removed_machines)} 台只属于它的机器：" + "、".join(item["name"] for item in removed_machines) + "。"
    return {"ok": True, "deleted": snapshot, "removed_machines": removed_machines, "note": note}
