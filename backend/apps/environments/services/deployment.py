from __future__ import annotations

import json
import logging
import queue
import re
import shlex
import threading
from concurrent.futures import ThreadPoolExecutor
from ipaddress import ip_address
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.common.services.ssh import SSH_SESSION_POOL, build_ssh_client, execute as ssh_execute, read_text, ssh_session
from apps.machines.models import AuthenticationType, Machine, MachineRole
from apps.environments.models import (
    DeploymentStatus,
    DeploymentStep,
    DeploymentStepStatus,
    Environment,
    EnvironmentDeployment,
    ResourceSettings,
)
from apps.environments.services.stations_parser import parse_stations_xml
from apps.environments.services.deployment_events import DeploymentEventBus

logger = logging.getLogger("tracelens.environment_deployment")
process_logger = logging.getLogger("tracelens.process.deployment")

_SENSITIVE_PROCESS_RE = re.compile(r"(?i)(api[_-]?key|password|passwd|token|secret)(\s*[=:]\s*)([^\s]+)")


def _safe_process_line(value: str) -> str:
    text = str(value or "").replace("\r", "").strip("\n")
    text = _SENSITIVE_PROCESS_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
    return text[:4000]


def _step_parameter_payload(deployment: EnvironmentDeployment, step_key: str) -> dict:
    config = deployment.configuration if isinstance(deployment.configuration, dict) else {}
    key = str(step_key or "").strip()
    gpb_ips = [str(item).strip() for item in (config.get("gpb_ips") or deployment.gpb_ips or []) if str(item).strip()]
    if key == STEP_STOP:
        return {
            "upper_ip": config.get("upper_ip") or deployment.upper_ip,
            "precheck_stop_lower": bool(config.get("precheck_stop_lower", False)),
        }
    if key == STEP_DEPLOY:
        return {
            "task_name": config.get("task_name") or deployment.task_name,
            "target_version": config.get("target_version") or deployment.target_version,
            "simulation_mode": config.get("simulation_mode") or deployment.simulation_mode,
            "include_sdk": bool(config.get("include_sdk", deployment.include_sdk)),
            "upper_ip": config.get("upper_ip") or deployment.upper_ip,
        }
    if key == STEP_TB:
        return {
            "tb_mode": config.get("tb_mode") or deployment.tb_mode,
            "upper_ip": config.get("upper_ip") or deployment.upper_ip,
            "gpb_ips": gpb_ips,
        }
    if key == STEP_INSTALL:
        payload = {
            "install_mode": config.get("install_mode") or deployment.install_mode,
            "install_port": config.get("install_port", deployment.install_port),
            "upper_ip": config.get("upper_ip") or deployment.upper_ip,
            "gpb_ips": gpb_ips,
        }
        if bool(config.get("include_dhh", deployment.include_dhh)):
            payload.update({
                "dhh_ip": config.get("dhh_ip") or deployment.dhh_ip,
                "dhh_user": config.get("dhh_user") or deployment.dhh_user,
                "dhh_machine_id": config.get("dhh_machine_id") or deployment.dhh_machine_id,
            })
        return payload
    if key == STEP_PRESTART_STOP:
        return {"gpb_ips": gpb_ips}
    if key == STEP_START:
        return {
            "simulation_mode": config.get("simulation_mode") or deployment.simulation_mode,
            "upper_ip": config.get("upper_ip") or deployment.upper_ip,
            "gpb_ips": gpb_ips,
            "display_env": config.get("display_env") or "",
        }
    if key == STEP_POST_START:
        script = str(config.get("post_start_script") or "").strip()
        return {"has_script": bool(script), "script": script[:1200]}
    return {}


def _step_parameter_log(deployment: EnvironmentDeployment, step_key: str) -> str:
    return _safe_process_line(json.dumps(_step_parameter_payload(deployment, step_key), ensure_ascii=False, default=str))


def _deployment_trace_log(deployment: EnvironmentDeployment) -> tuple[str, str, str]:
    config = deployment.configuration if isinstance(deployment.configuration, dict) else {}
    trace = config.get("_trace_context") if isinstance(config.get("_trace_context"), dict) else {}
    return (
        str(trace.get("operator") or "-").strip() or "-",
        str(trace.get("client_ip") or "-").strip() or "-",
        str(trace.get("source") or "-").strip() or "-",
    )


DEPLOYMENT_TIME_SYNC_TOLERANCE_SECONDS = 1

STEP_STOP = "stop"
STEP_DEPLOY = "deploy"
STEP_TB = "tb_deploy"
STEP_INSTALL = "install"
STEP_PRESTART_STOP = "prestart_stop"
STEP_START = "start"
STEP_POST_START = "post_start_script"
DEFAULT_SINGLE_GPB_TB_MODE = "10GPB_SPLIT_1-3"
SINGLE_GPB_TB_MODE_RE = re.compile(r"^[1-9]\d*GPB_SPLIT_[1-9]\d*-[1-9]\d*$", re.IGNORECASE)
REAL_GPB_SLOT_RE = re.compile(r"(?:^|_)SLOT(\d+)(?:_|$)", re.IGNORECASE)
REAL_GPB_SLOT_COUNT = 10
LOWER_RUNTIME_PROBE_COMMAND = "ps -ef | grep '[t]b_simulator' || true"

STEP_NAMES = {
    STEP_STOP: "停止上位机进程",
    STEP_DEPLOY: "拉取软件包",
    STEP_TB: "安装仿真",
    STEP_INSTALL: "安装软件包",
    STEP_PRESTART_STOP: "停止下位机进程",
    STEP_START: "启动环境",
    STEP_POST_START: "启动后脚本",
}

DEPLOYMENT_STEP_KEYS = (STEP_STOP, STEP_DEPLOY, STEP_TB, STEP_INSTALL, STEP_START, STEP_POST_START)


@dataclass(frozen=True)
class DeploymentCommand:
    key: str
    name: str
    command: str
    success_marker: str
    timeout_seconds: int = 3600
    skipped: bool = False
    auto: bool = False


class DeploymentStopRequested(RuntimeError):
    """Stop signal raised after the current deployment script is terminated or observed stopped."""


def _deployment_stop_requested(deployment_id: int) -> bool:
    return EnvironmentDeployment.objects.filter(
        pk=deployment_id,
        status=DeploymentStatus.STOPPING,
    ).exists()


def _raise_if_deployment_stop_requested(deployment_id: int) -> None:
    if _deployment_stop_requested(deployment_id):
        raise DeploymentStopRequested("部署任务已请求停止。")


def _finish_deployment_stopped(deployment_id: int, *, orphaned: bool = False) -> EnvironmentDeployment:
    """Finalize a stopped task without interrupting a live SSH command.

    ``orphaned`` is used after a backend restart when the database still says a
    step is running but no deployment executor heartbeat exists anymore.
    """
    now = timezone.now()
    with transaction.atomic():
        deployment = EnvironmentDeployment.objects.select_for_update().get(pk=deployment_id)
        pending_steps = DeploymentStep.objects.select_for_update().filter(
            deployment=deployment,
            status=DeploymentStepStatus.PENDING,
        )
        for step in pending_steps:
            step.status = DeploymentStepStatus.SKIPPED
            step.started_at = step.started_at or now
            step.finished_at = now
            step.message = "任务已停止，未执行此步骤。"
            step.save(update_fields=["status", "started_at", "finished_at", "message", "updated_at"])
        if orphaned:
            for step in DeploymentStep.objects.select_for_update().filter(
                deployment=deployment,
                status=DeploymentStepStatus.RUNNING,
            ):
                step.status = DeploymentStepStatus.STOPPED
                step.finished_at = now
                step.message = "后台部署执行器已不存在，任务已停止。"
                step.save(update_fields=["status", "finished_at", "message", "updated_at"])
        deployment.status = DeploymentStatus.STOPPED
        deployment.finished_at = now
        was_scheduled = deployment.started_at is None and deployment.scheduled_at is not None
        deployment.message = (
            "定时部署任务已取消。"
            if was_scheduled
            else (
                "部署任务已停止；未检测到活动部署执行器，可能后端服务已重启。"
                if orphaned
                else "部署任务已停止；当前部署脚本已终止，后续阶段未执行。"
            )
        )
        deployment.save(update_fields=["status", "finished_at", "message", "updated_at"])
    _publish_deployment_state(deployment.id)
    return deployment


def request_stop_deployment(environment: Environment, deployment_id: int) -> EnvironmentDeployment:
    """Terminate this task's current remote script, or close a stale task after restart."""
    runner_alive: bool | None = None
    with transaction.atomic():
        try:
            deployment = EnvironmentDeployment.objects.select_for_update().get(
                pk=deployment_id,
                environment=environment,
            )
        except EnvironmentDeployment.DoesNotExist as exc:
            raise ValueError("部署记录不存在。") from exc

        if deployment.status == DeploymentStatus.STOPPED:
            return deployment
        if deployment.status not in {DeploymentStatus.SCHEDULED, DeploymentStatus.PENDING, DeploymentStatus.RUNNING, DeploymentStatus.STOPPING}:
            raise ValueError("只有待执行、等待中、部署中或停止中的任务可以停止。")

        if deployment.status == DeploymentStatus.SCHEDULED:
            deployment.status = DeploymentStatus.STOPPING
            deployment.message = "正在取消定时部署任务。"
            deployment.save(update_fields=["status", "message", "updated_at"])
            runner_alive = False
        else:
            runner_alive = DeploymentEventBus.runner_alive(deployment.id)
            # A PENDING task has not started a business deployment stage yet. If Redis
            # is unavailable, treating it as not yet owned is safer than leaving it
            # stuck in STOPPING forever; a worker racing this transition will observe
            # STOPPING/STOPPED before it executes remote deployment commands.
            if deployment.status == DeploymentStatus.PENDING and runner_alive is None:
                runner_alive = False
        if deployment.status == DeploymentStatus.STOPPING and runner_alive is not False:
            return deployment

        deployment.status = DeploymentStatus.STOPPING
        deployment.message = (
            "未检测到活动部署执行器，正在直接收口停止状态。"
            if runner_alive is False
            else "已请求停止；正在终止当前部署脚本，后续阶段将不再执行。"
        )
        deployment.save(update_fields=["status", "message", "updated_at"])

    _publish_deployment_state(deployment.id)
    if runner_alive is False:
        return _finish_deployment_stopped(deployment.id, orphaned=True)
    if runner_alive is True:
        try:
            deployment = EnvironmentDeployment.objects.select_related("environment__upper_machine").get(pk=deployment.id)
            _terminate_remote_running_steps(deployment)
        except Exception:
            logger.exception("failed to terminate deployment remote process deployment=%s", deployment.id)
    return deployment


def _normal_relations(environment: Environment):
    relations = list(environment.machine_relations.select_related("target_machine").filter(is_active=True))
    normal = []
    dhh = None
    for relation in relations:
        machine = relation.target_machine
        metadata = relation.metadata or {}
        station_name = str(metadata.get("station_name") or machine.station_name or machine.name or "").strip().lower()
        station_type = str(metadata.get("station_type") or machine.station_type or "").strip().upper()
        if station_name == "dhh" or station_type == "DHH":
            dhh = machine
        else:
            normal.append(machine)
    return normal, dhh


def _real_gpb_slot_entries(environment: Environment) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    seen_slots: set[int] = set()
    relations = list(environment.machine_relations.select_related("target_machine").filter(is_active=True))
    for relation in relations:
        machine = relation.target_machine
        metadata = relation.metadata or {}
        station_name = str(metadata.get("station_name") or machine.station_name or machine.name or "").strip()
        station_type = str(metadata.get("station_type") or machine.station_type or "").strip().upper()
        if station_name.lower() == "dhh" or station_type == "DHH":
            continue
        matched = REAL_GPB_SLOT_RE.search(station_name)
        if not matched:
            continue
        slot = int(matched.group(1))
        if not 1 <= slot <= REAL_GPB_SLOT_COUNT or slot in seen_slots:
            continue
        seen_slots.add(slot)
        entries.append({"slot": slot, "ip": machine.host, "station_name": station_name})
    return sorted(entries, key=lambda item: int(item["slot"]))


def _real_install_slot_ips(environment: Environment, gpb_ips: list[str]) -> list[str]:
    selected = {str(item).strip() for item in gpb_ips if str(item).strip()}
    entries = _real_gpb_slot_entries(environment)
    slot_by_ip = {str(item["ip"]): int(item["slot"]) for item in entries}
    # sim0_real 按实际部署命令下发，不阻断 GPB 与 SLOT 映射校验。
    # stations.xml 解析出的槽位仅用于生成已识别槽位参数。
    values = ["none"] * REAL_GPB_SLOT_COUNT
    for host in selected:
        slot = slot_by_ip.get(host)
        if slot and 1 <= slot <= REAL_GPB_SLOT_COUNT:
            values[slot - 1] = host
    return values


def _recent_versions(environment: Environment) -> list[str]:
    values: list[str] = []
    current = str(environment.software_version or "").strip()
    if current:
        values.append(current)
    for version in (
        environment.deployments.exclude(target_version="")
        .order_by("-created_at")
        .values_list("target_version", flat=True)[:12]
    ):
        version = str(version or "").strip()
        if version and version not in values:
            values.append(version)
    return values


def _saved_post_start_scripts(environment: Environment) -> list[str]:
    values: list[str] = []
    for raw in environment.deployment_scripts or []:
        script = str(raw or "").strip()
        if script and script not in values:
            values.append(script)
        if len(values) >= 20:
            break
    return values


def _remember_post_start_script(environment: Environment, script: str) -> None:
    script = str(script or "").strip()
    if not script:
        return
    values = [script]
    for raw in environment.deployment_scripts or []:
        value = str(raw or "").strip()
        if value and value != script and value not in values:
            values.append(value)
        if len(values) >= 20:
            break
    environment.deployment_scripts = values[:20]
    environment.save(update_fields=["deployment_scripts", "updated_at"])


def _mode_required_gpb_count(mode: str) -> int:
    match = re.match(r"^(\d+)GPB(?:_|$)", str(mode or "").strip(), re.IGNORECASE)
    if not match:
        raise ValueError("GPB 部署模式格式无效，模式必须以 GPB 数量开头。")
    count = int(match.group(1))
    if count <= 0:
        raise ValueError("GPB 部署模式中的数量必须大于 0。")
    return count


def _default_gpb_mode(count: int) -> str:
    if count == 2:
        return "2GPB_PO"
    return f"{max(1, count)}GPB_TB"


def _install_mode_for_gpb_mode(mode: str) -> str:
    """Map a selected deployment/TB mode to the install.sh mode token."""
    value = str(mode or "").strip()
    if value.upper() == "4GPB_TB_UP":
        return "4GPB_TB"
    return value


def _available_gpb_modes(environment: Environment, detected_count: int) -> list[str]:
    # Common values are suggestions only: the frontend also allows free-form
    # modes as long as they begin with an explicit nGPB count.
    modes = [_default_gpb_mode(count) for count in range(1, 11)]
    # 4GPB_TB_UP has the same topology/count requirements as 4GPB_TB.
    # It is kept as the TB-stage token; install.sh is normalized to 4GPB_TB.
    if "4GPB_TB_UP" not in modes:
        insert_at = modes.index("4GPB_TB") + 1 if "4GPB_TB" in modes else len(modes)
        modes.insert(insert_at, "4GPB_TB_UP")
    if detected_count > 10:
        modes.append(_default_gpb_mode(detected_count))
    historical = (
        environment.deployments.exclude(install_mode="")
        .order_by("-created_at")
        .values_list("install_mode", flat=True)[:20]
    )
    for raw in historical:
        mode = str(raw or "").strip()
        if not mode or mode in modes or mode.upper() == "2GPB_TB":
            continue
        try:
            _mode_required_gpb_count(mode)
        except ValueError:
            continue
        modes.append(mode)
    return modes


def _parse_install_port(value) -> int | None:
    try:
        port = int(str(value or "").strip())
    except (TypeError, ValueError):
        return None
    return port if 0 <= port <= 11 else None


def _resolve_install_port(environment: Environment) -> int:
    """Resolve install.sh port from the current stations.xml root userId.

    The live XML is authoritative. The value saved during the latest topology scan is
    only a fallback so a transient XML read failure does not make the deployment UI
    unusable.
    """
    settings_obj = ResourceSettings.get_solo()
    try:
        content = read_text(environment.upper_machine, settings_obj.station_xml_path, max_bytes=1_000_000)
        topology = parse_stations_xml(content)
        port = _parse_install_port(topology.user_id)
        if port is not None:
            if str(environment.station_user_id or "").strip() != str(topology.user_id or "").strip():
                environment.station_user_id = str(topology.user_id or "").strip()
                environment.save(update_fields=["station_user_id", "updated_at"])
            return port
        logger.warning(
            "environment deployment stations userId invalid environment=%s value=%s",
            environment.id, topology.user_id,
        )
    except Exception as exc:
        logger.warning(
            "environment deployment stations port read failed environment=%s path=%s error=%s",
            environment.id, settings_obj.station_xml_path, exc,
        )
    saved = _parse_install_port(environment.station_user_id)
    return saved if saved is not None else 0


def _read_deployment_display(environment: Environment) -> str:
    """Read DISPLAY from the same upper-machine shell environment used for deployment."""
    try:
        # Use an interactive bash so ~/.bashrc is evaluated the same way as a normal
        # login terminal. Some environments return early from ~/.bashrc for
        # non-interactive shells, which would otherwise leave DISPLAY empty.
        marker = "__TRACELENS_DISPLAY__="
        exit_status, stdout, _stderr = _capture_command(
            environment.upper_machine,
            "bash -ic 'printf \"__TRACELENS_DISPLAY__=%s\\n\" \"$DISPLAY\"'",
            10,
        )
        if exit_status == 0:
            for line in reversed(str(stdout or "").splitlines()):
                if line.startswith(marker):
                    return line[len(marker):].strip()
    except Exception as exc:
        logger.warning("deployment DISPLAY read failed environment=%s error=%s", environment.id, exc)
    return ""


def build_defaults(environment: Environment) -> dict:
    lowers, dhh = _normal_relations(environment)
    gpb_ips = [machine.host for machine in lowers]
    count = len(gpb_ips)
    gpb_mode = _default_gpb_mode(count)
    return {
        "environment_id": environment.id,
        "task_name": f"DEPLOY-{environment.id}-{timezone.localtime().strftime('%Y%m%d%H%M%S')}",
        "target_version": str(environment.software_version or "").strip(),
        "recent_versions": _recent_versions(environment),
        "post_start_script": "",
        "saved_post_start_scripts": _saved_post_start_scripts(environment),
        "save_post_start_script": False,
        "simulation_mode": "sim0_sil",
        "include_sdk": True,
        "precheck_stop_lower": False,
        "upper_ip": environment.upper_machine.host,
        "available_gpb_ips": gpb_ips,
        "available_gpb_modes": _available_gpb_modes(environment, count),
        "available_gpb_slots": _real_gpb_slot_entries(environment),
        "gpb_ips": gpb_ips,
        "gpb_mode": gpb_mode,
        "tb_mode": DEFAULT_SINGLE_GPB_TB_MODE if count <= 1 else gpb_mode,
        "install_mode": gpb_mode,
        "required_gpb_count": max(1, count),
        "install_port": _resolve_install_port(environment),
        "display_env": _read_deployment_display(environment),
        "include_dhh": False,
        "dhh_ip": dhh.host if dhh else "",
        "dhh_user": "root",
        "dhh_machine_id": "TESTSPM",
    }


def _validation_defaults(environment: Environment) -> dict:
    """Build request-validation defaults without any remote SSH I/O.

    deployment-defaults is the one endpoint allowed to refresh stations.xml/DISPLAY.
    Preview/start requests already carry those values from the UI and must remain fast,
    so their fallback values come only from persisted topology metadata.
    """
    lowers, dhh = _normal_relations(environment)
    gpb_ips = [machine.host for machine in lowers]
    count = len(gpb_ips)
    gpb_mode = _default_gpb_mode(count)
    saved_port = _parse_install_port(environment.station_user_id)
    return {
        "environment_id": environment.id,
        "task_name": f"DEPLOY-{environment.id}-{timezone.localtime().strftime('%Y%m%d%H%M%S')}",
        "target_version": str(environment.software_version or "").strip(),
        "recent_versions": _recent_versions(environment),
        "post_start_script": "",
        "saved_post_start_scripts": _saved_post_start_scripts(environment),
        "save_post_start_script": False,
        "simulation_mode": "sim0_sil",
        "include_sdk": True,
        "precheck_stop_lower": False,
        "upper_ip": environment.upper_machine.host,
        "available_gpb_ips": gpb_ips,
        "available_gpb_modes": _available_gpb_modes(environment, count),
        "available_gpb_slots": _real_gpb_slot_entries(environment),
        "gpb_ips": gpb_ips,
        "gpb_mode": gpb_mode,
        "tb_mode": DEFAULT_SINGLE_GPB_TB_MODE if count <= 1 else gpb_mode,
        "install_mode": gpb_mode,
        "required_gpb_count": max(1, count),
        "install_port": saved_port if saved_port is not None else 0,
        "display_env": "",
        "include_dhh": False,
        "dhh_ip": dhh.host if dhh else "",
        "dhh_user": "root",
        "dhh_machine_id": "TESTSPM",
    }


def _quote(value: str) -> str:
    return shlex.quote(str(value))


def _double_quote(value: str) -> str:
    text = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    return f'"{text}"'


def _normalize_version_text(value: str) -> str:
    # Deployment output may wrap a version in quotes or use different spacing/labels.
    # Match the user-selected version itself instead of binding to a product-specific label.
    return re.sub(r"[\s\"\'`]+", " ", str(value or "")).strip().casefold()


def _output_contains_target_version(output: str, target_version: str) -> bool:
    target = _normalize_version_text(target_version)
    return bool(target) and target in _normalize_version_text(output)


def _command_overrides(payload: dict) -> dict[str, str]:
    raw = payload.get("commands")
    if raw in (None, ""):
        return {}
    if not isinstance(raw, list):
        raise ValueError("执行预览命令格式无效。")
    known = {STEP_STOP, STEP_DEPLOY, STEP_TB, STEP_INSTALL, STEP_START, STEP_POST_START}
    overrides: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("执行预览命令格式无效。")
        key = str(item.get("key") or "").strip()
        if key not in known:
            raise ValueError(f"未知部署阶段：{key or '空'}")
        command = str(item.get("command") or "").strip()
        if "\x00" in command or len(command) > 20000:
            raise ValueError(f"{STEP_NAMES[key]} 的执行命令格式无效。")
        overrides[key] = command
    return overrides


def build_commands(payload: dict) -> list[DeploymentCommand]:
    task_name = str(payload["task_name"]).strip()
    target_version = str(payload["target_version"]).strip()
    simulation_mode = str(payload["simulation_mode"]).strip()
    include_sdk = bool(payload.get("include_sdk", False))
    precheck_stop_lower = bool(payload.get("precheck_stop_lower", False))
    upper_ip = str(payload["upper_ip"]).strip()
    gpb_ips = [str(item).strip() for item in payload.get("gpb_ips") or [] if str(item).strip()]
    gpb_csv = ",".join(gpb_ips)
    gpb_mode = str(payload.get("gpb_mode") or payload.get("install_mode") or "").strip()
    tb_mode = str(payload.get("tb_mode") or gpb_mode).strip()
    install_mode = str(payload.get("install_mode") or _install_mode_for_gpb_mode(gpb_mode)).strip()
    install_port = int(payload.get("install_port", 0))

    if simulation_mode == "sim0_real":
        deploy_command = f"cd ~ && sh deploy.sh {_double_quote(target_version)} sim0_real"
    else:
        deploy_parts = [
            "cd ~ && sh ./deploy.sh",
            _quote(task_name),
            _quote(simulation_mode),
            _quote(target_version),
        ]
        if include_sdk:
            deploy_parts.append("SDK")
        deploy_command = " ".join(deploy_parts)

    initial_stop_command = "cd ~/SW && stop.sh -les" if precheck_stop_lower else "cd ~/SW && stop.sh -ls"
    commands = [
        DeploymentCommand(STEP_STOP, STEP_NAMES[STEP_STOP], initial_stop_command, "exit=0", timeout_seconds=900),
        DeploymentCommand(STEP_DEPLOY, STEP_NAMES[STEP_DEPLOY], deploy_command, "Successfully. + target version", timeout_seconds=5400),
    ]
    if simulation_mode == "sim0_sil":
        commands.append(DeploymentCommand(
            STEP_TB,
            STEP_NAMES[STEP_TB],
            f"cd ~ && sh ~/SW2/lch/bin/testbench/tb_deploy.sh {_quote(tb_mode)} {_quote(upper_ip)} {_quote(gpb_csv)} InnerSILPkg",
            f"TestBench deploy {tb_mode} successfully!",
            timeout_seconds=3600,
        ))
    else:
        commands.append(DeploymentCommand(STEP_TB, STEP_NAMES[STEP_TB], "", f"{simulation_mode} 跳过", skipped=True))

    if simulation_mode == "sim0_real":
        real_slot_ips = [str(item).strip() or "none" for item in payload.get("real_slot_ips") or []]
        if len(real_slot_ips) != REAL_GPB_SLOT_COUNT:
            raise ValueError(f"sim0_real 安装参数必须包含 {REAL_GPB_SLOT_COUNT} 个槽位。")
        install_command = (
            f"cd ~ && echo 'y' | ./SW2/script/install.sh {_quote(upper_ip)} {_quote(','.join(real_slot_ips))} "
            f"{install_port} --lchUser=root machine"
        )
    else:
        install_command = (
            f"cd ~ && echo 'y' | sh ~/SW2/script/install.sh {_quote(upper_ip)} {_quote(gpb_csv)} "
            f"{install_port} --lchUser=root {_quote(install_mode)}"
        )
        if bool(payload.get("include_dhh")):
            dhh_ip = str(payload.get("dhh_ip") or "").strip()
            dhh_user = str(payload.get("dhh_user") or "root").strip()
            dhh_machine_id = str(payload.get("dhh_machine_id") or "TESTSPM").strip()
            install_command += (
                f" {_quote(f'--dhhIp={dhh_ip}')}"
                f" {_quote(f'--dhhUser={dhh_user}')}"
                f" {_quote(f'--machineId={dhh_machine_id}')}"
            )
    commands.append(DeploymentCommand(
        STEP_INSTALL,
        STEP_NAMES[STEP_INSTALL],
        install_command,
        "退出码 0",
        timeout_seconds=5400,
    ))
    commands.append(DeploymentCommand(
        STEP_PRESTART_STOP,
        STEP_NAMES[STEP_PRESTART_STOP],
        "cd ~/SW && stop.sh -es",
        "退出码 0",
        timeout_seconds=180,
        auto=True,
    ))
    if simulation_mode == "sim2":
        start_command = "cd ~/SW && start.sh -f"
    elif simulation_mode == "sim0_real":
        start_command = "cd ~/SW && start.sh -ef"
    else:
        start_command = "cd ~/SW && start.sh -eif"
    commands.append(DeploymentCommand(
        STEP_START,
        STEP_NAMES[STEP_START],
        start_command,
        "The system is started successfully",
        timeout_seconds=1800,
    ))
    post_start_script = str(payload.get("post_start_script") or "").strip()
    commands.append(DeploymentCommand(
        STEP_POST_START,
        STEP_NAMES[STEP_POST_START],
        post_start_script,
        "退出码 0",
        timeout_seconds=3600,
        skipped=not bool(post_start_script),
    ))

    overrides = _command_overrides(payload)
    if overrides:
        updated: list[DeploymentCommand] = []
        for item in commands:
            command = overrides.get(item.key, item.command)
            if not item.skipped and not command.strip():
                raise ValueError(f"{item.name} 的最终执行命令不能为空。")
            updated.append(DeploymentCommand(
                item.key, item.name, command, item.success_marker,
                timeout_seconds=item.timeout_seconds, skipped=item.skipped, auto=item.auto,
            ))
        commands = updated

    selected_steps = payload.get("selected_steps")
    if selected_steps is not None:
        selected = {str(key).strip() for key in selected_steps if str(key).strip()}
        commands = [
            DeploymentCommand(
                item.key, item.name, item.command, item.success_marker,
                timeout_seconds=item.timeout_seconds,
                skipped=(
                    item.skipped
                    or (STEP_START not in selected if item.auto else item.key not in selected)
                ),
                auto=item.auto,
            )
            for item in commands
        ]
    return commands

def validate_payload(environment: Environment, payload: dict) -> dict:
    defaults = _validation_defaults(environment)
    normalized = {**defaults, **(payload or {})}
    normalized["task_name"] = str(normalized.get("task_name") or "").strip()
    normalized["target_version"] = str(normalized.get("target_version") or "").strip()
    normalized["simulation_mode"] = str(normalized.get("simulation_mode") or "").strip()
    normalized["upper_ip"] = str(normalized.get("upper_ip") or "").strip()
    normalized["gpb_ips"] = [str(item).strip() for item in normalized.get("gpb_ips") or [] if str(item).strip()]
    normalized["gpb_mode"] = str(
        normalized.get("gpb_mode") or normalized.get("install_mode") or ""
    ).strip()
    normalized["tb_mode"] = str(normalized.get("tb_mode") or "").strip()
    normalized["precheck_stop_lower"] = bool(normalized.get("precheck_stop_lower", False))
    normalized["include_dhh"] = bool(normalized.get("include_dhh", False))
    normalized["dhh_ip"] = str(normalized.get("dhh_ip") or "").strip()
    normalized["dhh_user"] = str(normalized.get("dhh_user") or "root").strip()
    normalized["dhh_machine_id"] = str(normalized.get("dhh_machine_id") or "TESTSPM").strip()
    normalized["display_env"] = str(normalized.get("display_env") or "").strip()
    normalized["post_start_script"] = str(normalized.get("post_start_script") or "").strip()
    normalized["save_post_start_script"] = bool(normalized.get("save_post_start_script", False))
    if "selected_steps" in (payload or {}):
        raw_selected_steps = (payload or {}).get("selected_steps")
        if not isinstance(raw_selected_steps, (list, tuple)):
            raise ValueError("执行步骤格式无效。")
        selected_steps: list[str] = []
        for raw_key in raw_selected_steps:
            key = str(raw_key or "").strip()
            if not key:
                continue
            if key not in DEPLOYMENT_STEP_KEYS:
                raise ValueError(f"未知部署步骤：{key}")
            if key not in selected_steps:
                selected_steps.append(key)
        if not selected_steps:
            raise ValueError("请至少选择一个需要执行的部署步骤。")
        normalized["selected_steps"] = selected_steps
    else:
        normalized.pop("selected_steps", None)
    try:
        normalized["install_port"] = int(normalized.get("install_port", 0))
    except (TypeError, ValueError):
        raise ValueError("安装端口必须是 0 到 11 的整数。")

    if not normalized["task_name"]:
        raise ValueError("部署任务名不能为空。")
    if not normalized["target_version"]:
        raise ValueError("请选择或输入目标版本号。")
    if normalized["display_env"] and (len(normalized["display_env"]) > 256 or "\x00" in normalized["display_env"] or "\n" in normalized["display_env"] or "\r" in normalized["display_env"]):
        raise ValueError("DISPLAY 环境变量格式无效。")
    if len(normalized["post_start_script"]) > 12000 or "\x00" in normalized["post_start_script"]:
        raise ValueError("启动后脚本格式无效或内容过长。")
    if normalized["simulation_mode"] not in {"sim0_sil", "sim2", "sim0_real"}:
        raise ValueError("仿真模式只支持 sim0_sil、sim2 或 sim0_real。")
    if normalized["upper_ip"] != environment.upper_machine.host:
        raise ValueError("部署命令中的上位机 IP 与当前环境不一致，请先确认环境或重新解析命令。")
    if not normalized["gpb_ips"]:
        raise ValueError("请至少填写一个 GPB IP。")
    if len(set(normalized["gpb_ips"])) != len(normalized["gpb_ips"]):
        raise ValueError("GPB IP 列表存在重复地址，请检查后再部署。")
    invalid_ips: list[str] = []
    for host in normalized["gpb_ips"]:
        try:
            ip_address(host)
        except ValueError:
            invalid_ips.append(host)
    if invalid_ips:
        raise ValueError(f"GPB IP 格式无效：{', '.join(invalid_ips)}")
    if not 0 <= normalized["install_port"] <= 11:
        raise ValueError("安装端口必须在 0 到 11 之间。")

    if normalized["simulation_mode"] == "sim0_real":
        # Real-machine deployment has no SIL/TB package and no GPB deployment-mode
        # argument. The 10-position install list comes from each LCH station SLOT.
        normalized["include_sdk"] = False
        normalized["include_dhh"] = False
        normalized["required_gpb_count"] = len(normalized["gpb_ips"])
        normalized["real_slot_ips"] = _real_install_slot_ips(environment, normalized["gpb_ips"])
        normalized["available_gpb_slots"] = _real_gpb_slot_entries(environment)
        normalized["install_mode"] = ""
    else:
        if not normalized["gpb_mode"]:
            raise ValueError("请选择 GPB 部署模式。")
        required_count = _mode_required_gpb_count(normalized["gpb_mode"])
        normalized["required_gpb_count"] = required_count
        if len(normalized["gpb_ips"]) != required_count:
            raise ValueError(
                f"{normalized['gpb_mode']} 需要 {required_count} 个 GPB IP，当前填写 {len(normalized['gpb_ips'])} 个。"
            )

        if required_count == 1:
            # install.sh always uses the normal single-GPB mode. tb_deploy.sh may use
            # a custom split topology such as a larger chassis cut down to one GPB.
            normalized["gpb_mode"] = "1GPB_TB"
            normalized["install_mode"] = "1GPB_TB"
            if normalized["simulation_mode"] == "sim0_sil":
                if not normalized["tb_mode"]:
                    normalized["tb_mode"] = DEFAULT_SINGLE_GPB_TB_MODE
                if not SINGLE_GPB_TB_MODE_RE.match(normalized["tb_mode"]):
                    raise ValueError("单 GPB 的 TB 定制模式必须使用 nGPB_SPLIT_机框-槽位 格式。")
            elif not normalized["tb_mode"]:
                normalized["tb_mode"] = DEFAULT_SINGLE_GPB_TB_MODE
        else:
            normalized["tb_mode"] = normalized["gpb_mode"]
            normalized["install_mode"] = _install_mode_for_gpb_mode(normalized["gpb_mode"])

    if normalized.get("selected_steps") is not None:
        naturally_runnable = {STEP_STOP, STEP_DEPLOY, STEP_INSTALL, STEP_START}
        if normalized["simulation_mode"] == "sim0_sil":
            naturally_runnable.add(STEP_TB)
        if normalized["post_start_script"]:
            naturally_runnable.add(STEP_POST_START)
        elif STEP_POST_START in normalized["selected_steps"]:
            raise ValueError("已选择启动后脚本步骤，但脚本内容为空。")
        if not naturally_runnable.intersection(normalized["selected_steps"]):
            raise ValueError("当前部署模式下没有可执行的已选步骤。")

    if "commands" in (payload or {}):
        overrides = _command_overrides(payload or {})
        normalized["commands"] = [
            {"key": key, "command": command}
            for key, command in overrides.items()
        ]
    else:
        normalized.pop("commands", None)

    if normalized["include_dhh"]:
        if not normalized["dhh_ip"]:
            raise ValueError("勾选部署 DHH 后必须填写 DHH IP。")
        try:
            ip_address(normalized["dhh_ip"])
        except ValueError as exc:
            raise ValueError("DHH IP 格式无效。") from exc
        if not normalized["dhh_user"]:
            raise ValueError("DHH User 不能为空。")
        if not normalized["dhh_machine_id"]:
            raise ValueError("Machine ID 不能为空。")
    else:
        # Keep the editable defaults in the draft/history snapshot, but they do not
        # participate in command generation or preflight unless DHH is enabled.
        normalized["dhh_user"] = normalized["dhh_user"] or "root"
        normalized["dhh_machine_id"] = normalized["dhh_machine_id"] or "TESTSPM"
    return normalized


def command_preview(environment: Environment, payload: dict) -> dict:
    normalized = validate_payload(environment, payload)
    commands = build_commands(normalized)
    return {
        **normalized,
        "commands": [
            {"key": item.key, "name": item.name, "command": item.command, "skipped": item.skipped}
            for item in commands if not item.auto
        ],
    }


def _collect_command_blocks(text: str) -> dict[str, str]:
    cleaned = str(text or "").replace("\\\n", " ")
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    # tb_deploy.sh contains the substring deploy.sh, so match the specific script first.
    markers = (("tb_deploy.sh", "tb"), ("install.sh", "install"), ("deploy.sh", "deploy"))
    for line in lines:
        detected = next((key for marker, key in markers if marker in line), None)
        if detected:
            current = detected
            blocks[current] = [line]
            continue
        if current and not re.search(r"\b(?:cd\s+~|start\.sh|stop\.sh|psql\b|python3\b)\b", line):
            blocks[current].append(line)
    return {key: " ".join(value).replace("\\", " ") for key, value in blocks.items()}


def _tokens_after_script(command: str, script_name: str) -> list[str]:
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"命令解析失败：{exc}") from exc
    index = next((idx for idx, token in enumerate(tokens) if script_name in token), None)
    if index is None:
        return []
    return tokens[index + 1 :]


def parse_command_text(environment: Environment, text: str) -> dict:
    blocks = _collect_command_blocks(text)
    patch: dict = {}
    parsed_tb_mode = ""
    parsed_install_mode = ""
    parsed_gpb_lists: list[list[str]] = []
    parsed_upper_ips: list[str] = []

    if "deploy" in blocks:
        args = _tokens_after_script(blocks["deploy"], "deploy.sh")
        if len(args) >= 2 and args[1] == "sim0_real":
            patch.update({
                "simulation_mode": "sim0_real",
                "target_version": args[0],
                "include_sdk": False,
                "include_dhh": False,
            })
        else:
            if len(args) < 3:
                raise ValueError("deploy.sh 至少需要任务名、仿真模式和版本号。")
            patch.update({
                "task_name": args[0],
                "simulation_mode": args[1],
                "target_version": args[2],
                "include_sdk": any(token.upper() == "SDK" for token in args[3:]),
            })

    if "tb" in blocks:
        args = _tokens_after_script(blocks["tb"], "tb_deploy.sh")
        if len(args) < 4:
            raise ValueError("tb_deploy.sh 参数不足，无法识别 GPB 模式、上位机 IP 和 GPB IP 列表。")
        parsed_tb_mode = args[0]
        patch["tb_mode"] = parsed_tb_mode
        parsed_upper_ips.append(args[1])
        parsed_gpb_lists.append([item.strip() for item in args[2].split(",") if item.strip()])
        patch["simulation_mode"] = "sim0_sil"

    if "install" in blocks:
        args = _tokens_after_script(blocks["install"], "install.sh")
        if len(args) < 4:
            raise ValueError("install.sh 参数不足，无法识别上位机 IP、GPB IP 和端口。")
        upper_ip, gpb_csv, port = args[0], args[1], args[2]
        slot_values = [item.strip() for item in gpb_csv.split(",")]
        is_real_install = patch.get("simulation_mode") == "sim0_real" or (
            len(slot_values) == REAL_GPB_SLOT_COUNT
            and all(item.lower() == "none" or item for item in slot_values)
            and not any(re.match(r"^\d+GPB(?:_|$)", token, re.IGNORECASE) for token in args[3:])
        )
        if is_real_install:
            if len(slot_values) != REAL_GPB_SLOT_COUNT:
                raise ValueError(f"sim0_real 的 install.sh 必须包含 {REAL_GPB_SLOT_COUNT} 个槽位参数。")
            real_gpb_ips = [item for item in slot_values if item and item.lower() != "none"]
            if not real_gpb_ips:
                raise ValueError("sim0_real 的 install.sh 中至少需要一个 GPB IP。")
            patch["simulation_mode"] = "sim0_real"
            patch["include_sdk"] = False
            patch["include_dhh"] = False
            patch["gpb_ips"] = real_gpb_ips
            parsed_gpb_lists.append(real_gpb_ips)
        else:
            if len(args) < 5:
                raise ValueError("install.sh 参数不足，无法识别上位机 IP、GPB IP、端口和 GPB 模式。")
            install_mode = ""
            for token in args[3:]:
                if re.match(r"^\d+GPB(?:_|$)", token, re.IGNORECASE):
                    install_mode = token
                    break
            if not install_mode:
                raise ValueError("install.sh 中没有识别到 GPB 部署模式。")
            parsed_install_mode = install_mode
            patch["gpb_mode"] = install_mode
            patch["install_mode"] = install_mode
            dhh_values: dict[str, str] = {}
            option_names = {"--dhhIp": "dhh_ip", "--dhhUser": "dhh_user", "--machineId": "dhh_machine_id"}
            index = 3
            while index < len(args):
                token = args[index]
                for option, field in option_names.items():
                    prefix = f"{option}="
                    if token.startswith(prefix):
                        dhh_values[field] = token[len(prefix):].strip()
                        break
                    if token == option and index + 1 < len(args):
                        dhh_values[field] = args[index + 1].strip()
                        index += 1
                        break
                index += 1
            if dhh_values:
                patch["include_dhh"] = True
                patch["dhh_ip"] = dhh_values.get("dhh_ip", "")
                patch["dhh_user"] = dhh_values.get("dhh_user", "root") or "root"
                patch["dhh_machine_id"] = dhh_values.get("dhh_machine_id", "TESTSPM") or "TESTSPM"
            parsed_gpb_lists.append([item.strip() for item in gpb_csv.split(",") if item.strip()])

        parsed_upper_ips.append(upper_ip)
        try:
            patch["install_port"] = int(port)
        except ValueError as exc:
            raise ValueError("install.sh 中的安装端口必须是整数。") from exc

    if not patch and not parsed_tb_mode and not parsed_install_mode:
        raise ValueError("没有识别到 deploy.sh、tb_deploy.sh 或 install.sh 命令。")

    distinct_upper = {host for host in parsed_upper_ips if host}
    if len(distinct_upper) > 1:
        raise ValueError("tb_deploy.sh 与 install.sh 的上位机 IP 不一致。")
    if distinct_upper:
        patch["upper_ip"] = next(iter(distinct_upper))

    gpb_ips: list[str] = []
    if parsed_gpb_lists:
        gpb_ips = parsed_gpb_lists[0]
        if any(items != gpb_ips for items in parsed_gpb_lists[1:]):
            raise ValueError("tb_deploy.sh 与 install.sh 的 GPB IP 列表不一致。")
        patch["gpb_ips"] = gpb_ips

    if parsed_install_mode:
        required_count = _mode_required_gpb_count(parsed_install_mode)
        if gpb_ips and len(gpb_ips) != required_count:
            raise ValueError(f"{parsed_install_mode} 与 GPB IP 数量不一致。")
        if required_count == 1:
            if parsed_install_mode.upper() != "1GPB_TB":
                raise ValueError("单 GPB 的 install.sh 模式必须保持 1GPB_TB。")
            patch["gpb_mode"] = "1GPB_TB"
            patch["install_mode"] = "1GPB_TB"
            # tb_deploy.sh deliberately allows a different custom split mode.
            if parsed_tb_mode:
                patch["tb_mode"] = parsed_tb_mode
        elif parsed_tb_mode:
            expected_install_mode = _install_mode_for_gpb_mode(parsed_tb_mode)
            if expected_install_mode.upper() != parsed_install_mode.upper():
                raise ValueError("多 GPB 场景下 tb_deploy.sh 与 install.sh 的 GPB 部署模式不匹配。")
            patch["gpb_mode"] = parsed_tb_mode
            patch["tb_mode"] = parsed_tb_mode
            patch["install_mode"] = parsed_install_mode
        else:
            patch["gpb_mode"] = parsed_install_mode
            patch["install_mode"] = parsed_install_mode
    elif parsed_tb_mode:
        if len(gpb_ips) == 1:
            patch["gpb_mode"] = "1GPB_TB"
            patch["install_mode"] = "1GPB_TB"
            patch["tb_mode"] = parsed_tb_mode
        else:
            patch["gpb_mode"] = parsed_tb_mode
            patch["tb_mode"] = parsed_tb_mode
            patch["install_mode"] = _install_mode_for_gpb_mode(parsed_tb_mode)

    defaults = _validation_defaults(environment)
    merged = {**defaults, **patch}
    return command_preview(environment, merged)

def _append_step_output(
    step_id: int,
    stdout: str = "",
    stderr: str = "",
    *,
    chunks: list[dict[str, str]] | None = None,
) -> None:
    if not stdout and not stderr:
        return
    ordered_chunks = [
        {"stream": str(item.get("stream") or ""), "text": str(item.get("text") or "")}
        for item in (chunks or [])
        if str(item.get("stream") or "") in {"stdout", "stderr"} and str(item.get("text") or "")
    ]
    if not ordered_chunks:
        if stdout:
            ordered_chunks.append({"stream": "stdout", "text": stdout})
        if stderr:
            ordered_chunks.append({"stream": "stderr", "text": stderr})

    step = DeploymentStep.objects.get(pk=step_id)
    if stdout:
        step.stdout = (step.stdout or "") + stdout
    if stderr:
        step.stderr = (step.stderr or "") + stderr
    # Persist raw output for the existing deployment detail UI and continue
    # publishing it over the deployment SSE channel.  Only backend console
    # logging is suppressed; the frontend behavior stays unchanged.
    step.process_log = (step.process_log or "") + "".join(item["text"] for item in ordered_chunks)
    step.save(update_fields=["stdout", "stderr", "process_log", "updated_at"])
    DeploymentEventBus.publish_log(
        step.deployment_id,
        step.key,
        stdout=stdout,
        stderr=stderr,
        chunks=ordered_chunks,
        stdout_length=len(step.stdout or ""),
        stderr_length=len(step.stderr or ""),
        process_log_length=len(step.process_log or ""),
    )


def _publish_deployment_state(deployment_id: int) -> None:
    DeploymentEventBus.publish_state(deployment_id)


def _execute_streaming_client(client, command: str, timeout_seconds: int, on_output: Callable[[str, str], None]) -> int:
    started = time.monotonic()
    last_output_at = started
    idle_output_timeout = min(
        max(30, int(getattr(settings, "TRACELENS_DEPLOYMENT_SSH_IDLE_OUTPUT_TIMEOUT", 300))),
        max(30, int(timeout_seconds)),
    )
    transport = client.get_transport()
    if transport is None or not transport.is_active():
        raise RuntimeError("SSH 会话已断开。")
    channel = transport.open_session(timeout=20)
    channel.exec_command(command)
    while True:
        produced = False
        if channel.recv_ready():
            data = channel.recv(65536).decode("utf-8", errors="replace")
            on_output(data, "")
            produced = True
        if channel.recv_stderr_ready():
            data = channel.recv_stderr(65536).decode("utf-8", errors="replace")
            on_output("", data)
            produced = True
        if produced:
            last_output_at = time.monotonic()
        if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
            return channel.recv_exit_status()
        now = time.monotonic()
        if now - started > timeout_seconds:
            channel.close()
            raise TimeoutError(f"命令执行超过 {timeout_seconds} 秒，已停止当前部署任务并断开 SSH。")
        if now - last_output_at > idle_output_timeout:
            channel.close()
            raise TimeoutError(
                f"SSH 连续 {idle_output_timeout} 秒没有任何输出，判定远程命令无响应，"
                "已停止当前部署步骤并断开 SSH。"
            )
        if not produced:
            time.sleep(0.2)


def _deployment_pid_file(deployment_id: int, step_key: str) -> str:
    safe_key = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(step_key or "step"))
    return f"/tmp/tracelens-deploy-{int(deployment_id)}-{safe_key}.pid"


def _deployment_shell_command(command: str, deployment: EnvironmentDeployment, step_key: str) -> str:
    # Each stage gets its own process group and task-specific PID file. This keeps
    # stop-task precise: only the script started by this deployment step is killed.
    display_env = str((deployment.configuration or {}).get("display_env") or "").strip()
    env_parts = ["source ~/.bashrc"]
    if display_env:
        env_parts.append(f"export DISPLAY={_quote(display_env)}")
    actual = " && ".join(env_parts + [command])
    pidfile = _deployment_pid_file(deployment.id, step_key)
    quoted_actual = _quote(actual)
    quoted_pidfile = _quote(pidfile)
    return (
        f"pidfile={quoted_pidfile}; rm -f \"$pidfile\"; "
        f"if command -v setsid >/dev/null 2>&1; then setsid bash -lc {quoted_actual} & "
        f"else bash -lc {quoted_actual} & fi; "
        "child=$!; printf '%s\n' \"$child\" > \"$pidfile\"; "
        "wait \"$child\"; rc=$?; rm -f \"$pidfile\"; exit $rc"
    )


def _terminate_remote_running_steps(deployment: EnvironmentDeployment) -> None:
    running_keys = list(DeploymentStep.objects.filter(
        deployment=deployment, status=DeploymentStepStatus.RUNNING,
    ).values_list("key", flat=True))
    # Every visible running step, including the automatic pre-start stop, owns a
    # task-specific PID file and can therefore be terminated precisely.
    pidfiles = [_deployment_pid_file(deployment.id, key) for key in running_keys]
    file_args = " ".join(_quote(item) for item in pidfiles)
    command = (
        f"for pidfile in {file_args}; do "
        "[ -s \"$pidfile\" ] || continue; pid=$(cat \"$pidfile\" 2>/dev/null); "
        "case \"$pid\" in ''|*[!0-9]*) continue;; esac; "
        "if kill -0 \"$pid\" 2>/dev/null; then kill -TERM -- -\"$pid\" 2>/dev/null || kill -TERM \"$pid\" 2>/dev/null || true; fi; done; "
        "sleep 1; "
        f"for pidfile in {file_args}; do "
        "[ -s \"$pidfile\" ] || continue; pid=$(cat \"$pidfile\" 2>/dev/null); "
        "case \"$pid\" in ''|*[!0-9]*) continue;; esac; "
        "if kill -0 \"$pid\" 2>/dev/null; then kill -KILL -- -\"$pid\" 2>/dev/null || kill -KILL \"$pid\" 2>/dev/null || true; fi; done; true"
    )
    client = build_ssh_client(deployment.environment.upper_machine)
    try:
        _execute_streaming_client(client, command, 12, lambda _stdout, _stderr: None)
    finally:
        client.close()


def _execute_streaming(machine, command: str, timeout_seconds: int, on_output: Callable[[str, str], None]) -> int:
    try:
        with ssh_session(machine) as lease:
            return _execute_streaming_client(lease.client, command, timeout_seconds, on_output)
    except TimeoutError:
        # Do not keep a transport that just hosted a wedged deployment channel.
        SSH_SESSION_POOL.invalidate(machine, "deployment_stream_timeout")
        raise


def _execute_streaming_dedicated(machine, command: str, timeout_seconds: int, on_output: Callable[[str, str], None]) -> int:
    # stop.sh and deploy.sh run concurrently on the same upper machine. The
    # pooled SSH lease intentionally serializes per-machine operations, so these
    # two long-running commands use independent authenticated transports.
    client = build_ssh_client(machine)
    try:
        return _execute_streaming_client(client, command, timeout_seconds, on_output)
    finally:
        client.close()


def _fresh_running_lower_hosts(environment: Environment) -> list[str]:
    """Probe lower machines immediately before start; never use the runtime cache here."""
    normal_machines, _dhh = _normal_relations(environment)
    machines = [machine for machine in normal_machines if not str(machine.host or "").strip().startswith("192.")]
    if not machines:
        return []

    def probe(machine):
        host = str(machine.host or "").strip()
        try:
            result = ssh_execute(machine, LOWER_RUNTIME_PROBE_COMMAND, timeout=8)
            return host, result.exit_status == 0 and bool(str(result.stdout or "").strip()), ""
        except Exception as exc:
            return host, False, str(exc)

    running: list[str] = []
    with ThreadPoolExecutor(max_workers=min(8, len(machines)), thread_name_prefix=f"env-{environment.id}-prestart-probe") as executor:
        futures = [executor.submit(probe, machine) for machine in machines]
        for future in futures:
            host, is_running, error = future.result()
            if error:
                logger.warning(
                    "environment deployment pre-start lower probe failed deployment_env=%s host=%s error=%s",
                    environment.id, host, error,
                )
            elif is_running:
                running.append(host)
    return sorted(running)


def _run_prestart_stop_step(
    deployment: EnvironmentDeployment,
    machine,
    command: DeploymentCommand,
) -> None:
    """Expose the pre-start safety stop as a normal timeline step.

    The step is automatic: when the final start stage is selected, TraceLens probes
    lower machines immediately before start. If nothing is running the step is marked
    skipped; otherwise stop.sh -es runs with the same realtime log stream as every
    other deployment stage.
    """
    step = DeploymentStep.objects.get(deployment=deployment, key=STEP_PRESTART_STOP)
    if command.skipped:
        now = timezone.now()
        step.status = DeploymentStepStatus.SKIPPED
        step.started_at = now
        step.finished_at = now
        step.message = "本次未执行启动环境，无需停止下位机进程。"
        step.save(update_fields=["status", "started_at", "finished_at", "message", "updated_at"])
        _publish_deployment_state(deployment.id)
        return

    deployment.current_step = STEP_PRESTART_STOP
    deployment.message = "正在检查下位机进程。"
    deployment.save(update_fields=["current_step", "message", "updated_at"])
    step.status = DeploymentStepStatus.RUNNING
    step.started_at = timezone.now()
    step.finished_at = None
    step.exit_status = None
    step.message = "正在检查下位机进程。"
    step.save(update_fields=["status", "started_at", "finished_at", "exit_status", "message", "updated_at"])
    _publish_deployment_state(deployment.id)

    running_hosts = _fresh_running_lower_hosts(deployment.environment)
    _raise_if_deployment_stop_requested(deployment.id)
    if not running_hosts:
        now = timezone.now()
        step.status = DeploymentStepStatus.SKIPPED
        step.finished_at = now
        step.exit_status = 0
        step.message = "下位机进程未运行，无需停止，跳过此步骤。"
        step.save(update_fields=["status", "finished_at", "exit_status", "message", "updated_at"])
        _publish_deployment_state(deployment.id)
        return

    step.message = f"检测到下位机进程运行中：{', '.join(running_hosts)}，正在停止。"
    step.save(update_fields=["message", "updated_at"])
    _publish_deployment_state(deployment.id)
    _run_serial_deployment_step(deployment, machine, command)


def _check_success(
    command: DeploymentCommand,
    deployment: EnvironmentDeployment,
    step: DeploymentStep,
    *,
    attempt_output: str | None = None,
) -> tuple[bool, str]:
    # Retry keeps historical stdout/stderr for auditability. Success markers must
    # only be evaluated against the current execution attempt, otherwise an old
    # marker could make a failed retry look successful.
    output = attempt_output if attempt_output is not None else f"{step.stdout or ''}\n{step.stderr or ''}"
    # Stopping the old environment is best-effort. A fresh machine may not have
    # ~/SW/stop.sh yet, and even other stop failures must not block package
    # deployment/install/start. Keep the real exit code and stderr on the step
    # for diagnostics, but always allow the deployment pipeline to continue.
    if command.key == STEP_STOP:
        if step.exit_status == 0:
            return True, "停止命令执行完成。"
        lowered = output.lower()
        if step.exit_status == 127 or "command not found" in lowered or "no such file or directory" in lowered:
            return True, "未发现停止脚本，无需停止，继续部署。"
        return True, f"停止命令退出码为 {step.exit_status}，作为非阻断步骤继续部署。"
    if step.exit_status != 0:
        return False, f"退出码为 {step.exit_status}。"
    target_version = deployment.target_version.strip()
    if command.key == STEP_DEPLOY:
        if "Successfully." not in output:
            return False, "拉包命令未确认成功。"
        if not _output_contains_target_version(output, target_version):
            return False, f"拉包输出未匹配目标版本：{target_version}。"
        return True, "软件包拉取成功且版本校验通过。"
    if command.key == STEP_TB:
        expected = re.compile(rf"TestBench\s+deploy\s+{re.escape(deployment.tb_mode)}\s+successfully!", re.IGNORECASE | re.DOTALL)
        if not expected.search(output):
            return False, f"未检测到 TestBench deploy {deployment.tb_mode} successfully!。"
        return True, "仿真环境部署成功。"
    if command.key == STEP_PRESTART_STOP:
        return True, "启动前运行进程已停止。"
    if command.key == STEP_INSTALL:
        # install.sh is authoritative for the install stage. Version text printed by
        # different product branches is not normalized consistently (for example
        # `Current XY Version` may use a different release label than the requested
        # package string), so do not turn a successful install into a deployment
        # failure by parsing or comparing version output here. A zero exit status
        # has already been checked above and is sufficient for this stage.
        return True, "安装命令执行成功。"
    if command.key == STEP_START:
        if "The system is started successfully" not in output:
            return False, "未检测到系统启动成功标志。"
        return True, "系统启动成功。"
    if command.key == STEP_POST_START:
        return True, "启动后脚本执行成功。"
    return True, "命令执行成功。"


def _validate_gpb_hosts(gpb_ips: list[str]) -> list[str]:
    hosts = [str(item).strip() for item in (gpb_ips or []) if str(item).strip()]
    if not hosts:
        raise ValueError("请先填写 GPB IP。")
    if len(set(hosts)) != len(hosts):
        raise ValueError("GPB IP 列表存在重复地址，请检查后再检测互信。")
    invalid: list[str] = []
    for host in hosts:
        try:
            ip_address(host)
        except ValueError:
            invalid.append(host)
    if invalid:
        raise ValueError(f"GPB IP 格式无效：{', '.join(invalid)}")
    return hosts


def _deployment_remote_targets(
    gpb_ips: list[str],
    *,
    include_dhh: bool = False,
    dhh_ip: str = "",
    dhh_user: str = "root",
) -> list[dict[str, str]]:
    hosts = _validate_gpb_hosts(gpb_ips)
    targets = [{"host": host, "user": "root", "role": "gpb"} for host in hosts]
    if include_dhh:
        dhh_host = str(dhh_ip or "").strip()
        username = str(dhh_user or "").strip()
        if not dhh_host:
            raise ValueError("勾选部署 DHH 后必须填写 DHH IP。")
        try:
            ip_address(dhh_host)
        except ValueError as exc:
            raise ValueError("DHH IP 格式无效。") from exc
        if not username:
            raise ValueError("DHH User 不能为空。")
        targets.append({"host": dhh_host, "user": username, "role": "dhh"})
    return targets


def _deployment_target_signature(targets: list[dict[str, str]]) -> str:
    return "|".join(f"{item['role']}:{item['user']}@{item['host']}" for item in targets)


def check_gpb_ssh_trust(
    environment: Environment,
    gpb_ips: list[str],
    *,
    include_dhh: bool = False,
    dhh_ip: str = "",
    dhh_user: str = "root",
) -> dict:
    targets = _deployment_remote_targets(
        gpb_ips,
        include_dhh=include_dhh,
        dhh_ip=dhh_ip,
        dhh_user=dhh_user,
    )
    machine = environment.upper_machine
    results: list[dict] = []
    token = "__TRACELENS_SSH_TRUST_OK__"
    for target in targets:
        host = target["host"]
        username = target["user"]
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        def collect(stdout: str, stderr: str) -> None:
            if stdout:
                stdout_parts.append(stdout)
            if stderr:
                stderr_parts.append(stderr)

        probe = (
            "ssh -o BatchMode=yes -o NumberOfPasswordPrompts=0 "
            "-o PreferredAuthentications=publickey -o PasswordAuthentication=no "
            "-o KbdInteractiveAuthentication=no -o StrictHostKeyChecking=no "
            "-o UserKnownHostsFile=/dev/null -o ConnectTimeout=6 "
            f"{_quote(username)}@{_quote(host)} 'printf {token}'"
        )
        forward_trusted = False
        forward_message = "未互信"
        try:
            exit_status = _execute_streaming(machine, probe, 12, collect)
            output = "".join(stdout_parts)
            forward_trusted = exit_status == 0 and token in output
            if forward_trusted:
                forward_message = "已互信"
            else:
                detail = "".join(stderr_parts).strip().splitlines()
                forward_message = detail[-1][:240] if detail else "SSH 公钥认证未通过"
        except Exception as exc:
            forward_message = str(exc)

        reverse_trusted: bool | None = None
        reverse_message = ""
        if target["role"] == "gpb":
            reverse_trusted, reverse_message = _check_lower_to_upper_ssh_trust(environment, target)
        trusted = forward_trusted and (reverse_trusted is not False)
        if trusted:
            message = "已双向互信" if target["role"] == "gpb" else "已互信"
        else:
            details: list[str] = []
            if not forward_trusted:
                details.append(f"上位机→下位机：{forward_message}")
            if reverse_trusted is False:
                details.append(f"下位机→上位机：{reverse_message}")
            message = "；".join(details) or "SSH 公钥认证未通过"
        results.append({
            "host": host,
            "user": username,
            "role": target["role"],
            "trusted": trusted,
            "forward_trusted": forward_trusted,
            "reverse_trusted": reverse_trusted,
            "forward_message": forward_message,
            "reverse_message": reverse_message,
            "message": message,
            "copy_command": f"ssh-copy-id {username}@{host}",
        })
    return {
        "environment_id": environment.id,
        "upper_ip": environment.upper_machine.host,
        "gpb_ips": [item["host"] for item in targets if item["role"] == "gpb"],
        "include_dhh": bool(include_dhh),
        "dhh_ip": str(dhh_ip or "").strip() if include_dhh else "",
        "dhh_user": str(dhh_user or "root").strip() if include_dhh else "",
        "target_signature": _deployment_target_signature(targets),
        "all_trusted": all(item["trusted"] for item in results),
        "results": results,
        "checked_at": timezone.now().isoformat(),
    }


def _capture_command(machine, command: str, timeout_seconds: int = 15) -> tuple[int, str, str]:
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []

    def collect(stdout: str, stderr: str) -> None:
        if stdout:
            stdout_parts.append(stdout)
        if stderr:
            stderr_parts.append(stderr)

    exit_status = _execute_streaming(machine, command, timeout_seconds, collect)
    return exit_status, "".join(stdout_parts), "".join(stderr_parts)


def _copy_machine_credentials(source: Machine, *, host: str, username: str) -> Machine:
    """Build an unsaved target connection using credentials already stored by TraceLens."""
    target = Machine(
        name=f"deployment-trust-{host}",
        host=host,
        ssh_port=source.ssh_port or 22,
        username=username,
        role=MachineRole.LOWER,
        auth_type=source.auth_type,
    )
    if source.auth_type == AuthenticationType.PASSWORD and source.encrypted_password:
        target.set_password(source.get_password())
    elif source.auth_type == AuthenticationType.PRIVATE_KEY and source.encrypted_private_key:
        target.set_private_key(source.get_private_key())
        if source.encrypted_private_key_passphrase:
            target.set_private_key_passphrase(source.get_private_key_passphrase())
    return target


def _deployment_target_credential_machine(environment: Environment, target: dict[str, str]) -> Machine:
    host = target["host"]
    username = target["user"]
    candidates = list(
        Machine.objects.filter(host=host, role=MachineRole.LOWER, is_active=True).order_by("id")
    )
    source = next((item for item in candidates if item.username == username and item.has_credential), None)
    if source is None:
        source = next((item for item in candidates if item.has_credential), None)
    if source is not None:
        return _copy_machine_credentials(source, host=host, username=username)

    if target["role"] == "gpb":
        settings_obj = ResourceSettings.get_solo()
        fallback = Machine(
            name=f"deployment-trust-{host}",
            host=host,
            ssh_port=settings_obj.lower_ssh_port or 22,
            username=username,
            role=MachineRole.LOWER,
            auth_type=settings_obj.lower_auth_type,
        )
        if settings_obj.lower_auth_type == AuthenticationType.PASSWORD and settings_obj.encrypted_lower_password:
            fallback.set_password(settings_obj.get_lower_password())
            return fallback
        if settings_obj.lower_auth_type == AuthenticationType.PRIVATE_KEY and settings_obj.encrypted_lower_private_key:
            fallback.set_private_key(settings_obj.get_lower_private_key())
            if settings_obj.encrypted_lower_private_key_passphrase:
                fallback.set_private_key_passphrase(settings_obj.get_lower_private_key_passphrase())
            return fallback

    raise ValueError(f"{host} 未配置可用于自动互信的 SSH 凭据")


def _check_lower_to_upper_ssh_trust(environment: Environment, target: dict[str, str]) -> tuple[bool, str]:
    # Verify the GPB/lower can SSH back to the upper machine with public-key auth.
    if target.get("role") != "gpb":
        return True, "无需反向互信"
    try:
        lower_machine = _deployment_target_credential_machine(environment, target)
        token = "__TRACELENS_REVERSE_SSH_TRUST_OK__"
        upper = environment.upper_machine
        upper_username = str(upper.username or "root").strip() or "root"
        probe = (
            "ssh -o BatchMode=yes -o NumberOfPasswordPrompts=0 "
            "-o PreferredAuthentications=publickey -o PasswordAuthentication=no "
            "-o KbdInteractiveAuthentication=no -o StrictHostKeyChecking=no "
            "-o UserKnownHostsFile=/dev/null -o ConnectTimeout=6 "
            f"{_quote(upper_username)}@{_quote(upper.host)} 'printf {token}'"
        )
        result = ssh_execute(lower_machine, probe, timeout=12)
        trusted = result.exit_status == 0 and token in (result.stdout or "")
        if trusted:
            return True, "已互信"
        detail = (result.stderr or result.stdout).strip().splitlines()
        return False, detail[-1][:240] if detail else "SSH 公钥认证未通过"
    except Exception as exc:
        return False, str(exc)


def _ensure_machine_public_key(machine: Machine, label: str) -> str:
    command = r'''umask 077
mkdir -p ~/.ssh || exit 1
chmod 700 ~/.ssh || exit 1
for key in ~/.ssh/id_ed25519.pub ~/.ssh/id_rsa.pub ~/.ssh/id_ecdsa.pub; do
  if [ -s "$key" ]; then cat "$key"; exit 0; fi
done
if ssh-keygen -q -t ed25519 -N '' -f ~/.ssh/id_ed25519 >/dev/null 2>&1; then
  cat ~/.ssh/id_ed25519.pub; exit 0
fi
ssh-keygen -q -t rsa -b 2048 -N '' -f ~/.ssh/id_rsa >/dev/null 2>&1 || exit 1
cat ~/.ssh/id_rsa.pub'''
    result = ssh_execute(machine, command, timeout=20)
    if result.exit_status != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ValueError(f"{label} SSH 公钥准备失败：{detail or f'exit={result.exit_status}'}")
    public_key = next(
        (line.strip() for line in result.stdout.splitlines() if line.strip().startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-"))),
        "",
    )
    if not public_key:
        raise ValueError(f"{label} SSH 公钥准备失败：未读取到有效公钥")
    return public_key


def _install_public_key_on_machine(machine: Machine, public_key: str) -> None:
    key_arg = _quote(public_key)
    command = (
        "umask 077; mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        "touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
        f"(grep -qxF -- {key_arg} ~/.ssh/authorized_keys || printf '%s\\n' {key_arg} >> ~/.ssh/authorized_keys)"
    )
    result = ssh_execute(machine, command, timeout=20)
    if result.exit_status != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise ValueError(detail[-1][:240] if detail else f"exit={result.exit_status}")


def _ensure_lower_public_key(environment: Environment, target: dict[str, str]) -> str:
    lower_machine = _deployment_target_credential_machine(environment, target)
    return _ensure_machine_public_key(lower_machine, f"{target['host']} 下位机")


def _install_lower_public_key_on_upper(environment: Environment, public_key: str) -> None:
    _install_public_key_on_machine(environment.upper_machine, public_key)


def _ensure_upper_public_key(machine: Machine) -> str:
    command = r'''umask 077
mkdir -p ~/.ssh || exit 1
chmod 700 ~/.ssh || exit 1
for key in ~/.ssh/id_ed25519.pub ~/.ssh/id_rsa.pub ~/.ssh/id_ecdsa.pub; do
  if [ -s "$key" ]; then cat "$key"; exit 0; fi
done
if ssh-keygen -q -t ed25519 -N '' -f ~/.ssh/id_ed25519 >/dev/null 2>&1; then
  cat ~/.ssh/id_ed25519.pub; exit 0
fi
ssh-keygen -q -t rsa -b 2048 -N '' -f ~/.ssh/id_rsa >/dev/null 2>&1 || exit 1
cat ~/.ssh/id_rsa.pub'''
    result = ssh_execute(machine, command, timeout=20)
    if result.exit_status != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ValueError(f"上位机 SSH 公钥准备失败：{detail or f'exit={result.exit_status}'}")
    public_key = next(
        (line.strip() for line in result.stdout.splitlines() if line.strip().startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-"))),
        "",
    )
    if not public_key:
        raise ValueError("上位机 SSH 公钥准备失败：未读取到有效公钥")
    return public_key


def _install_upper_public_key(environment: Environment, target: dict[str, str], public_key: str) -> None:
    machine = _deployment_target_credential_machine(environment, target)
    key_arg = _quote(public_key)
    command = (
        "umask 077; mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        "touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
        f"(grep -qxF -- {key_arg} ~/.ssh/authorized_keys || printf '%s\\n' {key_arg} >> ~/.ssh/authorized_keys)"
    )
    result = ssh_execute(machine, command, timeout=20)
    if result.exit_status != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise ValueError(detail[-1][:240] if detail else f"exit={result.exit_status}")


def ensure_gpb_ssh_trust(
    environment: Environment,
    gpb_ips: list[str],
    *,
    include_dhh: bool = False,
    dhh_ip: str = "",
    dhh_user: str = "root",
) -> dict:
    """Check deployment SSH trust and automatically repair missing targets."""
    initial = check_gpb_ssh_trust(
        environment,
        gpb_ips,
        include_dhh=include_dhh,
        dhh_ip=dhh_ip,
        dhh_user=dhh_user,
    )
    missing = [item for item in initial["results"] if not item["trusted"]]
    if not missing:
        for item in initial["results"]:
            item["auto_trust_attempted"] = False
            item["auto_trust_succeeded"] = False
        initial["auto_trust_attempted"] = False
        initial["auto_trust_succeeded"] = False
        return initial

    targets = _deployment_remote_targets(
        gpb_ips,
        include_dhh=include_dhh,
        dhh_ip=dhh_ip,
        dhh_user=dhh_user,
    )
    target_map = {(item["role"], item["host"], item["user"]): item for item in targets}
    errors: dict[tuple[str, str, str], list[str]] = {}
    attempted: set[tuple[str, str, str]] = set()
    try:
        public_key = _ensure_upper_public_key(environment.upper_machine)
    except Exception as exc:
        public_key = ""
        public_key_error = str(exc)
    else:
        public_key_error = ""

    for item in missing:
        key = (item["role"], item["host"], item["user"])
        attempted.add(key)
        target = target_map.get(key)
        if target is None:
            errors.setdefault(key, []).append("部署目标不存在")
            continue

        if not item.get("forward_trusted", False):
            if not public_key:
                errors.setdefault(key, []).append(f"上位机→下位机：{public_key_error}")
            else:
                try:
                    _install_upper_public_key(environment, target, public_key)
                except Exception as exc:
                    errors.setdefault(key, []).append(f"上位机→下位机：{exc}")

        if item["role"] == "gpb" and item.get("reverse_trusted") is False:
            try:
                lower_public_key = _ensure_lower_public_key(environment, target)
                _install_lower_public_key_on_upper(environment, lower_public_key)
            except Exception as exc:
                errors.setdefault(key, []).append(f"下位机→上位机：{exc}")

    checked = check_gpb_ssh_trust(
        environment,
        gpb_ips,
        include_dhh=include_dhh,
        dhh_ip=dhh_ip,
        dhh_user=dhh_user,
    )
    for item in checked["results"]:
        key = (item["role"], item["host"], item["user"])
        item["auto_trust_attempted"] = key in attempted
        item["auto_trust_succeeded"] = key in attempted and bool(item["trusted"])
        if item["auto_trust_succeeded"]:
            if item["role"] == "gpb":
                item["message"] = "已自动建立双向互信"
            else:
                item["message"] = "已自动建立互信"
        elif key in errors and not item["trusted"]:
            item["message"] = f"自动互信失败：{'；'.join(errors[key])}"
    checked["auto_trust_attempted"] = bool(attempted)
    checked["auto_trust_succeeded"] = bool(attempted) and all(
        item["trusted"] for item in checked["results"] if (item["role"], item["host"], item["user"]) in attempted
    )
    return checked


def _ensure_gpb_ssh_trust(
    environment: Environment,
    gpb_ips: list[str],
    *,
    include_dhh: bool = False,
    dhh_ip: str = "",
    dhh_user: str = "root",
) -> None:
    result = ensure_gpb_ssh_trust(
        environment,
        gpb_ips,
        include_dhh=include_dhh,
        dhh_ip=dhh_ip,
        dhh_user=dhh_user,
    )
    missing = [item for item in result["results"] if not item["trusted"]]
    if missing:
        labels = "；".join(f"{item['host']}（{item['message']}）" for item in missing)
        raise ValueError(f"SSH 自动互信未完成：{labels}")


def _read_epoch_from_machine(machine: Machine, label: str) -> int:
    # Use a marker because login shells / site profiles may print banners or
    # diagnostics around command output. Parsing a bare numeric line made a
    # successful `date` look invalid on some production hosts.
    marker = "__TRACELENS_EPOCH__="
    result = ssh_execute(machine, f"printf '{marker}'; date +%s", timeout=10)
    if result.exit_status != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or f"读取{label}时间失败")
    match = re.search(rf"(?m)^{re.escape(marker)}(\d{{9,}})\s*$", result.stdout or "")
    if not match:
        raise RuntimeError(f"{label} date 命令未返回有效时间")
    return int(match.group(1))


def _read_epoch_from_upper(machine: Machine) -> int:
    return _read_epoch_from_machine(machine, "上位机")


def _gpb_ssh_command(host: str, remote_command: str, username: str = "root") -> str:
    return (
        "ssh -o BatchMode=yes -o NumberOfPasswordPrompts=0 "
        "-o PreferredAuthentications=publickey -o PasswordAuthentication=no "
        "-o KbdInteractiveAuthentication=no -o StrictHostKeyChecking=no "
        "-o UserKnownHostsFile=/dev/null -o ConnectTimeout=6 "
        f"{_quote(username)}@{_quote(host)} {_quote(remote_command)}"
    )


def _read_gpb_epoch(machine, host: str, username: str = "root") -> int:
    exit_status, stdout, stderr = _capture_command(machine, _gpb_ssh_command(host, "date +%s", username), 15)
    if exit_status != 0:
        raise RuntimeError(stderr.strip() or "读取远端时间失败")
    match = re.search(r"(?m)^\s*(\d{9,})\s*$", stdout)
    if not match:
        raise RuntimeError("远端 date 命令未返回有效时间")
    return int(match.group(1))


def _sync_gpb_epoch(machine, host: str, upper_epoch: int, username: str = "root") -> None:
    from datetime import datetime, timezone as dt_timezone

    utc_text = datetime.fromtimestamp(upper_epoch, tz=dt_timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    remote = (
        f"if date -s '@{upper_epoch}' >/dev/null 2>&1 || "
        f"date -u -s '{utc_text}' >/dev/null 2>&1; then date +%s; "
        "else echo 'time sync failed' >&2; exit 1; fi"
    )
    exit_status, _stdout, stderr = _capture_command(machine, _gpb_ssh_command(host, remote, username), 20)
    if exit_status != 0:
        raise RuntimeError(stderr.strip() or "远端时间同步失败")


def _lower_to_upper_ssh_command(environment: Environment, remote_command: str) -> str:
    upper = environment.upper_machine
    upper_username = str(upper.username or "root").strip() or "root"
    return (
        "ssh -o BatchMode=yes -o NumberOfPasswordPrompts=0 "
        "-o PreferredAuthentications=publickey -o PasswordAuthentication=no "
        "-o KbdInteractiveAuthentication=no -o StrictHostKeyChecking=no "
        "-o UserKnownHostsFile=/dev/null -o ConnectTimeout=6 "
        f"{_quote(upper_username)}@{_quote(upper.host)} {_quote(remote_command)}"
    )


def _sync_gpb_from_upper(environment: Environment, target: dict[str, str]) -> None:
    """Synchronize one GPB using the field-proven lower -> upper flow.

    The lower machine obtains the upper machine's wall-clock value over the
    reverse SSH trust and applies it locally with date --set. This mirrors the
    command operators use manually and, importantly, verifies that the same
    upper account used by TraceLens is truly passwordless from the lower host.
    """
    lower_machine = _deployment_target_credential_machine(environment, target)
    fetch_upper = _lower_to_upper_ssh_command(
        environment,
        'date +"%Y-%m-%d %H:%M:%S"',
    )
    command = (
        f"upper_time=$({fetch_upper}) || {{ echo '下位机反向 SSH 获取上位机时间失败（仍可能需要密码）' >&2; exit 41; }}; "
        "[ -n \"$upper_time\" ] || { echo '上位机 date 未返回时间' >&2; exit 42; }; "
        "date --set=\"$upper_time\" >/dev/null 2>&1 || "
        "{ echo '下位机 date --set 失败：当前账号可能没有修改系统时间权限' >&2; exit 43; }; "
        "printf '__TRACELENS_SYNC_EPOCH__='; date +%s"
    )
    result = ssh_execute(lower_machine, command, timeout=20)
    if result.exit_status != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or "下位机时间同步失败")
    if not re.search(r"(?m)^__TRACELENS_SYNC_EPOCH__=\d{9,}\s*$", result.stdout or ""):
        raise RuntimeError("下位机时间同步完成但未返回有效时间")


def sync_gpb_times(
    environment: Environment,
    gpb_ips: list[str],
    *,
    auto_sync: bool = True,
    include_dhh: bool = False,
    dhh_ip: str = "",
    dhh_user: str = "root",
) -> dict:
    targets = _deployment_remote_targets(
        gpb_ips,
        include_dhh=include_dhh,
        dhh_ip=dhh_ip,
        dhh_user=dhh_user,
    )
    machine = environment.upper_machine
    results: list[dict] = []
    for target in targets:
        host = target["host"]
        username = target["user"]
        try:
            upper_before = _read_epoch_from_upper(machine)
            if target["role"] == "gpb":
                lower_machine = _deployment_target_credential_machine(environment, target)
                lower_before = _read_epoch_from_machine(lower_machine, "下位机")
            else:
                # DHH keeps the existing upper -> target flow; reverse trust is
                # intentionally not required for DHH deployment.
                lower_before = _read_gpb_epoch(machine, host, username)
            before_delta = lower_before - upper_before
            synced = abs(before_delta) <= DEPLOYMENT_TIME_SYNC_TOLERANCE_SECONDS
            synchronized = False
            if not synced and auto_sync:
                if target["role"] == "gpb":
                    _sync_gpb_from_upper(environment, target)
                else:
                    _sync_gpb_epoch(machine, host, _read_epoch_from_upper(machine), username)
                synchronized = True
            upper_after = _read_epoch_from_upper(machine)
            if target["role"] == "gpb":
                lower_after = _read_epoch_from_machine(lower_machine, "下位机")
            else:
                lower_after = _read_gpb_epoch(machine, host, username)
            after_delta = lower_after - upper_after
            success = abs(after_delta) <= DEPLOYMENT_TIME_SYNC_TOLERANCE_SECONDS
            results.append({
                "host": host,
                "user": username,
                "role": target["role"],
                "success": success,
                "synchronized": synchronized,
                "before_delta_seconds": before_delta,
                "after_delta_seconds": after_delta,
                "message": "时间已同步" if success else "时间偏差仍超过允许范围",
            })
        except Exception as exc:
            results.append({
                "host": host,
                "user": username,
                "role": target["role"],
                "success": False,
                "synchronized": False,
                "before_delta_seconds": None,
                "after_delta_seconds": None,
                "message": str(exc),
            })
    return {
        "environment_id": environment.id,
        "upper_ip": environment.upper_machine.host,
        "gpb_ips": [item["host"] for item in targets if item["role"] == "gpb"],
        "include_dhh": bool(include_dhh),
        "dhh_ip": str(dhh_ip or "").strip() if include_dhh else "",
        "dhh_user": str(dhh_user or "root").strip() if include_dhh else "",
        "target_signature": _deployment_target_signature(targets),
        "tolerance_seconds": DEPLOYMENT_TIME_SYNC_TOLERANCE_SECONDS,
        "all_synced": all(item["success"] for item in results),
        "results": results,
        "checked_at": timezone.now().isoformat(),
    }


def _ensure_gpb_time_synced(
    environment: Environment,
    gpb_ips: list[str],
    *,
    include_dhh: bool = False,
    dhh_ip: str = "",
    dhh_user: str = "root",
) -> dict:
    """Best-effort deployment clock check/sync. Time drift never blocks deployment."""
    try:
        result = sync_gpb_times(
            environment,
            gpb_ips,
            auto_sync=True,
            include_dhh=include_dhh,
            dhh_ip=dhh_ip,
            dhh_user=dhh_user,
        )
    except Exception as exc:
        logger.warning(
            "deployment time sync check failed environment=%s; continuing deployment: %s",
            environment.id,
            exc,
            exc_info=True,
        )
        return {"all_synced": False, "results": [], "warning": str(exc)}
    missing = [item["host"] for item in result["results"] if not item["success"]]
    if missing:
        logger.warning(
            "deployment time sync incomplete environment=%s hosts=%s; continuing deployment",
            environment.id,
            ",".join(missing),
        )
    return result

def _run_serial_deployment_step(
    deployment: EnvironmentDeployment,
    machine,
    command: DeploymentCommand,
    *,
    dedicated_ssh: bool = False,
) -> None:
    step = DeploymentStep.objects.get(deployment=deployment, key=command.key)
    if command.skipped:
        step.status = DeploymentStepStatus.SKIPPED
        step.started_at = timezone.now()
        step.finished_at = step.started_at
        step.message = "当前模式不需要执行此步骤。"
        step.save(update_fields=["status", "started_at", "finished_at", "message", "updated_at"])
        _publish_deployment_state(deployment.id)
        return

    operator, client_ip, source = _deployment_trace_log(deployment)
    process_logger.info(
        "[DEPLOY] task=%s step.start name=%s key=%s host=%s operator=%s client_ip=%s source=%s params=%s",
        deployment.id, command.name, command.key, getattr(machine, "host", "-"),
        operator, client_ip, source, _step_parameter_log(deployment, command.key),
    )
    deployment.current_step = command.key
    deployment.message = f"正在执行：{command.name}"
    deployment.save(update_fields=["current_step", "message", "updated_at"])
    step.status = DeploymentStepStatus.RUNNING
    step.started_at = timezone.now()
    step.finished_at = None
    step.exit_status = None
    step.save(update_fields=["status", "started_at", "finished_at", "exit_status", "updated_at"])
    _publish_deployment_state(deployment.id)
    last_flush = [time.monotonic()]
    stdout_buffer: list[str] = []
    stderr_buffer: list[str] = []
    ordered_buffer: list[dict[str, str]] = []
    attempt_stdout: list[str] = []
    attempt_stderr: list[str] = []

    def flush(stdout: str, stderr: str) -> None:
        if stdout:
            attempt_stdout.append(stdout)
            stdout_buffer.append(stdout)
            ordered_buffer.append({"stream": "stdout", "text": stdout})
        if stderr:
            attempt_stderr.append(stderr)
            stderr_buffer.append(stderr)
            ordered_buffer.append({"stream": "stderr", "text": stderr})
        now = time.monotonic()
        if now - last_flush[0] >= 0.5:
            _append_step_output(
                step.id,
                "".join(stdout_buffer),
                "".join(stderr_buffer),
                chunks=list(ordered_buffer),
            )
            stdout_buffer.clear()
            stderr_buffer.clear()
            ordered_buffer.clear()
            last_flush[0] = now

    execute = _execute_streaming_dedicated if dedicated_ssh else _execute_streaming
    try:
        exit_status = execute(machine, _deployment_shell_command(command.command, deployment, command.key), command.timeout_seconds, flush)
        if stdout_buffer or stderr_buffer:
            _append_step_output(
                step.id,
                "".join(stdout_buffer),
                "".join(stderr_buffer),
                chunks=list(ordered_buffer),
            )
            stdout_buffer.clear()
            stderr_buffer.clear()
            ordered_buffer.clear()
        step.refresh_from_db()
        step.exit_status = exit_status
        step.finished_at = timezone.now()
        if _deployment_stop_requested(deployment.id):
            step.status = DeploymentStepStatus.STOPPED
            step.message = "任务已停止，当前部署脚本已终止。"
            step.save(update_fields=["exit_status", "finished_at", "status", "message", "updated_at"])
            _publish_deployment_state(deployment.id)
            raise DeploymentStopRequested("部署任务已请求停止。")
        current_output = f"{''.join(attempt_stdout)}\n{''.join(attempt_stderr)}"
        success, message = _check_success(command, deployment, step, attempt_output=current_output)
        step.status = DeploymentStepStatus.SUCCESS if success else DeploymentStepStatus.FAILED
        step.message = message
        step.save(update_fields=["exit_status", "finished_at", "status", "message", "updated_at"])
        _publish_deployment_state(deployment.id)
        process_logger.info(
            "[DEPLOY] task=%s step.finish name=%s key=%s status=%s exit=%s message=%s",
            deployment.id, command.name, command.key, step.status, exit_status, _safe_process_line(message),
        )
        if not success:
            raise RuntimeError(message)
    except DeploymentStopRequested:
        if stdout_buffer or stderr_buffer:
            _append_step_output(
                step.id,
                "".join(stdout_buffer),
                "".join(stderr_buffer),
                chunks=list(ordered_buffer),
            )
        raise
    except Exception as exc:
        if stdout_buffer or stderr_buffer:
            _append_step_output(
                step.id,
                "".join(stdout_buffer),
                "".join(stderr_buffer),
                chunks=list(ordered_buffer),
            )
        step.refresh_from_db()
        if _deployment_stop_requested(deployment.id):
            step.status = DeploymentStepStatus.STOPPED
            step.finished_at = timezone.now()
            step.message = "任务已停止，当前部署脚本已终止。"
            step.save(update_fields=["status", "finished_at", "message", "updated_at"])
            _publish_deployment_state(deployment.id)
            raise DeploymentStopRequested("部署任务已请求停止。") from exc
        step.status = DeploymentStepStatus.FAILED
        step.finished_at = timezone.now()
        step.message = str(exc)
        step.save(update_fields=["status", "finished_at", "message", "updated_at"])
        _publish_deployment_state(deployment.id)
        process_logger.error(
            "[DEPLOY] task=%s step.failed name=%s key=%s error=%s",
            deployment.id, command.name, command.key, _safe_process_line(str(exc)),
        )
        raise


def _run_initial_steps_in_parallel(
    deployment: EnvironmentDeployment,
    machine,
    commands: list[DeploymentCommand],
) -> None:
    runnable = [command for command in commands if not command.skipped]
    if not runnable:
        return

    operator, client_ip, source = _deployment_trace_log(deployment)
    for command in runnable:
        process_logger.info(
            "[DEPLOY] task=%s step.start name=%s key=%s host=%s operator=%s client_ip=%s source=%s params=%s",
            deployment.id, command.name, command.key, getattr(machine, "host", "-"),
            operator, client_ip, source, _step_parameter_log(deployment, command.key),
        )

    step_by_key: dict[str, DeploymentStep] = {}
    attempt_stdout: dict[str, list[str]] = {command.key: [] for command in runnable}
    attempt_stderr: dict[str, list[str]] = {command.key: [] for command in runnable}
    stdout_buffer: dict[str, list[str]] = {command.key: [] for command in runnable}
    stderr_buffer: dict[str, list[str]] = {command.key: [] for command in runnable}
    ordered_buffer: dict[str, list[dict[str, str]]] = {command.key: [] for command in runnable}
    output_queue: queue.Queue[tuple[str, str, str]] = queue.Queue()

    started_at = timezone.now()
    for command in runnable:
        step = DeploymentStep.objects.get(deployment=deployment, key=command.key)
        step.status = DeploymentStepStatus.RUNNING
        step.started_at = started_at
        step.finished_at = None
        step.exit_status = None
        step.save(update_fields=["status", "started_at", "finished_at", "exit_status", "updated_at"])
        step_by_key[command.key] = step

    deployment.current_step = runnable[0].key
    deployment.message = (
        "正在并行执行：停止上位机进程 / 拉取软件包"
        if len(runnable) > 1
        else f"正在执行：{runnable[0].name}"
    )
    deployment.save(update_fields=["current_step", "message", "updated_at"])
    _publish_deployment_state(deployment.id)

    def worker(command: DeploymentCommand) -> int:
        def collect(stdout: str, stderr: str) -> None:
            output_queue.put((command.key, stdout, stderr))

        return _execute_streaming_dedicated(machine, _deployment_shell_command(command.command, deployment, command.key), command.timeout_seconds, collect)

    failures: list[str] = []
    futures = {}
    last_flush = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(runnable), thread_name_prefix=f"env-deploy-{deployment.id}-initial") as executor:
        for command in runnable:
            futures[executor.submit(worker, command)] = command

        pending = set(futures)
        while pending or not output_queue.empty():
            try:
                key, stdout, stderr = output_queue.get(timeout=0.2 if pending else 0.01)
                if stdout:
                    attempt_stdout[key].append(stdout)
                    stdout_buffer[key].append(stdout)
                    ordered_buffer[key].append({"stream": "stdout", "text": stdout})
                if stderr:
                    attempt_stderr[key].append(stderr)
                    stderr_buffer[key].append(stderr)
                    ordered_buffer[key].append({"stream": "stderr", "text": stderr})
            except queue.Empty:
                pass

            pending = {future for future in pending if not future.done()}
            now = time.monotonic()
            if now - last_flush >= 0.5:
                for command in runnable:
                    key = command.key
                    if stdout_buffer[key] or stderr_buffer[key]:
                        _append_step_output(
                            step_by_key[key].id,
                            "".join(stdout_buffer[key]),
                            "".join(stderr_buffer[key]),
                            chunks=list(ordered_buffer[key]),
                        )
                        stdout_buffer[key].clear()
                        stderr_buffer[key].clear()
                        ordered_buffer[key].clear()
                last_flush = now

        for command in runnable:
            key = command.key
            if stdout_buffer[key] or stderr_buffer[key]:
                _append_step_output(
                    step_by_key[key].id,
                    "".join(stdout_buffer[key]),
                    "".join(stderr_buffer[key]),
                    chunks=list(ordered_buffer[key]),
                )
                stdout_buffer[key].clear()
                stderr_buffer[key].clear()
                ordered_buffer[key].clear()

        for future, command in futures.items():
            step = step_by_key[command.key]
            try:
                exit_status = future.result()
                step.refresh_from_db()
                step.exit_status = exit_status
                step.finished_at = timezone.now()
                if _deployment_stop_requested(deployment.id):
                    step.status = DeploymentStepStatus.STOPPED
                    step.message = "任务已停止，当前部署脚本已终止。"
                    step.save(update_fields=["exit_status", "finished_at", "status", "message", "updated_at"])
                    _publish_deployment_state(deployment.id)
                    continue
                current_output = f"{''.join(attempt_stdout[command.key])}\n{''.join(attempt_stderr[command.key])}"
                success, message = _check_success(command, deployment, step, attempt_output=current_output)
                step.status = DeploymentStepStatus.SUCCESS if success else DeploymentStepStatus.FAILED
                step.message = message
                step.save(update_fields=["exit_status", "finished_at", "status", "message", "updated_at"])
                _publish_deployment_state(deployment.id)
                process_logger.info(
                    "[DEPLOY] task=%s parallel.step.finish name=%s key=%s status=%s exit=%s message=%s",
                    deployment.id, command.name, command.key, step.status, exit_status, _safe_process_line(message),
                )
                if not success:
                    failures.append(f"{command.name}：{message}")
            except Exception as exc:
                step.refresh_from_db()
                if _deployment_stop_requested(deployment.id):
                    step.status = DeploymentStepStatus.STOPPED
                    step.finished_at = timezone.now()
                    step.message = "任务已停止，当前部署脚本已终止。"
                    step.save(update_fields=["status", "finished_at", "message", "updated_at"])
                    _publish_deployment_state(deployment.id)
                    continue
                step.status = DeploymentStepStatus.FAILED
                step.finished_at = timezone.now()
                step.message = str(exc)
                step.save(update_fields=["status", "finished_at", "message", "updated_at"])
                _publish_deployment_state(deployment.id)
                process_logger.error(
                    "[DEPLOY] task=%s parallel.step.failed name=%s key=%s error=%s",
                    deployment.id, command.name, command.key, _safe_process_line(str(exc)),
                )
                failures.append(f"{command.name}：{exc}")

    if failures:
        raise RuntimeError("；".join(failures))


def _ensure_initial_steps_succeeded(deployment: EnvironmentDeployment) -> None:
    states = {
        step.key: step.status
        for step in DeploymentStep.objects.filter(deployment=deployment, key__in=[STEP_STOP, STEP_DEPLOY])
    }
    accepted = {DeploymentStepStatus.SUCCESS, DeploymentStepStatus.SKIPPED}
    missing = [STEP_NAMES[key] for key in (STEP_STOP, STEP_DEPLOY) if states.get(key) not in accepted]
    if missing:
        raise RuntimeError(f"并行前置阶段仍未成功：{'、'.join(missing)}。请重试失败阶段。")


def _deployment_runner_heartbeat(deployment_id: int, token: str, stop_event: threading.Event) -> None:
    interval = 2.0
    DeploymentEventBus.refresh_runner_lease(deployment_id, token)
    while not stop_event.wait(interval):
        DeploymentEventBus.refresh_runner_lease(deployment_id, token)


def _run_deployment(
    deployment_id: int,
    start_key: str | None = None,
    scheduled_preflight: bool = False,
    runner_token: str | None = None,
) -> None:
    close_old_connections()
    runner_token = runner_token or uuid.uuid4().hex
    heartbeat_stop = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_deployment_runner_heartbeat,
        args=(deployment_id, runner_token, heartbeat_stop),
        daemon=True,
        name=f"env-deploy-heartbeat-{deployment_id}",
    )
    DeploymentEventBus.refresh_runner_lease(deployment_id, runner_token)
    heartbeat_thread.start()
    process_logger.debug(
        "[DEPLOY] task=%s runner.start start_key=%s scheduled_preflight=%s",
        deployment_id, start_key or "<initial>", scheduled_preflight,
    )
    try:
        if scheduled_preflight:
            process_logger.debug("[DEPLOY] task=%s preflight.start", deployment_id)
            EnvironmentDeployment.objects.filter(pk=deployment_id, status=DeploymentStatus.PENDING).update(
                message="正在执行部署前检查：SSH 互信 / 时间同步（时间同步为告警项）。"
            )
            _publish_deployment_state(deployment_id)
            scheduled_deployment = EnvironmentDeployment.objects.select_related("environment__upper_machine").get(pk=deployment_id)
            _raise_if_deployment_stop_requested(deployment_id)
            _ensure_gpb_ssh_trust(
                scheduled_deployment.environment,
                scheduled_deployment.gpb_ips,
                include_dhh=scheduled_deployment.include_dhh,
                dhh_ip=scheduled_deployment.dhh_ip,
                dhh_user=scheduled_deployment.dhh_user,
            )
            time_result = _ensure_gpb_time_synced(
                scheduled_deployment.environment,
                scheduled_deployment.gpb_ips,
                include_dhh=scheduled_deployment.include_dhh,
                dhh_ip=scheduled_deployment.dhh_ip,
                dhh_user=scheduled_deployment.dhh_user,
            )
            if not time_result.get("all_synced", False):
                EnvironmentDeployment.objects.filter(pk=deployment_id).update(
                    message="上下位机时间未同步，按告警继续部署。"
                )
                _publish_deployment_state(deployment_id)
            _raise_if_deployment_stop_requested(deployment_id)
            process_logger.debug(
                "[DEPLOY] task=%s preflight.finish time_synced=%s",
                deployment_id, bool(time_result.get("all_synced", False)),
            )

        should_stop_before_start = False
        with transaction.atomic():
            deployment = EnvironmentDeployment.objects.select_for_update().select_related("environment__upper_machine").get(pk=deployment_id)
            if deployment.status == DeploymentStatus.STOPPED:
                return
            if deployment.status == DeploymentStatus.STOPPING:
                should_stop_before_start = True
            else:
                deployment.status = DeploymentStatus.RUNNING
                if deployment.started_at is None:
                    deployment.started_at = timezone.now()
                deployment.finished_at = None
                deployment.message = "部署任务已开始。" if start_key is None else f"从 {STEP_NAMES.get(start_key, start_key)} 阶段继续部署。"
                deployment.save(update_fields=["status", "started_at", "finished_at", "message", "updated_at"])
        if should_stop_before_start:
            _finish_deployment_stopped(deployment_id)
            return
        _publish_deployment_state(deployment.id)
        commands = build_commands(deployment.configuration)
        command_by_key = {command.key: command for command in commands}
        machine = deployment.environment.upper_machine

        if start_key is None:
            _raise_if_deployment_stop_requested(deployment.id)
            _run_initial_steps_in_parallel(
                deployment,
                machine,
                [command_by_key[STEP_STOP], command_by_key[STEP_DEPLOY]],
            )
            _raise_if_deployment_stop_requested(deployment.id)
            serial_start_key = STEP_TB
        elif start_key in {STEP_STOP, STEP_DEPLOY}:
            _raise_if_deployment_stop_requested(deployment.id)
            _run_serial_deployment_step(
                deployment,
                machine,
                command_by_key[start_key],
                dedicated_ssh=True,
            )
            _raise_if_deployment_stop_requested(deployment.id)
            _ensure_initial_steps_succeeded(deployment)
            serial_start_key = STEP_TB
        else:
            _raise_if_deployment_stop_requested(deployment.id)
            _ensure_initial_steps_succeeded(deployment)
            # Retrying the final start must always re-run the automatic safety guard.
            serial_start_key = STEP_PRESTART_STOP if start_key == STEP_START else start_key

        start_reached = False
        for command in commands:
            if command.key in {STEP_STOP, STEP_DEPLOY}:
                continue
            if not start_reached:
                if command.key != serial_start_key:
                    continue
                start_reached = True
            _raise_if_deployment_stop_requested(deployment.id)
            if command.key == STEP_PRESTART_STOP:
                _run_prestart_stop_step(deployment, machine, command)
            else:
                _run_serial_deployment_step(deployment, machine, command)
            _raise_if_deployment_stop_requested(deployment.id)

        if not start_reached:
            raise RuntimeError(f"找不到可重试的部署阶段：{serial_start_key}")
        _raise_if_deployment_stop_requested(deployment.id)
        stop_won_final_race = False
        with transaction.atomic():
            final_deployment = EnvironmentDeployment.objects.select_for_update().get(pk=deployment_id)
            if final_deployment.status == DeploymentStatus.STOPPING:
                stop_won_final_race = True
            else:
                final_deployment.status = DeploymentStatus.SUCCESS
                runnable_keys = [command.key for command in commands if not command.skipped]
                final_deployment.current_step = runnable_keys[-1] if runnable_keys else ""
                final_deployment.finished_at = timezone.now()
                final_deployment.message = "环境部署完成。"
                final_deployment.save(update_fields=["status", "current_step", "finished_at", "message", "updated_at"])
        if stop_won_final_race:
            _finish_deployment_stopped(deployment_id)
        else:
            _publish_deployment_state(deployment_id)
            process_logger.info("[DEPLOY] task=%s success", deployment_id)
    except DeploymentStopRequested:
        process_logger.warning("[DEPLOY] task=%s stopped", deployment_id)
        _finish_deployment_stopped(deployment_id)
    except Exception as exc:
        process_logger.error("[DEPLOY] task=%s failed error=%s", deployment_id, _safe_process_line(str(exc)))
        try:
            stop_won_failure_race = False
            with transaction.atomic():
                failed_deployment = EnvironmentDeployment.objects.select_for_update().get(pk=deployment_id)
                if failed_deployment.status == DeploymentStatus.STOPPING:
                    stop_won_failure_race = True
                else:
                    logger.exception("environment deployment failed id=%s", deployment_id)
                    failed_deployment.status = DeploymentStatus.FAILED
                    failed_deployment.finished_at = timezone.now()
                    failed_deployment.message = str(exc)
                    failed_deployment.save(update_fields=["status", "finished_at", "message", "updated_at"])
            if stop_won_failure_race:
                _finish_deployment_stopped(deployment_id)
            else:
                _publish_deployment_state(deployment_id)
        except Exception:
            logger.exception("failed to persist deployment failure id=%s", deployment_id)
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=0.5)
        DeploymentEventBus.clear_runner_lease(deployment_id, runner_token)
        process_logger.debug("[DEPLOY] task=%s runner.stop", deployment_id)
        close_old_connections()



def recover_orphaned_scheduled_deployments(limit: int = 20) -> int:
    """Return scheduled tasks stuck in PENDING before execution back to the queue.

    This covers the narrow restart window after the scheduler has claimed a due task
    but before `_run_deployment` has recorded `started_at`. A live runner heartbeat
    always wins, so a slow preflight is never duplicated.
    """
    cutoff = timezone.now() - timedelta(seconds=20)
    candidates = list(EnvironmentDeployment.objects.filter(
        status=DeploymentStatus.PENDING,
        scheduled_at__isnull=False,
        started_at__isnull=True,
        updated_at__lt=cutoff,
    ).order_by("updated_at").values_list("id", flat=True)[:max(1, int(limit))])
    recovered = 0
    for deployment_id in candidates:
        if DeploymentEventBus.runner_alive(deployment_id) is not False:
            continue
        changed = EnvironmentDeployment.objects.filter(
            pk=deployment_id,
            status=DeploymentStatus.PENDING,
            scheduled_at__isnull=False,
            started_at__isnull=True,
        ).update(status=DeploymentStatus.SCHEDULED, message="后端恢复后重新加入定时部署队列。")
        if changed:
            recovered += 1
            _publish_deployment_state(deployment_id)
    return recovered


def activate_due_scheduled_deployments(limit: int = 10, *, launch: bool = True) -> int:
    """Claim due scheduled deployments and start them without any browser session.

    SQLite is authoritative. A short Redis per-environment lock prevents multiple ASGI
    workers from claiming different due tasks for the same environment at the same time.
    The database status transition from SCHEDULED -> PENDING is the final claim guard.
    """
    now = timezone.now()
    candidates = list(
        EnvironmentDeployment.objects.filter(
            status=DeploymentStatus.SCHEDULED,
            scheduled_at__isnull=False,
            scheduled_at__lte=now,
        ).order_by("scheduled_at", "id").values_list("id", "environment_id")[:max(1, int(limit))]
    )
    started = 0
    for deployment_id, environment_id in candidates:
        lock_token = uuid.uuid4().hex
        lock_result = DeploymentEventBus.try_schedule_lock(environment_id, lock_token, ttl_seconds=10)
        if lock_result is False:
            continue
        try:
            with transaction.atomic():
                deployment = EnvironmentDeployment.objects.select_for_update().get(pk=deployment_id)
                if deployment.status != DeploymentStatus.SCHEDULED or not deployment.scheduled_at or deployment.scheduled_at > timezone.now():
                    continue
                busy_exists = EnvironmentDeployment.objects.filter(
                    environment_id=environment_id,
                    status__in=[DeploymentStatus.PENDING, DeploymentStatus.RUNNING, DeploymentStatus.STOPPING],
                ).exclude(pk=deployment_id).exists()
                if busy_exists:
                    if deployment.message != "计划时间已到，等待当前环境的部署任务结束。":
                        deployment.message = "计划时间已到，等待当前环境的部署任务结束。"
                        deployment.save(update_fields=["message", "updated_at"])
                        _publish_deployment_state(deployment.id)
                    continue
                claimed = EnvironmentDeployment.objects.filter(
                    pk=deployment_id, status=DeploymentStatus.SCHEDULED
                ).update(status=DeploymentStatus.PENDING, message="计划时间已到，正在执行部署前检查。")
                if claimed != 1:
                    continue
            _publish_deployment_state(deployment_id)
            if launch:
                thread = threading.Thread(
                    target=_run_deployment,
                    args=(deployment_id, None, True),
                    daemon=True,
                    name=f"env-deploy-scheduled-{deployment_id}",
                )
                thread.start()
            started += 1
        finally:
            if lock_result is True:
                DeploymentEventBus.release_schedule_lock(environment_id, lock_token)
    return started


def retry_deployment_step(environment: Environment, deployment_id: int, step_key: str) -> EnvironmentDeployment:
    step_key = str(step_key or "").strip()
    if not step_key:
        raise ValueError("请选择需要重试的失败阶段。")

    with transaction.atomic():
        locked_environment = Environment.objects.select_for_update().get(pk=environment.pk)
        try:
            deployment = EnvironmentDeployment.objects.select_for_update().get(
                pk=deployment_id,
                environment=locked_environment,
            )
        except EnvironmentDeployment.DoesNotExist as exc:
            raise ValueError("部署记录不存在。") from exc

        if deployment.status != DeploymentStatus.FAILED:
            raise ValueError("只有失败的部署任务可以重试。")
        if locked_environment.deployments.exclude(pk=deployment.pk).filter(
            status__in=[DeploymentStatus.PENDING, DeploymentStatus.RUNNING, DeploymentStatus.STOPPING]
        ).exists():
            raise ValueError("当前环境已有其他部署任务正在执行，暂时不能重试。")

        commands = build_commands(deployment.configuration)
        order_by_key = {item.key: index for index, item in enumerate(commands)}
        if step_key not in order_by_key:
            raise ValueError("部署阶段不存在，无法重试。")
        command_by_key = {item.key: item for item in commands}
        if command_by_key[step_key].skipped:
            raise ValueError("当前阶段在该部署模式下已跳过，不能重试。")

        try:
            failed_step = DeploymentStep.objects.select_for_update().get(deployment=deployment, key=step_key)
        except DeploymentStep.DoesNotExist as exc:
            raise ValueError("部署阶段记录不存在。") from exc
        if failed_step.status != DeploymentStepStatus.FAILED:
            raise ValueError("只有失败阶段才可以重试。")

        retry_number = failed_step.retry_count + 1
        timestamp = timezone.localtime().strftime("%Y-%m-%d %H:%M:%S")
        separator = f"\n\n===== 第 {retry_number} 次重试 · {timestamp} =====\n"
        failed_step.stdout = (failed_step.stdout or "") + separator
        failed_step.process_log = (failed_step.process_log or "") + separator
        failed_step.retry_count = retry_number
        failed_step.status = DeploymentStepStatus.PENDING
        failed_step.exit_status = None
        failed_step.finished_at = None
        failed_step.message = f"等待第 {retry_number} 次重试。"
        failed_step.save(update_fields=[
            "stdout", "process_log", "retry_count", "status", "exit_status", "finished_at", "message", "updated_at"
        ])

        retry_index = order_by_key[step_key]
        if step_key in {STEP_STOP, STEP_DEPLOY}:
            # stop.sh and deploy.sh are parallel peers. Retrying one failed peer
            # must preserve the successful result of the other peer, while all
            # dependent stages are reset for a deterministic resume.
            downstream_commands = [command for command in commands if command.key not in {STEP_STOP, STEP_DEPLOY}]
        else:
            downstream_commands = commands[retry_index + 1:]

        for command in downstream_commands:
            step = DeploymentStep.objects.select_for_update().get(deployment=deployment, key=command.key)
            if command.skipped:
                step.status = DeploymentStepStatus.SKIPPED
                step.message = "当前模式不需要执行此步骤。"
                step.save(update_fields=["status", "message", "updated_at"])
                continue
            step.status = DeploymentStepStatus.PENDING
            step.exit_status = None
            step.started_at = None
            step.finished_at = None
            step.message = ""
            step.save(update_fields=[
                "status", "exit_status", "started_at", "finished_at", "message", "updated_at"
            ])

        deployment.status = DeploymentStatus.PENDING
        deployment.current_step = step_key
        deployment.finished_at = None
        deployment.message = f"等待重试：{failed_step.name}"
        deployment.save(update_fields=["status", "current_step", "finished_at", "message", "updated_at"])

    _publish_deployment_state(deployment.id)
    return deployment

def _parse_scheduled_at(value) -> timezone.datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = parse_datetime(raw)
    if parsed is None:
        raise ValueError("计划执行时间格式无效。")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    if parsed <= timezone.now():
        raise ValueError("计划执行时间必须晚于当前时间。")
    return parsed


def start_deployment(environment: Environment, payload: dict) -> EnvironmentDeployment:
    request_payload = dict(payload or {})
    scheduled_at = _parse_scheduled_at(request_payload.pop("scheduled_at", None))
    normalized = validate_payload(environment, request_payload)
    # Remote SSH trust/time preflight is deliberately executed by deployment_worker,
    # never inside the HTTP request. The UI may already have checked it, but the worker
    # revalidates immediately before execution so task creation remains non-blocking.
    commands = build_commands(normalized)
    now = timezone.now()
    with transaction.atomic():
        locked_environment = Environment.objects.select_for_update().get(pk=environment.pk)
        if scheduled_at is None and locked_environment.deployments.filter(
            status__in=[DeploymentStatus.PENDING, DeploymentStatus.RUNNING, DeploymentStatus.STOPPING]
        ).exists():
            raise ValueError("当前环境已有部署任务正在执行，请等待任务完成后再发起新的部署。")
        if normalized.get("save_post_start_script") and normalized.get("post_start_script"):
            _remember_post_start_script(locked_environment, normalized["post_start_script"])
        deployment = EnvironmentDeployment.objects.create(
            environment=locked_environment,
            task_name=normalized["task_name"],
            target_version=normalized["target_version"],
            simulation_mode=normalized["simulation_mode"],
            include_sdk=bool(normalized.get("include_sdk")),
            upper_ip=normalized["upper_ip"],
            gpb_ips=normalized["gpb_ips"],
            tb_mode=normalized.get("tb_mode", ""),
            install_mode=normalized.get("install_mode", normalized.get("gpb_mode", "")),
            install_port=normalized["install_port"],
            include_dhh=bool(normalized.get("include_dhh")),
            dhh_ip=normalized.get("dhh_ip", ""),
            dhh_user=normalized.get("dhh_user", "root"),
            dhh_machine_id=normalized.get("dhh_machine_id", ""),
            configuration=normalized,
            command_snapshot=[
                {"key": item.key, "name": item.name, "command": item.command, "skipped": item.skipped}
                for item in commands if not item.auto
            ],
            status=DeploymentStatus.SCHEDULED if scheduled_at else DeploymentStatus.PENDING,
            scheduled_at=scheduled_at,
            message=(
                f"计划于 {timezone.localtime(scheduled_at).strftime('%Y-%m-%d %H:%M:%S')} 执行。"
                if scheduled_at
                else (
                    "等待部署执行器接管；当前未检测到 deployment_worker 心跳，请确认部署执行器已启动。"
                    if DeploymentEventBus.worker_alive() is False
                    else "等待部署执行器接管。"
                )
            ),
        )
        selected_steps = set(normalized.get("selected_steps") or []) if normalized.get("selected_steps") is not None else None
        for order, command in enumerate(commands, start=1):
            user_skipped = selected_steps is not None and command.key not in selected_steps
            DeploymentStep.objects.create(
                deployment=deployment,
                key=command.key,
                name=command.name,
                sort_order=order,
                status=DeploymentStepStatus.SKIPPED if command.skipped else DeploymentStepStatus.PENDING,
                command=command.command,
                success_marker=command.success_marker,
                message=(
                    "本次未执行启动环境，无需停止下位机进程。"
                    if command.auto and command.skipped
                    else "本次复制重试未选择执行此步骤。"
                    if command.skipped and user_skipped
                    else "当前模式不需要执行此步骤。" if command.skipped else ""
                ),
                started_at=now if command.skipped else None,
                finished_at=now if command.skipped else None,
            )
    operator, client_ip, source = _deployment_trace_log(deployment)
    process_logger.info(
        "[DEPLOY] task=%s created environment=%s operator=%s client_ip=%s source=%s version=%s mode=%s scheduled_at=%s",
        deployment.id, environment.id, operator, client_ip, source, deployment.target_version,
        deployment.simulation_mode, deployment.scheduled_at.isoformat() if deployment.scheduled_at else "immediate",
    )
    _publish_deployment_state(deployment.id)
    return deployment
