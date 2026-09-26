from __future__ import annotations

import logging
import re
from dataclasses import asdict
from copy import copy

from django.db import transaction
from django.utils import timezone

from apps.common.services.ssh import read_text
from apps.environments.models import (
    DiscoveryStatus,
    Environment,
    EnvironmentDiscovery,
    EnvironmentStatus,
    MachineRelation,
    RelationSource,
    ResourceSettings,
)
from apps.environments.services.stations_parser import StationsTopology, parse_stations_xml
from apps.machines.models import Machine, MachineOrigin, MachineRole

logger = logging.getLogger("tracelens.discovery")


_VERSION_LINE_RE = re.compile(
    r"\bCurrent(?:\s+[A-Za-z0-9_.-]+)*\s+Version\s*:\s*(?P<version>.+?)\s*$",
    re.IGNORECASE,
)


def parse_version_text(content: str) -> str:
    """Parse both `Current Version:` and product-qualified variants.

    Real version files contain lines such as `Current XY Version: XY V100...`.
    Keep the parser deliberately line-oriented so unrelated historic version lines are
    not mistaken for the current version.
    """
    for line in content.splitlines():
        match = _VERSION_LINE_RE.search(line.strip().strip("'\""))
        if match:
            return match.group("version").strip().strip("'\"")
    return ""


def read_machine_software_version(machine: Machine, settings_obj: ResourceSettings | None = None) -> str:
    settings_obj = settings_obj or ResourceSettings.get_solo()
    logger.info(
        "version.machine_query.start machine=%s host=%s path=%s",
        machine.id,
        machine.host,
        settings_obj.version_file_path,
    )
    content = read_text(machine, settings_obj.version_file_path, max_bytes=500_000)
    version = parse_version_text(content)
    if not version:
        logger.warning(
            "version.machine_query.marker_missing machine=%s host=%s path=%s",
            machine.id, machine.host, settings_obj.version_file_path,
        )
        raise ValueError(
            f"机器 {machine.host} 的版本文件 {settings_obj.version_file_path} 中未找到 "
            "Current Version: / Current <Product> Version:。"
        )
    machine.software_version = version
    machine.version_checked_at = timezone.now()
    machine.save(update_fields=["software_version", "version_checked_at", "updated_at"])
    logger.info("version.machine_query.success machine=%s host=%s version=%s", machine.id, machine.host, version)
    return version


def read_software_version(environment: Environment, settings_obj: ResourceSettings | None = None) -> str:
    settings_obj = settings_obj or ResourceSettings.get_solo()
    logger.info(
        "version.query.start environment=%s machine=%s path=%s",
        environment.id,
        environment.upper_machine_id,
        settings_obj.version_file_path,
    )
    version = read_machine_software_version(environment.upper_machine, settings_obj)
    environment.software_version = version
    environment.version_checked_at = timezone.now()
    environment.save(update_fields=["software_version", "version_checked_at", "updated_at"])
    logger.info("version.query.success environment=%s version=%s", environment.id, version)
    return version


def _relation_is_dhh(relation: MachineRelation) -> bool:
    machine = relation.target_machine
    metadata = relation.metadata or {}
    station_name = str(metadata.get("station_name") or machine.station_name or machine.name or "").strip().lower()
    station_type = str(metadata.get("station_type") or machine.station_type or "").strip().upper()
    return station_name == "dhh" or station_type == "DHH"


def read_environment_versions(environment: Environment, settings_obj: ResourceSettings | None = None) -> dict:
    """Read the configured version file on the upper machine and every normal lower machine.

    Lower-machine failures are returned per machine so one offline station does not hide the
    successfully queried versions of the rest of the environment. DHH and 192.* small-network
    endpoints keep their existing special handling and are excluded from consistency checks.
    """
    settings_obj = settings_obj or ResourceSettings.get_solo()
    upper_version = read_software_version(environment, settings_obj)
    lower_results = []
    mismatched_machine_ids = []
    relations = list(
        environment.machine_relations.select_related("target_machine").filter(is_active=True)
    )
    for relation in relations:
        machine = relation.target_machine
        if _relation_is_dhh(relation):
            continue
        metadata = relation.metadata or {}
        mapped_host = str(metadata.get("topology_ip") or "").strip()
        probe_machine = machine
        if mapped_host and mapped_host != str(machine.host or "").strip():
            probe_machine = copy(machine)
            setattr(probe_machine, "_tracelens_host_override", mapped_host)
        if str(machine.host or "").strip().startswith("192.") and not mapped_host:
            lower_results.append({
                "machine_id": machine.id,
                "host": machine.host,
                "version": machine.software_version,
                "skipped": True,
                "message": "小网环境（192.*）且没有大网 IP 映射，跳过 SSH 版本查询。",
            })
            continue
        try:
            version = read_machine_software_version(probe_machine, settings_obj)
            mismatch = bool(upper_version and version and version.strip() != upper_version.strip())
            if mismatch:
                mismatched_machine_ids.append(machine.id)
            lower_results.append({
                "machine_id": machine.id,
                "host": machine.host,
                "version": version,
                "mismatch": mismatch,
                "skipped": False,
                "message": "",
            })
        except Exception as exc:
            logger.warning(
                "version.lower_query.failed environment=%s machine=%s host=%s probe_host=%s error=%s",
                environment.id, machine.id, machine.host, mapped_host or machine.host, exc,
            )
            lower_results.append({
                "machine_id": machine.id,
                "host": machine.host,
                "version": machine.software_version,
                "mismatch": False,
                "skipped": False,
                "message": str(exc),
            })
    return {
        "environment_id": environment.id,
        "version": upper_version,
        "checked_at": environment.version_checked_at,
        "lower_versions": lower_results,
        "version_mismatch": bool(mismatched_machine_ids),
        "mismatched_machine_ids": mismatched_machine_ids,
    }


def _apply_lower_credentials(machine: Machine, settings_obj: ResourceSettings) -> None:
    machine.username = settings_obj.lower_username
    machine.ssh_port = settings_obj.lower_ssh_port
    machine.auth_type = settings_obj.lower_auth_type
    if settings_obj.lower_auth_type == "password":
        machine.set_password(settings_obj.get_lower_password())
        machine.encrypted_private_key = ""
        machine.encrypted_private_key_passphrase = ""
    elif settings_obj.lower_auth_type == "private_key":
        machine.set_private_key(settings_obj.get_lower_private_key())
        machine.set_private_key_passphrase(settings_obj.get_lower_private_key_passphrase())
        machine.encrypted_password = ""


@transaction.atomic
def synchronize_topology(environment: Environment, topology: StationsTopology) -> dict:
    settings_obj = ResourceSettings.get_solo()
    now = timezone.now()
    upper = environment.upper_machine
    logger.info(
        "topology.sync.start environment=%s upper=%s user_id=%s lower_count=%d",
        environment.id,
        upper.id,
        topology.user_id,
        len(topology.lowers),
    )
    upper.station_id = topology.upper.station_id
    upper.station_type = "SCH"
    upper.station_name = topology.upper.name
    upper.save(update_fields=["station_id", "station_type", "station_name", "updated_at"])

    environment.station_user_id = topology.user_id
    environment.status = EnvironmentStatus.READY
    environment.last_discovered_at = now
    environment.save(update_fields=["station_user_id", "status", "last_discovered_at", "updated_at"])

    active_target_ids: set[int] = set()
    created_count = 0
    updated_count = 0
    topology_targets = [*topology.lowers, *([topology.dhh] if topology.dhh else [])]
    for station in topology_targets:
        # ATLog 详细日志链接.html already maps each station to its reachable
        # 10.* address. If stations.xml exposes the business/192.* address,
        # reuse the existing ATLog relation by station name instead of creating
        # a duplicate Machine for the 192.* endpoint.
        atlog_relation = next((
            relation for relation in environment.machine_relations.select_related("target_machine").filter(is_active=True)
            if str((relation.metadata or {}).get("source") or "").strip().lower() == "atlog"
            and str((relation.metadata or {}).get("station_name") or "").strip().lower() == station.name.strip().lower()
        ), None)
        machine = atlog_relation.target_machine if atlog_relation is not None else Machine.objects.filter(host=station.host, role=MachineRole.LOWER).first()
        created = machine is None
        if machine is None:
            machine = Machine(
                name=station.name,
                host=station.host,
                role=MachineRole.LOWER,
                origin=MachineOrigin.XML_DISCOVERY,
            )
        else:
            machine.name = station.name
        machine.station_id = station.station_id
        machine.station_type = station.station_type
        machine.station_name = station.name
        is_dhh = station.station_type == "DHH" or station.name.strip().lower() == "dhh"
        if is_dhh:
            # DHH is an independently maintainable SSH resource. Before a user
            # has explicitly saved DHH credentials, initialize it to root and do
            # not inherit the generic lower-machine account. Once managed, later
            # stations rescans preserve the saved per-IP login configuration.
            if not machine.dhh_credentials_managed:
                machine.username = "root"
                machine.ssh_port = 22
                machine.auth_type = "none"
                machine.clear_credentials()
        else:
            _apply_lower_credentials(machine, settings_obj)
        machine.full_clean()
        machine.save()
        active_target_ids.add(machine.id)
        created_count += int(created)
        updated_count += int(not created)
        station_metadata = {
            "station_id": station.station_id,
            "station_type": station.station_type,
            "station_name": station.name,
            "station_user_id": topology.user_id,
        }
        if atlog_relation is not None:
            # Keep the ATLog report mapping: the Machine host remains the
            # business/login identity, while topology_ip remains the reachable
            # 10.* address used for log access and runtime probes. Record the
            # stations.xml host separately so the two sources can be audited.
            station_metadata = {
                **(atlog_relation.metadata or {}),
                **station_metadata,
                "source": "atlog",
                "topology_ip": str((atlog_relation.metadata or {}).get("topology_ip") or ""),
                "stations_xml_ip": station.host,
            }
            relation_source = atlog_relation.source
        else:
            relation_source = RelationSource.XML
        relation, _ = MachineRelation.objects.update_or_create(
            environment=environment,
            target_machine=machine,
            defaults={
                "source_machine": upper,
                "source": relation_source,
                "is_active": True,
                "discovered_at": now,
                "metadata": station_metadata,
            },
        )
        relation.full_clean()
        relation.save()
        logger.info(
            "topology.sync.lower environment=%s machine=%s host=%s station_id=%s created=%s",
            environment.id,
            machine.id,
            machine.host,
            machine.station_id,
            created,
        )

    disabled = MachineRelation.objects.filter(environment=environment).exclude(
        target_machine_id__in=active_target_ids
    ).update(is_active=False)
    logger.info(
        "topology.sync.finish environment=%s created=%d updated=%d disabled=%d",
        environment.id,
        created_count,
        updated_count,
        disabled,
    )
    return {
        "found_count": len(topology_targets),
        "created_count": created_count,
        "updated_count": updated_count,
        "user_id": topology.user_id,
        "upper": asdict(topology.upper),
        "lowers": [asdict(item) for item in topology.lowers],
        "dhh": asdict(topology.dhh) if topology.dhh else None,
        "is_dhh_environment": topology.dhh is not None,
    }


def discover_environment(environment: Environment) -> EnvironmentDiscovery:
    settings_obj = ResourceSettings.get_solo()
    logger.info(
        "topology.discover.start environment=%s machine=%s xml=%s",
        environment.id,
        environment.upper_machine_id,
        settings_obj.station_xml_path,
    )
    record = EnvironmentDiscovery.objects.create(
        environment=environment,
        status=DiscoveryStatus.RUNNING,
        xml_path=settings_obj.station_xml_path,
        started_at=timezone.now(),
    )
    try:
        content = read_text(environment.upper_machine, settings_obj.station_xml_path)
        logger.info("topology.discover.xml_loaded environment=%s chars=%d", environment.id, len(content))
        topology = parse_stations_xml(content)
        logger.info(
            "topology.discover.xml_parsed environment=%s sch=%s lowers=%d user_id=%s",
            environment.id,
            topology.upper.host,
            len(topology.lowers),
            topology.user_id,
        )
        summary = synchronize_topology(environment, topology)
        # 环境添加/刷新资源只负责拓扑、版本、资源状态同步。
        # 事件码配置属于日志诊断知识，不在资源刷新流程中扫描，避免 SSH
        # 额外访问 ~/SW/resource/*/eh/*_event.json 导致刷新变慢。
        # 事件码解析仅由 AI 诊断/日志分析流程按需触发。
        summary["event_config_sync"] = {
            "status": "skipped",
            "reason": "resource_refresh_not_scan_event_config",
        }
        record.status = DiscoveryStatus.SUCCESS
        record.found_count = summary["found_count"]
        record.created_count = summary["created_count"]
        record.updated_count = summary["updated_count"]
        record.summary = summary
        record.message = "stations.xml 解析并同步成功。"
        logger.info("topology.discover.success environment=%s found=%d", environment.id, record.found_count)
    except Exception as exc:
        logger.exception("topology.discover.failed environment=%s", environment.id)
        environment.status = EnvironmentStatus.ERROR
        environment.save(update_fields=["status", "updated_at"])
        record.status = DiscoveryStatus.FAILED
        record.message = str(exc)
    record.finished_at = timezone.now()
    record.save()
    return record
