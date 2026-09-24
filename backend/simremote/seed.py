"""把模拟机群写入 TraceLens 的环境资源。

做四件事：
1. 配置下位机账号（ResourceSettings.lower_*），让资源页能带上凭据；
2. 写入上位机 / 下位机 Machine（注意唯一约束是 host+ssh_port+username）；
3. 建立环境拓扑（Environment + MachineRelation）；
4. 启用运行日志检索并刷新日志目录索引（subsystems / global_catalog）。

注意 ``Environment.upper_machine`` 是 **OneToOneField**：一台机器只能属于一个
环境，所以这里按上位机认领环境，而不是无脑 get_or_create(name=...)。
"""

from __future__ import annotations

from apps.environments.models import (
    Environment,
    EnvironmentStatus,
    LogPathCategory,
    LogPathProfile,
    MachineRelation,
    RelationSource,
    ResourceSettings,
)
from apps.logsources.services.remote_logs import scan_environment_logs
from apps.machines.models import AuthenticationType, Machine, MachineOrigin, MachineRole

from . import fleet


def _upsert_machine(spec: fleet.MachineSpec) -> Machine:
    machine, _created = Machine.objects.get_or_create(
        host=spec.host,
        ssh_port=spec.ssh_port,
        username=fleet.SIM_USERNAME,
        defaults={"name": spec.name, "role": spec.role},
    )
    machine.name = spec.name
    machine.role = spec.role
    machine.station_id = spec.station_id
    machine.station_type = spec.station_type
    machine.station_name = spec.station_name
    machine.description = spec.description
    machine.origin = MachineOrigin.MANUAL
    machine.auth_type = AuthenticationType.PASSWORD
    machine.is_active = True
    machine.software_version = fleet.SOFTWARE_VERSION
    machine.set_password(fleet.SIM_PASSWORD)
    machine.save()
    return machine


def _ensure_environment(upper: Machine) -> Environment:
    environment = Environment.objects.filter(upper_machine=upper).first()
    if environment is None:
        environment = Environment.objects.filter(name=fleet.ENVIRONMENT_NAME).first()
    if environment is None:
        environment = Environment(name=fleet.ENVIRONMENT_NAME, upper_machine=upper)
    environment.name = fleet.ENVIRONMENT_NAME
    environment.upper_machine = upper
    environment.status = EnvironmentStatus.READY
    environment.software_version = fleet.SOFTWARE_VERSION
    environment.station_user_id = fleet.STATION_USER_ID
    environment.description = "本地模拟环境（假 SSH/SFTP 机群，用于日志链路联调）"
    environment.save()
    return environment


def _wire_topology(environment: Environment, upper: Machine) -> list[Machine]:
    lowers: list[Machine] = []
    for spec in fleet.FLEET:
        if spec.role != MachineRole.LOWER:
            continue
        machine = _upsert_machine(spec)
        lowers.append(machine)
        MachineRelation.objects.update_or_create(
            environment=environment,
            source_machine=upper,
            target_machine=machine,
            defaults={"source": RelationSource.MANUAL, "is_active": True},
        )
    return lowers


def _configure_lower_credentials(lowers: list[Machine]) -> ResourceSettings:
    settings_obj = ResourceSettings.get_solo()
    if lowers:
        settings_obj.lower_username = fleet.SIM_USERNAME
        settings_obj.lower_ssh_port = lowers[0].ssh_port
        settings_obj.lower_auth_type = AuthenticationType.PASSWORD
        settings_obj.set_lower_password(fleet.SIM_PASSWORD)
    settings_obj.save()
    return settings_obj


def _enable_run_profile() -> None:
    """运行日志默认是关闭的，这里打开以便前端能查 run 事件日志。"""
    LogPathProfile.objects.filter(category=LogPathCategory.RUN).update(enabled=True)


def seed(*, refresh: bool = True) -> dict:
    settings_obj = ResourceSettings.get_solo()
    upper = _upsert_machine(fleet.UPPER)
    environment = _ensure_environment(upper)
    lowers = _wire_topology(environment, upper)
    _configure_lower_credentials(lowers)
    _enable_run_profile()

    payload: dict = {
        "environment": environment.name,
        "environment_id": environment.id,
        "status_label": environment.get_status_display(),
        "software_version": environment.software_version,
        "upper": f"{upper.name} ({upper.host}:{upper.ssh_port})",
        "lowers": [f"{item.name} ({item.host}:{item.ssh_port})" for item in lowers],
        "profiles": [
            f"{item.display_name}{'（启用）' if item.enabled else '（关闭）'}"
            for item in settings_obj.log_path_profiles.order_by("sort_order", "id")
        ],
        "catalog_global": [],
        "subsystems": [],
        "catalog_error": None,
    }

    if refresh:
        try:
            result = scan_environment_logs(environment, refresh=True)
        except Exception as exc:  # noqa: BLE001 - 扫描失败不应阻塞资源写入
            payload["catalog_error"] = f"{type(exc).__name__}: {exc}"
            return payload
        payload["subsystems"] = list(result.get("subsystems") or [])
        catalog = result.get("global_catalog") or []
        payload["catalog_global"] = [item.get("name") for item in catalog if isinstance(item, dict)]
        payload["catalog_fm_count"] = sum(
            len(item.get("fms") or []) for item in catalog if isinstance(item, dict)
        )
        payload["sources"] = result.get("sources") or []

    return payload
