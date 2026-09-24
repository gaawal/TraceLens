from __future__ import annotations

import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.common.services.ssh import SSH_SESSION_POOL, execute
from apps.logsources.services.cache_identity import safe_component
from apps.logsources.services.redis_store import RedisLogStore
from apps.environments.models import DeploymentStatus, Environment, EnvironmentDeployment, EnvironmentDiscovery, EnvironmentFolder, MachineRelation, ResourceSettings
from apps.environments.serializers import (
    EnvironmentDiscoverySerializer,
    EnvironmentFolderSerializer,
    EnvironmentSerializer,
    EnvironmentDeploymentSerializer,
    EnvironmentDeploymentSummarySerializer,
    MachineRelationSerializer,
    ResourceSettingsSerializer,
    XmlPreviewRequestSerializer,
)
from apps.environments.services.discovery import discover_environment, read_environment_versions
from apps.environments.services.deployment import build_defaults, command_preview, ensure_gpb_ssh_trust, parse_command_text, request_stop_deployment, retry_deployment_step, start_deployment, sync_gpb_times
from apps.environments.services.deployment_events import DeploymentEventBus
from apps.environments.services.stations_parser import StationsXmlError, parse_stations_xml
from apps.machines.models import ConnectionStatus, MachineOrigin

logger = logging.getLogger("tracelens.environment_api")


DHH_PROCESS_COMMAND = "java -jar /home/root/SW/bin/sw/LDSEquipmentDataProcessService.jar"
DHH_PROCESS_PROBE_COMMAND = "ps -eo args= | grep 'java -jar /home/root/SW/bin/sw/[L]DSEquipmentDataProcessService.jar'"
LOWER_TIME_SYNC_TOLERANCE_SECONDS = int(getattr(settings, "TRACELENS_LOWER_TIME_SYNC_TOLERANCE_SECONDS", 60))
_EPOCH_LINE_RE = re.compile(r"^TRACELENS_EPOCH=(\d+)$", re.MULTILINE)
_DATE_LINE_RE = re.compile(r"^TRACELENS_DATE=(.+)$", re.MULTILINE)
_TB_BLOCK_RE = re.compile(r"TRACELENS_TB_BEGIN\n(.*?)\nTRACELENS_TB_END", re.DOTALL)


def _request_trace_context(request, *, source: str = "manual") -> dict[str, str]:
    user = getattr(request, "user", None)
    operator = ""
    try:
        if user is not None and getattr(user, "is_authenticated", False):
            operator = str(user.get_username() or "").strip()
    except Exception:
        operator = ""
    forwarded = str(request.META.get("HTTP_X_FORWARDED_FOR") or "").strip()
    client_ip = (forwarded.split(",", 1)[0].strip() if forwarded else str(request.META.get("REMOTE_ADDR") or "").strip())
    return {"operator": operator or "页面用户", "client_ip": client_ip, "source": source}


def _extract_epoch(output: str) -> int | None:
    match = _EPOCH_LINE_RE.search(str(output or ""))
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _extract_date_text(output: str) -> str:
    match = _DATE_LINE_RE.search(str(output or ""))
    return match.group(1).strip() if match else ""


def _time_sync_fields(upper_epoch: int | None, lower_epoch: int | None) -> dict:
    if upper_epoch is None or lower_epoch is None:
        return {
            "time_sync_state": "unknown",
            "time_sync_required": False,
            "time_delta_seconds": None,
        }
    delta = int(lower_epoch) - int(upper_epoch)
    required = abs(delta) > LOWER_TIME_SYNC_TOLERANCE_SECONDS
    return {
        "time_sync_state": "out_of_sync" if required else "synced",
        "time_sync_required": required,
        "time_delta_seconds": delta,
    }


def _remote_time_probe_command(include_simulator: bool = False) -> str:
    parts = [
        "printf 'TRACELENS_EPOCH='; date +%s",
        "printf 'TRACELENS_DATE='; date '+%Y-%m-%d %H:%M:%S %z'",
    ]
    if include_simulator:
        parts.extend([
            "printf 'TRACELENS_TB_BEGIN\n'",
            "ps -ef | grep '[t]b_simulator' || true",
            "printf 'TRACELENS_TB_END\n'",
        ])
    parts.append("true")
    return "; ".join(parts)


def _read_remote_epoch(machine) -> tuple[int, str]:
    result = execute(machine, _remote_time_probe_command(False), timeout=8)
    if result.exit_status != 0:
        raise RuntimeError(result.stderr or "远程 date 命令执行失败")
    epoch = _extract_epoch(result.stdout)
    if epoch is None:
        raise RuntimeError("远程 date 命令未返回有效时间")
    return epoch, _extract_date_text(result.stdout)


def _sync_remote_time(machine, upper_epoch: int) -> tuple[int, str]:
    upper_utc_text = datetime.fromtimestamp(upper_epoch, tz=dt_timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    command = (
        f"if date -s '@{upper_epoch}' >/dev/null 2>&1 || "
        f"date -u -s '{upper_utc_text}' >/dev/null 2>&1; then "
        "printf 'TRACELENS_EPOCH='; date +%s; "
        "printf 'TRACELENS_DATE='; date '+%Y-%m-%d %H:%M:%S %z'; "
        "else echo '同步时间失败：当前账号可能没有修改系统时间权限。' >&2; exit 1; fi"
    )
    result = execute(machine, command, timeout=12)
    if result.exit_status != 0:
        raise RuntimeError(result.stderr.strip() or "下位机时间同步失败")
    epoch = _extract_epoch(result.stdout)
    if epoch is None:
        raise RuntimeError("时间同步后未能读取下位机时间")
    return epoch, _extract_date_text(result.stdout)


def _is_small_network_lower(host: str) -> bool:
    """192.* lower-machine addresses are small-network endpoints and are not SSH-probed."""
    return str(host or "").strip().startswith("192.")


def _is_dhh_relation(relation) -> bool:
    machine = relation.target_machine
    metadata = relation.metadata or {}
    station_name = str(metadata.get("station_name") or machine.station_name or machine.name or "").strip().lower()
    station_type = str(metadata.get("station_type") or machine.station_type or "").strip().upper()
    return station_name == "dhh" or station_type == "DHH"


def _is_atlog_dual_ip_relation(relation) -> bool:
    metadata = relation.metadata or {}
    return str(metadata.get("source") or "").strip().lower() == "atlog" and bool(str(metadata.get("topology_ip") or "").strip())


def _skip_lower_ssh_probe(relation) -> bool:
    # ATLog lower machines have two addresses. The report's business IP may be
    # 192.*, but runtime probes must use the reachable topology/big-network IP.
    return _is_small_network_lower(relation.target_machine.host) and not _is_atlog_dual_ip_relation(relation)


def _runtime_probe_machine(relation):
    """Return a connection view using the mapped reachable topology IP."""
    machine = relation.target_machine
    metadata = relation.metadata or {}
    probe_host = str(metadata.get("topology_ip") or "").strip()
    if not probe_host or probe_host == str(machine.host or "").strip():
        return machine
    from copy import copy
    view = copy(machine)
    setattr(view, "_tracelens_host_override", probe_host)
    return view


def _small_network_runtime(machine) -> dict:
    return {
        "machine_id": machine.id,
        "host": machine.host,
        "username": machine.username,
        "online": False,
        "tb_simulator_running": False,
        "skipped": True,
        "skip_reason": "small_network",
        "message": "小网环境（192.*），跳过 SSH 状态检测。",
        "time_sync_state": "skipped",
        "time_sync_required": False,
        "time_delta_seconds": None,
        "remote_epoch": None,
        "remote_date": "",
    }


def _small_network_dhh_runtime(machine) -> dict:
    return {
        "machine_id": machine.id,
        "host": machine.host,
        "username": machine.username,
        "online": False,
        "service_running": False,
        "service_name": "LDSEquipmentDataProcessService",
        "process_command": DHH_PROCESS_COMMAND,
        "skipped": True,
        "skip_reason": "small_network",
        "message": "DHH 为 192.* 小网，跳过 SSH 服务状态检测。",
    }


def _runtime_status_cache_key(environment: Environment) -> str:
    upper = environment.upper_machine
    lower_identity = "|".join(
        sorted(
            f"{relation.target_machine.host}:{relation.target_machine.username}:{str((relation.metadata or {}).get('topology_ip') or '')}"
            for relation in environment.machine_relations.select_related("target_machine").filter(is_active=True)
        )
    )
    topology = hashlib.sha1(lower_identity.encode("utf-8")).hexdigest()[:12]
    return (
        f"tracelens:{RedisLogStore.CACHE_SCHEMA}:runtime-dhh-v3:"
        f"host-{safe_component(upper.host)}:user-{safe_component(upper.username)}:topology-{topology}"
    )


def _hydrate_runtime_status(environment: Environment, payload: dict, cache_status: str) -> dict:
    # Runtime cache ownership follows the physical host+username, not temporary
    # Environment/Machine database ids. Rebind cached physical status to the
    # current environment's ids before returning it to the UI.
    upper = environment.upper_machine
    cached_upper = dict(payload.get("upper") or {})
    cached_upper.update({"machine_id": upper.id, "host": upper.host})

    relations = list(environment.machine_relations.select_related("target_machine").filter(is_active=True))
    cached_by_host = {str(item.get("host") or ""): dict(item) for item in payload.get("lowers") or []}
    lowers = []
    dhh = None
    cached_dhh = dict(payload.get("dhh") or {})
    for relation in relations:
        machine = relation.target_machine
        if _is_dhh_relation(relation):
            if _is_small_network_lower(machine.host):
                dhh = _small_network_dhh_runtime(machine)
            else:
                dhh = cached_dhh if str(cached_dhh.get("host") or "") == machine.host else {
                    "online": False,
                    "service_running": False,
                    "service_name": "LDSEquipmentDataProcessService",
                    "process_command": DHH_PROCESS_COMMAND,
                    "skipped": False,
                    "message": "缓存中没有该 DHH 状态。",
                }
                dhh.update({"machine_id": machine.id, "host": machine.host, "username": machine.username, "skipped": False})
            continue
        if _skip_lower_ssh_probe(relation):
            lowers.append(_small_network_runtime(machine))
            continue
        item = cached_by_host.get(machine.host, {
            "online": False,
            "tb_simulator_running": False,
            "skipped": False,
            "message": "缓存中没有该下位机状态。",
        })
        item.update({"machine_id": machine.id, "host": machine.host, "skipped": False})
        lowers.append(item)

    return {
        **payload,
        "environment_id": environment.id,
        "upper": cached_upper,
        "lowers": lowers,
        "dhh": dhh,
        "cache_status": cache_status,
    }


def _probe_environment_runtime(environment: Environment) -> dict:
    relations = list(environment.machine_relations.select_related("target_machine").filter(is_active=True))
    normal_relations = [relation for relation in relations if not _is_dhh_relation(relation)]
    dhh_relation = next((relation for relation in relations if _is_dhh_relation(relation)), None)
    logger.info(
        "environment.runtime_status.start environment=%s upper=%s lowers=%d dhh=%s",
        environment.id, environment.upper_machine.host, len(normal_relations),
        dhh_relation.target_machine.host if dhh_relation else "-",
    )
    upper = environment.upper_machine
    upper_online = False
    upper_message = ""
    upper_epoch = None
    upper_date = ""
    try:
        result = execute(
            upper,
            "printf 'TRACELENS_OK\n'; " + _remote_time_probe_command(False),
            timeout=8,
        )
        upper_online = result.exit_status == 0 and "TRACELENS_OK" in result.stdout
        upper_epoch = _extract_epoch(result.stdout) if upper_online else None
        upper_date = _extract_date_text(result.stdout) if upper_online else ""
        upper.connection_status = ConnectionStatus.ONLINE if upper_online else ConnectionStatus.OFFLINE
        upper.last_connection_checked_at = timezone.now()
        upper.save(update_fields=["connection_status", "last_connection_checked_at", "updated_at"])
    except Exception as exc:
        upper_message = str(exc)
        upper.connection_status = ConnectionStatus.OFFLINE
        upper.last_connection_checked_at = timezone.now()
        upper.save(update_fields=["connection_status", "last_connection_checked_at", "updated_at"])
        logger.warning("environment.runtime_status.upper_failed environment=%s host=%s error=%s", environment.id, upper.host, exc)

    lowers = []
    for relation in normal_relations:
        machine = relation.target_machine
        probe_machine = _runtime_probe_machine(relation)
        if _skip_lower_ssh_probe(relation):
            lowers.append(_small_network_runtime(machine))
            logger.info(
                "environment.runtime_status.lower_skipped environment=%s host=%s reason=small_network",
                environment.id, machine.host,
            )
            continue
        running = False
        online = False
        message = ""
        lower_epoch = None
        lower_date = ""
        try:
            result = execute(probe_machine, _remote_time_probe_command(True), timeout=8)
            online = result.exit_status == 0
            lower_epoch = _extract_epoch(result.stdout) if online else None
            lower_date = _extract_date_text(result.stdout) if online else ""
            tb_match = _TB_BLOCK_RE.search(result.stdout or "") if online else None
            running = bool(tb_match and tb_match.group(1).strip())
            machine.connection_status = ConnectionStatus.ONLINE if online else ConnectionStatus.OFFLINE
            machine.last_connection_checked_at = timezone.now()
            machine.save(update_fields=["connection_status", "last_connection_checked_at", "updated_at"])
        except Exception as exc:
            message = str(exc)
            machine.connection_status = ConnectionStatus.OFFLINE
            machine.last_connection_checked_at = timezone.now()
            machine.save(update_fields=["connection_status", "last_connection_checked_at", "updated_at"])
            logger.warning("environment.runtime_status.lower_failed environment=%s host=%s probe_host=%s error=%s", environment.id, machine.host, getattr(probe_machine, "host", machine.host), exc)
        lowers.append({
            "machine_id": machine.id,
            "host": machine.host,
            "username": machine.username,
            "online": online,
            "tb_simulator_running": running,
            "skipped": False,
            "message": message,
            "remote_epoch": lower_epoch,
            "remote_date": lower_date,
            **_time_sync_fields(upper_epoch, lower_epoch),
        })

    dhh = None
    if dhh_relation is not None:
        machine = dhh_relation.target_machine
        mapped_host = str((dhh_relation.metadata or {}).get("topology_ip") or "").strip()
        if _is_small_network_lower(machine.host) and not mapped_host:
            dhh = _small_network_dhh_runtime(machine)
            logger.info(
                "environment.runtime_status.dhh_skipped environment=%s host=%s reason=small_network",
                environment.id, machine.host,
            )
        else:
            probe_machine = _runtime_probe_machine(dhh_relation)
            online = False
            service_running = False
            message = ""
            try:
                result = execute(probe_machine, DHH_PROCESS_PROBE_COMMAND, timeout=8)
                # grep exit 0 = service process found, 1 = SSH worked but process was not found.
                online = result.exit_status in (0, 1)
                service_running = result.exit_status == 0 and bool(result.stdout.strip())
                if online and not service_running:
                    message = "DHH 可连接，但 LDSEquipmentDataProcessService 未运行。"
                machine.connection_status = ConnectionStatus.ONLINE if online else ConnectionStatus.OFFLINE
                machine.last_connection_checked_at = timezone.now()
                machine.save(update_fields=["connection_status", "last_connection_checked_at", "updated_at"])
            except Exception as exc:
                message = str(exc)
                machine.connection_status = ConnectionStatus.OFFLINE
                machine.last_connection_checked_at = timezone.now()
                machine.save(update_fields=["connection_status", "last_connection_checked_at", "updated_at"])
                logger.warning("environment.runtime_status.dhh_failed environment=%s host=%s error=%s", environment.id, machine.host, exc)
            dhh = {
                "machine_id": machine.id,
                "host": machine.host,
                "username": machine.username,
                "online": online,
                "service_running": service_running,
                "service_name": "LDSEquipmentDataProcessService",
                "process_command": DHH_PROCESS_COMMAND,
                "skipped": False,
                "message": message,
            }
            logger.info(
                "environment.runtime_status.dhh environment=%s host=%s online=%s service_running=%s",
                environment.id, machine.host, online, service_running,
            )

    probed_lowers = [item for item in lowers if not item.get("skipped")]
    logger.info(
        "environment.runtime_status.finish environment=%s upper_online=%s simulator_running=%d/%d skipped=%d dhh_running=%s",
        environment.id, upper_online,
        sum(1 for item in probed_lowers if item["tb_simulator_running"]),
        len(probed_lowers), len(lowers) - len(probed_lowers),
        None if dhh is None or dhh.get("skipped") else dhh.get("service_running"),
    )
    return {
        "environment_id": environment.id,
        "upper": {
            "machine_id": upper.id,
            "host": upper.host,
            "online": upper_online,
            "message": upper_message,
            "remote_epoch": upper_epoch,
            "remote_date": upper_date,
        },
        "lowers": lowers,
        "dhh": dhh,
        "checked_at": timezone.now().isoformat(timespec="seconds"),
    }


def _get_environment_runtime(environment: Environment, *, force: bool = False) -> dict:
    cache_key = _runtime_status_cache_key(environment)
    if not force:
        cached = RedisLogStore.get_json(cache_key)
        if isinstance(cached, dict):
            logger.info(
                "environment.runtime_status.cache_hit environment=%s host=%s checked_at=%s",
                environment.id, environment.upper_machine.host, cached.get("checked_at"),
            )
            return _hydrate_runtime_status(environment, cached, "hit")
    payload = _probe_environment_runtime(environment)
    ttl = int(getattr(settings, "TRACELENS_RUNTIME_STATUS_TTL", 60))
    stored = RedisLogStore.set_json(cache_key, payload, ttl)
    logger.info(
        "environment.runtime_status.cache_store environment=%s host=%s ttl=%s stored=%s force=%s",
        environment.id, environment.upper_machine.host, ttl, stored, force,
    )
    return _hydrate_runtime_status(environment, payload, "refreshed" if force else "miss")


class EnvironmentViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = EnvironmentSerializer
    queryset = Environment.objects.select_related("upper_machine", "folder").prefetch_related("machine_relations__target_machine")
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    @action(detail=True, methods=["get"], url_path="runtime-status")
    def runtime_status(self, request, pk=None):
        environment = self.get_object()
        force = str(request.query_params.get("refresh", "")).lower() in {"1", "true", "yes"}
        try:
            return Response(_get_environment_runtime(environment, force=force))
        except Exception as exc:
            logger.exception("environment.runtime_status.failed environment=%s", environment.id)
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)


    @action(detail=True, methods=["post"], url_path="sync-lower-time")
    def sync_lower_time(self, request, pk=None):
        environment = self.get_object()
        raw_ids = request.data.get("machine_ids")
        if raw_ids is None and request.data.get("machine_id") is not None:
            raw_ids = [request.data.get("machine_id")]
        if not isinstance(raw_ids, list) or not raw_ids:
            return Response({"message": "machine_ids 必须是非空数组。"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            machine_ids = list(dict.fromkeys(int(value) for value in raw_ids))
        except (TypeError, ValueError):
            return Response({"message": "machine_ids 必须全部为整数。"}, status=status.HTTP_400_BAD_REQUEST)

        relation_by_machine = {
            relation.target_machine_id: relation
            for relation in environment.machine_relations.select_related("target_machine").filter(is_active=True, target_machine_id__in=machine_ids)
        }
        try:
            upper_epoch, upper_date = _read_remote_epoch(environment.upper_machine)
        except Exception as exc:
            logger.exception("environment.sync_lower_time.upper_failed environment=%s", environment.id)
            return Response({"message": f"读取上位机时间失败：{exc}"}, status=status.HTTP_502_BAD_GATEWAY)

        results = []
        for machine_id in machine_ids:
            relation = relation_by_machine.get(machine_id)
            if relation is None:
                results.append({"machine_id": machine_id, "success": False, "message": "该机器不属于当前环境。"})
                continue
            machine = relation.target_machine
            if _is_dhh_relation(relation):
                results.append({"machine_id": machine_id, "host": machine.host, "success": False, "message": "DHH 不属于模拟器下位机时间同步范围。"})
                continue
            if _is_small_network_lower(machine.host) and not _is_atlog_dual_ip_relation(relation):
                results.append({"machine_id": machine_id, "host": machine.host, "success": False, "message": "小网下位机跳过 SSH 时间同步。"})
                continue
            probe_machine = _runtime_probe_machine(relation)
            try:
                before_epoch, before_date = _read_remote_epoch(probe_machine)
                after_epoch, after_date = _sync_remote_time(probe_machine, upper_epoch)
                SSH_SESSION_POOL.invalidate(probe_machine, "time_synchronized")
                sync_fields = _time_sync_fields(upper_epoch, after_epoch)
                results.append({
                    "machine_id": machine.id,
                    "host": machine.host,
                    "success": not sync_fields["time_sync_required"],
                    "message": "时间同步完成。" if not sync_fields["time_sync_required"] else "时间已写入，但与上位机仍存在较大偏差。",
                    "before_epoch": before_epoch,
                    "before_date": before_date,
                    "after_epoch": after_epoch,
                    "after_date": after_date,
                    **sync_fields,
                })
                logger.info(
                    "environment.sync_lower_time.success environment=%s machine=%s host=%s before=%s after=%s upper=%s",
                    environment.id, machine.id, machine.host, before_epoch, after_epoch, upper_epoch,
                )
            except Exception as exc:
                logger.exception("environment.sync_lower_time.failed environment=%s machine=%s host=%s", environment.id, machine.id, machine.host)
                results.append({"machine_id": machine.id, "host": machine.host, "success": False, "message": str(exc)})

        runtime_status = _get_environment_runtime(environment, force=True)
        all_success = bool(results) and all(item.get("success") for item in results)
        return Response({
            "success": all_success,
            "environment_id": environment.id,
            "upper_epoch": upper_epoch,
            "upper_date": upper_date,
            "results": results,
            "runtime_status": runtime_status,
        })

    @action(detail=False, methods=["get"], url_path="runtime-status")
    def runtime_status_all(self, request):
        environments = list(self.get_queryset())
        force = str(request.query_params.get("refresh", "")).lower() in {"1", "true", "yes"}
        logger.info("environment.runtime_status_all.start environments=%d force=%s", len(environments), force)
        results = []
        # This endpoint is retained for compatibility. The frontend no longer calls it
        # automatically; normal UI status checks are per-open-resource only.
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(environments)))) as executor:
            futures = {executor.submit(_get_environment_runtime, env, force=force): env.id for env in environments}
            for future in as_completed(futures):
                env_id = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    logger.exception("environment.runtime_status_all.item_failed environment=%s", env_id)
                    results.append({"environment_id": env_id, "upper": {"online": False}, "lowers": [], "message": str(exc)})
        results.sort(key=lambda item: item["environment_id"])
        logger.info("environment.runtime_status_all.finish environments=%d", len(results))
        return Response(results)

    def perform_update(self, serializer):
        instance = serializer.save()
        logger.info(
            "environment.updated environment=%s name=%s folder=%s upper=%s host=%s",
            instance.id, instance.name, instance.folder_id, instance.upper_machine_id, instance.upper_machine.host,
        )

    def perform_destroy(self, instance):
        upper = instance.upper_machine
        lower_candidates = list(
            instance.machine_relations.select_related("target_machine").values_list("target_machine_id", flat=True)
        )
        logger.info("environment.delete.start environment=%s upper=%s", instance.id, upper.id)
        SSH_SESSION_POOL.invalidate(upper, "environment_deleted")
        with transaction.atomic():
            instance.delete()
            upper.delete()
            # Remove XML-created lower machines that are no longer referenced by any environment.
            from apps.machines.models import Machine

            for machine in Machine.objects.filter(id__in=lower_candidates, origin=MachineOrigin.XML_DISCOVERY):
                if not machine.incoming_relations.exists():
                    SSH_SESSION_POOL.invalidate(machine, "orphan_lower_deleted")
                    machine.delete()
        logger.info("environment.delete.finish upper=%s", upper.id)

    @action(detail=False, methods=["post"], url_path="bulk")
    def bulk(self, request):
        action_name = str(request.data.get("action", "")).strip()
        try:
            ids = sorted({int(value) for value in request.data.get("ids", [])})
        except (TypeError, ValueError):
            return Response({"message": "ids 必须是整数数组。"}, status=status.HTTP_400_BAD_REQUEST)
        existing = list(self.get_queryset().filter(id__in=ids))
        existing_ids = {item.id for item in existing}
        skipped = [item for item in ids if item not in existing_ids]
        if action_name == "delete":
            for environment in existing:
                self.perform_destroy(environment)
            logger.info("environment.bulk.delete requested=%d affected=%d skipped=%s", len(ids), len(existing), skipped)
            return Response({"action": action_name, "requested": len(ids), "affected": len(existing), "skipped_ids": skipped})
        if action_name == "runtime_status":
            results = []
            with ThreadPoolExecutor(max_workers=min(8, max(1, len(existing)))) as executor:
                futures = {executor.submit(_get_environment_runtime, env, force=True): env.id for env in existing}
                for future in as_completed(futures):
                    env_id = futures[future]
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        results.append({"environment_id": env_id, "upper": {"online": False}, "lowers": [], "message": str(exc)})
            results.sort(key=lambda item: item["environment_id"])
            logger.info("environment.bulk.runtime requested=%d affected=%d skipped=%s", len(ids), len(results), skipped)
            return Response({"action": action_name, "requested": len(ids), "affected": len(results), "skipped_ids": skipped, "results": results})
        if action_name == "overview_refresh":
            def refresh_overview_item(environment: Environment) -> dict:
                try:
                    runtime_status = _get_environment_runtime(environment, force=True)
                except Exception as exc:
                    logger.exception("environment.bulk.overview.runtime_failed environment=%s", environment.id)
                    runtime_status = {
                        "environment_id": environment.id,
                        "upper": {"online": False, "machine_id": environment.upper_machine_id, "host": environment.upper_machine.host},
                        "lowers": [],
                        "message": str(exc),
                    }
                version = ""
                version_error = ""
                version_result = None
                try:
                    version_result = read_environment_versions(environment)
                    version = version_result["version"]
                except Exception as exc:
                    version_error = str(exc)
                    logger.warning("environment.bulk.overview.version_failed environment=%s error=%s", environment.id, exc)
                return {
                    "environment_id": environment.id,
                    "runtime_status": runtime_status,
                    "version": version,
                    "version_error": version_error,
                    "version_result": version_result,
                }

            overview_results = []
            with ThreadPoolExecutor(max_workers=min(8, max(1, len(existing)))) as executor:
                futures = {executor.submit(refresh_overview_item, env): env.id for env in existing}
                for future in as_completed(futures):
                    env_id = futures[future]
                    try:
                        overview_results.append(future.result())
                    except Exception as exc:
                        logger.exception("environment.bulk.overview.item_failed environment=%s", env_id)
                        overview_results.append({
                            "environment_id": env_id,
                            "runtime_status": {"environment_id": env_id, "upper": {"online": False}, "lowers": [], "message": str(exc)},
                            "version": "",
                            "version_error": str(exc),
                        })
            overview_results.sort(key=lambda item: item["environment_id"])
            logger.info("environment.bulk.overview requested=%d affected=%d skipped=%s", len(ids), len(overview_results), skipped)
            return Response({
                "action": action_name,
                "requested": len(ids),
                "affected": len(overview_results),
                "skipped_ids": skipped,
                "overview_results": overview_results,
            })
        return Response({"message": "不支持的批量操作。"}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="preview-xml")
    def preview_xml(self, request, pk=None):
        self.get_object()
        serializer = XmlPreviewRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            topology = parse_stations_xml(serializer.validated_data["xml_content"])
        except StationsXmlError as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({
            "user_id": topology.user_id,
            "upper": {"id": topology.upper.station_id, "type": topology.upper.station_type, "name": topology.upper.name, "host": topology.upper.host},
            "lowers": [{"id": item.station_id, "type": item.station_type, "name": item.name, "host": item.host} for item in topology.lowers],
            "dhh": ({"id": topology.dhh.station_id, "type": topology.dhh.station_type, "name": topology.dhh.name, "host": topology.dhh.host} if topology.dhh else None),
            "is_dhh_environment": topology.dhh is not None,
        })

    @action(detail=True, methods=["post"])
    def discover(self, request, pk=None):
        record = discover_environment(self.get_object())
        payload = EnvironmentDiscoverySerializer(record).data
        return Response(payload, status=status.HTTP_200_OK if record.status == "success" else status.HTTP_502_BAD_GATEWAY)

    @action(detail=True, methods=["post"], url_path="query-version")
    def query_version(self, request, pk=None):
        environment = self.get_object()
        try:
            result = read_environment_versions(environment)
        except Exception as exc:
            logger.exception("version.api.failed environment=%s", environment.id)
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(result)




    @action(detail=False, methods=["get"], url_path="active-deployments")
    def active_deployments(self, request):
        environment_ids = set(self.get_queryset().values_list("id", flat=True))
        cached = DeploymentEventBus.active_summaries(environment_ids)
        if cached is not None:
            return Response(cached)
        records = EnvironmentDeployment.objects.filter(
            environment_id__in=environment_ids,
            status__in=[DeploymentStatus.PENDING, DeploymentStatus.RUNNING, DeploymentStatus.STOPPING],
        ).order_by("environment_id", "-created_at")
        latest_by_environment = {}
        for deployment in records:
            latest_by_environment.setdefault(deployment.environment_id, deployment)
        deployments = list(latest_by_environment.values())
        DeploymentEventBus.warm_active_index(deployments)
        return Response(EnvironmentDeploymentSummarySerializer(deployments, many=True).data)

    @action(detail=True, methods=["get"], url_path="deployment-defaults")
    def deployment_defaults(self, request, pk=None):
        environment = self.get_object()
        return Response(build_defaults(environment))

    @action(detail=True, methods=["post"], url_path="deployment-preview")
    def deployment_preview(self, request, pk=None):
        environment = self.get_object()
        try:
            return Response(command_preview(environment, request.data or {}))
        except (TypeError, ValueError) as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="deployment-parse")
    def deployment_parse(self, request, pk=None):
        environment = self.get_object()
        try:
            return Response(parse_command_text(environment, str(request.data.get("commands") or "")))
        except (TypeError, ValueError) as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="deployment-ssh-trust")
    def deployment_ssh_trust(self, request, pk=None):
        environment = self.get_object()
        try:
            gpb_ips = request.data.get("gpb_ips") or []
            if not isinstance(gpb_ips, list):
                raise ValueError("GPB IP 必须使用列表格式。")
            return Response(ensure_gpb_ssh_trust(
                environment,
                gpb_ips,
                include_dhh=bool(request.data.get("include_dhh", False)),
                dhh_ip=str(request.data.get("dhh_ip") or ""),
                dhh_user=str(request.data.get("dhh_user") or "root"),
            ))
        except (TypeError, ValueError) as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="deployment-time-sync")
    def deployment_time_sync(self, request, pk=None):
        environment = self.get_object()
        try:
            gpb_ips = request.data.get("gpb_ips") or []
            if not isinstance(gpb_ips, list):
                raise ValueError("GPB IP 必须使用列表格式。")
            include_dhh = bool(request.data.get("include_dhh", False))
            dhh_ip = str(request.data.get("dhh_ip") or "")
            dhh_user = str(request.data.get("dhh_user") or "root")
            trust = ensure_gpb_ssh_trust(
                environment,
                gpb_ips,
                include_dhh=include_dhh,
                dhh_ip=dhh_ip,
                dhh_user=dhh_user,
            )
            if not trust.get("all_trusted"):
                missing = [item for item in trust.get("results", []) if not item.get("trusted")]
                labels = "；".join(f"{item.get('host')}（{item.get('message')}）" for item in missing)
                raise ValueError(f"SSH 双向互信未完成：{labels}")
            return Response(sync_gpb_times(
                environment,
                gpb_ips,
                auto_sync=True,
                include_dhh=include_dhh,
                dhh_ip=dhh_ip,
                dhh_user=dhh_user,
            ))
        except (TypeError, ValueError) as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["get", "post"], url_path="deployments")
    def deployments(self, request, pk=None):
        environment = self.get_object()
        if request.method == "GET":
            records = environment.deployments.order_by("-created_at")[:50]
            return Response(EnvironmentDeploymentSummarySerializer(records, many=True).data)
        try:
            deployment_payload = dict(request.data or {})
            deployment_payload["_trace_context"] = _request_trace_context(request, source="manual")
            deployment = start_deployment(environment, deployment_payload)
        except (TypeError, ValueError) as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        deployment = EnvironmentDeployment.objects.prefetch_related("steps").get(pk=deployment.pk)
        return Response(EnvironmentDeploymentSerializer(deployment, context={"log_step_key": deployment.current_step}).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"], url_path="latest-deployment")
    def latest_deployment(self, request, pk=None):
        environment = self.get_object()
        deployment = environment.deployments.order_by("-created_at").first()
        if deployment is None:
            return Response(None)
        return Response(EnvironmentDeploymentSummarySerializer(deployment).data)

    @action(detail=True, methods=["post"], url_path=r"deployments/(?P<deployment_id>[0-9]+)/retry")
    def deployment_retry(self, request, pk=None, deployment_id=None):
        environment = self.get_object()
        try:
            deployment = retry_deployment_step(environment, int(deployment_id), (request.data or {}).get("step_key"))
        except (TypeError, ValueError) as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        deployment = EnvironmentDeployment.objects.prefetch_related("steps").get(pk=deployment.pk)
        return Response(EnvironmentDeploymentSerializer(deployment, context={"log_step_key": deployment.current_step}).data, status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=["post"], url_path=r"deployments/(?P<deployment_id>[0-9]+)/stop")
    def deployment_stop(self, request, pk=None, deployment_id=None):
        environment = self.get_object()
        try:
            deployment = request_stop_deployment(environment, int(deployment_id))
        except (TypeError, ValueError) as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        deployment = EnvironmentDeployment.objects.prefetch_related("steps").get(pk=deployment.pk)
        return Response(
            EnvironmentDeploymentSerializer(deployment, context={"log_step_key": deployment.current_step}).data,
            status=status.HTTP_202_ACCEPTED,
        )

    @action(detail=True, methods=["get"], url_path=r"deployments/(?P<deployment_id>[0-9]+)")
    def deployment_detail(self, request, pk=None, deployment_id=None):
        environment = self.get_object()
        try:
            deployment = environment.deployments.prefetch_related("steps").get(pk=deployment_id)
        except EnvironmentDeployment.DoesNotExist:
            return Response({"message": "部署记录不存在。"}, status=status.HTTP_404_NOT_FOUND)
        log_step_key = str(request.query_params.get("step_key") or deployment.current_step or "").strip()
        return Response(EnvironmentDeploymentSerializer(deployment, context={"log_step_key": log_step_key}).data)


class EnvironmentFolderViewSet(viewsets.ModelViewSet):
    serializer_class = EnvironmentFolderSerializer

    def get_queryset(self):
        return EnvironmentFolder.objects.annotate(environment_count=Count("environments")).order_by("sort_order", "name", "id")

    def perform_create(self, serializer):
        instance = serializer.save()
        logger.info("environment_folder.created id=%s name=%s parent=%s", instance.id, instance.name, instance.parent_id)

    def perform_update(self, serializer):
        instance = serializer.save()
        logger.info("environment_folder.updated id=%s name=%s parent=%s", instance.id, instance.name, instance.parent_id)


    def perform_destroy(self, instance):
        logger.info("environment_folder.deleted id=%s name=%s environments=%s", instance.id, instance.name, instance.environments.count())
        instance.delete()

class ResourceSettingsViewSet(viewsets.ViewSet):
    @action(detail=False, methods=["get", "patch"], url_path="current")
    def current(self, request):
        instance = ResourceSettings.get_solo()
        if request.method == "PATCH":
            serializer = ResourceSettingsSerializer(instance, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            if "display_rules" in request.data and not instance.display_rules_initialized:
                instance.display_rules_initialized = True
                instance.save(update_fields=["display_rules_initialized", "updated_at"])
            logger.info("resource_settings.updated")
        return Response(ResourceSettingsSerializer(instance).data)


class MachineRelationViewSet(viewsets.ModelViewSet):
    serializer_class = MachineRelationSerializer
    queryset = MachineRelation.objects.select_related("environment", "source_machine", "target_machine").all()


class EnvironmentDiscoveryViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = EnvironmentDiscoverySerializer
    queryset = EnvironmentDiscovery.objects.select_related("environment").all()
