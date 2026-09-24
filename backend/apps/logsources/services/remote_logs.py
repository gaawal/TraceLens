from __future__ import annotations

import logging
import re
import shlex
import stat
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Iterator

from django.db import transaction
from django.utils import timezone

from apps.common.logging import reset_request_id, set_request_id
from apps.common.services.ssh import SshOperationError, build_ssh_client, ssh_session
from apps.environments.models import Environment, LogPathCategory, LogPathScope, ResourceSettings
from apps.logsources.models import (
    LogCatalogStatus,
    LogFmDefinition,
    LogModuleKind,
    LogResourceCatalog,
    LogResourceCatalogItem,
    LogSubsystemDefinition,
)
from apps.logsources.services.archive_selector import LogArtifact, parse_archive_timestamp, parse_log_name, select_artifacts_for_window
from apps.logsources.services.cache_identity import LogCacheScope
from apps.logsources.services.content_cache import log_content_cache
from apps.logsources.services.file_index import log_file_index, parse_line_time
from apps.logsources.services.reverse_reader import reverse_lines
from apps.logsources.services.search_progress import LogSearchCancelled, LogSearchProgressStore
from apps.logsources.services.search_result_cache import LogSearchResultCache

logger = logging.getLogger("tracelens.remote_logs")
process_logger = logging.getLogger("tracelens.process.live")

_ARCHIVE_CHAIN_SEPARATOR = "::"


def _split_archive_member_chain(member_name: str) -> list[str]:
    return [part for part in str(member_name or "").split(_ARCHIVE_CHAIN_SEPARATOR) if part]


def _tar_member_extract_pipeline(path: str, member_name: str) -> str:
    parts = _split_archive_member_chain(member_name)
    if not parts:
        raise ValueError("压缩包成员不能为空")
    command = f"tar -xOzf {shlex.quote(path)} {shlex.quote(parts[0])}"
    for member in parts[1:]:
        command += f" | tar -xOzf - {shlex.quote(member)}"
    return command

_DHH_LOG_CATEGORIES = {LogPathCategory.DEBUG, LogPathCategory.EXECUTOR, LogPathCategory.RUN}
_LOG_ROOT_ROUTING_CATALOG = "catalog"
_LOG_ROOT_ROUTING_UPPER_FIRST = "upper_first"
_LOG_ROOT_ROUTING_DHH_FALLBACK = "dhh_fallback"

@dataclass(frozen=True, slots=True)
class LogRootTarget:
    machine: object
    source_category: str
    source_name: str
    root: str
    synthetic_subsystem: str = ""
    match_rules: tuple[str, ...] = ("fm", "fm_timestamp", "archive")



def environment_machines(environment: Environment):
    yield environment.upper_machine
    for relation in environment.machine_relations.select_related("target_machine").filter(is_active=True):
        yield relation.target_machine


def _relation_station_name(relation) -> str:
    machine = relation.target_machine
    metadata = relation.metadata or {}
    return str(metadata.get("station_name") or machine.station_name or machine.name or "").strip().lower()


def _relation_station_type(relation) -> str:
    machine = relation.target_machine
    metadata = relation.metadata or {}
    return str(metadata.get("station_type") or machine.station_type or "").strip().upper()


def _is_dhh_relation(relation) -> bool:
    return _relation_station_name(relation) == "dhh" or _relation_station_type(relation) == "DHH"


def _active_topology_relations(environment: Environment):
    return list(
        environment.machine_relations.select_related("target_machine").filter(
            is_active=True, target_machine__is_active=True
        )
    )


def _log_machine_for_relation(relation):
    """Use the topology/log IP for log access while keeping Machine.host as the login IP.

    ATLog ``详细日志链接.html`` records two lower-machine addresses: the business
    address is persisted as the Machine SSH identity, while ``metadata.topology_ip``
    is the small-network address used to read logs and resolve executor folders.
    """
    machine = relation.target_machine
    metadata = relation.metadata or {}
    log_host = str(metadata.get("topology_ip") or "").strip()
    if not log_host or log_host == str(machine.host or "").strip():
        return machine
    from copy import copy
    view = copy(machine)
    setattr(view, "_tracelens_host_override", log_host)
    return view


def _executor_lower_machines(environment: Environment) -> list[object]:
    return [
        _log_machine_for_relation(relation)
        for relation in _active_topology_relations(environment)
        if not _is_dhh_relation(relation)
    ]


def _dhh_machine(environment: Environment):
    for relation in _active_topology_relations(environment):
        if _is_dhh_relation(relation):
            return relation.target_machine
    return None


def _is_small_network_host(host: str) -> bool:
    return str(host or "").strip().startswith("192.")


def _format_root(template: str, *, machine, upper, lower=None, path_username: str | None = None) -> str:
    values = {
        "username": path_username or machine.username,
        "machine_ip": machine.host,
        "upper_machine_ip": upper.host,
        "lower_machine_ip": lower.host if lower else machine.host,
    }
    try:
        return template.format(**values).rstrip("/")
    except KeyError as exc:
        raise ValueError(f"日志目录模板包含未知变量：{exc.args[0]}") from exc


def _dhh_root_for_profile(settings_obj: ResourceSettings, category: str) -> str:
    """Return the dedicated physical log root for a DHH category.

    DHH is a self-contained log host.  Its debug/run/executor paths are not
    derived from the upper-machine path profiles and never substitute an SSH
    username.
    """
    roots = {
        LogPathCategory.DEBUG: str(settings_obj.dhh_debug_log_root or "").strip(),
        LogPathCategory.EXECUTOR: str(settings_obj.dhh_executor_log_root or "").strip(),
        LogPathCategory.RUN: str(settings_obj.dhh_run_log_root or "").strip(),
    }
    return roots.get(category, "").rstrip("/")


def expand_log_roots(
    environment: Environment,
    categories: set[str] | None = None,
    *,
    routing: str = _LOG_ROOT_ROUTING_CATALOG,
) -> list[LogRootTarget]:
    """Expand physical log roots for catalog scans or query routing.

    Catalog scans preserve the existing DHH-dedicated behavior. Query routing
    is two-phase for DHH environments: ``upper_first`` searches the normal
    upper-machine path rule, while ``dhh_fallback`` exposes only the dedicated
    DHH root. This lets the planner use DHH only for modules whose requested
    time window has no candidate log on the upper machine.
    """
    settings_obj = ResourceSettings.get_solo()
    profiles = settings_obj.log_path_profiles.filter(enabled=True).order_by("sort_order", "id")
    if categories:
        profiles = profiles.filter(category__in=categories)
    upper = environment.upper_machine
    lowers = _executor_lower_machines(environment)
    dhh = _dhh_machine(environment)
    targets: list[LogRootTarget] = []
    for profile in profiles:
        rules = tuple(profile.match_rules or ["fm", "fm_timestamp", "archive"])
        dhh_routable = dhh is not None and profile.category in _DHH_LOG_CATEGORIES

        if dhh_routable and routing == _LOG_ROOT_ROUTING_UPPER_FIRST:
            if not profile.path_template.strip():
                logger.info("log.root.upper_first.skip_empty category=%s", profile.category)
                continue
            # A DHH environment still prefers the normal upper-machine path.
            # Do not SSH-probe lower machines here: the DHH root is the fallback
            # physical source when the upper path has no candidate log.
            if profile.scope == LogPathScope.LOWER_ONLY:
                logger.info(
                    "log.root.upper_first.skip_lower_only environment=%s category=%s",
                    environment.id, profile.category,
                )
                continue
            if profile.scope == LogPathScope.UPPER_FOR_LOWER:
                for lower in lowers:
                    targets.append(LogRootTarget(
                        machine=upper,
                        source_category=profile.category,
                        source_name=profile.display_name,
                        root=_format_root(profile.path_template, machine=upper, upper=upper, lower=lower),
                        synthetic_subsystem=f"{profile.display_name} · {lower.station_name or lower.host}",
                        match_rules=rules,
                    ))
            else:
                targets.append(LogRootTarget(
                    machine=upper,
                    source_category=profile.category,
                    source_name=profile.display_name,
                    root=_format_root(profile.path_template, machine=upper, upper=upper),
                    match_rules=rules,
                ))
            logger.info(
                "log.root.dhh.upper_first environment=%s upper=%s category=%s targets=%d",
                environment.id, upper.host, profile.category,
                sum(1 for item in targets if item.source_category == profile.category),
            )
            continue

        if dhh_routable and routing in {_LOG_ROOT_ROUTING_CATALOG, _LOG_ROOT_ROUTING_DHH_FALLBACK}:
            if _is_small_network_host(dhh.host):
                logger.info(
                    "log.root.dhh.skip_small_network environment=%s host=%s category=%s routing=%s",
                    environment.id, dhh.host, profile.category, routing,
                )
                continue
            dhh_root = _dhh_root_for_profile(settings_obj, profile.category)
            if not dhh_root:
                logger.info(
                    "log.root.dhh.skip_empty_root environment=%s host=%s category=%s routing=%s",
                    environment.id, dhh.host, profile.category, routing,
                )
                continue
            targets.append(LogRootTarget(
                machine=dhh,
                source_category=profile.category,
                source_name=profile.display_name,
                root=dhh_root,
                match_rules=rules,
            ))
            logger.info(
                "log.root.dhh.%s environment=%s host=%s category=%s root=%s rules=%s",
                "fallback" if routing == _LOG_ROOT_ROUTING_DHH_FALLBACK else "route",
                environment.id, dhh.host, profile.category, dhh_root, list(rules),
            )
            continue

        # The DHH fallback pass must never duplicate ordinary/non-DHH roots.
        if routing == _LOG_ROOT_ROUTING_DHH_FALLBACK:
            continue

        if not profile.path_template.strip():
            logger.info("log.root.skip_empty category=%s", profile.category)
            continue
        if profile.scope == LogPathScope.UPPER_FOR_LOWER:
            for lower in lowers:
                targets.append(LogRootTarget(
                    machine=upper, source_category=profile.category, source_name=profile.display_name,
                    root=_format_root(profile.path_template, machine=upper, upper=upper, lower=lower),
                    synthetic_subsystem=f"{profile.display_name} · {lower.station_name or lower.host}",
                    match_rules=rules,
                ))
        elif profile.scope == LogPathScope.UPPER_ONLY:
            targets.append(LogRootTarget(
                machine=upper, source_category=profile.category, source_name=profile.display_name,
                root=_format_root(profile.path_template, machine=upper, upper=upper), match_rules=rules,
            ))
        elif profile.scope == LogPathScope.LOWER_ONLY:
            for lower in lowers:
                targets.append(LogRootTarget(
                    machine=lower, source_category=profile.category, source_name=profile.display_name,
                    root=_format_root(profile.path_template, machine=lower, upper=upper, lower=lower), match_rules=rules,
                ))
        else:
            for machine in [upper, *lowers]:
                targets.append(LogRootTarget(
                    machine=machine, source_category=profile.category, source_name=profile.display_name,
                    root=_format_root(profile.path_template, machine=machine, upper=upper, lower=machine if machine.role == "lower" else None),
                    match_rules=rules,
                ))
    logger.info(
        "log.root.expanded environment=%s profiles=%d targets=%d categories=%s dhh=%s routing=%s",
        environment.id, profiles.count(), len(targets), sorted(categories or []), dhh.host if dhh else "", routing,
    )
    return targets

def _list_tar_members(client, path: str) -> list[str]:
    logger.info("log.tar.list.start path=%s", path)
    command = f"tar -tzf {shlex.quote(path)}"
    _, stdout, stderr = client.exec_command(command, timeout=60)
    output = stdout.read().decode("utf-8", errors="replace")
    error = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    if code != 0:
        raise SshOperationError(f"读取压缩包目录失败 {path}：{error}")
    members = [line.strip() for line in output.splitlines() if line.strip()]
    logger.info("log.tar.list.finish path=%s members=%d", path, len(members))
    return members


def _catalog_payload(target: LogRootTarget, catalog: LogResourceCatalog, *, cache_state: str) -> dict:
    grouped: dict[str, list[str]] = {}
    for item in catalog.items.all().order_by("subsystem", "fm"):
        if not _is_discovery_catalog_name_allowed(item.subsystem) or not _is_discovery_catalog_name_allowed(item.fm):
            continue
        grouped.setdefault(item.subsystem, []).append(item.fm)
    return {
        "machine_id": target.machine.id,
        "machine_name": target.machine.name,
        "role": target.machine.role,
        "source_category": target.source_category,
        "source_name": target.source_name,
        "root": target.root,
        "status": "success" if catalog.status == LogCatalogStatus.SUCCESS or grouped else "error",
        "scan_status": catalog.status,
        "cache_state": cache_state,
        "scanned_at": catalog.scanned_at,
        "message": catalog.message,
        "subsystems": [
            {
                "name": subsystem,
                "fms": sorted(set(fms)),
                "archive_count": 0,
                "discovery_basis": "log_filename_dedupe",
            }
            for subsystem, fms in sorted(grouped.items())
        ],
    }


def _executor_module_name(filename: str, reference: datetime | None = None) -> str | None:
    """Extract the executor module from ``<executor>_cp_xx[stamp].log``."""
    basename = PurePosixPath(filename).name
    if not basename.endswith(".log"):
        return None
    parsed = parse_log_name(basename, reference)
    stem = (parsed[0] if parsed else basename[:-4]).strip()
    match = re.match(r"^(?P<module>.+?)_cp(?:_|$)", stem, re.IGNORECASE)
    if not match:
        return None
    module = match.group("module").strip("_- ")
    return module or None


def _normal_module_name(
    filename: str, rules: set[str], reference: datetime | None = None
) -> str | None:
    """Resolve a normal FM from filename only; never open log/archive content.

    Supported production forms are:
      - <fm>.log
      - <fm>_YYYYMMDDHHMMSSmmm.log (and other supported timestamp forms)
      - <fm>_YYYYMMDD.tar.gz

    Daily archives are therefore a valid module-discovery signal even when the
    current ``<fm>.log`` has already rotated away.
    """
    basename = PurePosixPath(filename).name
    if basename.endswith(".tar.gz"):
        if "archive" not in rules:
            return None
        stem = basename[:-7]
        match = re.match(r"^(?P<fm>.+)_(?P<stamp>\d[\dT_:\-.]*)$", stem)
        if not match or parse_archive_timestamp(match.group("stamp"), reference) is None:
            return None
        return match.group("fm").strip() or None

    if not basename.endswith(".log"):
        return None
    parsed = parse_log_name(basename, reference)
    if not parsed:
        return None
    fm, boundary = parsed
    if boundary is None and "fm" not in rules:
        return None
    if boundary is not None and "fm_timestamp" not in rules:
        return None
    return fm.strip() or None


def _is_discovery_catalog_name_allowed(value: str | None) -> bool:
    """Return whether an auto-discovered subsystem/module name may enter the catalog.

    Directory scans can see backup/snapshot folders and rotated artifacts such as
    ``backup_1``, ``RSRCP_20260905`` or ``module2``.  These are not production
    subsystem/module identifiers.  Auto discovery therefore accepts only names
    that contain neither digits nor underscores.  This rule applies to discovery
    and catalog reads; it does not delete historical database rows.
    """
    name = str(value or "").strip()
    return bool(name) and re.search(r"[\d_]", name) is None


def _discover_log_modules(
    client, target: LogRootTarget, *, environment: Environment | None = None, sftp=None
) -> dict[str, set[str]]:
    """Lightweight module discovery for normal and executor log trees.

    Executor logs are stored on the upper machine as::

        elog/<lower-machine-folder>/<subsystem>/<executor>_cp_xx*.log

    The lower-machine folder list is authoritative from the current
    environment topology.  This prevents a scan from depending on whichever
    folders happen to exist under ``elog`` and, more importantly, guarantees
    that every configured lower machine is checked.
    """
    root = target.root.rstrip("/")
    direct_rules = set(target.match_rules or ("fm", "fm_timestamp", "archive"))
    executor_mode = "executor_tree" in direct_rules
    executor_lower_hosts: set[str] = set()
    if executor_mode:
        if environment is None:
            logger.warning("log.catalog.executor.skip environment_missing root=%s", root)
            return {}
        executor_lower_hosts = {
            str(machine.host or "").strip()
            for machine in _executor_lower_machines(environment)
            if str(machine.host or "").strip()
        }
        if not executor_lower_hosts:
            logger.info("log.catalog.executor.skip_no_lower_machine environment=%s root=%s", environment.id, root)
            return {}

        existing_roots: list[str] = []
        if sftp is not None:
            for lower_host in sorted(executor_lower_hosts):
                lower_root = f"{root}/{lower_host}"
                try:
                    attrs = sftp.stat(lower_root)
                except OSError:
                    logger.info(
                        "log.catalog.executor.lower_folder_missing environment=%s lower=%s path=%s",
                        environment.id, lower_host, lower_root,
                    )
                    continue
                if stat.S_ISDIR(attrs.st_mode):
                    existing_roots.append(lower_root)
        else:
            existing_roots = [f"{root}/{lower_host}" for lower_host in sorted(executor_lower_hosts)]

        if not existing_roots:
            logger.info(
                "log.catalog.executor.skip_no_existing_lower_folder environment=%s lowers=%s root=%s",
                environment.id, sorted(executor_lower_hosts), root,
            )
            return {}
        quoted_roots = " ".join(shlex.quote(item) for item in existing_roots)
        # Each configured lower-machine folder is searched. Relative to that
        # folder the executor log is exactly <subsystem>/<file>.
        command = f"find {quoted_roots} -mindepth 2 -maxdepth 2 -type f -name '*.log' -print"
    elif {"fm", "fm_timestamp", "archive"} & direct_rules:
        # Normal modules are discovered from filenames only.  A module may be
        # represented solely by its rotated log or daily archive, so include
        # every enabled production naming form without reading file contents.
        name_terms: list[str] = []
        if {"fm", "fm_timestamp"} & direct_rules:
            name_terms.append("-name '*.log'")
        if "archive" in direct_rules:
            name_terms.append("-name '*.tar.gz'")
        name_expr = " -o ".join(name_terms)
        command = (
            f"find {shlex.quote(root)} -mindepth 2 -maxdepth 2 -type f "
            f"\\( {name_expr} \\) -print"
        )
    else:
        logger.info("log.catalog.module_find.skip root=%s rules=%s reason=no_module_discovery_rule", root, sorted(direct_rules))
        return {}

    logger.info(
        "log.catalog.module_find.start environment_machine=%s category=%s root=%s executor=%s lower_folders=%s command=%s",
        target.machine.id, target.source_category, root, executor_mode,
        sorted(executor_lower_hosts) if executor_mode else [], command,
    )
    started = time.monotonic()
    _, stdout, stderr = client.exec_command(command, timeout=45)
    output = stdout.read().decode("utf-8", errors="replace")
    error = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    if code != 0:
        lowered_error = error.lower()
        if "no such file or directory" in lowered_error or "not a directory" in lowered_error:
            logger.info("log.catalog.module_find.skip_missing root=%s error=%s", root, error.strip())
            return {}
        raise SshOperationError(f"扫描日志模块失败 {root}：{error.strip() or f'exit={code}'}")

    grouped: dict[str, set[str]] = {}
    prefix = f"{root}/"
    raw_files = 0
    ignored = 0
    reference = datetime.now()
    for raw_path in output.splitlines():
        full_path = raw_path.strip()
        if not full_path:
            continue
        raw_files += 1
        relative = full_path[len(prefix):] if full_path.startswith(prefix) else full_path
        parts = PurePosixPath(relative).parts
        if executor_mode:
            if len(parts) != 3:
                ignored += 1
                continue
            lower_ip, subsystem, filename = parts
            if lower_ip not in executor_lower_hosts:
                ignored += 1
                continue
            fm = _executor_module_name(filename, reference)
        else:
            if len(parts) != 2:
                ignored += 1
                continue
            subsystem, filename = parts
            fm = _normal_module_name(filename, direct_rules, reference)
        subsystem = subsystem.strip()
        fm = str(fm or "").strip()
        if not _is_discovery_catalog_name_allowed(subsystem) or not _is_discovery_catalog_name_allowed(fm):
            ignored += 1
            logger.debug(
                "log.catalog.module_find.ignore_nonproduction_name subsystem=%s fm=%s path=%s",
                subsystem, fm, full_path,
            )
            continue
        grouped.setdefault(subsystem, set()).add(fm)

    logger.info(
        "log.catalog.module_find.finish machine=%s category=%s root=%s executor=%s files=%d ignored=%d subsystems=%d modules=%d elapsed_ms=%d",
        target.machine.id, target.source_category, root, executor_mode, raw_files, ignored, len(grouped),
        sum(len(items) for items in grouped.values()), int((time.monotonic() - started) * 1000),
    )
    return grouped

def _global_catalog_tree(*, include_disabled: bool = False) -> list[dict]:
    subsystems = LogSubsystemDefinition.objects.prefetch_related("fms").order_by("sort_order", "name")
    if not include_disabled:
        subsystems = subsystems.filter(enabled=True)
    result: list[dict] = []
    for subsystem in subsystems:
        if not _is_discovery_catalog_name_allowed(subsystem.name):
            continue
        fms = subsystem.fms.all().order_by("sort_order", "name")
        if not include_disabled:
            fms = fms.filter(enabled=True)
        valid_fms = [fm for fm in fms if _is_discovery_catalog_name_allowed(fm.name)]
        result.append({
            "id": subsystem.id,
            "name": subsystem.name,
            "display_name": subsystem.display_name,
            "effective_name": subsystem.display_name or subsystem.name,
            "enabled": subsystem.enabled,
            "sort_order": subsystem.sort_order,
            "description": subsystem.description,
            "last_discovered_at": subsystem.last_discovered_at,
            "fms": [
                {
                    "id": fm.id,
                    "subsystem": subsystem.id,
                    "name": fm.name,
                    "kind": fm.kind,
                    "display_name": fm.display_name,
                    "effective_name": fm.display_name or fm.name,
                    "enabled": fm.enabled,
                    "sort_order": fm.sort_order,
                    "description": fm.description,
                    "last_discovered_at": fm.last_discovered_at,
                }
                for fm in valid_fms
            ],
        })
    return result


def _merge_global_catalog(grouped: dict[str, set[str]], *, module_kind: str = LogModuleKind.NORMAL) -> tuple[int, int]:
    """将扫描结果追加进全局字典，不自动删除任何已有配置。"""

    now = timezone.now()
    added_subsystems = 0
    added_fms = 0
    with transaction.atomic():
        for raw_subsystem, raw_fms in sorted(grouped.items()):
            subsystem_name = raw_subsystem.strip()
            if not _is_discovery_catalog_name_allowed(subsystem_name):
                logger.debug("log.dictionary.subsystem.ignored_nonproduction name=%s", subsystem_name)
                continue
            subsystem = LogSubsystemDefinition.objects.filter(name__iexact=subsystem_name).first()
            if subsystem is None:
                subsystem = LogSubsystemDefinition.objects.create(
                    name=subsystem_name,
                    last_discovered_at=now,
                )
                added_subsystems += 1
                logger.info("log.dictionary.subsystem.added name=%s", subsystem_name)
            else:
                LogSubsystemDefinition.objects.filter(pk=subsystem.pk).update(last_discovered_at=now)

            for raw_fm in sorted(raw_fms):
                fm_name = raw_fm.strip()
                if not _is_discovery_catalog_name_allowed(fm_name):
                    logger.debug(
                        "log.dictionary.fm.ignored_nonproduction subsystem=%s fm=%s",
                        subsystem_name, fm_name,
                    )
                    continue
                fm = LogFmDefinition.objects.filter(
                    subsystem=subsystem,
                    name__iexact=fm_name,
                    kind=module_kind,
                ).first()
                if fm is None:
                    LogFmDefinition.objects.create(
                        subsystem=subsystem,
                        name=fm_name,
                        kind=module_kind,
                        last_discovered_at=now,
                    )
                    added_fms += 1
                    logger.info(
                        "log.dictionary.fm.added subsystem=%s fm=%s",
                        subsystem.name,
                        fm_name,
                    )
                else:
                    LogFmDefinition.objects.filter(pk=fm.pk).update(last_discovered_at=now)
    return added_subsystems, added_fms


def _catalog_items_as_grouped(catalog: LogResourceCatalog) -> dict[str, set[str]]:
    grouped: dict[str, set[str]] = {}
    for item in catalog.items.all():
        if not _is_discovery_catalog_name_allowed(item.subsystem) or not _is_discovery_catalog_name_allowed(item.fm):
            continue
        grouped.setdefault(item.subsystem, set()).add(item.fm)
    return grouped


def _catalog_for_target(environment: Environment, target: LogRootTarget) -> LogResourceCatalog | None:
    return (
        LogResourceCatalog.objects.filter(
            environment=environment,
            machine=target.machine,
            source_category=target.source_category,
            root=target.root,
        )
        .prefetch_related("items")
        .first()
    )


def _save_catalog(
    environment: Environment,
    target: LogRootTarget,
    grouped: dict[str, set[str]],
    *, module_kind: str = LogModuleKind.NORMAL,
) -> LogResourceCatalog:
    with transaction.atomic():
        catalog, _ = LogResourceCatalog.objects.select_for_update().get_or_create(
            environment=environment,
            machine=target.machine,
            source_category=target.source_category,
            root=target.root,
            defaults={"source_name": target.source_name},
        )
        catalog.source_name = target.source_name
        catalog.status = LogCatalogStatus.SUCCESS
        catalog.message = ""
        catalog.scanned_at = timezone.now()
        catalog.save()
        LogResourceCatalogItem.objects.bulk_create(
            [
                LogResourceCatalogItem(catalog=catalog, subsystem=subsystem, fm=fm, kind=module_kind)
                for subsystem, fms in grouped.items()
                for fm in sorted(fms)
            ],
            batch_size=1000,
            ignore_conflicts=True,
        )
    return LogResourceCatalog.objects.prefetch_related("items").get(pk=catalog.pk)


def _save_catalog_error(
    environment: Environment,
    target: LogRootTarget,
    message: str,
) -> LogResourceCatalog:
    catalog, _ = LogResourceCatalog.objects.get_or_create(
        environment=environment,
        machine=target.machine,
        source_category=target.source_category,
        root=target.root,
        defaults={"source_name": target.source_name},
    )
    catalog.source_name = target.source_name
    catalog.status = LogCatalogStatus.ERROR
    catalog.message = message
    catalog.scanned_at = timezone.now()
    catalog.save()
    return LogResourceCatalog.objects.prefetch_related("items").get(pk=catalog.pk)


def scan_environment_logs(
    environment: Environment,
    categories: set[str] | None = None,
    *,
    refresh: bool = False,
) -> dict:
    started = time.monotonic()
    result = {
        "environment_id": environment.id,
        "machines": [],
        "sources": [],
        "subsystems": [],
        "global_catalog": [],
        "cache": {
            "refresh_requested": refresh,
            "hits": 0,
            "refreshed": 0,
            "stale": 0,
            "added_subsystems": 0,
            "added_fms": 0,
        },
    }
    targets = expand_log_roots(environment, categories)
    logger.info(
        "log.catalog.start environment=%s targets=%d refresh=%s strategy=single_find_subsystem_module_dedupe",
        environment.id,
        len(targets),
        refresh,
    )
    for target in targets:
        catalog = _catalog_for_target(environment, target)
        has_cached_items = bool(catalog and catalog.items.exists())
        can_use_cache = bool(
            catalog
            and not refresh
            and (catalog.status == LogCatalogStatus.SUCCESS or has_cached_items)
        )
        if can_use_cache:
            # 读取缓存时不再反向写全局字典。这样管理员在配置页的删除/禁用操作
            # 不会因为普通页面访问被立即恢复；只有真实远程扫描才追加新条目。
            cache_state = "cached" if catalog.status == LogCatalogStatus.SUCCESS else "stale"
            source_payload = _catalog_payload(target, catalog, cache_state=cache_state)
            result["cache"]["hits"] += 1
            if cache_state == "stale":
                result["cache"]["stale"] += 1
                source_payload["status"] = "success"
                source_payload["message"] = catalog.message or "上次扫描失败，继续使用已有数据库缓存。"
            logger.info(
                "log.catalog.cache_hit environment=%s machine=%s category=%s root=%s state=%s scanned_at=%s items=%d",
                environment.id,
                target.machine.id,
                target.source_category,
                target.root,
                cache_state,
                catalog.scanned_at,
                catalog.items.count(),
            )
        else:
            logger.info(
                "log.catalog.cache_%s environment=%s machine=%s category=%s root=%s",
                "refresh" if catalog is not None else "miss",
                environment.id,
                target.machine.id,
                target.source_category,
                target.root,
            )
            try:
                with ssh_session(target.machine) as lease:
                    grouped = _discover_log_modules(lease.client, target, environment=environment, sftp=lease.sftp)
                    module_kind = LogModuleKind.EXECUTOR if "executor_tree" in set(target.match_rules or ()) else LogModuleKind.NORMAL
                    catalog = _save_catalog(environment, target, grouped, module_kind=module_kind)
                    added_subsystems, added_fms = _merge_global_catalog(grouped, module_kind=module_kind)
                    result["cache"]["added_subsystems"] += added_subsystems
                    result["cache"]["added_fms"] += added_fms
                    source_payload = _catalog_payload(target, catalog, cache_state="refreshed")
                    result["cache"]["refreshed"] += 1
                    logger.info(
                        "log.catalog.persisted environment=%s machine=%s category=%s session=%s reused=%s local_subsystems=%d local_fms=%d added_global_subsystems=%d added_global_fms=%d",
                        environment.id,
                        target.machine.id,
                        target.source_category,
                        lease.session_id,
                        lease.reused,
                        len(grouped),
                        sum(len(items) for items in grouped.values()),
                        added_subsystems,
                        added_fms,
                    )
            except Exception as exc:
                logger.exception(
                    "log.catalog.target.failed machine=%s category=%s root=%s",
                    target.machine.id,
                    target.source_category,
                    target.root,
                )
                catalog = _save_catalog_error(environment, target, str(exc))
                has_cache = catalog.items.exists()
                source_payload = _catalog_payload(
                    target,
                    catalog,
                    cache_state="stale" if has_cache else "error",
                )
                if has_cache:
                    result["cache"]["stale"] += 1
                    source_payload["status"] = "success"
                    source_payload["message"] = f"刷新失败，继续使用上次扫描记录：{exc}"
                else:
                    source_payload["status"] = "error"
        result["sources"].append(source_payload)
        result["machines"].append(source_payload)

    global_catalog = _global_catalog_tree(include_disabled=False)
    result["global_catalog"] = global_catalog
    result["subsystems"] = [item["name"] for item in global_catalog]
    logger.info(
        "log.catalog.finish environment=%s sources=%d global_subsystems=%d global_fms=%d cache_hits=%d refreshed=%d stale=%d added_subsystems=%d added_fms=%d elapsed_ms=%d",
        environment.id,
        len(result["sources"]),
        len(global_catalog),
        sum(len(item["fms"]) for item in global_catalog),
        result["cache"]["hits"],
        result["cache"]["refreshed"],
        result["cache"]["stale"],
        result["cache"]["added_subsystems"],
        result["cache"]["added_fms"],
        int((time.monotonic() - started) * 1000),
    )
    return result

def _collect_directory_artifacts(
    *, target: LogRootTarget, client, sftp, directory: str, subsystem: str,
    fm_filter: set[str] | None, reference: datetime | None,
) -> list[LogArtifact]:
    artifacts: list[LogArtifact] = []
    try:
        children = sftp.listdir_attr(directory)
    except OSError:
        return artifacts
    for child in children:
        path = f"{directory.rstrip('/')}/{child.filename}"
        parsed = parse_log_name(child.filename, reference)
        if parsed:
            fm, boundary = parsed
            if fm_filter and fm not in fm_filter:
                continue
            artifacts.append(LogArtifact(
                target.machine.id, target.machine.name, subsystem, fm, path,
                "archived" if boundary else "current", boundary, size=child.st_size,
                source_category=target.source_category, source_name=target.source_name,
            ))
        elif child.filename.endswith(".tar.gz"):
            for member in _list_tar_members(client, path):
                parsed_member = parse_log_name(member, reference)
                if not parsed_member:
                    continue
                fm, boundary = parsed_member
                if boundary is None or (fm_filter and fm not in fm_filter):
                    continue
                artifacts.append(LogArtifact(
                    target.machine.id, target.machine.name, subsystem, fm, path,
                    "tar_member", boundary, member_name=member, size=child.st_size,
                    source_category=target.source_category, source_name=target.source_name,
                ))
    return artifacts


def _cached_subsystems_for_target(
    environment: Environment,
    target: LogRootTarget,
) -> set[str]:
    """返回该环境/机器/日志根目录已经发现过的子系统。

    这里只读数据库，不连接远程机器。环境首次轻量扫描后，日志定位可以直接
    按这些目录定向读取，避免再次遍历日志根目录下的所有子目录。
    """

    catalog = _catalog_for_target(environment, target)
    if catalog is None:
        return set()
    return {
        item.subsystem
        for item in catalog.items.all()
        if _is_discovery_catalog_name_allowed(item.subsystem) and _is_discovery_catalog_name_allowed(item.fm)
    }


def _cached_fms_by_subsystem(
    environment: Environment,
    target: LogRootTarget,
) -> dict[str, set[str]]:
    """Return the environment-scoped subsystem/FM discovery map from DB only."""
    catalog = _catalog_for_target(environment, target)
    grouped: dict[str, set[str]] = {}
    if catalog is None:
        return grouped
    for item in catalog.items.all():
        if _is_discovery_catalog_name_allowed(item.subsystem) and _is_discovery_catalog_name_allowed(item.fm):
            grouped.setdefault(item.subsystem, set()).add(item.fm)
    return grouped


def _catalog_target_key(target: LogRootTarget) -> tuple[int, str, str]:
    return (int(target.machine.id), str(target.source_category), str(target.root))


def _catalog_pairs(grouped: dict[str, set[str]]) -> set[tuple[str, str]]:
    return {
        (str(subsystem), str(fm))
        for subsystem, fms in grouped.items()
        for fm in fms
        if subsystem and fm
    }


def _fold_pair(subsystem: str, fm: str) -> tuple[str, str]:
    return (str(subsystem or "").strip().casefold(), str(fm or "").strip().casefold())


def _catalog_has_pair(grouped: dict[str, set[str]], subsystem: str, fm: str) -> bool:
    wanted = _fold_pair(subsystem, fm)
    return any(_fold_pair(actual_subsystem, actual_fm) == wanted for actual_subsystem, values in grouped.items() for actual_fm in values)


def _matching_catalog_subsystems(grouped: dict[str, set[str]], requested: set[str]) -> set[str]:
    if not requested:
        return set(grouped)
    wanted = {str(item).strip().casefold() for item in requested if str(item).strip()}
    return {actual for actual in grouped if str(actual).strip().casefold() in wanted}


def _requested_fms_for_catalog_subsystem(
    grouped: dict[str, set[str]],
    actual_subsystem: str,
    *,
    requested_target_map: dict[str, set[str]],
    requested_fms: set[str],
) -> set[str]:
    """Return canonical requested FM spellings that exist in this catalog.

    Resource discovery stores the physical Linux spelling (for example
    ``spwsp``) while the global dictionary may already contain ``SPWSP``.
    Matching therefore follows the same case-insensitive semantics as the
    dictionary, but keeps the requested spelling for result metadata.
    """
    catalog_values = set(grouped.get(actual_subsystem, set()))
    catalog_folds = {str(value).strip().casefold() for value in catalog_values}
    requested_for_subsystem: set[str] = set()
    if requested_target_map:
        for requested_subsystem, values in requested_target_map.items():
            if str(requested_subsystem).strip().casefold() == str(actual_subsystem).strip().casefold():
                requested_for_subsystem.update(values)
    else:
        requested_for_subsystem.update(requested_fms or catalog_values)
    return {
        str(value).strip() for value in requested_for_subsystem
        if str(value).strip() and str(value).strip().casefold() in catalog_folds
    }


def _refresh_catalog_target_for_query(
    environment: Environment,
    target: LogRootTarget,
    *,
    operation_id: str = "",
) -> dict[str, set[str]]:
    """Refresh one physical catalog target after a query detects a stale catalog miss."""
    if operation_id:
        LogSearchProgressStore.raise_if_cancelled(operation_id)
        LogSearchProgressStore.directory_started(
            operation_id, directory=target.root, subsystem="", modules=set(),
            action="目录索引缺少所选模块，正在自动刷新",
        )
    with ssh_session(target.machine) as lease:
        grouped = _discover_log_modules(lease.client, target, environment=environment, sftp=lease.sftp)
    module_kind = LogModuleKind.EXECUTOR if "executor_tree" in set(target.match_rules or ()) else LogModuleKind.NORMAL
    _save_catalog(environment, target, grouped, module_kind=module_kind)
    _merge_global_catalog(grouped, module_kind=module_kind)
    removed = LogSearchResultCache.invalidate_environment(environment.id)
    logger.info(
        "log.plan.catalog_auto_refresh environment=%s machine=%s category=%s root=%s subsystems=%d fms=%d invalidated_results=%d",
        environment.id, target.machine.id, target.source_category, target.root,
        len(grouped), sum(len(values) for values in grouped.values()), removed,
    )
    return _cached_fms_by_subsystem(environment, target)


def _collect_target_artifacts_fallback(
    *,
    environment: Environment,
    target: LogRootTarget,
    lease,
    subsystem_filter: set[str],
    fm_filter: set[str],
    reference: datetime | None,
    window_end: datetime | None = None,
    operation_id: str = "",
) -> list[LogArtifact]:
    """无数据库目录缓存时的兼容路径。

    只列一次根目录，再仅进入命中的子系统目录。该路径主要服务尚未完成首次
    轻量扫描的非 debug 日志来源。
    """

    artifacts: list[LogArtifact] = []
    try:
        if operation_id:
            LogSearchProgressStore.directory_started(
                operation_id, directory=target.root, subsystem="", modules=fm_filter,
                action="正在读取日志根目录",
            )
        root_items = lease.sftp.listdir_attr(target.root)
    except OSError as exc:
        logger.info(
            "log.plan.root_unavailable machine=%s root=%s error=%s",
            target.machine.id,
            target.root,
            exc,
        )
        return artifacts

    root_has_files = False
    for item in root_items:
        path = f"{target.root.rstrip('/')}/{item.filename}"
        if stat.S_ISDIR(item.st_mode):
            subsystem = item.filename
            if subsystem_filter and subsystem not in subsystem_filter:
                continue
            if operation_id:
                LogSearchProgressStore.directory_started(
                    operation_id, directory=path, subsystem=subsystem, modules=fm_filter,
                    action="正在读取子系统日志目录",
                )
            if fm_filter:
                artifacts.extend(
                    log_file_index.collect_artifacts(
                        environment=environment, target=target, lease=lease,
                        directory=path, subsystem=subsystem, fms=set(fm_filter),
                        reference=reference, window_start=reference or datetime.min,
                        window_end=window_end or reference or datetime.max,
                        operation_id=operation_id,
                    )
                )
            else:
                artifacts.extend(
                    _collect_directory_artifacts(
                        target=target, client=lease.client, sftp=lease.sftp,
                        directory=path, subsystem=subsystem, fm_filter=None, reference=reference,
                    )
                )
        elif parse_log_name(item.filename, reference) or item.filename.endswith(".tar.gz"):
            root_has_files = True

    if root_has_files:
        subsystem = target.synthetic_subsystem or target.source_name
        if not subsystem_filter or subsystem in subsystem_filter:
            if fm_filter:
                artifacts.extend(
                    log_file_index.collect_artifacts(
                        environment=environment, target=target, lease=lease,
                        directory=target.root, subsystem=subsystem, fms=set(fm_filter),
                        reference=reference, window_start=reference or datetime.min,
                        window_end=window_end or reference or datetime.max,
                        operation_id=operation_id,
                    )
                )
            else:
                artifacts.extend(
                    _collect_directory_artifacts(
                        target=target, client=lease.client, sftp=lease.sftp,
                        directory=target.root, subsystem=subsystem, fm_filter=None, reference=reference,
                    )
                )
    return artifacts


def collect_artifacts(
    environment: Environment,
    subsystem_filter: set[str] | None = None,
    fm_filter: set[str] | None = None,
    reference: datetime | None = None,
    window_end: datetime | None = None,
    source_categories: set[str] | None = None,
    fm_targets: list[dict[str, str]] | None = None,
    operation_id: str = "",
    routing: str = _LOG_ROOT_ROUTING_CATALOG,
) -> list[LogArtifact]:
    artifacts: list[LogArtifact] = []
    requested_subsystems = set(subsystem_filter or [])
    requested_fms = set(fm_filter or [])
    typed_targets_supplied = bool(fm_targets)
    requested_target_map: dict[str, set[str]] = {}
    executor_target_map: dict[str, set[str]] = {}
    for item in fm_targets or []:
        subsystem = str(item.get("subsystem", "")).strip()
        fm = str(item.get("fm", "")).strip()
        kind = str(item.get("kind", LogModuleKind.NORMAL)).strip() or LogModuleKind.NORMAL
        if subsystem and fm:
            if kind == LogModuleKind.EXECUTOR:
                executor_target_map.setdefault(subsystem, set()).add(fm)
            else:
                requested_target_map.setdefault(subsystem, set()).add(fm)
    combined_target_map = {key: set(values) for key, values in requested_target_map.items()}
    for key, values in executor_target_map.items():
        combined_target_map.setdefault(key, set()).update(values)
    if combined_target_map:
        requested_subsystems = set(combined_target_map)
        requested_fms = {fm for values in combined_target_map.values() for fm in values}
    targets = expand_log_roots(environment, source_categories, routing=routing)
    # Resource catalogs are intentionally persistent, but a report can refer to
    # a subsystem/FM created after the last resource scan. Detect only pairs
    # missing from *all* applicable cached targets so expected per-machine
    # differences do not cause needless refreshes on every query.
    normal_requested_pairs = {
        (subsystem, fm)
        for subsystem, fms_for_subsystem in requested_target_map.items()
        for fm in fms_for_subsystem
    }
    cached_catalogs: dict[tuple[int, str, str], dict[str, set[str]]] = {}
    known_catalog_pairs_folded: set[tuple[str, str]] = set()
    if normal_requested_pairs:
        for catalog_target in targets:
            rules = set(catalog_target.match_rules or ())
            if "run_flat" in rules or "executor_tree" in rules:
                continue
            grouped = _cached_fms_by_subsystem(environment, catalog_target)
            cached_catalogs[_catalog_target_key(catalog_target)] = grouped
            known_catalog_pairs_folded.update(_fold_pair(subsystem, fm) for subsystem, fm in _catalog_pairs(grouped))
    missing_catalog_pairs = {
        pair for pair in normal_requested_pairs
        if _fold_pair(*pair) not in known_catalog_pairs_folded
    }
    logger.info(
        "log.plan.collect.start environment=%s targets=%d subsystem_filters=%d fm_filters=%d routing=%s strategy=fingerprint_index catalog_missing_pairs=%s",
        environment.id,
        len(targets),
        len(requested_subsystems),
        len(requested_fms),
        routing,
        sorted(missing_catalog_pairs),
    )

    for target_index, target in enumerate(targets, start=1):
        target_rules = set(target.match_rules or ())
        if operation_id:
            LogSearchProgressStore.target_started(
                operation_id,
                host=target.machine.host,
                username=target.machine.username,
                source_category=target.source_category,
                root=target.root,
                target_current=target_index,
                target_total=len(targets),
            )
        if "run_flat" in target_rules:
            logger.info(
                "log.run.target.start machine=%s category=%s root=%s",
                target.machine.id, target.source_category, target.root,
            )
            with ssh_session(target.machine) as lease:
                if operation_id:
                    LogSearchProgressStore.directory_started(
                        operation_id, directory=target.root, subsystem="运行日志", modules={"event"},
                        action="正在读取运行日志目录",
                    )
                artifacts.extend(
                    log_file_index.collect_run_flat_artifacts(
                        environment=environment, target=target, lease=lease, root=target.root,
                        reference=reference, window_start=reference or datetime.min,
                        window_end=window_end or reference or datetime.max, operation_id=operation_id,
                    )
                )
            continue
        if "executor_tree" in target_rules:
            executor_subsystems = set(executor_target_map)
            if not executor_subsystems:
                logger.info(
                    "log.executor.target.skip_no_module machine=%s category=%s root=%s",
                    target.machine.id, target.source_category, target.root,
                )
                continue
            logger.info(
                "log.executor.target.start machine=%s category=%s root=%s subsystems=%s",
                target.machine.id, target.source_category, target.root, sorted(executor_subsystems),
            )
            with ssh_session(target.machine) as lease:
                if operation_id:
                    LogSearchProgressStore.directory_started(
                        operation_id, directory=target.root, subsystem="执行器日志",
                        modules={fm for values in executor_target_map.values() for fm in values},
                        action="正在定位执行器日志目录",
                    )
                artifacts.extend(
                    log_file_index.collect_executor_tree_artifacts(
                        environment=environment, target=target, lease=lease, root=target.root,
                        subsystems=executor_subsystems, modules_by_subsystem=executor_target_map, reference=reference,
                        window_start=reference or datetime.min,
                        window_end=window_end or reference or datetime.max,
                        operation_id=operation_id,
                    )
                )
            continue

        target_cache_key = _catalog_target_key(target)
        cached_grouped = cached_catalogs.get(target_cache_key)
        if cached_grouped is None:
            cached_grouped = _cached_fms_by_subsystem(environment, target)
        direct_subsystem = target.synthetic_subsystem or target.source_name

        if missing_catalog_pairs and typed_targets_supplied and requested_target_map:
            logger.info(
                "log.plan.catalog_miss_auto_refresh environment=%s machine=%s category=%s root=%s missing_pairs=%s",
                environment.id, target.machine.id, target.source_category, target.root,
                sorted(missing_catalog_pairs),
            )
            try:
                cached_grouped = _refresh_catalog_target_for_query(
                    environment, target, operation_id=operation_id,
                )
                cached_catalogs[target_cache_key] = cached_grouped
                refreshed_pairs = {_fold_pair(subsystem, fm) for subsystem, fm in _catalog_pairs(cached_grouped)}
                missing_catalog_pairs = {pair for pair in missing_catalog_pairs if _fold_pair(*pair) not in refreshed_pairs}
            except LogSearchCancelled:
                raise
            except Exception as exc:
                # Keep the previous catalog as a degraded fallback. A refresh
                # failure must not break queries that the old snapshot can serve.
                logger.exception(
                    "log.plan.catalog_auto_refresh.failed environment=%s machine=%s category=%s root=%s error=%s",
                    environment.id, target.machine.id, target.source_category, target.root, exc,
                )

        if cached_grouped:
            candidate_subsystems = _matching_catalog_subsystems(cached_grouped, requested_subsystems)
            if not candidate_subsystems:
                logger.info(
                    "log.plan.target.skip_no_catalog_match machine=%s category=%s root=%s requested=%s",
                    target.machine.id,
                    target.source_category,
                    target.root,
                    sorted(requested_subsystems),
                )
                continue

            with ssh_session(target.machine) as lease:
                for subsystem in sorted(candidate_subsystems):
                    candidate_fms = _requested_fms_for_catalog_subsystem(
                        cached_grouped,
                        subsystem,
                        requested_target_map=requested_target_map,
                        requested_fms=requested_fms,
                    )
                    if not candidate_fms:
                        continue
                    directory = (
                        target.root
                        if subsystem == direct_subsystem
                        else f"{target.root.rstrip('/')}/{subsystem}"
                    )
                    logger.info(
                        "log.plan.target.indexed machine=%s host=%s user=%s category=%s subsystem=%s fms=%s directory=%s",
                        target.machine.id,
                        target.machine.host,
                        target.machine.username,
                        target.source_category,
                        subsystem,
                        sorted(candidate_fms),
                        directory,
                    )
                    if operation_id:
                        LogSearchProgressStore.directory_started(
                            operation_id, directory=directory, subsystem=subsystem, modules=candidate_fms,
                            action="正在读取子系统日志目录",
                        )
                    artifacts.extend(
                        log_file_index.collect_artifacts(
                            environment=environment,
                            target=target,
                            lease=lease,
                            directory=directory,
                            subsystem=subsystem,
                            fms=candidate_fms,
                            reference=reference,
                            window_start=reference or datetime.min,
                            window_end=window_end or reference or datetime.max,
                            operation_id=operation_id,
                        )
                    )
            continue

        # No environment resource catalog yet: retain the old single-root fallback.
        logger.info(
            "log.plan.target.catalog_missing machine=%s category=%s root=%s fallback=single_root_listing",
            target.machine.id,
            target.source_category,
            target.root,
        )
        with ssh_session(target.machine) as lease:
            if operation_id:
                LogSearchProgressStore.directory_started(
                    operation_id, directory=target.root, subsystem="", modules=requested_fms,
                    action="正在读取日志根目录",
                )
            artifacts.extend(
                _collect_target_artifacts_fallback(
                    environment=environment, target=target, lease=lease,
                    subsystem_filter=requested_subsystems, fm_filter=requested_fms,
                    reference=reference, window_end=window_end, operation_id=operation_id,
                )
            )

    logger.info(
        "log.plan.collect.finish environment=%s artifacts=%d strategy=fingerprint_index",
        environment.id,
        len(artifacts),
    )
    return artifacts


def _enabled_profile_rules(categories: set[str] | None = None) -> dict[str, set[str]]:
    profiles = ResourceSettings.get_solo().log_path_profiles.filter(enabled=True)
    if categories:
        profiles = profiles.filter(category__in=categories)
    return {profile.category: set(profile.match_rules or []) for profile in profiles}


def _missing_dhh_fallback_request(
    *,
    primary_selected: list[LogArtifact],
    source_categories: set[str],
    fm_targets: list[dict[str, str]],
) -> tuple[set[str], list[dict[str, str]]]:
    """Return only categories/targets not satisfied by upper-machine logs.

    Debug logs use category-level upper-machine priority: if any requested
    debug artifact exists on the upper machine, DHH debug is not queried at
    all. Executor/run logs retain the existing per-module fallback behavior.
    """
    rules_by_category = _enabled_profile_rules(source_categories or None)
    selected_by_category: dict[str, set[tuple[str, str]]] = {}
    for artifact in primary_selected:
        selected_by_category.setdefault(artifact.source_category, set()).add(_fold_pair(artifact.subsystem, artifact.fm))

    fallback_categories: set[str] = set()
    fallback_targets: list[dict[str, str]] = []
    seen_targets: set[tuple[str, str, str]] = set()
    for category, rules in rules_by_category.items():
        if category not in _DHH_LOG_CATEGORIES:
            continue

        # DHH debug logs use strict upper-machine priority: once the upper
        # machine has any debug artifact for this query window, do not touch
        # the DHH debug mirror at all. Only a completely empty upper-machine
        # debug result is allowed to fall back to DHH. This is intentionally
        # category-level rather than per-module to avoid duplicate/partial
        # reads from both physical sources.
        if category == LogPathCategory.DEBUG:
            if selected_by_category.get(category):
                logger.info(
                    "log.plan.dhh.debug_fallback.skip reason=upper_has_debug_artifact selected=%d",
                    len(selected_by_category[category]),
                )
                continue
            fallback_categories.add(category)
            expected_kind = LogModuleKind.NORMAL
            applicable = [
                item for item in fm_targets
                if str(item.get("kind", LogModuleKind.NORMAL) or LogModuleKind.NORMAL) == expected_kind
            ]
            for item in applicable:
                subsystem = str(item.get("subsystem", "")).strip()
                fm = str(item.get("fm", "")).strip()
                if not subsystem or not fm:
                    continue
                key = (subsystem, fm, expected_kind)
                if key in seen_targets:
                    continue
                seen_targets.add(key)
                fallback_targets.append({"subsystem": subsystem, "fm": fm, "kind": expected_kind})
            logger.info(
                "log.plan.dhh.debug_fallback.enable reason=upper_debug_empty targets=%d",
                len(applicable),
            )
            continue

        if "run_flat" in rules:
            if not selected_by_category.get(category):
                fallback_categories.add(category)
            continue

        expected_kind = LogModuleKind.EXECUTOR if "executor_tree" in rules else LogModuleKind.NORMAL
        applicable = [
            item for item in fm_targets
            if str(item.get("kind", LogModuleKind.NORMAL) or LogModuleKind.NORMAL) == expected_kind
        ]
        if not applicable:
            continue
        found = selected_by_category.get(category, set())
        missing_for_category = []
        for item in applicable:
            subsystem = str(item.get("subsystem", "")).strip()
            fm = str(item.get("fm", "")).strip()
            if not subsystem or not fm or _fold_pair(subsystem, fm) in found:
                continue
            key = (subsystem, fm, expected_kind)
            if key in seen_targets:
                continue
            seen_targets.add(key)
            missing_for_category.append({"subsystem": subsystem, "fm": fm, "kind": expected_kind})
        if missing_for_category:
            fallback_categories.add(category)
            fallback_targets.extend(missing_for_category)
    return fallback_categories, fallback_targets


def build_log_plan(
    environment: Environment,
    start: datetime,
    end: datetime,
    subsystems=None,
    fms=None,
    source_categories=None,
    *,
    fm_targets=None,
    operation_id: str = "",
) -> list[LogArtifact]:
    if operation_id:
        LogSearchProgressStore.raise_if_cancelled(operation_id)

    requested_categories = set(source_categories or [])
    requested_targets = list(fm_targets or [])
    dhh = _dhh_machine(environment)

    if dhh is None:
        artifacts = collect_artifacts(
            environment,
            set(subsystems or []),
            set(fms or []),
            reference=start,
            window_end=end,
            source_categories=requested_categories,
            fm_targets=requested_targets,
            operation_id=operation_id,
        )
        selected = select_artifacts_for_window(artifacts, start, end)
    else:
        # DHH environments prefer the ordinary upper-machine path. DEBUG uses
        # category-level fallback (any upper debug hit suppresses DHH debug);
        # EXECUTOR/RUN retain per-module fallback for missing candidates.
        primary_artifacts = collect_artifacts(
            environment,
            set(subsystems or []),
            set(fms or []),
            reference=start,
            window_end=end,
            source_categories=requested_categories,
            fm_targets=requested_targets,
            operation_id=operation_id,
            routing=_LOG_ROOT_ROUTING_UPPER_FIRST,
        )
        selected = select_artifacts_for_window(primary_artifacts, start, end)
        logger.info(
            "log.plan.dhh.upper_first environment=%s upper=%s dhh=%s selected=%d small_network=%s",
            environment.id, environment.upper_machine.host, dhh.host, len(selected), _is_small_network_host(dhh.host),
        )

        if _is_small_network_host(dhh.host):
            logger.info(
                "log.plan.dhh.fallback.skip environment=%s dhh=%s reason=small_network",
                environment.id, dhh.host,
            )
        else:
            fallback_categories, fallback_targets = _missing_dhh_fallback_request(
                primary_selected=selected,
                source_categories=requested_categories,
                fm_targets=requested_targets,
            )
            if fallback_categories:
                fallback_subsystems = {str(item.get("subsystem", "")).strip() for item in fallback_targets if str(item.get("subsystem", "")).strip()}
                fallback_fms = {str(item.get("fm", "")).strip() for item in fallback_targets if str(item.get("fm", "")).strip()}
                logger.info(
                    "log.plan.dhh.fallback.start environment=%s dhh=%s categories=%s missing_targets=%s",
                    environment.id, dhh.host, sorted(fallback_categories),
                    [(item.get("subsystem"), item.get("fm"), item.get("kind")) for item in fallback_targets],
                )
                fallback_artifacts = collect_artifacts(
                    environment,
                    fallback_subsystems,
                    fallback_fms,
                    reference=start,
                    window_end=end,
                    source_categories=fallback_categories,
                    fm_targets=fallback_targets,
                    operation_id=operation_id,
                    routing=_LOG_ROOT_ROUTING_DHH_FALLBACK,
                )
                fallback_selected = select_artifacts_for_window(fallback_artifacts, start, end)
                selected.extend(fallback_selected)
                logger.info(
                    "log.plan.dhh.fallback.finish environment=%s selected=%d total=%d",
                    environment.id, len(fallback_selected), len(selected),
                )
            else:
                logger.info(
                    "log.plan.dhh.fallback.skip environment=%s dhh=%s reason=upper_has_requested_logs",
                    environment.id, dhh.host,
                )

    selected = sorted(
        {item.identity: item for item in selected}.values(),
        key=lambda item: (
            item.machine_name,
            item.subsystem,
            item.fm,
            item.start_time or item.boundary_time or datetime.max,
        ),
    )
    if operation_id:
        LogSearchProgressStore.raise_if_cancelled(operation_id)
        LogSearchProgressStore.plan_ready(operation_id, artifact_total=len(selected))
    logger.info(
        "log.plan.selected environment=%s start=%s end=%s selected=%d dhh_strategy=%s",
        environment.id,
        start.isoformat(),
        end.isoformat(),
        len(selected),
        "upper_then_dhh" if dhh is not None else "normal",
    )
    for item in selected:
        logger.info(
            "log.plan.artifact machine=%s category=%s subsystem=%s fm=%s kind=%s boundary=%s path=%s member=%s",
            item.machine_id,
            item.source_category,
            item.subsystem,
            item.fm,
            item.kind,
            item.boundary_time,
            item.path,
            item.member_name,
        )
    return selected



_LIVE_LOG_INTERVAL_SECONDS = 1
_LIVE_LOG_MAX_READ_BYTES = 4 * 1024 * 1024
_LIVE_LOG_MAX_PARTIAL_BYTES = 512 * 1024


def _live_artifact_from_path(target: LogRootTarget, *, subsystem: str, fm: str, path: str, size: int = 0) -> LogArtifact:
    return LogArtifact(
        target.machine.id,
        target.machine.name,
        subsystem,
        fm,
        path,
        "current",
        None,
        size=max(0, int(size or 0)),
        source_category=target.source_category,
        source_name=target.source_name,
    )


def _live_requested_maps(
    environment: Environment,
    target: LogRootTarget,
    *,
    subsystems: set[str],
    fms: set[str],
    fm_targets: list[dict[str, str]],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    normal: dict[str, set[str]] = {}
    executor: dict[str, set[str]] = {}
    cached = _cached_fms_by_subsystem(environment, target)

    def physical_pair(requested_subsystem: str, requested_fm: str) -> tuple[str, str]:
        """Map the global/UI spelling back to the physical Linux spelling.

        The global catalog merges names case-insensitively. A UI target such as
        SPWSP must therefore still resolve a physical ``spwsp.log`` and the
        corresponding lowercase subsystem directory during live monitoring.
        """
        subsystem_fold = requested_subsystem.casefold()
        fm_fold = requested_fm.casefold()
        for actual_subsystem, actual_fms in cached.items():
            if str(actual_subsystem).strip().casefold() != subsystem_fold:
                continue
            for actual_fm in actual_fms:
                if str(actual_fm).strip().casefold() == fm_fold:
                    return str(actual_subsystem), str(actual_fm)
        return requested_subsystem, requested_fm

    for item in fm_targets:
        subsystem = str(item.get("subsystem", "")).strip()
        fm = str(item.get("fm", "")).strip()
        kind = str(item.get("kind", LogModuleKind.NORMAL)).strip() or LogModuleKind.NORMAL
        if not subsystem or not fm:
            continue
        subsystem, fm = physical_pair(subsystem, fm)
        (executor if kind == LogModuleKind.EXECUTOR else normal).setdefault(subsystem, set()).add(fm)
    if normal or executor:
        return normal, executor

    if cached:
        for subsystem, catalog_fms in cached.items():
            if subsystems and subsystem not in subsystems:
                continue
            selected = set(catalog_fms)
            if fms:
                selected.intersection_update(fms)
            if selected:
                normal[subsystem] = selected
        return normal, executor

    # Legacy environments can have no resource catalog yet. Preserve the old
    # request shape as a final fallback without inventing extra modules.
    for subsystem in sorted(subsystems):
        if fms:
            normal[subsystem] = set(fms)
    return normal, executor


def _discover_live_current_artifacts_for_targets(
    environment: Environment,
    targets: list[LogRootTarget],
    *,
    subsystems: set[str],
    fms: set[str],
    fm_targets: list[dict[str, str]],
) -> list[LogArtifact]:
    artifacts: list[LogArtifact] = []
    now = datetime.now()
    for target in targets:
        rules = set(target.match_rules or ())
        normal_map, executor_map = _live_requested_maps(
            environment,
            target,
            subsystems=subsystems,
            fms=fms,
            fm_targets=fm_targets,
        )
        with ssh_session(target.machine) as lease:
            if "run_flat" in rules:
                # Live monitoring is module-scoped. event.log is a flat, shared
                # runtime log and must not be attached when the user selected a
                # concrete module for real-time monitoring.
                if fm_targets:
                    continue
                path = f"{target.root.rstrip('/')}/event.log"
                try:
                    attr = lease.sftp.stat(path)
                except OSError:
                    continue
                if stat.S_ISREG(attr.st_mode):
                    artifacts.append(_live_artifact_from_path(
                        target, subsystem="运行日志", fm="event", path=path, size=attr.st_size,
                    ))
                continue

            if "executor_tree" in rules:
                if not executor_map:
                    continue
                lower_hosts = sorted({str(machine.host or "").strip() for machine in _executor_lower_machines(environment) if str(machine.host or "").strip()})
                for lower_host in lower_hosts:
                    for subsystem, modules in sorted(executor_map.items()):
                        directory = f"{target.root.rstrip('/')}/{lower_host}/{subsystem}"
                        try:
                            items = lease.sftp.listdir_attr(directory)
                        except OSError:
                            continue
                        for item in items:
                            if not stat.S_ISREG(item.st_mode) or not item.filename.endswith(".log"):
                                continue
                            module = _executor_module_name(item.filename, now)
                            requested_module = next((value for value in modules if value.casefold() == str(module or "").casefold()), None)
                            if requested_module is None:
                                continue
                            parsed = parse_log_name(item.filename, now)
                            if parsed and parsed[1] is not None:
                                # timestamped executor .log is a rotated archive
                                continue
                            artifacts.append(_live_artifact_from_path(
                                target,
                                subsystem=subsystem,
                                fm=requested_module,
                                path=f"{directory}/{item.filename}",
                                size=item.st_size,
                            ))
                continue

            direct_subsystem = target.synthetic_subsystem or target.source_name
            for subsystem, modules in sorted(normal_map.items()):
                directory = target.root if subsystem == direct_subsystem else f"{target.root.rstrip('/')}/{subsystem}"
                try:
                    directory_items = lease.sftp.listdir_attr(directory)
                except OSError:
                    continue
                current_by_fold = {
                    item.filename.casefold(): item
                    for item in directory_items
                    if stat.S_ISREG(item.st_mode) and item.filename.lower().endswith(".log")
                }
                for fm in sorted(modules):
                    item = current_by_fold.get(f"{fm}.log".casefold())
                    if item is None:
                        continue
                    path = f"{directory.rstrip('/')}/{item.filename}"
                    artifacts.append(_live_artifact_from_path(
                        target, subsystem=subsystem, fm=fm, path=path, size=item.st_size,
                    ))
    return artifacts


def build_live_log_plan(
    environment: Environment,
    subsystems=None,
    fms=None,
    source_categories=None,
    *,
    fm_targets=None,
) -> list[LogArtifact]:
    """Resolve only appendable *current* files for real-time monitoring.

    This intentionally does not reuse a historical query window. The user may
    enable live monitoring after inspecting an older range, but the live stream
    must always attach to the current file for the same selected modules.
    """
    requested_categories = set(source_categories or [])
    requested_subsystems = set(subsystems or [])
    requested_fms = set(fms or [])
    requested_targets = list(fm_targets or [])
    dhh = _dhh_machine(environment)
    routing = _LOG_ROOT_ROUTING_UPPER_FIRST if dhh is not None else _LOG_ROOT_ROUTING_CATALOG
    primary_targets = expand_log_roots(environment, requested_categories, routing=routing)
    primary = _discover_live_current_artifacts_for_targets(
        environment,
        primary_targets,
        subsystems=requested_subsystems,
        fms=requested_fms,
        fm_targets=requested_targets,
    )

    if dhh is not None and not _is_small_network_host(dhh.host):
        fallback_categories, fallback_targets = _missing_dhh_fallback_request(
            primary_selected=primary,
            source_categories=requested_categories,
            fm_targets=requested_targets,
        )
        if fallback_categories:
            fallback_subsystems = {
                str(item.get("subsystem", "")).strip()
                for item in fallback_targets
                if str(item.get("subsystem", "")).strip()
            } or requested_subsystems
            fallback_fms = {
                str(item.get("fm", "")).strip()
                for item in fallback_targets
                if str(item.get("fm", "")).strip()
            } or requested_fms
            fallback_roots = expand_log_roots(
                environment, fallback_categories, routing=_LOG_ROOT_ROUTING_DHH_FALLBACK,
            )
            primary.extend(_discover_live_current_artifacts_for_targets(
                environment,
                fallback_roots,
                subsystems=fallback_subsystems,
                fms=fallback_fms,
                fm_targets=fallback_targets,
            ))

    deduped = {item.identity: item for item in primary}
    plan = sorted(deduped.values(), key=lambda item: (item.machine_name, item.source_category, item.subsystem, item.fm, item.path))
    logger.info(
        "log.live.plan environment=%s artifacts=%d categories=%s targets=%s",
        environment.id,
        len(plan),
        sorted(requested_categories),
        [(item.get("subsystem"), item.get("fm"), item.get("kind")) for item in requested_targets],
    )
    return plan


def _live_marker_block(artifact: LogArtifact) -> bytes:
    return (
        f"__TRACELENS_LOG_CATEGORY__={artifact.source_category}\n"
        f"__TRACELENS_LOG_SUBSYSTEM__={artifact.subsystem}\n"
        f"__TRACELENS_LOG_MODULE__={artifact.fm}\n"
        f"__TRACELENS_LOG_SOURCE_PATH__={artifact.path}\n"
    ).encode("utf-8")


def _stream_live_log_events_polling(
    environment: Environment,
    plan: list[LogArtifact],
    *,
    interval_seconds: int = _LIVE_LOG_INTERVAL_SECONDS,
) -> Iterator[dict]:
    """Compatibility fallback for hosts where a persistent remote tail is unavailable.

    This path performs SFTP offset polling and is intentionally not used during
    normal real-time monitoring.
    """
    interval = max(1, int(interval_seconds or _LIVE_LOG_INTERVAL_SECONDS))
    machine_map = {machine.id: machine for machine in environment_machines(environment)}
    cursors: dict[str, dict] = {}
    process_logger.info(
        "[LIVE] start environment=%s interval=%ss planned_files=%s",
        environment.id, interval, len(plan),
    )

    # Start exactly at EOF: enabling real-time mode never replays the already
    # loaded query result.
    for artifact in plan:
        machine = machine_map.get(artifact.machine_id)
        if machine is None:
            continue
        try:
            with ssh_session(machine) as lease:
                attr = lease.sftp.stat(artifact.path)
        except Exception as exc:
            logger.warning("log.live.stat_initial_failed path=%s error=%s", artifact.path, exc)
            continue
        cursors[artifact.identity] = {
            "artifact": artifact,
            "offset": max(0, int(attr.st_size or 0)),
            "inode": getattr(attr, "st_ino", None),
            "mtime": getattr(attr, "st_mtime", None),
            "carry": b"",
        }

    if not cursors:
        process_logger.error("[LIVE] environment=%s no current log file could be opened", environment.id)
        yield {"type": "error", "message": "实时监听未能打开任何 current 日志文件。"}
        return

    for state in cursors.values():
        artifact = state["artifact"]
        process_logger.info(
            "[LIVE] ready environment=%s machine=%s subsystem=%s module=%s path=%s offset=%s",
            environment.id, artifact.machine_id, artifact.subsystem, artifact.fm, artifact.path, state["offset"],
        )

    yield {
        "type": "ready",
        "interval_seconds": interval,
        "artifacts": [
            {
                "machine_id": state["artifact"].machine_id,
                "source_category": state["artifact"].source_category,
                "subsystem": state["artifact"].subsystem,
                "fm": state["artifact"].fm,
                "path": state["artifact"].path,
            }
            for state in cursors.values()
        ],
    }

    while True:
        time.sleep(interval)
        chunks: list[dict] = []
        total_bytes = 0
        total_lines = 0
        for state in list(cursors.values()):
            artifact: LogArtifact = state["artifact"]
            machine = machine_map.get(artifact.machine_id)
            if machine is None:
                continue
            try:
                with ssh_session(machine) as lease:
                    attr = lease.sftp.stat(artifact.path)
                    size = max(0, int(attr.st_size or 0))
                    inode = getattr(attr, "st_ino", None)
                    replaced = bool(state["inode"] is not None and inode is not None and inode != state["inode"])
                    if replaced or size < int(state["offset"]):
                        logger.info(
                            "log.live.rotate path=%s old_offset=%s new_size=%s inode_changed=%s",
                            artifact.path, state["offset"], size, replaced,
                        )
                        state["offset"] = 0
                        state["carry"] = b""
                    state["inode"] = inode
                    state["mtime"] = getattr(attr, "st_mtime", None)
                    offset = int(state["offset"])
                    if size <= offset:
                        continue
                    read_size = min(size - offset, _LIVE_LOG_MAX_READ_BYTES)
                    with lease.sftp.open(artifact.path, "rb") as handle:
                        handle.seek(offset)
                        appended = handle.read(read_size) or b""
                    if isinstance(appended, str):
                        appended = appended.encode("utf-8", errors="replace")
                    state["offset"] = offset + len(appended)
            except Exception as exc:
                logger.warning("log.live.read_failed path=%s error=%s", artifact.path, exc)
                continue

            if not appended:
                continue
            combined = bytes(state["carry"]) + bytes(appended)
            last_newline = combined.rfind(b"\n")
            if last_newline < 0:
                state["carry"] = combined[-_LIVE_LOG_MAX_PARTIAL_BYTES:]
                continue
            complete = combined[: last_newline + 1]
            state["carry"] = combined[last_newline + 1:]
            if len(state["carry"]) > _LIVE_LOG_MAX_PARTIAL_BYTES:
                state["carry"] = state["carry"][-_LIVE_LOG_MAX_PARTIAL_BYTES:]
            text = (_live_marker_block(artifact) + complete).decode("utf-8", errors="replace")
            line_count = complete.count(b"\n")
            total_bytes += len(complete)
            total_lines += line_count
            chunks.append({
                "source_category": artifact.source_category,
                "subsystem": artifact.subsystem,
                "fm": artifact.fm,
                "path": artifact.path,
                "line_count": line_count,
                "text": text,
            })

        if chunks:
            process_logger.info(
                "[LIVE] append environment=%s files=%s lines=%s bytes=%s modules=%s",
                environment.id, len(chunks), total_lines, total_bytes,
                ",".join(sorted({str(item.get("fm") or "-") for item in chunks})),
            )
            yield {
                "type": "logs",
                "chunks": chunks,
                "bytes": total_bytes,
                "lines": total_lines,
                "server_time": timezone.now().isoformat(),
            }
        else:
            # Explicit heartbeat keeps Vite/nginx/proxies from buffering or
            # timing out an otherwise quiet long-lived stream.
            yield {"type": "heartbeat", "server_time": timezone.now().isoformat()}


class _LiveTailUnavailable(RuntimeError):
    """Raised when the remote host cannot provide the persistent tail capability."""


_LIVE_LOG_HEARTBEAT_SECONDS = 10.0
_LIVE_LOG_CHANNEL_IDLE_SLEEP_SECONDS = 0.08
_LIVE_LOG_CHANNEL_READ_BYTES = 256 * 1024
_LIVE_LOG_RECONNECT_BASE_SECONDS = 1.0
_LIVE_LOG_RECONNECT_MAX_SECONDS = 30.0


def _live_machine_map(environment: Environment) -> dict[int, object]:
    """Return the same effective machines used by log-root resolution.

    Lower-machine log access can use ``metadata.topology_ip`` instead of the
    persisted SSH identity. Reconstruct that transient host override here so a
    live tail connects to exactly the same endpoint as planning/search.
    """
    result: dict[int, object] = {environment.upper_machine.id: environment.upper_machine}
    for relation in _active_topology_relations(environment):
        machine = _log_machine_for_relation(relation)
        if machine.id:
            result[machine.id] = machine
    return result


def _live_shell_path(path: str) -> str:
    value = str(path or "").strip()
    if value.startswith("~/"):
        return '"$HOME"/' + shlex.quote(value[2:])
    return shlex.quote(value)


def _drain_live_tail_stderr(channel, *, max_bytes: int = 64 * 1024) -> str:
    chunks: list[bytes] = []
    received = 0
    try:
        while channel.recv_stderr_ready() and received < max_bytes:
            part = channel.recv_stderr(min(8192, max_bytes - received))
            if not part:
                break
            chunks.append(part)
            received += len(part)
    except Exception:
        return ""
    return b"".join(chunks).decode("utf-8", errors="replace").strip()


def _close_live_tail_runtime(clients: dict[int, object], states: list[dict]) -> None:
    for state in states:
        try:
            state["channel"].close()
        except Exception:
            pass
    for client in clients.values():
        try:
            client.close()
        except Exception:
            pass


def _open_live_tail_runtime(
    environment: Environment,
    plan: list[LogArtifact],
) -> tuple[dict[int, object], list[dict]]:
    """Open one persistent SSH transport per machine and one tail channel per file."""
    machine_map = _live_machine_map(environment)
    grouped: dict[int, list[LogArtifact]] = {}
    for artifact in plan:
        if artifact.machine_id in machine_map:
            grouped.setdefault(artifact.machine_id, []).append(artifact)

    if not grouped:
        raise SshOperationError("实时监听没有找到可连接的日志主机。")

    clients: dict[int, object] = {}
    states: list[dict] = []
    try:
        for machine_id, artifacts in grouped.items():
            machine = machine_map[machine_id]
            client = build_ssh_client(machine)
            clients[machine_id] = client
            for artifact in artifacts:
                # ``-F`` follows the file name and retries after rotation/recreate.
                # ``-n 0`` starts exactly at the subscribe point and does not
                # replay logs that are already rendered in the browser.
                command = f"LC_ALL=C exec tail -n 0 -F -- {_live_shell_path(artifact.path)}"
                _, stdout, stderr = client.exec_command(command, get_pty=False)
                channel = stdout.channel
                states.append({
                    "artifact": artifact,
                    "machine": machine,
                    "stdout": stdout,
                    "stderr": stderr,
                    "channel": channel,
                    "carry": b"",
                    "stderr_last": "",
                })
                process_logger.info(
                    "[LIVE] tail.open environment=%s machine=%s subsystem=%s module=%s path=%s",
                    environment.id, artifact.machine_id, artifact.subsystem, artifact.fm, artifact.path,
                )

        # Give shells a very small window to reject an unsupported tail command.
        time.sleep(0.08)
        for state in states:
            channel = state["channel"]
            if not channel.exit_status_ready():
                continue
            status = channel.recv_exit_status()
            stderr_text = _drain_live_tail_stderr(channel)
            if status == 0:
                continue
            lowered = stderr_text.casefold()
            if status == 127 or "not found" in lowered or "unrecognized option" in lowered or "invalid option" in lowered:
                raise _LiveTailUnavailable(stderr_text or f"tail exited with status {status}")
            raise SshOperationError(stderr_text or f"远端 tail 启动失败，退出码 {status}。")
        return clients, states
    except Exception:
        _close_live_tail_runtime(clients, states)
        raise


def _complete_live_tail_chunk(state: dict, appended: bytes) -> dict | None:
    artifact: LogArtifact = state["artifact"]
    combined = bytes(state.get("carry") or b"") + bytes(appended or b"")
    last_newline = combined.rfind(b"\n")
    if last_newline < 0:
        state["carry"] = combined[-_LIVE_LOG_MAX_PARTIAL_BYTES:]
        return None
    complete = combined[: last_newline + 1]
    state["carry"] = combined[last_newline + 1:]
    if len(state["carry"]) > _LIVE_LOG_MAX_PARTIAL_BYTES:
        state["carry"] = state["carry"][-_LIVE_LOG_MAX_PARTIAL_BYTES:]
    if not complete:
        return None
    line_count = complete.count(b"\n")
    return {
        "source_category": artifact.source_category,
        "subsystem": artifact.subsystem,
        "fm": artifact.fm,
        "path": artifact.path,
        "line_count": line_count,
        "text": (_live_marker_block(artifact) + complete).decode("utf-8", errors="replace"),
        "_bytes": len(complete),
    }


def _stream_live_log_events_tail(environment: Environment, plan: list[LogArtifact]) -> Iterator[dict]:
    """Stream remote appends over persistent SSH ``tail -F`` channels.

    There is no repeated SFTP stat/read loop on the normal path. One dedicated
    SSH transport remains open for the lifetime of the browser SSE subscription;
    Paramiko only reads bytes that the remote ``tail -F`` process pushes.
    """
    reconnect_attempt = 0
    ready_sent = False
    while True:
        clients: dict[int, object] = {}
        states: list[dict] = []
        try:
            clients, states = _open_live_tail_runtime(environment, plan)
            reconnect_attempt = 0
            if not ready_sent:
                ready_sent = True
                process_logger.info(
                    "[LIVE] ready environment=%s transport=ssh_tail files=%s machines=%s",
                    environment.id, len(states), len(clients),
                )
                yield {
                    "type": "ready",
                    "transport": "ssh_tail",
                    "interval_seconds": 0,
                    "heartbeat_seconds": int(_LIVE_LOG_HEARTBEAT_SECONDS),
                    "artifacts": [
                        {
                            "machine_id": state["artifact"].machine_id,
                            "source_category": state["artifact"].source_category,
                            "subsystem": state["artifact"].subsystem,
                            "fm": state["artifact"].fm,
                            "path": state["artifact"].path,
                        }
                        for state in states
                    ],
                }
            else:
                process_logger.info("[LIVE] reconnected environment=%s transport=ssh_tail", environment.id)
                yield {"type": "reconnected", "transport": "ssh_tail", "server_time": timezone.now().isoformat()}

            last_heartbeat = time.monotonic()
            while True:
                chunks: list[dict] = []
                total_bytes = 0
                total_lines = 0
                dead_reason = ""

                for state in states:
                    channel = state["channel"]
                    stderr_text = _drain_live_tail_stderr(channel)
                    if stderr_text and stderr_text != state.get("stderr_last"):
                        state["stderr_last"] = stderr_text
                        # File-rotation/retry notices are useful in backend logs
                        # but are not user-facing failures; ``tail -F`` handles them.
                        logger.info("log.live.tail.stderr path=%s message=%s", state["artifact"].path, stderr_text[:500])

                    appended_parts: list[bytes] = []
                    received = 0
                    try:
                        while channel.recv_ready() and received < _LIVE_LOG_MAX_READ_BYTES:
                            part = channel.recv(min(_LIVE_LOG_CHANNEL_READ_BYTES, _LIVE_LOG_MAX_READ_BYTES - received))
                            if not part:
                                break
                            appended_parts.append(part)
                            received += len(part)
                    except Exception as exc:
                        dead_reason = f"SSH tail 读取失败：{exc}"
                        break

                    if appended_parts:
                        chunk = _complete_live_tail_chunk(state, b"".join(appended_parts))
                        if chunk:
                            total_bytes += int(chunk.pop("_bytes", 0))
                            total_lines += int(chunk.get("line_count") or 0)
                            chunks.append(chunk)

                    if channel.exit_status_ready() and not channel.recv_ready():
                        try:
                            status = channel.recv_exit_status()
                        except Exception:
                            status = -1
                        dead_reason = _drain_live_tail_stderr(channel) or f"远端 tail 已结束（退出码 {status}）"
                        break

                if dead_reason:
                    raise SshOperationError(dead_reason)

                if chunks:
                    process_logger.info(
                        "[LIVE] append environment=%s transport=ssh_tail files=%s lines=%s bytes=%s modules=%s",
                        environment.id, len(chunks), total_lines, total_bytes,
                        ",".join(sorted({str(item.get("fm") or "-") for item in chunks})),
                    )
                    yield {
                        "type": "logs",
                        "chunks": chunks,
                        "bytes": total_bytes,
                        "lines": total_lines,
                        "server_time": timezone.now().isoformat(),
                    }
                    last_heartbeat = time.monotonic()
                    continue

                now = time.monotonic()
                if now - last_heartbeat >= _LIVE_LOG_HEARTBEAT_SECONDS:
                    yield {"type": "heartbeat", "server_time": timezone.now().isoformat()}
                    last_heartbeat = now
                time.sleep(_LIVE_LOG_CHANNEL_IDLE_SLEEP_SECONDS)
        except GeneratorExit:
            raise
        except _LiveTailUnavailable:
            raise
        except Exception as exc:
            reconnect_attempt += 1
            delay = min(
                _LIVE_LOG_RECONNECT_MAX_SECONDS,
                _LIVE_LOG_RECONNECT_BASE_SECONDS * (2 ** min(reconnect_attempt - 1, 5)),
            )
            process_logger.warning(
                "[LIVE] reconnect environment=%s attempt=%s delay=%.1fs error=%s",
                environment.id, reconnect_attempt, delay, exc,
            )
            yield {
                "type": "reconnecting",
                "attempt": reconnect_attempt,
                "retry_in_seconds": delay,
                "message": str(exc),
                "server_time": timezone.now().isoformat(),
            }
            time.sleep(delay)
        finally:
            _close_live_tail_runtime(clients, states)


def stream_live_log_events(
    environment: Environment,
    plan: list[LogArtifact],
    *,
    interval_seconds: int = _LIVE_LOG_INTERVAL_SECONDS,
) -> Iterator[dict]:
    """Use persistent SSH push first; retain SFTP polling only as a compatibility fallback."""
    process_logger.info(
        "[LIVE] start environment=%s transport=ssh_tail planned_files=%s",
        environment.id, len(plan),
    )
    try:
        yield from _stream_live_log_events_tail(environment, plan)
    except _LiveTailUnavailable as exc:
        process_logger.warning(
            "[LIVE] fallback environment=%s transport=sftp_poll reason=%s",
            environment.id, exc,
        )
        yield {
            "type": "fallback",
            "transport": "sftp_poll",
            "message": "远端 tail -F 不可用，已自动切换兼容监听模式。",
            "server_time": timezone.now().isoformat(),
        }
        yield from _stream_live_log_events_polling(
            environment,
            plan,
            interval_seconds=interval_seconds,
        )

_DIRECT_BINARY_SEEK_MIN_BYTES = 8 * 1024 * 1024
_DIRECT_BINARY_SEEK_MAX_PROBES = 28
_DIRECT_BINARY_SEEK_SCAN_LINES = 64
_DIRECT_BINARY_SEEK_SAFETY_BYTES = 128 * 1024
_DIRECT_BINARY_SEEK_MAX_LINE_BYTES = 512 * 1024


def _probe_timestamp_after_offset(handle, offset: int, size: int) -> tuple[int, datetime] | None:
    """Find the next timestamped complete line at/after a byte offset."""
    if size <= 0:
        return None
    offset = max(0, min(int(offset), max(0, size - 1)))
    handle.seek(offset)
    if offset > 0:
        handle.readline(_DIRECT_BINARY_SEEK_MAX_LINE_BYTES)
    for _ in range(_DIRECT_BINARY_SEEK_SCAN_LINES):
        line_offset = int(handle.tell())
        line = handle.readline(_DIRECT_BINARY_SEEK_MAX_LINE_BYTES)
        if not line:
            return None
        timestamp = parse_line_time(line)
        if timestamp is not None:
            return line_offset, timestamp
    return None


def _find_direct_time_offset(handle, size: int, target: datetime) -> int | None:
    """Approximate a byte position immediately before target by timestamp bisection."""
    if size < _DIRECT_BINARY_SEEK_MIN_BYTES:
        return None
    low = 0
    high = size
    best_before = 0
    successful_probes = 0
    for _ in range(_DIRECT_BINARY_SEEK_MAX_PROBES):
        if high - low <= _DIRECT_BINARY_SEEK_SAFETY_BYTES:
            break
        midpoint = low + (high - low) // 2
        probe = _probe_timestamp_after_offset(handle, midpoint, size)
        if probe is None:
            high = midpoint
            continue
        line_offset, timestamp = probe
        successful_probes += 1
        if timestamp < target:
            best_before = max(best_before, line_offset)
            low = max(midpoint + 1, line_offset + 1)
        else:
            high = min(high, line_offset)
    if successful_probes == 0:
        return None
    return max(0, best_before - _DIRECT_BINARY_SEEK_SAFETY_BYTES)


def _stream_direct_forward_from_offset(handle, *, offset: int, start: datetime, end: datetime, operation_id: str) -> Iterator[bytes]:
    handle.seek(max(0, offset))
    if offset > 0:
        handle.readline(_DIRECT_BINARY_SEEK_MAX_LINE_BYTES)
    scanned = 0
    while True:
        line = handle.readline(_DIRECT_BINARY_SEEK_MAX_LINE_BYTES)
        if not line:
            break
        scanned += 1
        if scanned == 1 or scanned % 1000 == 0:
            LogSearchProgressStore.raise_if_cancelled(operation_id)
        timestamp = parse_line_time(line)
        if timestamp is None or timestamp < start:
            continue
        if timestamp > end:
            break
        line_bytes = line.encode("utf-8", errors="replace") if isinstance(line, str) else bytes(line)
        yield line_bytes if line_bytes.endswith(b"\n") else line_bytes + b"\n"


def _stream_direct(machine, artifact: LogArtifact, start: datetime, end: datetime, operation_id: str = "-") -> Iterator[bytes]:
    scanned = matched = 0
    stop_reason = "file_start"
    logger.info("log.read.direct.start machine=%s path=%s start=%s end=%s", machine.id, artifact.path, start, end)
    with ssh_session(machine) as lease:
        with lease.sftp.open(artifact.path, "rb") as handle:
            size = handle.stat().st_size
            logger.info("log.read.direct.open machine=%s session=%s reused=%s bytes=%d", machine.id, lease.session_id, lease.reused, size)
            start_offset = _find_direct_time_offset(handle, size, start)
            if start_offset is not None:
                logger.info(
                    "log.read.direct.binary_seek machine=%s path=%s bytes=%d start_offset=%d saved_prefix=%d",
                    machine.id, artifact.path, size, start_offset, max(0, start_offset),
                )
                for line in _stream_direct_forward_from_offset(
                    handle, offset=start_offset, start=start, end=end, operation_id=operation_id,
                ):
                    matched += 1
                    if matched % 5000 == 0:
                        logger.info("log.read.direct.progress machine=%s path=%s matched=%d strategy=binary_seek", machine.id, artifact.path, matched)
                    yield line
                stop_reason = "after_end_time_or_eof"
            else:
                for line in reverse_lines(handle):
                    scanned += 1
                    if scanned == 1 or scanned % 1000 == 0:
                        LogSearchProgressStore.raise_if_cancelled(operation_id)
                    timestamp = parse_line_time(line)
                    if timestamp is None:
                        continue
                    if timestamp > end:
                        continue
                    if timestamp < start:
                        stop_reason = "before_start_time"
                        break
                    matched += 1
                    if matched % 5000 == 0:
                        logger.info("log.read.direct.progress machine=%s path=%s scanned=%d matched=%d strategy=reverse_tail", machine.id, artifact.path, scanned, matched)
                    line_bytes = line.encode("utf-8", errors="replace") if isinstance(line, str) else bytes(line)
                    yield line_bytes if line_bytes.endswith(b"\n") else line_bytes + b"\n"
    logger.info("log.read.direct.finish machine=%s path=%s scanned=%d matched=%d stop=%s", machine.id, artifact.path, scanned, matched, stop_reason)

def _stream_tar_member(machine, artifact: LogArtifact, start: datetime, end: datetime, operation_id: str = "-") -> Iterator[bytes]:
    scanned = matched = 0
    logger.info("log.read.tar.start machine=%s path=%s member=%s", machine.id, artifact.path, artifact.member_name)
    with ssh_session(machine) as lease:
        start_second = start.strftime("%Y-%m-%d %H:%M:%S")
        end_second = end.strftime("%Y-%m-%d %H:%M:%S")
        awk_program = '{ if (substr($0,1,1) != "[") next; ts=substr($0,2,19); if (ts < s) next; if (ts > e) exit; print $0 }'
        command = (
            f"{_tar_member_extract_pipeline(artifact.path, artifact.member_name)} | "
            f"LC_ALL=C awk -v s={shlex.quote(start_second)} -v e={shlex.quote(end_second)} {shlex.quote(awk_program)}"
        )
        _, stdout, stderr = lease.client.exec_command(command, timeout=300)
        while True:
            line = stdout.readline()
            if not line:
                break
            scanned += 1
            if scanned == 1 or scanned % 1000 == 0:
                LogSearchProgressStore.raise_if_cancelled(operation_id)
            timestamp = parse_line_time(line)
            if timestamp is None or timestamp < start:
                continue
            if timestamp > end:
                break
            matched += 1
            if matched % 5000 == 0:
                logger.info("log.read.tar.progress machine=%s member=%s scanned=%d matched=%d", machine.id, artifact.member_name, scanned, matched)
            line_bytes = line.encode("utf-8", errors="replace") if isinstance(line, str) else bytes(line)
            yield line_bytes if line_bytes.endswith(b"\n") else line_bytes + b"\n"
        code = stdout.channel.recv_exit_status()
        if code != 0:
            message = stderr.read().decode("utf-8", errors="replace")
            raise SshOperationError(f"读取压缩包成员失败：{message}")
    logger.info("log.read.tar.finish machine=%s member=%s scanned=%d matched=%d", machine.id, artifact.member_name, scanned, matched)


def _artifact_cache_scope(environment: Environment, machine, artifact: LogArtifact) -> LogCacheScope:
    return LogCacheScope(
        host=machine.host,
        username=machine.username,
        source_category=artifact.source_category,
        subsystem=artifact.subsystem,
        fm=artifact.fm,
    )


def stream_log_window(
    environment: Environment,
    plan: list[LogArtifact],
    start: datetime,
    end: datetime,
    operation_id: str = "-",
    include_internal_markers: bool = True,
) -> Iterator[bytes]:
    token = set_request_id(operation_id)
    total_bytes = total_lines = 0
    started = time.monotonic()
    try:
        machine_map = {machine.id: machine for machine in environment_machines(environment)}
        logger.info("log.stream.start environment=%s artifacts=%d start=%s end=%s", environment.id, len(plan), start, end)
        # Frontend parser sorts by timestamp; newest artifacts are emitted first.
        for index, artifact in enumerate(reversed(plan), start=1):
            LogSearchProgressStore.raise_if_cancelled(operation_id)
            if operation_id and operation_id != "-":
                archive_name = PurePosixPath(artifact.path).name
                if artifact.member_name:
                    member_parts = _split_archive_member_chain(artifact.member_name)
                    display_name = " → ".join([archive_name, *(PurePosixPath(part).name for part in member_parts)])
                    full_path = f"{artifact.path}::{artifact.member_name}"
                else:
                    display_name = archive_name
                    full_path = artifact.path
                LogSearchProgressStore.artifact_started(
                    operation_id,
                    identity=artifact.identity,
                    display_name=display_name,
                    full_path=full_path,
                    subsystem=artifact.subsystem,
                    module=artifact.fm,
                )
            # 结构化解析流需要内部来源标记；“原始日志”查看模式明确关闭这些
            # TraceLens 元数据，只返回真实日志文本，效果接近直接用文本编辑器打开。
            if include_internal_markers:
                yield f"__TRACELENS_LOG_CATEGORY__={artifact.source_category}\n".encode("utf-8")
                yield f"__TRACELENS_LOG_SUBSYSTEM__={artifact.subsystem}\n".encode("utf-8")
                yield f"__TRACELENS_LOG_MODULE__={artifact.fm}\n".encode("utf-8")
                source_path = artifact.path + (f"::{artifact.member_name}" if artifact.member_name else "")
                yield f"__TRACELENS_LOG_SOURCE_PATH__={source_path}\n".encode("utf-8")
            machine = machine_map[artifact.machine_id]
            scope = _artifact_cache_scope(environment, machine, artifact)
            generation = artifact.content_version or artifact.fingerprint or "legacy"
            cache_kwargs = {
                "scope": scope,
                "path": artifact.path,
                "member_name": artifact.member_name,
                "generation": generation,
                "start": start,
                "end": end,
            }
            # Current FM.log is append-only. A query whose end is beyond the
            # indexed last timestamp is still an open/incomplete window, so it
            # must not reuse/store a stable content chunk yet. Historical
            # windows fully behind last_ts remain valid across appends because
            # current-file generation is preserved until rotate/truncate.
            content_window_stable = not (
                artifact.kind == "current"
                and (artifact.end_time is None or end > artifact.end_time)
            )
            cached_content = log_content_cache.get(**cache_kwargs) if content_window_stable else None
            if cached_content is not None:
                artifact_lines = cached_content.count(b"\n")
                total_lines += artifact_lines
                total_bytes += len(cached_content)
                logger.info(
                    "log.stream.artifact.cache_hit index=%d/%d identity=%s lines=%d bytes=%d",
                    index, len(plan), artifact.identity, artifact_lines, len(cached_content),
                )
                yield cached_content
                if operation_id and operation_id != "-":
                    LogSearchProgressStore.artifact_done(operation_id, identity=artifact.identity)
                continue

            logger.info("log.stream.artifact.remote index=%d/%d identity=%s", index, len(plan), artifact.identity)
            iterator = (
                _stream_tar_member(machine, artifact, start, end, operation_id)
                if artifact.kind == "tar_member"
                else _stream_direct(machine, artifact, start, end, operation_id)
            )
            artifact_lines = 0
            buffer = bytearray()
            cacheable = True
            max_cache_bytes = log_content_cache.max_bytes
            for chunk in iterator:
                artifact_lines += 1
                if artifact_lines == 1 or artifact_lines % 1000 == 0:
                    LogSearchProgressStore.raise_if_cancelled(operation_id)
                total_lines += 1
                total_bytes += len(chunk)
                if cacheable:
                    if len(buffer) + len(chunk) <= max_cache_bytes:
                        buffer.extend(chunk)
                    else:
                        cacheable = False
                        buffer.clear()
                yield chunk
            if content_window_stable and cacheable and buffer:
                log_content_cache.put(content=bytes(buffer), **cache_kwargs)
            logger.info(
                "log.stream.artifact.finish index=%d/%d lines=%d cached=%s",
                index, len(plan), artifact_lines, bool(content_window_stable and cacheable and buffer),
            )
            if operation_id and operation_id != "-":
                LogSearchProgressStore.artifact_done(operation_id, identity=artifact.identity)
        if operation_id and operation_id != "-":
            LogSearchProgressStore.patch(operation_id, result_count=total_lines, output_bytes=total_bytes)
            LogSearchProgressStore.finish(operation_id)
        logger.info(
            "log.stream.finish environment=%s lines=%d bytes=%d elapsed_ms=%d",
            environment.id,
            total_lines,
            total_bytes,
            int((time.monotonic() - started) * 1000),
        )
    except LogSearchCancelled:
        if operation_id and operation_id != "-":
            LogSearchProgressStore.cancelled(operation_id)
        logger.info("log.stream.cancelled environment=%s operation=%s lines=%d bytes=%d", environment.id, operation_id, total_lines, total_bytes)
        return
    except GeneratorExit:
        logger.warning("log.stream.client_disconnected environment=%s lines=%d bytes=%d", environment.id, total_lines, total_bytes)
        raise
    except Exception as exc:
        if operation_id and operation_id != "-":
            LogSearchProgressStore.fail(operation_id, str(exc))
        logger.exception("log.stream.failed environment=%s lines=%d bytes=%d", environment.id, total_lines, total_bytes)
        raise
    finally:
        reset_request_id(token)

