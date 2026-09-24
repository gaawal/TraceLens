from __future__ import annotations

import hashlib
import json
import logging
import posixpath
import stat
from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.common.services.ssh import SshOperationError, ssh_session
from apps.environments.models import Environment
from apps.logsources.services.redis_store import RedisLogStore
from apps.logsources.models import (
    EventCodeDefinition,
    EventConfigSource,
    LogFmDefinition,
    LogSubsystemDefinition,
)

logger = logging.getLogger("tracelens.event_config")

_EVENT_SUFFIX = "_event.json"
_MAX_EVENT_JSON_BYTES = 64 * 1024 * 1024
_EVENT_INDEX_TTL_SECONDS = 7 * 24 * 60 * 60
_EVENT_LOOKUP_TTL_SECONDS = 6 * 60 * 60


def _event_index_key(environment_id: int) -> str:
    return f"tracelens:{RedisLogStore.CACHE_SCHEMA}:event-config:index:{int(environment_id)}"


def _event_revision_key(environment_id: int | None) -> str:
    suffix = str(int(environment_id)) if environment_id not in (None, "") else "all"
    return f"tracelens:{RedisLogStore.CACHE_SCHEMA}:event-config:revision:{suffix}"


def _event_revision(environment_id: int | None) -> int:
    value = RedisLogStore.get_json(_event_revision_key(environment_id))
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _bump_event_revision(environment_id: int) -> None:
    RedisLogStore.increment(_event_revision_key(environment_id))
    RedisLogStore.increment(_event_revision_key(None))


@dataclass(frozen=True, slots=True)
class RemoteEventConfigFile:
    path: str
    subsystem: str
    component_code: str
    file_name: str
    mtime: float
    size: int


def _canonical_subsystem(value: str) -> str:
    return str(value or "").strip().upper()


def _component_from_filename(file_name: str, subsystem: str) -> str:
    """Parse ``{subsystem}_{component repository}_event.json`` safely.

    Component repository names may themselves contain underscores, therefore the
    parser removes only the subsystem prefix and the fixed suffix.
    """
    name = str(file_name or "").strip()
    if not name.lower().endswith(_EVENT_SUFFIX):
        return ""
    stem = name[: -len(_EVENT_SUFFIX)]
    prefix = f"{str(subsystem or '').strip().lower()}_"
    if stem.lower().startswith(prefix):
        component = stem[len(prefix):]
    elif "_" in stem:
        component = stem.split("_", 1)[1]
    else:
        component = ""
    return component.strip()[:128]


def _subsystem_definition(name: str) -> LogSubsystemDefinition:
    canonical = _canonical_subsystem(name)
    existing = LogSubsystemDefinition.objects.filter(name__iexact=canonical).first()
    if existing is not None:
        return existing
    return LogSubsystemDefinition.objects.create(name=canonical, display_name="", enabled=True)


def _list_remote_event_files(environment: Environment) -> list[RemoteEventConfigFile]:
    machine = environment.upper_machine
    rows: list[RemoteEventConfigFile] = []
    with ssh_session(machine) as lease:
        try:
            home = lease.sftp.normalize(".")
            resource_root = posixpath.join(home.rstrip("/"), "SW", "resource")
            subsystem_attrs = lease.sftp.listdir_attr(resource_root)
        except Exception as exc:
            raise SshOperationError(f"扫描事件码目录失败 {machine.host}:~/SW/resource：{exc}") from exc

        for subsystem_attr in subsystem_attrs:
            if not stat.S_ISDIR(int(subsystem_attr.st_mode or 0)):
                continue
            subsystem = _canonical_subsystem(subsystem_attr.filename)
            if not subsystem:
                continue
            eh_dir = posixpath.join(resource_root, subsystem_attr.filename, "eh")
            try:
                attrs = lease.sftp.listdir_attr(eh_dir)
            except OSError:
                continue
            except Exception as exc:
                logger.warning(
                    "event_config.scan.eh_failed environment=%s subsystem=%s path=%s error=%s",
                    environment.id,
                    subsystem,
                    eh_dir,
                    exc,
                )
                continue
            for attr in attrs:
                if stat.S_ISDIR(int(attr.st_mode or 0)):
                    continue
                file_name = str(attr.filename or "")
                if not file_name.lower().endswith(_EVENT_SUFFIX):
                    continue
                component = _component_from_filename(file_name, subsystem)
                if not component:
                    logger.warning(
                        "event_config.scan.invalid_filename environment=%s subsystem=%s file=%s",
                        environment.id,
                        subsystem,
                        file_name,
                    )
                    continue
                rows.append(RemoteEventConfigFile(
                    path=posixpath.join(eh_dir, file_name),
                    subsystem=subsystem,
                    component_code=component,
                    file_name=file_name,
                    mtime=float(attr.st_mtime or 0),
                    size=max(0, int(attr.st_size or 0)),
                ))
    rows.sort(key=lambda item: (item.subsystem.casefold(), item.component_code.casefold(), item.file_name.casefold()))
    return rows


def _read_remote_json(environment: Environment, item: RemoteEventConfigFile) -> tuple[dict[str, Any], str]:
    if item.size > _MAX_EVENT_JSON_BYTES:
        raise SshOperationError(f"事件码配置文件过大：{item.path}（{item.size} bytes）")
    machine = environment.upper_machine
    with ssh_session(machine) as lease:
        try:
            with lease.sftp.open(item.path, "rb") as remote:
                raw = remote.read(_MAX_EVENT_JSON_BYTES + 1)
        except Exception as exc:
            raise SshOperationError(f"读取事件码配置失败 {item.path}：{exc}") from exc
    if len(raw) > _MAX_EVENT_JSON_BYTES:
        raise SshOperationError(f"事件码配置文件超过允许大小：{item.path}")
    file_hash = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw.decode("utf-8-sig", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"事件码 JSON 无法解析 {item.path}：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"事件码 JSON 顶层必须是对象：{item.path}")
    return payload, file_hash


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _normalized_configs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    configs = payload.get("configs")
    if not isinstance(configs, list):
        return []
    # DisplayCode is the environment-local stable identity. If a malformed file
    # repeats it, keep the final dictionary just as the product config convention
    # already does for repeated event codes.
    by_display: dict[str, dict[str, Any]] = {}
    for raw in configs:
        if not isinstance(raw, dict):
            continue
        display_code = str(raw.get("DisplayCode") or "").strip()
        if not display_code:
            continue
        by_display[display_code.casefold()] = raw
    return list(by_display.values())


def _refresh_event_component_metadata(subsystem_name: str, component_code: str) -> LogFmDefinition:
    """Merge event-code facts into the subsystem/module dictionary.

    The module name is the Event component name. ``target_modules`` is entirely
    engineer-managed and is never guessed or overwritten by environment refresh.
    """
    subsystem_def = _subsystem_definition(subsystem_name)
    component = str(component_code or "").strip()
    row = (
        LogFmDefinition.objects.filter(subsystem=subsystem_def, name__iexact=component)
        .order_by("kind", "id")
        .first()
    )
    if row is None:
        row = LogFmDefinition.objects.create(
            subsystem=subsystem_def,
            name=component,
            kind="normal",
            display_name="",
            enabled=True,
            event_component=True,
        )

    definitions = EventCodeDefinition.objects.filter(
        active=True,
        subsystem__name__iexact=subsystem_def.name,
        component_code__iexact=component,
    ).select_related("source_config")
    display_codes = sorted(
        {str(item.display_code) for item in definitions if str(item.display_code or "").strip()},
        key=str.casefold,
    )
    config_files = sorted(
        {str(item.source_config.file_name) for item in definitions if str(item.source_config.file_name or "").strip()},
        key=str.casefold,
    )
    source_exists = EventConfigSource.objects.filter(
        active=True,
        subsystem__name__iexact=subsystem_def.name,
        component_code__iexact=component,
    ).exists()
    now = timezone.now()
    row.event_component = source_exists
    row.event_display_codes = display_codes[:2000]
    row.event_config_files = config_files[:200]
    row.event_code_count = len(display_codes)
    row.last_event_discovered_at = now
    if source_exists:
        row.last_discovered_at = now
    row.save(update_fields=[
        "event_component", "event_display_codes", "event_config_files", "event_code_count",
        "last_event_discovered_at", "last_discovered_at", "updated_at",
    ])
    return row


@transaction.atomic
def _sync_one_file(environment: Environment, item: RemoteEventConfigFile, payload: dict[str, Any], file_hash: str) -> dict[str, Any]:
    now = timezone.now()
    subsystem_def = _subsystem_definition(item.subsystem)
    configs = _normalized_configs(payload)
    source_values = sorted(
        {str(config.get("Source") or "").strip() for config in configs if str(config.get("Source") or "").strip()},
        key=str.casefold,
    )
    source_value = (source_values[0] if source_values else "")[:64]
    warnings: list[str] = []
    mismatched_sources = [value for value in source_values if value.casefold() != subsystem_def.name.casefold()]
    if mismatched_sources:
        warnings.append(f"Source 与目录子系统不一致：{', '.join(mismatched_sources[:5])}")
    if len(source_values) > 1:
        warnings.append(f"同一配置文件出现多个 Source：{', '.join(source_values[:5])}")

    source_row, _ = EventConfigSource.objects.update_or_create(
        environment=environment,
        file_path=item.path,
        defaults={
            "subsystem": subsystem_def,
            "component_code": item.component_code,
            "source": source_value,
            "file_name": item.file_name,
            "version": str(payload.get("Version") or "").strip()[:32],
            "file_hash": file_hash,
            "remote_mtime": item.mtime,
            "remote_size": item.size,
            "active": True,
            "sync_message": "；".join(warnings),
            "last_seen_at": now,
        },
    )

    display_codes = [str(config.get("DisplayCode") or "").strip() for config in configs]
    display_codes = [value for value in display_codes if value]
    existing_rows = list(
        EventCodeDefinition.objects.filter(environment=environment, display_code__in=display_codes)
    )
    existing_by_display = {str(row.display_code or "").casefold(): row for row in existing_rows}
    create_rows: list[EventCodeDefinition] = []
    update_rows: list[EventCodeDefinition] = []
    update_fields = [
        "source_config", "subsystem", "component_code", "source", "code", "code_string",
        "severity", "category", "recovery_class", "auto_clear", "send_to_host",
        "send_to_active_exception_gui", "description", "object_params", "raw_config",
        "active", "last_seen_at", "updated_at",
    ]

    for config in configs:
        display_code = str(config.get("DisplayCode") or "").strip()
        if not display_code:
            continue
        values = {
            "source_config": source_row,
            "subsystem": subsystem_def,
            "component_code": item.component_code,
            "source": str(config.get("Source") or "").strip()[:64],
            "code": str(config.get("Code") or "").strip()[:64],
            "code_string": str(config.get("CodeString") or "").strip()[:256],
            "severity": str(config.get("Severity") or "").strip()[:32],
            "category": str(config.get("Category") or "").strip()[:64],
            "recovery_class": str(config.get("RecoveryClass") or "").strip()[:64],
            "auto_clear": _bool(config.get("AutoClear")),
            "send_to_host": _bool(config.get("SendToHost")),
            "send_to_active_exception_gui": _bool(config.get("SendToActiveExceptionGUI")),
            "description": str(config.get("Description") or ""),
            "object_params": config.get("ObjectParams") if isinstance(config.get("ObjectParams"), list) else [],
            "raw_config": config,
            "active": True,
            "last_seen_at": now,
        }
        current = existing_by_display.get(display_code.casefold())
        if current is None:
            create_rows.append(EventCodeDefinition(environment=environment, display_code=display_code, **values))
            continue
        for field_name, value in values.items():
            setattr(current, field_name, value)
        current.updated_at = now
        update_rows.append(current)

    if create_rows:
        EventCodeDefinition.objects.bulk_create(create_rows, batch_size=500)
    if update_rows:
        EventCodeDefinition.objects.bulk_update(update_rows, update_fields, batch_size=500)

    stale_qs = source_row.event_codes.filter(active=True)
    if display_codes:
        stale_qs = stale_qs.exclude(display_code__in=display_codes)
    stale_count = stale_qs.update(active=False, last_seen_at=now)

    _refresh_event_component_metadata(subsystem_def.name, item.component_code)
    return {
        "file": item.file_name,
        "subsystem": subsystem_def.name,
        "component": item.component_code,
        "config_count": len(configs),
        "created": len(create_rows),
        "updated": len(update_rows),
        "deactivated_codes": stale_count,
        "warnings": warnings,
    }


def sync_environment_event_configs(environment: Environment, *, force: bool = False) -> dict[str, Any]:
    """Incrementally synchronize ``~/SW/resource/*/eh/*_event.json``.

    Redis keeps the last remote file metadata snapshot so unchanged files are
    recognized before any JSON content read. The database remains authoritative;
    Redis failure simply falls back to DB mtime/size comparison.
    """
    started = timezone.now()
    cached_index = RedisLogStore.get_json(_event_index_key(environment.id))
    cached_files = cached_index.get("files", {}) if isinstance(cached_index, dict) else {}
    remote_files = _list_remote_event_files(environment)
    existing = {
        row.file_path: row
        for row in EventConfigSource.objects.filter(environment=environment).select_related("subsystem")
    }
    seen_paths: set[str] = set()
    affected_pairs: set[tuple[str, str]] = set()
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    unchanged_ids: list[int] = []
    redis_hits = 0
    db_hits = 0
    hash_hits = 0

    for item in remote_files:
        seen_paths.add(item.path)
        previous = existing.get(item.path)
        affected_pairs.add((item.subsystem, item.component_code))
        cached_meta = cached_files.get(item.path) if isinstance(cached_files, dict) else None
        redis_unchanged = bool(
            isinstance(cached_meta, dict)
            and not force
            and float(cached_meta.get("mtime") or 0) == float(item.mtime or 0)
            and int(cached_meta.get("size") or 0) == int(item.size or 0)
        )
        db_unchanged = bool(
            previous
            and previous.active
            and not str(previous.sync_message or "").strip()
            and not force
            and float(previous.remote_mtime or 0) == float(item.mtime or 0)
            and int(previous.remote_size or 0) == int(item.size or 0)
        )
        if previous and db_unchanged and redis_unchanged:
            redis_hits += 1
            unchanged_ids.append(previous.id)
            continue
        if previous and db_unchanged:
            db_hits += 1
            unchanged_ids.append(previous.id)
            continue
        try:
            payload, file_hash = _read_remote_json(environment, item)
            if previous and previous.file_hash and previous.file_hash == file_hash and not force:
                previous.remote_mtime = item.mtime
                previous.remote_size = item.size
                previous.active = True
                previous.sync_message = ""
                previous.last_seen_at = started
                previous.save(update_fields=["remote_mtime", "remote_size", "active", "sync_message", "last_seen_at", "updated_at"])
                hash_hits += 1
                continue
            results.append(_sync_one_file(environment, item, payload, file_hash))
        except Exception as exc:
            logger.exception(
                "event_config.sync.file_failed environment=%s path=%s",
                environment.id,
                item.path,
            )
            errors.append(f"{item.file_name}: {exc}")
            if previous is not None:
                previous.active = True
                previous.sync_message = str(exc)[:2000]
                previous.last_seen_at = started
                previous.save(update_fields=["active", "sync_message", "last_seen_at", "updated_at"])

    if unchanged_ids:
        EventConfigSource.objects.filter(id__in=unchanged_ids).update(last_seen_at=started)

    stale_sources = EventConfigSource.objects.filter(environment=environment, active=True).exclude(file_path__in=seen_paths)
    stale_pairs = list(stale_sources.values_list("subsystem__name", "component_code"))
    affected_pairs.update((str(sub), str(component)) for sub, component in stale_pairs)
    stale_source_ids = list(stale_sources.values_list("id", flat=True))
    if stale_source_ids:
        EventConfigSource.objects.filter(id__in=stale_source_ids).update(active=False, last_seen_at=started)
        EventCodeDefinition.objects.filter(environment=environment, source_config_id__in=stale_source_ids).update(active=False, last_seen_at=started)

    for subsystem_name, component_code in sorted(affected_pairs, key=lambda item: (item[0].casefold(), item[1].casefold())):
        if results or stale_source_ids or (subsystem_name, component_code) in set(stale_pairs):
            _refresh_event_component_metadata(subsystem_name, component_code)

    active_sources_qs = EventConfigSource.objects.filter(environment=environment, active=True).select_related("subsystem")
    active_sources = list(active_sources_qs)
    active_codes = EventCodeDefinition.objects.filter(environment=environment, active=True)
    index_payload = {
        "environment_id": environment.id,
        "updated_at": started.isoformat(),
        "files": {
            row.file_path: {
                "mtime": float(row.remote_mtime or 0),
                "size": int(row.remote_size or 0),
                "hash": str(row.file_hash or ""),
                "subsystem": row.subsystem.name,
                "component": row.component_code,
            }
            for row in active_sources
        },
    }
    redis_stored = RedisLogStore.set_json(_event_index_key(environment.id), index_payload, _EVENT_INDEX_TTL_SECONDS)
    changed = bool(results or stale_source_ids)
    if changed:
        _bump_event_revision(environment.id)

    summary = {
        "status": "partial" if errors else "success",
        "environment_id": environment.id,
        "root": "~/SW/resource/*/eh/*_event.json",
        "found_files": len(remote_files),
        "active_files": len(active_sources),
        "active_event_codes": active_codes.count(),
        "parsed_files": len(results),
        "skipped_unchanged": redis_hits + db_hits + hash_hits,
        "deactivated_files": len(stale_source_ids),
        "errors": errors[:20],
        "components": sorted({f"{row.subsystem.name}/{row.component_code}" for row in active_sources}, key=str.casefold)[:500],
        "cache": {
            "redis_index_hit": isinstance(cached_index, dict),
            "redis_metadata_hits": redis_hits,
            "database_metadata_hits": db_hits,
            "content_hash_hits": hash_hits,
            "redis_index_stored": redis_stored,
            "incremental": True,
        },
    }
    logger.info(
        "event_config.sync.finish environment=%s status=%s files=%d codes=%d parsed=%d redis_hits=%d db_hits=%d hash_hits=%d errors=%d",
        environment.id,
        summary["status"],
        summary["active_files"],
        summary["active_event_codes"],
        summary["parsed_files"],
        redis_hits,
        db_hits,
        hash_hits,
        len(errors),
    )
    return summary


def resolve_event_definition(
    *,
    environment_id: int | None = None,
    display_code: str = "",
    code: str = "",
    code_string: str = "",
) -> dict[str, Any]:
    """Resolve an event identity to Event component and prioritized log targets."""
    key_type = ""
    key_value = ""
    if str(display_code or "").strip():
        key_type, key_value = "display_code", str(display_code).strip()
    elif str(code_string or "").strip():
        key_type, key_value = "code_string", str(code_string).strip()
    elif str(code or "").strip():
        key_type, key_value = "code", str(code).strip()
    else:
        return {"found": False, "reason": "必须提供 DisplayCode、CodeString 或 Code。", "matches": []}

    normalized_env = int(environment_id) if environment_id not in (None, "") else None
    revision = _event_revision(normalized_env)
    lookup_digest = hashlib.sha256(key_value.casefold().encode("utf-8")).hexdigest()[:24]
    cache_key = (
        f"tracelens:{RedisLogStore.CACHE_SCHEMA}:event-config:lookup:"
        f"{normalized_env or 'all'}:{revision}:{key_type}:{lookup_digest}"
    )
    cached = RedisLogStore.get_json(cache_key)
    if isinstance(cached, dict):
        return cached

    queryset = EventCodeDefinition.objects.filter(active=True).select_related("environment", "subsystem", "source_config")
    if normalized_env is not None:
        queryset = queryset.filter(environment_id=normalized_env)
    queryset = queryset.filter(**{f"{key_type}__iexact": key_value})

    matches = []
    matched_module_ids: set[int] = set()
    for item in queryset[:20]:
        source_module = (
            LogFmDefinition.objects.filter(
                subsystem=item.subsystem,
                name__iexact=item.component_code,
                enabled=True,
            )
            .select_related("subsystem")
            .prefetch_related("target_modules__subsystem")
            .order_by("kind", "id")
            .first()
        )
        targets = []
        if source_module is not None:
            target_rows = list(source_module.target_modules.filter(enabled=True, subsystem__enabled=True).select_related("subsystem"))
            target_rows.sort(key=lambda module: (int(module.query_priority or 100), module.subsystem.name.casefold(), module.name.casefold()))
            targets = [
                {
                    "subsystem": module.subsystem.name,
                    "module": module.name,
                    "kind": module.kind,
                    "priority": int(module.query_priority or 100),
                }
                for module in target_rows
            ]
            if targets:
                matched_module_ids.add(source_module.id)
        matches.append({
            "environment_id": item.environment_id,
            "environment_name": item.environment.name,
            "display_code": item.display_code,
            "code": item.code,
            "code_string": item.code_string,
            "source": item.source,
            "severity": item.severity,
            "category": item.category,
            "description": item.description,
            "subsystem": item.subsystem.name,
            "component": item.component_code,
            "event_component": item.component_code,
            "config_file": item.source_config.file_name,
            "config_path": item.source_config.file_path,
            "module_definition_id": source_module.id if source_module else None,
            "target_subsystem": targets[0]["subsystem"] if targets else item.subsystem.name,
            "target_modules": [target["module"] for target in targets],
            "target_targets": targets,
            "mapping_complete": bool(targets),
        })
    if matched_module_ids:
        LogFmDefinition.objects.filter(id__in=matched_module_ids).update(
            matched_count=F("matched_count") + 1,
            last_matched_at=timezone.now(),
        )
    result = {
        "found": bool(matches),
        "lookup": {"type": key_type, "value": key_value},
        "match_count": len(matches),
        "matches": matches,
        "cache": {"hit": False, "revision": revision},
    }
    RedisLogStore.set_json(cache_key, result, _EVENT_LOOKUP_TTL_SECONDS)
    return result

