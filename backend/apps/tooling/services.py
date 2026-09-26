from __future__ import annotations

from typing import Any

import re
from difflib import SequenceMatcher
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db.models import F, Q
from django.utils import timezone

from apps.environments.models import Environment, ResourceSettings
from apps.environments.serializers import EnvironmentSerializer
from apps.logsources.serializers import (
    LogQuerySkillSerializer,
    LogWindowRequestSerializer,
    SemanticSourceBatchRequestSerializer,
    SemanticSourceRequestSerializer,
)
from apps.logsources.services.remote_logs import build_log_plan, scan_environment_logs
from apps.logsources.services.search_progress import LogSearchProgressStore
from apps.logsources.services.search_result_cache import LogSearchResultCache
from apps.logsources.services.semantic_source import recognize_semantic_batch, recognize_semantic_description
from apps.logsources.services.query_skills import LogQuerySkillError, match_query_skills, query_skill_step
from apps.logsources.services.event_configs import resolve_event_definition
from apps.logsources.models import EventCodeDefinition, LogFmDefinition, LogQuerySkill, LogSourceRule, LogSubsystemDefinition
from apps.knowledge.models import AbnormalCase


class ToolInputError(ValueError):
    pass


def get_current_time(payload: dict[str, Any]) -> dict[str, Any]:
    """Return actual runtime time instead of relying on the model's internal date/time."""
    timezone_name = str(payload.get("timezone") or settings.TIME_ZONE or "UTC").strip() or "UTC"
    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ToolInputError(
            f"无法识别时区 {timezone_name!r}。请传 IANA 时区名称，例如 Asia/Shanghai、Asia/Singapore 或 UTC。"
        ) from exc
    now = datetime.now(tz)
    return {
        "timezone": timezone_name,
        "iso": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "weekday": now.strftime("%A"),
        "epoch_ms": int(now.timestamp() * 1000),
    }


def _environment(environment_id: Any) -> Environment:
    try:
        normalized = int(environment_id)
    except (TypeError, ValueError) as exc:
        raise ToolInputError("environment_id 必须是有效整数。") from exc
    try:
        return (
            Environment.objects.select_related("upper_machine", "folder")
            .prefetch_related("machine_relations__target_machine")
            .get(pk=normalized)
        )
    except Environment.DoesNotExist as exc:
        raise ToolInputError("环境不存在。") from exc


def get_environment_info(payload: dict[str, Any]) -> dict[str, Any]:
    environment = _environment(payload.get("environment_id"))
    return EnvironmentSerializer(environment).data


def _parse_assistant_datetime(value: Any, field_name: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ToolInputError(f"{field_name} 不能为空。")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolInputError(f"{field_name} 必须是 ISO 日期时间。") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def get_log_catalog(payload: dict[str, Any]) -> dict[str, Any]:
    environment = _environment(payload.get("environment_id"))
    categories = {
        str(item).strip()
        for item in (payload.get("source_categories") or [])
        if str(item).strip()
    }
    refresh = bool(payload.get("refresh", False))
    result = scan_environment_logs(environment, categories or None, refresh=refresh)
    if refresh:
        removed = LogSearchResultCache.invalidate_environment(environment.id)
        result.setdefault("cache", {})["result_cache_invalidated"] = removed
    return result



def _normalize_log_entity(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _sorted_target_modules(module: LogFmDefinition) -> list[LogFmDefinition]:
    targets = list(module.target_modules.filter(enabled=True, subsystem__enabled=True).select_related("subsystem"))
    targets.sort(key=lambda item: (int(item.query_priority or 100), item.subsystem.name.casefold(), item.name.casefold()))
    return targets


def _component_candidate_rows(component_name: str, *, limit: int = 8) -> list[dict[str, Any]]:
    """Return fuzzy component choices from the unified subsystem/module table."""
    name = str(component_name or "").strip()
    needle = _normalize_log_entity(name)
    if not needle:
        return []

    scored: dict[tuple[str, str, str], dict[str, Any]] = {}

    def add(subsystem: Any, module: Any, kind: Any = "normal", *, alias: Any = "", source: str = "database", priority: int = 100) -> None:
        subsystem_text = str(subsystem or "").strip()
        module_text = str(module or "").strip()
        kind_text = str(kind or "normal").strip() or "normal"
        if not subsystem_text or not module_text:
            return
        names = [module_text, str(alias or "").strip()]
        best_score = 0.0
        matched_on = module_text
        for candidate_name in names:
            candidate_norm = _normalize_log_entity(candidate_name)
            if not candidate_norm:
                continue
            ratio = SequenceMatcher(None, needle, candidate_norm).ratio()
            if candidate_norm.startswith(needle) or needle.startswith(candidate_norm):
                ratio = max(ratio, 0.93)
            elif needle in candidate_norm or candidate_norm in needle:
                ratio = max(ratio, 0.86)
            if ratio > best_score:
                best_score = ratio
                matched_on = candidate_name
        if best_score < 0.55:
            return
        key = (subsystem_text.casefold(), module_text.casefold(), kind_text.casefold())
        row = {
            "subsystem": subsystem_text,
            "fm": module_text,
            "kind": kind_text,
            "priority": int(priority or 100),
            "score": round(best_score, 3),
            "matched_on": matched_on,
            "source": source,
        }
        existing = scored.get(key)
        if existing is None or float(row["score"]) > float(existing.get("score") or 0):
            scored[key] = row

    modules = list(
        LogFmDefinition.objects.filter(enabled=True, subsystem__enabled=True)
        .select_related("subsystem")
        .prefetch_related("target_modules__subsystem")
        .order_by("subsystem__name", "query_priority", "name", "kind")[:800]
    )
    for module in modules:
        targets = _sorted_target_modules(module) if module.event_component else []
        for target in targets:
            add(
                target.subsystem.name,
                target.name,
                target.kind,
                alias=module.name,
                source="module_target_config",
                priority=target.query_priority,
            )
        add(module.subsystem.name, module.name, module.kind, alias=module.display_name, source="module_definition", priority=module.query_priority)

    for row in LogSourceRule.objects.filter(enabled=True).exclude(module="").values("subsystem", "module", "component")[:600]:
        add(row.get("subsystem"), row.get("module"), "normal", alias=row.get("component"), source="log_source_rule")

    rows = sorted(
        scored.values(),
        key=lambda item: (
            -float(item.get("score") or 0),
            int(item.get("priority") or 100),
            str(item.get("subsystem") or "").casefold(),
            str(item.get("fm") or "").casefold(),
        ),
    )
    return rows[:max(1, int(limit))]


def _component_target_rows(component_name: str) -> dict[str, Any]:
    """Resolve an exact Event component/module name through the unified table."""
    name = str(component_name or "").strip()
    if not name:
        raise ToolInputError("component_name 不能为空。")

    exact_rows = list(
        LogFmDefinition.objects.filter(enabled=True, subsystem__enabled=True)
        .filter(Q(name__iexact=name) | Q(display_name__iexact=name))
        .select_related("subsystem")
        .prefetch_related("target_modules__subsystem")
        .order_by("subsystem__name", "kind", "id")
    )

    configured_rows = [row for row in exact_rows if row.event_component and row.target_modules.exists()]
    if len(configured_rows) == 1:
        source = configured_rows[0]
        targets = [
            {
                "subsystem": target.subsystem.name,
                "fm": target.name,
                "kind": target.kind,
                "priority": int(target.query_priority or 100),
            }
            for target in _sorted_target_modules(source)
        ]
        if targets:
            return {
                "component_name": name,
                "found": True,
                "exact_match": True,
                "ambiguous": False,
                "selection_required": False,
                "source": "module_target_config",
                "source_subsystem": source.subsystem.name,
                "source_module_id": source.id,
                "targets": targets,
            }

    # A normal module without explicit targets resolves to itself.  Multiple exact
    # rows stay ambiguous because the subsystem is not known from component_name alone.
    targets = []
    seen = set()
    for module in exact_rows:
        key = (module.subsystem.name.casefold(), module.name.casefold(), module.kind)
        if key in seen:
            continue
        seen.add(key)
        targets.append({
            "subsystem": module.subsystem.name,
            "fm": module.name,
            "kind": module.kind,
            "priority": int(module.query_priority or 100),
        })

    alias_rows = list(
        LogSourceRule.objects.filter(enabled=True)
        .filter(Q(component__iexact=name) | Q(module__iexact=name))
        .exclude(module="")
        .values("subsystem", "module")[:100]
    )
    for row in alias_rows:
        module_name = str(row.get("module") or "").strip()
        subsystem_name = str(row.get("subsystem") or "").strip()
        if not module_name:
            continue
        query = LogFmDefinition.objects.filter(enabled=True, subsystem__enabled=True, name__iexact=module_name)
        if subsystem_name:
            query = query.filter(subsystem__name__iexact=subsystem_name)
        for module in query.select_related("subsystem")[:20]:
            key = (module.subsystem.name.casefold(), module.name.casefold(), module.kind)
            if key in seen:
                continue
            seen.add(key)
            targets.append({
                "subsystem": module.subsystem.name,
                "fm": module.name,
                "kind": module.kind,
                "priority": int(module.query_priority or 100),
            })

    targets.sort(key=lambda item: (int(item.get("priority") or 100), item["subsystem"].casefold(), item["fm"].casefold()))
    if not targets:
        candidates = _component_candidate_rows(name)
        return {
            "component_name": name,
            "found": bool(candidates),
            "exact_match": False,
            "ambiguous": bool(candidates),
            "selection_required": bool(candidates),
            "source": "database",
            "targets": [],
            "candidates": candidates,
            "reason": "没有完全匹配；已找到相近日志目标，请由用户选择后再查询。" if candidates else "数据库中没有可用的组件/模块候选。",
        }
    if len(targets) > 1:
        choices = [dict(item, score=1.0, matched_on=name, source="exact") for item in targets[:20]]
        return {
            "component_name": name,
            "found": True,
            "exact_match": False,
            "ambiguous": True,
            "selection_required": True,
            "source": "database",
            "targets": targets[:20],
            "candidates": choices,
            "reason": "存在多个完全同名的日志目标，请由用户选择本次要查询的日志。",
        }
    return {
        "component_name": name,
        "found": True,
        "exact_match": True,
        "ambiguous": False,
        "selection_required": False,
        "source": "database",
        "targets": targets,
    }



def resolve_environment_event(payload: dict[str, Any]) -> dict[str, Any]:
    environment_id = payload.get("environment_id")
    if environment_id not in (None, ""):
        _environment(environment_id)
    return resolve_event_definition(
        environment_id=int(environment_id) if environment_id not in (None, "") else None,
        display_code=str(payload.get("display_code") or ""),
        code=str(payload.get("code") or ""),
        code_string=str(payload.get("code_string") or ""),
    )

def resolve_environment_component(payload: dict[str, Any]) -> dict[str, Any]:
    environment_id = payload.get("environment_id")
    environment = _environment(environment_id) if environment_id not in (None, "") else None
    result = _component_target_rows(str(payload.get("component_name") or ""))
    if environment is not None:
        result["environment_id"] = environment.id
    return result


def resolve_log_components(payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve event-log component names to configured subsystem/module targets.

    The database is the vocabulary source; ``available_targets`` is the concrete
    ATLog debug tree for the current case.  Returned selectable targets must exist
    in both places so an Agent cannot invent a module that has no log file.
    """
    event_components = [str(item).strip() for item in (payload.get("event_components") or []) if str(item).strip()]
    event_display_codes = [str(item).strip() for item in (payload.get("event_display_codes") or []) if str(item).strip()]
    environment_id = payload.get("environment_id")
    event_text = str(payload.get("event_text") or "")[:120000]

    # DisplayCode is the strongest event identity. Environment refresh has already
    # indexed it from ~/SW/resource/<subsystem>/eh/*_event.json, where the folder
    # gives the subsystem and the filename gives the component repository.
    resolved_event_definitions: list[dict[str, str]] = []
    event_component_subsystems: dict[str, str] = {}
    for display_code in event_display_codes[:100]:
        definitions_qs = EventCodeDefinition.objects.filter(active=True, display_code__iexact=display_code).select_related("subsystem", "source_config")
        if environment_id not in (None, ""):
            try:
                definitions_qs = definitions_qs.filter(environment_id=int(environment_id))
            except (TypeError, ValueError):
                pass
        definitions_for_code = list(definitions_qs[:20])
        pairs = {(item.subsystem.name.casefold(), item.component_code.casefold()) for item in definitions_for_code}
        if len(pairs) != 1:
            continue
        item = definitions_for_code[0]
        component = str(item.component_code or "").strip()
        subsystem_name = str(item.subsystem.name or "").strip()
        if component and all(_normalize_log_entity(existing) != _normalize_log_entity(component) for existing in event_components):
            event_components.append(component)
        if component and subsystem_name:
            event_component_subsystems[_normalize_log_entity(component)] = _normalize_log_entity(subsystem_name)
        resolved_event_definitions.append({
            "display_code": str(item.display_code or ""),
            "subsystem": subsystem_name,
            "component": component,
            "config_file": str(item.source_config.file_name or ""),
        })
    available_targets = []
    for item in payload.get("available_targets") or []:
        if not isinstance(item, dict):
            continue
        subsystem = str(item.get("subsystem") or "").strip()
        module = str(item.get("module") or item.get("fm") or "").strip()
        if subsystem and module:
            available_targets.append({"subsystem": subsystem, "module": module})

    actual_by_pair = {
        (_normalize_log_entity(item["subsystem"]), _normalize_log_entity(item["module"])): item
        for item in available_targets
    }
    actual_by_module: dict[str, list[dict[str, str]]] = {}
    actual_by_subsystem: dict[str, list[dict[str, str]]] = {}
    for item in available_targets:
        actual_by_module.setdefault(_normalize_log_entity(item["module"]), []).append(item)
        actual_by_subsystem.setdefault(_normalize_log_entity(item["subsystem"]), []).append(item)

    definitions = list(
        LogFmDefinition.objects.filter(enabled=True, subsystem__enabled=True)
        .select_related("subsystem")
        .values(
            "name", "display_name", "kind",
            "subsystem__name", "subsystem__display_name",
        )
    )
    subsystems = list(
        LogSubsystemDefinition.objects.filter(enabled=True)
        .values("name", "display_name")
    )
    aliases = list(
        LogSourceRule.objects.filter(enabled=True)
        .exclude(component="", module="")
        .values("component", "module", "subsystem")[:1000]
    )

    token_sources: dict[str, set[str]] = {}
    for token in event_components:
        norm = _normalize_log_entity(token)
        if norm:
            token_sources.setdefault(norm, set()).add(token)

    lowered_text = event_text.lower()
    for row in definitions:
        for candidate in (row.get("name"), row.get("display_name")):
            text = str(candidate or "").strip()
            norm = _normalize_log_entity(text)
            if text and len(norm) >= 3 and text.lower() in lowered_text:
                token_sources.setdefault(norm, set()).add(text)
    for row in subsystems:
        for candidate in (row.get("name"), row.get("display_name")):
            text = str(candidate or "").strip()
            norm = _normalize_log_entity(text)
            if text and len(norm) >= 3 and text.lower() in lowered_text:
                token_sources.setdefault(norm, set()).add(text)

    # Event组件 -> 目标模块 now lives directly on LogFmDefinition.  When the
    # target exists in the current ATLog debug tree it can be read deterministically.
    configured_targets: list[dict[str, Any]] = []
    configured_unavailable: list[dict[str, str]] = []
    configured_matched_components: set[str] = set()
    source_rows = list(
        LogFmDefinition.objects.filter(enabled=True, event_component=True, subsystem__enabled=True)
        .select_related("subsystem")
        .prefetch_related("target_modules__subsystem")
    )
    source_by_norm_multi: dict[str, list[LogFmDefinition]] = {}
    source_by_pair: dict[tuple[str, str], LogFmDefinition] = {}
    for row in source_rows:
        component_norm = _normalize_log_entity(row.name)
        source_by_norm_multi.setdefault(component_norm, []).append(row)
        source_by_pair[(_normalize_log_entity(row.subsystem.name), component_norm)] = row
    matched_source_ids: set[int] = set()
    seen_configured_targets: set[tuple[str, str, str]] = set()
    for event_name in event_components:
        norm = _normalize_log_entity(event_name)
        source_subsystem = event_component_subsystems.get(norm, "")
        row = source_by_pair.get((source_subsystem, norm)) if source_subsystem else None
        if row is None:
            same_name = source_by_norm_multi.get(norm) or []
            row = same_name[0] if len(same_name) == 1 else None
        if not row:
            continue
        target_modules = _sorted_target_modules(row)
        if not target_modules:
            continue
        configured_matched_components.add(norm)
        for target_module in target_modules:
            pair = (_normalize_log_entity(target_module.subsystem.name), _normalize_log_entity(target_module.name))
            actual = actual_by_pair.get(pair)
            if actual:
                target_key = (norm, actual["subsystem"].lower(), actual["module"].lower())
                if target_key in seen_configured_targets:
                    continue
                seen_configured_targets.add(target_key)
                configured_targets.append({
                    "subsystem": actual["subsystem"],
                    "module": actual["module"],
                    "score": 1000 - min(int(target_module.query_priority or 100), 900),
                    "priority": int(target_module.query_priority or 100),
                    "matched_by": "module_target_config",
                    "event_component": event_name,
                    "db_name": str(row.name or event_name),
                    "available": True,
                    "configured": True,
                    "source_module_id": int(row.id),
                })
                matched_source_ids.add(int(row.id))
            else:
                configured_unavailable.append({
                    "event_component": event_name,
                    "subsystem": str(target_module.subsystem.name or ""),
                    "module": str(target_module.name or ""),
                })
    if matched_source_ids:
        LogFmDefinition.objects.filter(pk__in=matched_source_ids).update(
            matched_count=F("matched_count") + 1,
            last_matched_at=timezone.now(),
        )

    candidates: dict[tuple[str, str], dict[str, Any]] = {}

    def add_target(actual: dict[str, str], *, score: int, matched_by: str, event_name: str, db_name: str = "") -> None:
        key = (actual["subsystem"].lower(), actual["module"].lower())
        previous = candidates.get(key)
        item = {
            "subsystem": actual["subsystem"],
            "module": actual["module"],
            "score": score,
            "matched_by": matched_by,
            "event_component": event_name,
            "db_name": db_name,
            "available": True,
        }
        if previous is None or score > int(previous.get("score") or 0):
            candidates[key] = item

    for norm, originals in token_sources.items():
        event_name = sorted(originals, key=len)[0]
        # Exact module/display-name mapping from the global module table.
        for row in definitions:
            module_names = {_normalize_log_entity(row.get("name")), _normalize_log_entity(row.get("display_name"))}
            subsystem_names = {_normalize_log_entity(row.get("subsystem__name")), _normalize_log_entity(row.get("subsystem__display_name"))}
            module_names.discard("")
            subsystem_names.discard("")
            if norm in module_names:
                db_sub = _normalize_log_entity(row.get("subsystem__name"))
                db_mod = _normalize_log_entity(row.get("name"))
                actual = actual_by_pair.get((db_sub, db_mod))
                if actual is None:
                    choices = actual_by_module.get(db_mod) or actual_by_module.get(norm) or []
                    actual = choices[0] if len(choices) == 1 else None
                if actual:
                    add_target(actual, score=100, matched_by="module_exact", event_name=event_name, db_name=str(row.get("name") or ""))
            if norm in subsystem_names:
                db_sub = _normalize_log_entity(row.get("subsystem__name"))
                for actual in actual_by_subsystem.get(db_sub, [])[:24]:
                    add_target(actual, score=86, matched_by="subsystem_exact", event_name=event_name, db_name=str(row.get("subsystem__name") or ""))

        # Configured source-rule aliases (component -> module/subsystem).
        for row in aliases:
            alias_names = {_normalize_log_entity(row.get("component")), _normalize_log_entity(row.get("module"))}
            alias_names.discard("")
            if norm not in alias_names:
                continue
            sub_norm = _normalize_log_entity(row.get("subsystem"))
            mod_norm = _normalize_log_entity(row.get("module")) or norm
            actual = actual_by_pair.get((sub_norm, mod_norm)) if sub_norm else None
            if actual is None:
                choices = actual_by_module.get(mod_norm) or []
                actual = choices[0] if len(choices) == 1 else None
            if actual:
                add_target(actual, score=96, matched_by="source_rule_alias", event_name=event_name, db_name=str(row.get("component") or row.get("module") or ""))

        # Case-tree exact match is allowed only when the same name is also known by DB.
        if norm in actual_by_module:
            known_norms = {
                _normalize_log_entity(row.get("name")) for row in definitions
            } | {
                _normalize_log_entity(row.get("display_name")) for row in definitions
            }
            if norm in known_norms:
                for actual in actual_by_module[norm][:8]:
                    add_target(actual, score=92, matched_by="db_and_case_exact", event_name=event_name, db_name=actual["module"])

    # Configured target modules are authoritative and can skip LLM target guessing.
    for item in configured_targets:
        key = (item["subsystem"].lower(), item["module"].lower())
        candidates[key] = item
    ordered = sorted(candidates.values(), key=lambda item: (-int(item.get("score") or 0), item["subsystem"].lower(), item["module"].lower()))
    resolved_norms = {_normalize_log_entity(candidate.get("event_component")) for candidate in ordered}
    return {
        "event_components": event_components,
        "event_display_codes": event_display_codes,
        "event_definitions": resolved_event_definitions[:40],
        "database_module_count": len(definitions),
        "database_subsystem_count": len(subsystems),
        "available_target_count": len(available_targets),
        "candidate_count": len(ordered),
        "candidates": ordered[:80],
        "configured_targets": configured_targets[:24],
        "configured_unavailable": configured_unavailable[:24],
        "configured_match_count": len(configured_targets),
        "unresolved_components": [
            item for item in event_components
            if _normalize_log_entity(item) not in resolved_norms
        ],
    }


def build_log_plan_tool(payload: dict[str, Any]) -> dict[str, Any]:
    environment = _environment(payload.get("environment_id"))
    request_payload = {
        key: value for key, value in payload.items()
        if key not in {"environment_id", "operation_id"}
    }
    serializer = LogWindowRequestSerializer(data=request_payload)
    if not serializer.is_valid():
        raise ToolInputError(str(serializer.errors))
    start = serializer.naive(serializer.validated_data["start_time"])
    end = serializer.naive(serializer.validated_data["end_time"])
    plan = build_log_plan(
        environment,
        start,
        end,
        serializer.validated_data["subsystems"],
        serializer.validated_data["fms"],
        serializer.validated_data["source_categories"],
        fm_targets=serializer.validated_data.get("fm_targets", []),
        operation_id=str(payload.get("operation_id") or "tool-service"),
    )
    return {
        "environment_id": environment.id,
        "start_time": start,
        "end_time": end,
        "count": len(plan),
        "artifacts": [
            {
                "machine_id": item.machine_id,
                "machine_name": item.machine_name,
                "source_category": item.source_category,
                "source_name": item.source_name,
                "subsystem": item.subsystem,
                "fm": item.fm,
                "kind": item.kind,
                "path": item.path,
                "member_name": item.member_name,
                "boundary_time": item.boundary_time,
                "size": item.size,
            }
            for item in plan
        ],
    }


def get_search_progress(payload: dict[str, Any]) -> dict[str, Any]:
    _environment(payload.get("environment_id"))
    operation_id = str(payload.get("operation_id") or "").strip()
    if not operation_id:
        raise ToolInputError("operation_id 不能为空。")
    result = LogSearchProgressStore.get(operation_id)
    if result is None:
        return {"operation_id": operation_id, "stage": "pending", "percent": 0, "done": False}
    return result


def recognize_semantic_source(payload: dict[str, Any]) -> dict[str, Any]:
    environment = _environment(payload.get("environment_id"))
    request_payload = {key: value for key, value in payload.items() if key != "environment_id"}
    serializer = SemanticSourceRequestSerializer(data=request_payload)
    if not serializer.is_valid():
        raise ToolInputError(str(serializer.errors))
    result = recognize_semantic_description(
        environment,
        source_file=serializer.validated_data["source_file"],
        source_line=serializer.validated_data.get("source_line"),
        function_name=serializer.validated_data.get("function_name"),
        targets=serializer.validated_data["fm_targets"],
    )
    if result is None:
        return {"found": False, "message": "未找到源码语义"}
    return {"found": True, **result}


def recognize_semantic_sources_batch(payload: dict[str, Any]) -> dict[str, Any]:
    environment = _environment(payload.get("environment_id"))
    request_payload = {key: value for key, value in payload.items() if key != "environment_id"}
    serializer = SemanticSourceBatchRequestSerializer(data=request_payload)
    if not serializer.is_valid():
        raise ToolInputError(str(serializer.errors))
    return recognize_semantic_batch(
        environment,
        items=serializer.validated_data["items"],
        targets=serializer.validated_data["fm_targets"],
    )


# ---- Agent-ready deterministic log primitives ---------------------------------
# These tools intentionally accept normalized log-entry objects instead of UI state.
# Both remote-search results and imported/local logs can therefore use the same logic.

_MAX_TOOL_ENTRY_TEXT_BYTES = 12_000_000


def _tool_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("entries")
    if not isinstance(raw, list):
        raise ToolInputError("entries 必须是日志对象数组。")
    entries: list[dict[str, Any]] = []
    used_bytes = 0
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ToolInputError(f"entries[{index}] 必须是对象。")
        # Primitive log tools are bounded by actual text size, not by an
        # arbitrary time span or row count.  This keeps dense short logs usable
        # while preventing a huge normalized payload from exhausting memory.
        text = _entry_text(item)
        used_bytes += len(text.encode("utf-8", errors="replace"))
        if used_bytes > _MAX_TOOL_ENTRY_TEXT_BYTES:
            raise ToolInputError(
                f"日志文本超过 {_MAX_TOOL_ENTRY_TEXT_BYTES // 1_000_000} MB。请先使用函数折叠摘要压缩后再继续分析。"
            )
        entries.append(item)
    return entries


def _entry_text(entry: dict[str, Any]) -> str:
    return str(entry.get("message") or entry.get("raw") or entry.get("text") or "")


def _entry_level(entry: dict[str, Any]) -> str:
    return str(entry.get("severity") or entry.get("level") or "").strip().upper()


def _entry_module(entry: dict[str, Any]) -> str:
    return str(entry.get("module") or entry.get("fm") or entry.get("component") or "").strip()


def _timestamp_ns(entry: dict[str, Any]) -> int | None:
    value = entry.get("timestamp_ns")
    if value is not None:
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    raw = str(entry.get("timestamp") or entry.get("time") or "").strip()
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    candidates = [normalized]
    # Common TraceLens text logs use a space separator and may be naive local time.
    if " " in normalized and "T" not in normalized:
        candidates.append(normalized.replace(" ", "T", 1))
    parsed: datetime | None = None
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
            break
        except ValueError:
            continue
    if parsed is None:
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%m-%d %H:%M:%S.%f", "%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(raw, fmt)
                if fmt.startswith("%m-"):
                    parsed = parsed.replace(year=datetime.now().year)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1_000_000_000)


def _entry_summary(entry: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        "index": index,
        "timestamp": entry.get("timestamp") or entry.get("time") or "",
        "timestamp_ns": _timestamp_ns(entry),
        "level": _entry_level(entry),
        "subsystem": str(entry.get("subsystem") or ""),
        "module": _entry_module(entry),
        "function_name": str(entry.get("function_name") or entry.get("function") or ""),
        "message": _entry_text(entry),
        "source_file": str(entry.get("source_file") or entry.get("source_path") or entry.get("file") or ""),
        "line_number": entry.get("line_number") or entry.get("line"),
        "process_id": entry.get("process_id") or entry.get("pid"),
        "thread_id": entry.get("thread_id") or entry.get("tid"),
    }


def search_errors(payload: dict[str, Any]) -> dict[str, Any]:
    entries = _tool_entries(payload)
    levels = {str(item).strip().upper() for item in (payload.get("levels") or ["ERROR", "FATAL", "CRITICAL"]) if str(item).strip()}
    limit = max(1, min(int(payload.get("limit") or 500), 5000))
    matches = []
    for index, entry in enumerate(entries):
        if _entry_level(entry) not in levels:
            continue
        matches.append(_entry_summary(entry, index))
        if len(matches) >= limit:
            break
    return {"count": len(matches), "truncated": len(matches) >= limit, "levels": sorted(levels), "matches": matches}


def search_keyword(payload: dict[str, Any]) -> dict[str, Any]:
    entries = _tool_entries(payload)
    keyword = str(payload.get("keyword") or "")
    if not keyword:
        raise ToolInputError("keyword 不能为空。")
    case_sensitive = bool(payload.get("case_sensitive", False))
    whole_word = bool(payload.get("whole_word", False))
    limit = max(1, min(int(payload.get("limit") or 500), 5000))
    flags = 0 if case_sensitive else re.IGNORECASE
    matcher = re.compile(rf"(?<!\w){re.escape(keyword)}(?!\w)" if whole_word else re.escape(keyword), flags)
    matches = []
    for index, entry in enumerate(entries):
        text = " ".join((_entry_text(entry), _entry_module(entry), str(entry.get("function_name") or entry.get("function") or "")))
        if not matcher.search(text):
            continue
        matches.append(_entry_summary(entry, index))
        if len(matches) >= limit:
            break
    return {"count": len(matches), "truncated": len(matches) >= limit, "keyword": keyword, "matches": matches}


def get_log_context(payload: dict[str, Any]) -> dict[str, Any]:
    entries = _tool_entries(payload)
    if not entries:
        return {"anchor_index": None, "start_index": 0, "end_index": -1, "entries": []}
    try:
        anchor_index = int(payload.get("anchor_index"))
    except (TypeError, ValueError) as exc:
        raise ToolInputError("anchor_index 必须是有效日志下标。") from exc
    if anchor_index < 0 or anchor_index >= len(entries):
        raise ToolInputError("anchor_index 超出日志范围。")
    before = max(0, min(int(payload.get("before_lines") or 20), 500))
    after = max(0, min(int(payload.get("after_lines") or 20), 500))
    start = max(0, anchor_index - before)
    end = min(len(entries), anchor_index + after + 1)
    return {
        "anchor_index": anchor_index,
        "start_index": start,
        "end_index": end - 1,
        "entries": [_entry_summary(entry, index) for index, entry in enumerate(entries[start:end], start=start)],
    }


def get_log_time_range(payload: dict[str, Any]) -> dict[str, Any]:
    entries = _tool_entries(payload)
    points = [(index, _timestamp_ns(entry)) for index, entry in enumerate(entries)]
    points = [(index, value) for index, value in points if value is not None]
    if not points:
        return {"found": False, "count": len(entries), "timestamped_count": 0}
    first_index, start_ns = min(points, key=lambda item: item[1])
    last_index, end_ns = max(points, key=lambda item: item[1])
    return {
        "found": True,
        "count": len(entries),
        "timestamped_count": len(points),
        "start_index": first_index,
        "end_index": last_index,
        "start_timestamp_ns": start_ns,
        "end_timestamp_ns": end_ns,
        "span_ms": round((end_ns - start_ns) / 1_000_000, 3),
        "start": _entry_summary(entries[first_index], first_index),
        "end": _entry_summary(entries[last_index], last_index),
    }


def build_timeline(payload: dict[str, Any]) -> dict[str, Any]:
    entries = _tool_entries(payload)
    levels = {str(item).strip().upper() for item in (payload.get("levels") or []) if str(item).strip()}
    limit = max(1, min(int(payload.get("limit") or 5000), 20_000))
    events = []
    for index, entry in enumerate(entries):
        ts = _timestamp_ns(entry)
        if ts is None or (levels and _entry_level(entry) not in levels):
            continue
        events.append(_entry_summary(entry, index))
    events.sort(key=lambda item: (item.get("timestamp_ns") or 0, item["index"]))
    truncated = len(events) > limit
    events = events[:limit]
    modules = sorted({str(item.get("module") or "") for item in events if item.get("module")})
    return {"count": len(events), "truncated": truncated, "modules": modules, "events": events}


def find_event_clusters(payload: dict[str, Any]) -> dict[str, Any]:
    entries = _tool_entries(payload)
    levels = {str(item).strip().upper() for item in (payload.get("levels") or ["ERROR", "FATAL", "CRITICAL"]) if str(item).strip()}
    max_gap_ms = max(0.1, min(float(payload.get("max_gap_ms") or 1000), 60_000.0))
    min_events = max(2, min(int(payload.get("min_events") or 2), 100))
    points: list[tuple[int, int, dict[str, Any]]] = []
    for index, entry in enumerate(entries):
        ts = _timestamp_ns(entry)
        if ts is None or (levels and _entry_level(entry) not in levels):
            continue
        points.append((ts, index, entry))
    points.sort(key=lambda item: (item[0], item[1]))
    gap_ns = int(max_gap_ms * 1_000_000)
    groups: list[list[tuple[int, int, dict[str, Any]]]] = []
    current: list[tuple[int, int, dict[str, Any]]] = []
    for point in points:
        if current and point[0] - current[-1][0] > gap_ns:
            if len(current) >= min_events:
                groups.append(current)
            current = []
        current.append(point)
    if len(current) >= min_events:
        groups.append(current)
    clusters = []
    for cluster_index, group in enumerate(groups, start=1):
        start_ns, end_ns = group[0][0], group[-1][0]
        modules = sorted({_entry_module(entry) for _, _, entry in group if _entry_module(entry)})
        clusters.append({
            "cluster_id": cluster_index,
            "start_timestamp_ns": start_ns,
            "end_timestamp_ns": end_ns,
            "span_ms": round((end_ns - start_ns) / 1_000_000, 3),
            "event_count": len(group),
            "modules": modules,
            "events": [_entry_summary(entry, index) for _, index, entry in group[:100]],
            "events_truncated": len(group) > 100,
        })
    return {"count": len(clusters), "levels": sorted(levels), "max_gap_ms": max_gap_ms, "clusters": clusters}


def extract_process_events(payload: dict[str, Any]) -> dict[str, Any]:
    entries = _tool_entries(payload)
    patterns = [
        ("crash", re.compile(r"\b(segfault|core dump|coredump|abort(?:ed)?|crash(?:ed)?|fatal signal)\b", re.I)),
        ("restart", re.compile(r"\b(restart(?:ed|ing)?|re-launch|relaunch)\b", re.I)),
        ("shutdown", re.compile(r"\b(shutdown|shutting down|stopp(?:ed|ing)|process exit(?:ed)?|exiting)\b", re.I)),
        ("kill", re.compile(r"\b(kill(?:ed|ing)?|SIGKILL|SIGTERM)\b", re.I)),
        ("startup", re.compile(r"\b(start(?:ed|ing)?|startup|launch(?:ed|ing)?|process ready|service ready)\b", re.I)),
    ]
    result = []
    for index, entry in enumerate(entries):
        text = _entry_text(entry)
        event_type = next((name for name, pattern in patterns if pattern.search(text)), None)
        if not event_type:
            continue
        item = _entry_summary(entry, index)
        item["event_type"] = event_type
        result.append(item)
    return {"count": len(result), "events": result[:5000], "truncated": len(result) > 5000}


def get_related_modules(payload: dict[str, Any]) -> dict[str, Any]:
    module = str(payload.get("module") or payload.get("fm") or "").strip()
    subsystem = str(payload.get("subsystem") or "").strip()
    if not module:
        raise ToolInputError("module 不能为空。")
    qs = LogFmDefinition.objects.select_related("subsystem").prefetch_related("target_modules__subsystem", "targeted_by_modules__subsystem").filter(name__iexact=module)
    if subsystem:
        qs = qs.filter(subsystem__name__iexact=subsystem)
    rows = list(qs[:20])
    if not rows:
        return {"found": False, "module": module, "subsystem": subsystem, "matches": []}
    matches = []
    for item in rows:
        matches.append({
            "id": item.id,
            "subsystem": item.subsystem.name,
            "module": item.name,
            "display_name": item.display_name,
            "kind": item.kind,
            "target_modules": [
                {"subsystem": dep.subsystem.name, "module": dep.name, "display_name": dep.display_name, "kind": dep.kind, "priority": int(dep.query_priority or 100)}
                for dep in sorted(item.target_modules.all(), key=lambda value: (int(value.query_priority or 100), value.subsystem.name.casefold(), value.name.casefold()))
            ],
            "targeted_by": [
                {"subsystem": dep.subsystem.name, "module": dep.name, "display_name": dep.display_name, "kind": dep.kind}
                for dep in item.targeted_by_modules.all()
            ],
        })
    return {"found": True, "module": module, "subsystem": subsystem, "matches": matches}


def match_cases(payload: dict[str, Any]) -> dict[str, Any]:
    query = str(payload.get("query") or "").strip()
    module = str(payload.get("module") or "").strip().lower()
    limit = max(1, min(int(payload.get("limit") or 10), 50))
    min_score = max(0.0, min(float(payload.get("min_score") if payload.get("min_score") is not None else 70.0), 100.0))
    environment_id = payload.get("environment_id")
    qs = AbnormalCase.objects.filter(enabled=True)
    if environment_id not in (None, ""):
        try:
            env_id = int(environment_id)
        except (TypeError, ValueError) as exc:
            raise ToolInputError("environment_id 必须是有效整数。") from exc
        qs = qs.filter(Q(environment_id=env_id) | Q(environment__isnull=True))
    query_tokens = {token for token in re.split(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", query.lower()) if len(token) >= 2}
    results = []
    for case in qs.order_by("-matched_count", "-updated_at")[:500]:
        evidence_texts = []
        evidence_modules = set()
        for evidence in (case.evidences or []):
            if not isinstance(evidence, dict):
                continue
            evidence_modules.add(str(evidence.get("module") or evidence.get("component") or "").strip().lower())
            evidence_texts.extend([
                str(evidence.get("raw") or ""), str(evidence.get("template") or ""),
                str(evidence.get("function_name") or ""), " ".join(str(token) for token in (evidence.get("tokens") or [])),
            ])
        if module and module not in evidence_modules:
            continue
        searchable = " ".join([
            case.name, case.category, case.symptom, case.root_cause, case.solution, case.description,
            " ".join(str(tag) for tag in (case.tags or [])), *evidence_texts,
        ]).lower()
        if not query_tokens:
            score = 60.0 if module and module in evidence_modules else 20.0
        else:
            matched = {token for token in query_tokens if token in searchable}
            if not matched:
                continue
            coverage = len(matched) / len(query_tokens)
            title_bonus = 0.15 if any(token in case.name.lower() for token in matched) else 0.0
            root_bonus = 0.1 if any(token in case.root_cause.lower() for token in matched) else 0.0
            score = min(100.0, (coverage + title_bonus + root_bonus) * 100)
        if score <= min_score:
            continue
        results.append({
            "case_id": case.id,
            "name": case.name,
            "category": case.category,
            "symptom": case.symptom,
            "root_cause": case.root_cause,
            "solution": case.solution,
            "tags": case.tags or [],
            "evidence_count": case.evidence_count,
            "matched_count": case.matched_count,
            "score": round(score, 1),
        })
    results.sort(key=lambda item: (item["score"], item["matched_count"]), reverse=True)
    return {"count": min(len(results), limit), "query": query, "module": module, "threshold": min_score, "matches": results[:limit]}

# ---------------------------------------------------------------------------
# AI assistant operation tools
# ---------------------------------------------------------------------------

def list_environments(payload: dict[str, Any]) -> dict[str, Any]:
    query = str(payload.get("query") or "").strip()
    qs = (
        Environment.objects.select_related("upper_machine", "folder")
        .prefetch_related("machine_relations__target_machine")
        .order_by("name", "id")
    )
    if query:
        qs = qs.filter(
            Q(name__icontains=query)
            | Q(description__icontains=query)
            | Q(upper_machine__host__icontains=query)
            | Q(upper_machine__name__icontains=query)
        ).distinct()
    limit = max(1, min(int(payload.get("limit") or 50), 200))
    rows = [EnvironmentSerializer(item).data for item in qs[:limit]]
    return {"count": len(rows), "environments": rows}


def find_environment(payload: dict[str, Any]) -> dict[str, Any]:
    query = str(payload.get("query") or "").strip()
    if not query:
        raise ToolInputError("query 不能为空。")
    exact = (
        Environment.objects.select_related("upper_machine", "folder")
        .prefetch_related("machine_relations__target_machine")
        .filter(Q(name__iexact=query) | Q(upper_machine__host__iexact=query))
        .first()
    )
    if exact is not None:
        return {"query": query, "found": True, "exact": True, "environment": EnvironmentSerializer(exact).data, "candidates": []}
    qs = (
        Environment.objects.select_related("upper_machine", "folder")
        .prefetch_related("machine_relations__target_machine")
        .filter(
            Q(name__icontains=query)
            | Q(description__icontains=query)
            | Q(upper_machine__host__icontains=query)
            | Q(upper_machine__name__icontains=query)
        )
        .distinct()
        .order_by("name", "id")[:20]
    )
    rows = [EnvironmentSerializer(item).data for item in qs]
    return {
        "query": query,
        "found": len(rows) == 1,
        "exact": False,
        "environment": rows[0] if len(rows) == 1 else None,
        "candidates": rows,
    }


def get_environment_runtime_status(payload: dict[str, Any]) -> dict[str, Any]:
    environment = _environment(payload.get("environment_id"))
    # Keep the runtime probing implementation centralized in the environment API.
    from apps.environments.views import _get_environment_runtime

    return _get_environment_runtime(environment, force=bool(payload.get("refresh", False)))


def refresh_environment(payload: dict[str, Any]) -> dict[str, Any]:
    environment = _environment(payload.get("environment_id"))
    from apps.environments.services.discovery import discover_environment
    from apps.environments.serializers import EnvironmentDiscoverySerializer

    record = discover_environment(environment)
    environment.refresh_from_db()
    return {
        "environment": EnvironmentSerializer(_environment(environment.id)).data,
        "discovery": EnvironmentDiscoverySerializer(record).data,
    }


def query_environment_version(payload: dict[str, Any]) -> dict[str, Any]:
    environment = _environment(payload.get("environment_id"))
    from apps.environments.services.discovery import read_environment_versions

    return read_environment_versions(environment)


def _deployment_merged_payload(environment: Environment, payload: dict[str, Any]) -> dict[str, Any]:
    from apps.environments.services.deployment import build_defaults

    # Deployment actions must start from the same live defaults shown by the UI,
    # including stations.xml install port and DISPLAY.  This keeps Agent-driven
    # deployment behavior aligned with the manual deployment dialog.
    defaults = build_defaults(environment)
    nested = payload.get("parameters") if isinstance(payload.get("parameters"), dict) else {}
    accepted = {
        "task_name", "target_version", "simulation_mode", "include_sdk", "precheck_stop_lower",
        "upper_ip", "gpb_ips", "gpb_mode", "tb_mode", "install_mode", "install_port", "display_env",
        "include_dhh", "dhh_ip", "dhh_user", "dhh_machine_id", "post_start_script",
        "save_post_start_script", "selected_steps", "commands", "scheduled_at", "_trace_context",
    }
    result = dict(defaults)
    for source in (payload, nested):
        for key in accepted:
            if key in source and source[key] is not None:
                result[key] = source[key]
    return result


def get_deployment_defaults(payload: dict[str, Any]) -> dict[str, Any]:
    environment = _environment(payload.get("environment_id"))
    from apps.environments.services.deployment import build_defaults

    return build_defaults(environment)


def preview_deployment(payload: dict[str, Any]) -> dict[str, Any]:
    environment = _environment(payload.get("environment_id"))
    from apps.environments.services.deployment import command_preview

    return command_preview(environment, _deployment_merged_payload(environment, payload))


def list_environment_deployments(payload: dict[str, Any]) -> dict[str, Any]:
    environment = _environment(payload.get("environment_id"))
    from apps.environments.serializers import EnvironmentDeploymentSummarySerializer

    limit = max(1, min(int(payload.get("limit") or 20), 100))
    records = environment.deployments.order_by("-created_at")[:limit]
    rows = EnvironmentDeploymentSummarySerializer(records, many=True).data
    return {"count": len(rows), "deployments": rows}


def get_latest_deployment(payload: dict[str, Any]) -> dict[str, Any]:
    environment = _environment(payload.get("environment_id"))
    from apps.environments.serializers import EnvironmentDeploymentSummarySerializer

    deployment = environment.deployments.order_by("-created_at").first()
    return {"deployment": EnvironmentDeploymentSummarySerializer(deployment).data if deployment else None}


def get_deployment_detail(payload: dict[str, Any]) -> dict[str, Any]:
    environment = _environment(payload.get("environment_id"))
    try:
        deployment_id = int(payload.get("deployment_id"))
    except (TypeError, ValueError) as exc:
        raise ToolInputError("deployment_id 必须是有效整数。") from exc
    from apps.environments.models import EnvironmentDeployment
    from apps.environments.serializers import EnvironmentDeploymentSerializer

    try:
        deployment = environment.deployments.prefetch_related("steps").get(pk=deployment_id)
    except EnvironmentDeployment.DoesNotExist as exc:
        raise ToolInputError("部署记录不存在。") from exc
    step_key = str(payload.get("step_key") or deployment.current_step or "").strip()
    return EnvironmentDeploymentSerializer(deployment, context={"log_step_key": step_key}).data


def get_active_deployments(payload: dict[str, Any]) -> dict[str, Any]:
    from apps.environments.models import DeploymentStatus, EnvironmentDeployment
    from apps.environments.serializers import EnvironmentDeploymentSummarySerializer

    qs = EnvironmentDeployment.objects.filter(
        status__in=[DeploymentStatus.SCHEDULED, DeploymentStatus.PENDING, DeploymentStatus.RUNNING, DeploymentStatus.STOPPING]
    ).select_related("environment").order_by("environment_id", "-created_at")
    environment_id = payload.get("environment_id")
    if environment_id not in (None, ""):
        qs = qs.filter(environment_id=_environment(environment_id).id)
    latest: dict[int, Any] = {}
    for item in qs[:500]:
        latest.setdefault(item.environment_id, item)
    rows = EnvironmentDeploymentSummarySerializer(list(latest.values()), many=True).data
    return {"count": len(rows), "deployments": rows}


def start_environment_deployment(payload: dict[str, Any]) -> dict[str, Any]:
    environment = _environment(payload.get("environment_id"))
    from apps.environments.services.deployment import start_deployment
    from apps.environments.serializers import EnvironmentDeploymentSerializer

    deployment = start_deployment(environment, _deployment_merged_payload(environment, payload))
    deployment = environment.deployments.prefetch_related("steps").get(pk=deployment.pk)
    return EnvironmentDeploymentSerializer(deployment, context={"log_step_key": deployment.current_step}).data


def stop_environment_deployment(payload: dict[str, Any]) -> dict[str, Any]:
    environment = _environment(payload.get("environment_id"))
    try:
        deployment_id = int(payload.get("deployment_id"))
    except (TypeError, ValueError) as exc:
        raise ToolInputError("deployment_id 必须是有效整数。") from exc
    from apps.environments.services.deployment import request_stop_deployment
    from apps.environments.serializers import EnvironmentDeploymentSerializer

    deployment = request_stop_deployment(environment, deployment_id)
    deployment = environment.deployments.prefetch_related("steps").get(pk=deployment.pk)
    return EnvironmentDeploymentSerializer(deployment, context={"log_step_key": deployment.current_step}).data


def retry_environment_deployment_step(payload: dict[str, Any]) -> dict[str, Any]:
    environment = _environment(payload.get("environment_id"))
    try:
        deployment_id = int(payload.get("deployment_id"))
    except (TypeError, ValueError) as exc:
        raise ToolInputError("deployment_id 必须是有效整数。") from exc
    step_key = str(payload.get("step_key") or "").strip()
    if not step_key:
        raise ToolInputError("step_key 不能为空。")
    from apps.environments.services.deployment import retry_deployment_step
    from apps.environments.serializers import EnvironmentDeploymentSerializer

    deployment = retry_deployment_step(environment, deployment_id, step_key)
    deployment = environment.deployments.prefetch_related("steps").get(pk=deployment.pk)
    return EnvironmentDeploymentSerializer(deployment, context={"log_step_key": deployment.current_step}).data


def ensure_deployment_ssh_trust(payload: dict[str, Any]) -> dict[str, Any]:
    environment = _environment(payload.get("environment_id"))
    gpb_ips = payload.get("gpb_ips") or []
    if not isinstance(gpb_ips, list):
        raise ToolInputError("gpb_ips 必须是列表。")
    from apps.environments.services.deployment import ensure_gpb_ssh_trust

    return ensure_gpb_ssh_trust(
        environment,
        [str(item).strip() for item in gpb_ips if str(item).strip()],
        include_dhh=bool(payload.get("include_dhh", False)),
        dhh_ip=str(payload.get("dhh_ip") or ""),
        dhh_user=str(payload.get("dhh_user") or "root"),
    )


def sync_deployment_time(payload: dict[str, Any]) -> dict[str, Any]:
    environment = _environment(payload.get("environment_id"))
    gpb_ips = payload.get("gpb_ips") or []
    if not isinstance(gpb_ips, list):
        raise ToolInputError("gpb_ips 必须是列表。")
    from apps.environments.services.deployment import sync_gpb_times

    return sync_gpb_times(
        environment,
        [str(item).strip() for item in gpb_ips if str(item).strip()],
        include_dhh=bool(payload.get("include_dhh", False)),
        dhh_ip=str(payload.get("dhh_ip") or ""),
        dhh_user=str(payload.get("dhh_user") or "root"),
    )


_ASSISTANT_LOG_LINE_REGEX = re.compile(
    r"^\[([^\]]+)]\s+\[([^\]]+)]\s+\[([^\]]+)]\s+\[([^\]]+)]\s+\[([^\]]+)]\s+\[([^\]]+)]\s+\[([^\]]+)]\s+\[([^\]]+)]\s*(.*)$"
)
_ASSISTANT_EXECUTOR_BASE_REGEX = re.compile(
    r"^\[([^\]]+)]\s+\[([^\]]+)]\s+\[([^\]]+)]\s+\[([^\]]+)]\s+\[([^\]]+)]\s+\[([^\]]+)]\s*(.*)$"
)
_ASSISTANT_FUNCTION_PREFIX_REGEX = re.compile(r"^\s*([A-Za-z_~][\w:<>~.\-]*\(\))")
_ASSISTANT_EXECUTOR_FUNCTION_REGEX = re.compile(
    r"(?:=>|<=)\s*(?:\[(?:100|101)\]\s*)*\[([A-Za-z_~][\w:<>~.\-]*)\]"
)
_ASSISTANT_BRACKET_BOUNDARY_REGEX = re.compile(
    r"\[([A-Za-z_~][\w:<>~.\-]*)\]\s*\[(?:START|END)\]",
    re.IGNORECASE,
)
_ASSISTANT_START_MARKER_REGEX = re.compile(r">\s*\(\s*\)")
_ASSISTANT_END_MARKER_REGEX = re.compile(r"<\s*\(\s*\)")
_ASSISTANT_ERROR_TOKENS = ("ERROR", "FATAL", "CRITICAL", "EXCEPTION", "TRACEBACK", "FAILED")
_ASSISTANT_WARNING_TOKENS = ("WARN", "WARNING", "WRAN")


def _assistant_read_bracket_fields(text: str, limit: int = 4) -> list[tuple[str, int]]:
    fields: list[tuple[str, int]] = []
    offset = 0
    while len(fields) < limit:
        match = re.match(r"^\s*\[([^\]]+)]", text[offset:])
        if not match:
            break
        offset += match.end()
        fields.append((match.group(1).strip(), offset))
    return fields


def _assistant_looks_like_rpc(value: str) -> bool:
    return len(str(value or "").split(":")) == 3


def _assistant_executor_message(raw_line: str) -> str | None:
    base = _ASSISTANT_EXECUTOR_BASE_REGEX.match(raw_line)
    if not base:
        return None
    tail = base.group(7)
    fields = _assistant_read_bracket_fields(tail, 4)
    if len(fields) >= 2 and fields[0][0] in {"100", "101"} and _assistant_looks_like_rpc(fields[1][0]):
        return tail[fields[1][1]:].lstrip()
    if len(fields) >= 3 and _assistant_looks_like_rpc(fields[1][0]) and fields[2][0] in {"100", "101"}:
        return tail[fields[2][1]:].lstrip()
    return None


def _assistant_parse_fold_fields(raw_line: str) -> dict[str, str]:
    timestamp = ""
    level = ""
    message = raw_line.strip()
    standard = _ASSISTANT_LOG_LINE_REGEX.match(raw_line)
    if standard:
        timestamp = standard.group(1).strip()
        level = standard.group(2).strip()
        message = _assistant_executor_message(raw_line) or standard.group(9).strip()
    else:
        executor = _ASSISTANT_EXECUTOR_BASE_REGEX.match(raw_line)
        if executor:
            timestamp = executor.group(1).strip()
            level = executor.group(2).strip()
            message = _assistant_executor_message(raw_line) or executor.group(7).strip()

    function_name = ""
    conventional = _ASSISTANT_FUNCTION_PREFIX_REGEX.match(message)
    if conventional:
        function_name = conventional.group(1)
    else:
        for candidate in (message, raw_line):
            bracket = _ASSISTANT_BRACKET_BOUNDARY_REGEX.search(candidate)
            if bracket:
                function_name = f"{bracket.group(1)}()"
                break
            directional = _ASSISTANT_EXECUTOR_FUNCTION_REGEX.search(candidate)
            if directional:
                function_name = f"{directional.group(1)}()"
                break

    start_match = _ASSISTANT_START_MARKER_REGEX.search(message)
    end_match = _ASSISTANT_END_MARKER_REGEX.search(message)
    marker = "none"
    marker_match = None
    if start_match and (not end_match or start_match.start() < end_match.start()):
        marker = "start"
        marker_match = start_match
    elif end_match:
        marker = "end"
        marker_match = end_match

    boundary_function = function_name
    if marker_match:
        prefix = message[: marker_match.start()]
        bracket_names = re.findall(r"\[([A-Za-z_~][\w:<>~.\-]*)\]", prefix)
        if bracket_names:
            boundary_function = f"{bracket_names[-1]}()"

    return {
        "timestamp": timestamp,
        "level": level,
        "message": message,
        "function_name": function_name,
        "boundary_function": boundary_function,
        "marker": marker,
    }


def _assistant_log_signature(text: str) -> str:
    value = str(text or "").strip()
    value = re.sub(r"\b0x[0-9a-fA-F]+\b", "0x#", value)
    value = re.sub(r"\b[0-9a-fA-F]{16,}\b", "#HEX", value)
    value = re.sub(r"\b\d{6,}\b", "#N", value)
    value = re.sub(r"\s+", " ", value)
    return value[:360]


def _assistant_compact_log_line(parsed: dict[str, str], raw_line: str, limit: int = 700) -> str:
    message = str(parsed.get("message") or raw_line).strip()
    timestamp = str(parsed.get("timestamp") or "").strip()
    level = str(parsed.get("level") or "").strip()
    prefix = " ".join(part for part in (timestamp, level) if part and part != "—")
    compact = f"{prefix} {message}".strip()
    compact = re.sub(r"\s+", " ", compact)
    return compact[:limit]


def _assistant_new_fold_state() -> dict[str, Any]:
    return {
        "functions": {},
        "stack": [],
        "last_owner": "",
        "line_index": 0,
        "scanned_lines": 0,
        "matched_lines": 0,
        "source_markers": [],
    }


def _assistant_function_bucket(state: dict[str, Any], owner: str) -> dict[str, Any]:
    functions = state["functions"]
    if owner not in functions:
        functions[owner] = {
            "name": owner,
            "first_index": state["line_index"],
            "last_index": state["line_index"],
            "occurrences": 0,
            "starts": 0,
            "ends": 0,
            "line_count": 0,
            "matched_count": 0,
            "error_count": 0,
            "warning_count": 0,
            "first_time": "",
            "last_time": "",
            "first_line": "",
            "last_line": "",
            "samples": [],
            "sample_signatures": set(),
        }
    return functions[owner]


def _assistant_feed_folded_log_line(
    state: dict[str, Any],
    raw_line: str,
    *,
    keyword: str = "",
    errors_only: bool = False,
) -> None:
    stripped = str(raw_line or "").rstrip("\r\n")
    if not stripped:
        return
    if stripped.startswith("__TRACELENS_"):
        markers = state["source_markers"]
        if len(markers) < 12:
            markers.append(stripped[:500])
        return

    state["line_index"] += 1
    state["scanned_lines"] += 1
    parsed = _assistant_parse_fold_fields(stripped)
    marker = parsed["marker"]
    boundary_name = parsed["boundary_function"]
    function_name = parsed["function_name"]
    stack: list[str] = state["stack"]

    if marker == "start" and boundary_name:
        owner = boundary_name
        stack.append(boundary_name)
    elif marker == "end" and boundary_name:
        owner = boundary_name
    elif function_name:
        owner = function_name
    elif stack:
        owner = stack[-1]
    else:
        owner = "未归属函数"

    bucket = _assistant_function_bucket(state, owner)
    if marker == "start" and boundary_name == owner:
        bucket["starts"] += 1
        bucket["occurrences"] += 1
    elif marker == "end" and boundary_name == owner:
        bucket["ends"] += 1
    elif state["last_owner"] != owner and not (stack and owner in stack[:-1]):
        bucket["occurrences"] += 1

    bucket["line_count"] += 1
    bucket["last_index"] = state["line_index"]
    timestamp = parsed["timestamp"]
    if timestamp:
        if not bucket["first_time"]:
            bucket["first_time"] = timestamp
        bucket["last_time"] = timestamp

    upper = f"{parsed['level']} {parsed['message']}".upper()
    is_error = any(token in upper for token in _ASSISTANT_ERROR_TOKENS)
    is_warning = any(token in upper for token in _ASSISTANT_WARNING_TOKENS)
    keyword_match = bool(keyword and keyword.casefold() in stripped.casefold())
    matched = keyword_match if keyword else is_error if errors_only else True
    if matched:
        state["matched_lines"] += 1
        bucket["matched_count"] += 1
    if is_error:
        bucket["error_count"] += 1
    if is_warning:
        bucket["warning_count"] += 1

    compact_line = _assistant_compact_log_line(parsed, stripped)
    if not bucket["first_line"]:
        bucket["first_line"] = compact_line
    bucket["last_line"] = compact_line

    # Feed the model only high-density evidence. Boundaries preserve the same
    # function ownership used by the page folding UI; errors/keyword hits are
    # always preferred, normal debug keeps only a tiny representative sample.
    high_signal = matched and (keyword_match or is_error or is_warning)
    should_sample = high_signal or marker != "none" or (not errors_only and not keyword and len(bucket["samples"]) < 1)
    if should_sample:
        signature = _assistant_log_signature(compact_line)
        if signature and signature not in bucket["sample_signatures"] and len(bucket["samples"]) < 8:
            bucket["sample_signatures"].add(signature)
            bucket["samples"].append(compact_line)

    if marker == "end" and boundary_name:
        for index in range(len(stack) - 1, -1, -1):
            if stack[index] == boundary_name:
                del stack[index:]
                break
    state["last_owner"] = owner


def _assistant_fold_score(bucket: dict[str, Any], *, keyword: str, errors_only: bool) -> int:
    score = int(bucket.get("error_count") or 0) * 1000 + int(bucket.get("warning_count") or 0) * 120
    if keyword or errors_only:
        score += int(bucket.get("matched_count") or 0) * 700
    score += min(int(bucket.get("line_count") or 0), 80)
    if int(bucket.get("starts") or 0) != int(bucket.get("ends") or 0):
        score += 80
    if bucket.get("name") == "未归属函数":
        score -= 20
    return score


def _assistant_render_folded_context(
    state: dict[str, Any],
    *,
    keyword: str = "",
    errors_only: bool = False,
    max_context_chars: int = 9000,
) -> tuple[str, list[dict[str, Any]], bool, int]:
    buckets = list(state["functions"].values())
    if keyword or errors_only:
        buckets = [bucket for bucket in buckets if int(bucket.get("matched_count") or 0) > 0]
    for bucket in buckets:
        bucket["score"] = _assistant_fold_score(bucket, keyword=keyword, errors_only=errors_only)
        bucket["incomplete"] = int(bucket.get("starts") or 0) > int(bucket.get("ends") or 0)

    ranked = sorted(buckets, key=lambda row: (-int(row["score"]), int(row["first_index"])))
    selected: list[dict[str, Any]] = []
    estimated = 0
    for bucket in ranked:
        # Low-signal functions still contribute one terse line until the text
        # budget is nearly full; high-signal functions reserve more room for samples.
        sample_count = 3 if bucket["error_count"] or bucket["matched_count"] and (keyword or errors_only) else 1
        samples = list(bucket["samples"][:sample_count])
        duration = ""
        if bucket["first_time"] or bucket["last_time"]:
            duration = f" | {bucket['first_time'] or '-'}~{bucket['last_time'] or '-'}"
        summary = (
            f"[函数 {bucket['name']}] ×{max(1, int(bucket['occurrences'] or 0))}"
            f" | 行{bucket['line_count']} | ERROR {bucket['error_count']} | WARN {bucket['warning_count']}"
            f" | 命中 {bucket['matched_count']}{duration}"
            + (" | 边界未闭合" if bucket["incomplete"] else "")
        )
        detail_lines = [summary]
        for sample in samples:
            detail_lines.append(f"  · {sample}")
        block_text = "\n".join(detail_lines)
        if selected and estimated + len(block_text) + 1 > max_context_chars:
            continue
        if not selected and len(block_text) > max_context_chars:
            block_text = block_text[:max_context_chars]
        selected.append({
            "name": bucket["name"],
            "occurrences": max(1, int(bucket["occurrences"] or 0)),
            "line_count": int(bucket["line_count"] or 0),
            "matched_count": int(bucket["matched_count"] or 0),
            "error_count": int(bucket["error_count"] or 0),
            "warning_count": int(bucket["warning_count"] or 0),
            "first_time": bucket["first_time"],
            "last_time": bucket["last_time"],
            "incomplete": bool(bucket["incomplete"]),
            "samples": samples,
            "_text": block_text,
            "_first_index": int(bucket["first_index"]),
        })
        estimated += len(block_text) + 1
        if estimated >= max_context_chars:
            break

    # After priority selection, restore page-like temporal order so the model
    # still sees the execution sequence instead of a severity-sorted list.
    selected.sort(key=lambda row: row["_first_index"])
    omitted = max(0, len(buckets) - len(selected))
    header = (
        f"函数折叠摘要：扫描 {state['scanned_lines']} 行，匹配 {state['matched_lines']} 行，"
        f"识别 {len(buckets)} 个函数组，投喂 {len(selected)} 个函数组"
    )
    if omitted:
        header += f"，省略 {omitted} 个低信号函数组"
    parts = [header]
    parts.extend(str(row["_text"]) for row in selected)
    context = "\n".join(parts)
    context_truncated = omitted > 0 or len(context) > max_context_chars
    context = context[:max_context_chars]

    public_rows = []
    for row in selected:
        item = dict(row)
        item.pop("_text", None)
        item.pop("_first_index", None)
        public_rows.append(item)
    return context, public_rows, context_truncated, omitted


def query_environment_logs(payload: dict[str, Any]) -> dict[str, Any]:
    """Read component-directed logs and compress them by the same function ownership used by the UI.

    Time defines *where* to read, not how much evidence may be sent to the model.
    Size protection is based on scanned text bytes and the final folded-context
    character budget.  This avoids rejecting a useful 70-minute range merely
    because of duration while still keeping LLM context deterministic.
    """
    environment = _environment(payload.get("environment_id"))
    component_name = str(payload.get("component_name") or "").strip()
    supplied_targets = list(payload.get("fm_targets") or [])
    component_resolution: dict[str, Any] | None = None

    if not supplied_targets and component_name:
        component_resolution = _component_target_rows(component_name)
        if component_resolution.get("selection_required"):
            candidates = list(component_resolution.get("candidates") or component_resolution.get("targets") or [])[:8]
            return {
                "selection_required": True,
                "component_name": component_name,
                "environment_id": environment.id,
                "environment_name": environment.name,
                "start_time": payload.get("start_time"),
                "end_time": payload.get("end_time"),
                "source_categories": [
                    str(item).strip() for item in (payload.get("source_categories") or []) if str(item).strip()
                ] or ["debug"],
                "candidates": candidates,
                "targets": candidates,
                "reason": component_resolution.get("reason") or "存在多个日志目标，请先选择本次要查询的日志。",
            }
        if not component_resolution.get("exact_match"):
            raise ToolInputError(
                f"组件 {component_name} 没有可用的精确目标或候选。请在日志定位页面选择组件后重试。"
            )
        supplied_targets = list(component_resolution.get("targets") or [])

    if not supplied_targets:
        raise ToolInputError("日志查询缺少目标组件。请在页面选择日志目标，或传入非空 fm_targets。")

    source_categories = [
        str(item).strip() for item in (payload.get("source_categories") or []) if str(item).strip()
    ] or ["debug"]
    request_payload = {
        "start_time": payload.get("start_time"),
        "end_time": payload.get("end_time"),
        "subsystems": payload.get("subsystems") or [],
        "fms": payload.get("fms") or [],
        "fm_targets": supplied_targets,
        "source_categories": source_categories,
        "keyword": str(payload.get("keyword") or "").strip(),
    }
    serializer = LogWindowRequestSerializer(data=request_payload)
    if not serializer.is_valid():
        details = []
        for field, errors in serializer.errors.items():
            values = errors if isinstance(errors, (list, tuple)) else [errors]
            details.append(f"{field}: {'; '.join(str(item) for item in values)}")
        raise ToolInputError("日志查询参数无效：" + "；".join(details))

    start = serializer.naive(serializer.validated_data["start_time"])
    end = serializer.naive(serializer.validated_data["end_time"])
    keyword = str(serializer.validated_data.get("keyword") or "").strip()
    errors_only = bool(payload.get("errors_only", False))

    from apps.logsources.services.remote_logs import stream_log_window

    # Size-based guardrails: scan enough source text to understand the function
    # flow, then compress aggressively before the evidence reaches the LLM.
    max_scan_bytes = max(256 * 1024, min(int(payload.get("max_scan_bytes") or 12_000_000), 64_000_000))
    max_context_chars = max(2000, min(int(payload.get("max_context_chars") or 9000), 30_000))
    plan = build_log_plan(
        environment,
        start,
        end,
        serializer.validated_data["subsystems"],
        serializer.validated_data["fms"],
        serializer.validated_data["source_categories"],
        fm_targets=serializer.validated_data.get("fm_targets", []),
        operation_id="assistant-json-query",
    )
    artifact_count = len({repr(item) for item in plan})
    fold_state = _assistant_new_fold_state()
    scanned_bytes = 0
    scan_truncated = False
    carry = ""

    for chunk in stream_log_window(
        environment,
        plan,
        start,
        end,
        operation_id="-",
        include_internal_markers=True,
    ):
        if scanned_bytes >= max_scan_bytes:
            scan_truncated = True
            break
        remaining = max_scan_bytes - scanned_bytes
        piece = chunk[:remaining]
        scanned_bytes += len(piece)
        if len(piece) < len(chunk):
            scan_truncated = True
        decoded = carry + piece.decode("utf-8", errors="replace")
        parts = decoded.splitlines(keepends=True)
        carry = "" if not parts or parts[-1].endswith(("\n", "\r")) else parts.pop()
        for line in parts:
            _assistant_feed_folded_log_line(
                fold_state,
                line.rstrip("\r\n"),
                keyword=keyword,
                errors_only=errors_only,
            )
        if scan_truncated:
            break
    if carry and not scan_truncated:
        _assistant_feed_folded_log_line(
            fold_state,
            carry[:8192],
            keyword=keyword,
            errors_only=errors_only,
        )

    folded_context, function_summaries, context_truncated, omitted_function_count = _assistant_render_folded_context(
        fold_state,
        keyword=keyword,
        errors_only=errors_only,
        max_context_chars=max_context_chars,
    )

    targets = [
        {
            "subsystem": str(item.get("subsystem") or ""),
            "fm": str(item.get("fm") or item.get("module") or ""),
            "kind": str(item.get("kind") or "normal"),
        }
        for item in serializer.validated_data.get("fm_targets", [])
    ]
    source_categories = list(serializer.validated_data.get("source_categories") or [])
    start_text = start.isoformat()
    end_text = end.isoformat()
    matched_count = int(fold_state["matched_lines"])
    evidence_status = "found_evidence" if matched_count else ("no_match" if artifact_count else "source_not_found")

    # Compatibility evidence list: only the already-compressed samples are
    # returned.  The LLM uses folded_context directly, not raw log lines.
    evidence_lines: list[str] = []
    seen_evidence: set[str] = set()
    for item in function_summaries:
        for sample in item.get("samples") or []:
            value = str(sample).strip()
            signature = _assistant_log_signature(value)
            if value and signature not in seen_evidence:
                seen_evidence.add(signature)
                evidence_lines.append(value)
            if len(evidence_lines) >= 80:
                break
        if len(evidence_lines) >= 80:
            break

    return {
        "environment_id": environment.id,
        "environment_name": environment.name,
        "component_name": component_name,
        "component_exact_match": bool(component_resolution.get("exact_match")) if component_resolution else None,
        "targets": targets,
        "source_categories": source_categories,
        "start_time": start_text,
        "end_time": end_text,
        "filter_mode": "errors" if errors_only else "keyword" if keyword else "all",
        "keyword": keyword,
        "artifact_count": artifact_count,
        "line_count": matched_count,
        "evidence_line_count": matched_count,
        "scanned_line_count": int(fold_state["scanned_lines"]),
        "scanned_bytes": scanned_bytes,
        "scan_byte_budget": max_scan_bytes,
        "scan_truncated": scan_truncated,
        "compression_mode": "function_fold",
        "function_count": len(fold_state["functions"]),
        "included_function_count": len(function_summaries),
        "omitted_function_count": omitted_function_count,
        "context_char_count": len(folded_context),
        "context_char_budget": max_context_chars,
        "context_truncated": context_truncated,
        "evidence_status": evidence_status,
        "truncated": bool(scan_truncated or context_truncated),
        "function_summaries": function_summaries,
        "folded_context": folded_context,
        "lines": evidence_lines,
        "source_markers": list(fold_state["source_markers"]),
        "ui_action": {
            "type": "open_log_locator",
            "environment_id": environment.id,
            "environment_name": environment.name,
            "start_time": start_text,
            "end_time": end_text,
            "source_categories": source_categories,
            "fm_targets": targets,
            "keyword": keyword,
            "errors_only": errors_only,
            "auto_search": True,
            "takeover_focus": "logs",
        },
    }

def list_log_query_skills(payload: dict[str, Any]) -> dict[str, Any]:
    """Return subsystem-scoped diagnostic retrieval skills in compact form."""
    queryset = LogQuerySkill.objects.select_related("subsystem").filter(enabled=True)
    subsystem = str(payload.get("subsystem") or "").strip()
    if subsystem:
        queryset = queryset.filter(
            Q(subsystem__name__iexact=subsystem) | Q(subsystem__display_name__iexact=subsystem)
        )
    query = str(payload.get("query") or "").strip().casefold()
    rows = []
    for skill in queryset.order_by("subsystem__sort_order", "subsystem__name", "-priority", "name")[:200]:
        item = {
            "id": skill.id,
            "name": skill.name,
            "subsystem": skill.subsystem.name,
            "subsystem_display_name": skill.subsystem.display_name,
            "priority": skill.priority,
            "trigger_modules": list(skill.trigger_modules or [])[:16],
            "trigger_keywords": list(skill.trigger_keywords or [])[:24],
            "description": str(skill.description or "")[:800],
            "step_count": len(skill.steps or []),
            "steps": list(skill.steps or [])[:8],
        }
        if query and query not in " ".join([
            item["name"], item["subsystem"], item["subsystem_display_name"], item["description"],
            *item["trigger_modules"], *item["trigger_keywords"],
        ]).casefold():
            continue
        rows.append(item)
    limit = max(1, min(int(payload.get("limit") or 40), 100))
    return {"count": len(rows), "skills": rows[:limit], "subsystem": subsystem}


def match_log_query_skills(payload: dict[str, Any]) -> dict[str, Any]:
    subsystem = str(payload.get("subsystem") or "").strip()
    if not subsystem:
        raise ToolInputError("匹配日志查询 Skill 必须提供 subsystem；Skill 严格按子系统隔离。")
    module = str(payload.get("module") or "").strip()
    text = str(payload.get("evidence_text") or payload.get("text") or "").strip()
    matches = match_query_skills(
        subsystem=subsystem,
        module=module,
        text=text,
        limit=max(1, min(int(payload.get("limit") or 6), 20)),
    )
    return {
        "subsystem": subsystem,
        "module": module,
        "count": len(matches),
        "matches": matches,
    }


def create_log_query_skill(payload: dict[str, Any]) -> dict[str, Any]:
    """Create a subsystem-bound diagnostic skill from a user/AI supplied strategy."""
    raw = payload.get("skill")
    if not isinstance(raw, dict):
        raise ToolInputError("skill 必须是日志查询 Skill 对象。")
    subsystem_name = str(raw.get("subsystem") or raw.get("subsystem_name") or "").strip()
    subsystem_id = raw.get("subsystem_id")
    subsystem = None
    if subsystem_id not in (None, ""):
        try:
            subsystem = LogSubsystemDefinition.objects.get(pk=int(subsystem_id))
        except (ValueError, LogSubsystemDefinition.DoesNotExist) as exc:
            raise ToolInputError("指定的子系统不存在。") from exc
    elif subsystem_name:
        subsystem = LogSubsystemDefinition.objects.filter(
            Q(name__iexact=subsystem_name) | Q(display_name__iexact=subsystem_name)
        ).order_by("id").first()
    if subsystem is None:
        raise ToolInputError("创建日志查询 Skill 必须明确绑定一个已存在的子系统。")
    serializer = LogQuerySkillSerializer(data={
        "subsystem": subsystem.id,
        "name": raw.get("name"),
        "enabled": raw.get("enabled", True),
        "priority": raw.get("priority", 100),
        "trigger_modules": raw.get("trigger_modules") or [],
        "trigger_keywords": raw.get("trigger_keywords") or [],
        "description": raw.get("description") or "",
        "steps": raw.get("steps") or [],
    })
    if not serializer.is_valid():
        detail = "；".join(
            f"{field}: {'; '.join(str(item) for item in (errors if isinstance(errors, (list, tuple)) else [errors]))}"
            for field, errors in serializer.errors.items()
        )
        raise ToolInputError("日志查询 Skill 参数无效：" + detail)
    skill = serializer.save()
    return {
        "status": "created",
        "skill": LogQuerySkillSerializer(skill).data,
        "ui_action": {"type": "open_log_rule_settings", "tab": "query-skill"},
    }


def query_log_query_skill_step(payload: dict[str, Any]) -> dict[str, Any]:
    environment = _environment(payload.get("environment_id"))
    try:
        skill = LogQuerySkill.objects.select_related("subsystem").get(pk=int(payload.get("skill_id")))
    except (TypeError, ValueError, LogQuerySkill.DoesNotExist) as exc:
        raise ToolInputError("日志查询 Skill 不存在。") from exc
    if not skill.enabled:
        raise ToolInputError("日志查询 Skill 已停用。")
    step_index = int(payload.get("step_index") or 0)
    steps = list(skill.steps or [])
    if step_index < 0 or step_index >= len(steps):
        raise ToolInputError("日志查询 Skill 步骤不存在。")
    step = steps[step_index] if isinstance(steps[step_index], dict) else {}
    start = _parse_assistant_datetime(payload.get("start_time"), "start_time")
    end = _parse_assistant_datetime(payload.get("end_time"), "end_time")
    if end < start:
        raise ToolInputError("end_time 不能早于 start_time。")
    source_type = str(step.get("source_type") or "standard")
    if source_type == "standard":
        module = str(step.get("module") or payload.get("module") or "").strip()
        if not module:
            raise ToolInputError("标准日志 Skill 步骤必须配置 module。")
        result = query_environment_logs({
            "environment_id": environment.id,
            "start_time": (start - timedelta(seconds=max(0, min(int(step.get("time_before_seconds") or 5), 600)))).isoformat(),
            "end_time": (end + timedelta(seconds=max(0, min(int(step.get("time_after_seconds") or 5), 600)))).isoformat(),
            "source_categories": [str(step.get("source_category") or "debug")],
            "fm_targets": [{"subsystem": skill.subsystem.name, "fm": module, "kind": "normal"}],
            "max_context_chars": max(2000, min(int(payload.get("max_context_chars") or 9000), 30000)),
        })
        keywords = [str(value).strip() for value in (step.get("keywords") or []) if str(value).strip()]
        if keywords:
            markers = [line for line in result.get("lines", []) if str(line).startswith("__TRACELENS_")]
            matched = [
                line for line in result.get("lines", [])
                if not str(line).startswith("__TRACELENS_")
                and any(keyword.casefold() in str(line).casefold() for keyword in keywords)
            ]
            result["lines"] = [*markers, *matched]
            result["evidence_line_count"] = len(matched)
            result["line_count"] = len(result["lines"])
            result["evidence_status"] = "found_evidence" if matched else ("no_match" if result.get("artifact_count") else "source_not_found")
        next_step = steps[step_index + 1] if step_index + 1 < len(steps) and isinstance(steps[step_index + 1], dict) else None
        result.update({
            "skill_id": skill.id,
            "skill_name": skill.name,
            "skill_step_index": step_index,
            "skill_step_name": str(step.get("name") or f"步骤 {step_index + 1}"),
            "skill_step_count": len(steps),
            "next_step": ({
                "index": step_index + 1,
                "name": str(next_step.get("name") or f"步骤 {step_index + 2}"),
                "when": str(next_step.get("when") or "insufficient_evidence"),
                "source_type": str(next_step.get("source_type") or "standard"),
            } if next_step else None),
            "subsystem": skill.subsystem.name,
            "source_type": "standard",
        })
        return result

    try:
        result = query_skill_step(
            environment=environment,
            skill=skill,
            step_index=step_index,
            start=start,
            end=end,
            max_lines=max(20, min(int(payload.get("max_lines") or 160), 500)),
        )
    except LogQuerySkillError as exc:
        raise ToolInputError(str(exc)) from exc
    next_step = steps[step_index + 1] if step_index + 1 < len(steps) and isinstance(steps[step_index + 1], dict) else None
    result.update({
        "evidence_status": str(result.get("status") or ""),
        "skill_step_index": step_index,
        "skill_step_name": str(step.get("name") or f"步骤 {step_index + 1}"),
        "skill_step_count": len(steps),
        "next_step": ({
            "index": step_index + 1,
            "name": str(next_step.get("name") or f"步骤 {step_index + 2}"),
            "when": str(next_step.get("when") or "insufficient_evidence"),
            "source_type": str(next_step.get("source_type") or "standard"),
        } if next_step else None),
        "source_type": "custom_path",
    })
    return result



def list_log_semantic_rules(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a compact catalog of configured semantic/label rules for Agent selection."""
    settings_obj = ResourceSettings.get_solo()
    rows = settings_obj.display_rules if isinstance(settings_obj.display_rules, list) else []
    enabled_only = bool(payload.get("enabled_only", True))
    compact = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if enabled_only and row.get("enabled") is False:
            continue
        compact.append({
            "id": str(row.get("id") or ""),
            "name": str(row.get("name") or row.get("keyword") or "未命名规则"),
            "enabled": row.get("enabled") is not False,
            "scope": str(row.get("scope") or "log"),
            "mode": str(row.get("displayMode") or row.get("display_mode") or "semantic"),
            "keyword": str(row.get("keyword") or "")[:160],
            "semantic": str(row.get("displayTemplate") or row.get("display_template") or row.get("supplementalDescription") or row.get("supplemental_description") or "")[:260],
            "label": str(row.get("customLabelTemplate") or row.get("custom_label_template") or "")[:160],
        })
    query = str(payload.get("query") or "").strip().casefold()
    if query:
        compact = [row for row in compact if query in " ".join(str(v) for v in row.values()).casefold()]
    limit = min(max(int(payload.get("limit") or 50), 1), 100)
    return {"count": len(compact), "rules": compact[:limit]}


def list_data_extraction_rules(payload: dict[str, Any]) -> dict[str, Any]:
    """Return compact extraction capabilities; never send full templates/samples to the LLM."""
    settings_obj = ResourceSettings.get_solo()
    rows = settings_obj.data_extraction_rules if isinstance(settings_obj.data_extraction_rules, list) else []
    enabled_only = bool(payload.get("enabled_only", True))
    wanted_fields = {str(item).strip().casefold() for item in (payload.get("field_names") or []) if str(item).strip()}
    query = str(payload.get("query") or "").strip().casefold()
    compact = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if enabled_only and row.get("enabled") is False:
            continue
        fields = []
        for field in row.get("fields") or []:
            if not isinstance(field, dict):
                continue
            name = str(field.get("name") or field.get("key") or "").strip()
            key = str(field.get("key") or name).strip()
            if not key and not name:
                continue
            fields.append({
                "key": key,
                "name": name or key,
                "type": str(field.get("valueType") or field.get("value_type") or "string"),
                "unit": str(field.get("plotUnit") or field.get("plot_unit") or field.get("sourceUnit") or field.get("source_unit") or "source"),
            })
        field_tokens = {str(item.get("key") or "").casefold() for item in fields} | {str(item.get("name") or "").casefold() for item in fields}
        if wanted_fields and not wanted_fields.issubset(field_tokens):
            continue
        item = {
            "id": str(row.get("id") or ""),
            "name": str(row.get("name") or "未命名提取器"),
            "description": str(row.get("description") or "")[:220],
            "enabled": row.get("enabled") is not False,
            "keyword": str(row.get("matchKeyword") or row.get("match_keyword") or "")[:120],
            "sources": [str(x) for x in (row.get("sourceCategories") or row.get("source_categories") or [])][:8],
            "subsystems": [str(x) for x in (row.get("subsystems") or [])][:8],
            "modules": [str(x) for x in (row.get("modules") or [])][:12],
            "fields": fields[:24],
        }
        if query and query not in " ".join([item["name"], item["description"], item["keyword"], *(f["name"] for f in fields), *(f["key"] for f in fields)]).casefold():
            continue
        compact.append(item)
    limit = min(max(int(payload.get("limit") or 30), 1), 60)
    return {"count": len(compact), "rules": compact[:limit]}


def set_log_semantic_labels(payload: dict[str, Any]) -> dict[str, Any]:
    enabled = bool(payload.get("enabled", True))
    return {"ui_action": {"type": "set_log_semantic_labels", "enabled": enabled}}


def run_data_extraction(payload: dict[str, Any]) -> dict[str, Any]:
    rule_ids = [str(item).strip() for item in (payload.get("rule_ids") or []) if str(item).strip()]
    field_names = [str(item).strip() for item in (payload.get("field_names") or []) if str(item).strip()]
    if not rule_ids and not field_names:
        raise ToolInputError("请至少提供 rule_ids 或 field_names。可先调用 list_data_extraction_rules 选择已有数据提取器。")
    return {
        "ui_action": {
            "type": "run_data_extraction",
            "rule_ids": rule_ids,
            "field_names": field_names,
            "auto_start": bool(payload.get("auto_start", True)),
            "open_data_page": bool(payload.get("open_data_page", True)),
        }
    }



def create_data_extraction_capability(payload: dict[str, Any]) -> dict[str, Any]:
    """Create a validated-on-frontend extraction capability from real log evidence.

    The LLM provides only a semantic candidate (field keys/sample values/scope).
    TraceLens frontend converts it to the native DataExtractionRule, validates the
    candidate against currently loaded parsed log entries, persists it only when
    it matches, then optionally starts extraction.
    """
    rule = payload.get("rule")
    if not isinstance(rule, dict):
        raise ToolInputError("rule 必须是数据提取规则对象。")
    fields = rule.get("fields")
    if not isinstance(fields, list) or not fields:
        raise ToolInputError("rule.fields 至少需要一个目标字段。")
    compact_fields: list[dict[str, Any]] = []
    for index, field in enumerate(fields[:32]):
        if not isinstance(field, dict):
            raise ToolInputError(f"rule.fields[{index}] 必须是对象。")
        key = str(field.get("key") or field.get("name") or "").strip()
        name = str(field.get("name") or key).strip()
        if not key or not name:
            raise ToolInputError(f"rule.fields[{index}] 缺少 key/name。")
        value_type = str(field.get("value_type") or field.get("valueType") or "").strip().lower()
        if value_type and value_type not in {"number", "integer", "boolean", "string"}:
            raise ToolInputError(f"rule.fields[{index}].value_type 仅支持 number/integer/boolean/string。")
        structured_path = field.get("structured_path") or field.get("structuredPath") or []
        if structured_path and not isinstance(structured_path, list):
            raise ToolInputError(f"rule.fields[{index}].structured_path 必须是字符串数组。")
        compact_fields.append({
            "key": key[:160],
            "name": name[:160],
            "sample_value": str(field.get("sample_value") or field.get("sampleValue") or "")[:500],
            "value_type": value_type,
            "source_unit": str(field.get("source_unit") or field.get("sourceUnit") or "auto")[:64],
            "plot_unit": str(field.get("plot_unit") or field.get("plotUnit") or "source")[:64],
            "structured_path": [str(item).strip() for item in structured_path[:12] if str(item).strip()],
            "structured_root_hint": str(field.get("structured_root_hint") or field.get("structuredRootHint") or "")[:120],
        })
    sample_message = str(rule.get("sample_message") or rule.get("sampleMessage") or "")
    if not sample_message:
        raise ToolInputError("请从真实日志证据中提供 rule.sample_message，用于前端验证并生成稳定提取模板。")
    normalized = {
        "name": str(rule.get("name") or "AI 自动数据提取").strip()[:120] or "AI 自动数据提取",
        "description": str(rule.get("description") or "TracePilot 根据当前日志样例自动生成").strip()[:400],
        "match_keyword": str(rule.get("match_keyword") or rule.get("matchKeyword") or "")[:240],
        "case_sensitive": bool(rule.get("case_sensitive") or rule.get("caseSensitive", False)),
        "source_categories": [str(item).strip() for item in (rule.get("source_categories") or rule.get("sourceCategories") or [])[:8] if str(item).strip()],
        "subsystems": [str(item).strip() for item in (rule.get("subsystems") or [])[:8] if str(item).strip()],
        "modules": [str(item).strip() for item in (rule.get("modules") or [])[:12] if str(item).strip()],
        "sample_message": sample_message[:8000],
        "fields": compact_fields,
    }
    return {
        "status": "candidate_ready",
        "field_count": len(compact_fields),
        "fields": [item["name"] for item in compact_fields],
        "persist_rule": bool(payload.get("persist_rule", True)),
        "auto_start": bool(payload.get("auto_start", True)),
        "open_data_page": bool(payload.get("open_data_page", True)),
        "open_rule_settings": bool(payload.get("open_rule_settings", False)),
        "min_matches": max(1, min(int(payload.get("min_matches") or 1), 100)),
        "ui_action": {
            "type": "create_data_extraction_capability",
            "rule": normalized,
            "persist_rule": bool(payload.get("persist_rule", True)),
            "auto_start": bool(payload.get("auto_start", True)),
            "open_data_page": bool(payload.get("open_data_page", True)),
            "open_rule_settings": bool(payload.get("open_rule_settings", False)),
            "min_matches": max(1, min(int(payload.get("min_matches") or 1), 100)),
        },
    }

def create_log_semantic_rule(payload: dict[str, Any]) -> dict[str, Any]:
    """Create a semantic-rule candidate for the live browser rule editor/store.

    The backend deliberately returns a normalized UI action instead of writing the
    browser-managed rule store itself.  The frontend validates the native
    DisplayRule shape before persisting it and can show the created rule in the
    existing Log Rules page.
    """
    rule = payload.get("rule")
    if not isinstance(rule, dict):
        raise ToolInputError("rule 必须是语义规则对象。")
    kind = str(rule.get("kind") or "keyword").strip().lower()
    if kind not in {"keyword", "template"}:
        raise ToolInputError("rule.kind 仅支持 keyword 或 template。")
    scope = str(rule.get("scope") or "log").strip().lower()
    if scope not in {"function", "log", "both"}:
        raise ToolInputError("rule.scope 仅支持 function/log/both。")
    display_mode = str(rule.get("display_mode") or rule.get("displayMode") or "semantic").strip().lower()
    if display_mode not in {"semantic", "label", "both"}:
        raise ToolInputError("rule.display_mode 仅支持 semantic/label/both。")
    keyword = str(rule.get("keyword") or "").strip()
    sample_message = str(rule.get("sample_message") or rule.get("sampleMessage") or "").strip()
    if kind == "keyword" and not keyword:
        raise ToolInputError("keyword 语义规则必须提供 rule.keyword。")
    if kind == "template" and not sample_message:
        raise ToolInputError("template 语义规则必须提供真实日志 rule.sample_message。")
    display_template = str(rule.get("display_template") or rule.get("displayTemplate") or "").strip()
    custom_label = str(rule.get("custom_label_template") or rule.get("customLabelTemplate") or "").strip()
    if display_mode != "label" and not display_template:
        raise ToolInputError("语义规则需要 display_template。")
    if display_mode != "semantic" and not custom_label:
        raise ToolInputError("标签规则需要 custom_label_template。")
    parameters = []
    for index, item in enumerate((rule.get("parameters") or [])[:24]):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("name") or f"参数{index + 1}").strip()
        sample_value = str(item.get("sample_value") or item.get("sampleValue") or "").strip()
        if not label or not sample_value:
            continue
        parameters.append({"label": label[:80], "sample_value": sample_value[:500]})
    if kind == "template" and not parameters:
        raise ToolInputError("template 语义规则至少需要一个来自真实日志的 parameters 参数样例。")
    normalized = {
        "name": str(rule.get("name") or "AI 语义规则").strip()[:120] or "AI 语义规则",
        "enabled": bool(rule.get("enabled", True)),
        "kind": kind,
        "scope": scope,
        "keyword": keyword[:300],
        "display_template": display_template[:800],
        "display_mode": display_mode,
        "custom_label_template": custom_label[:400],
        "custom_label_color": str(rule.get("custom_label_color") or rule.get("customLabelColor") or "#2563eb")[:32],
        "show_label_on_timeline": bool(rule.get("show_label_on_timeline", rule.get("showLabelOnTimeline", True))),
        "supplemental_description": str(rule.get("supplemental_description") or rule.get("supplementalDescription") or "")[:1200],
        "sample_message": sample_message[:8000],
        "parameters": parameters,
    }
    return {
        "status": "candidate_ready",
        "rule_name": normalized["name"],
        "kind": kind,
        "scope": scope,
        "ui_action": {
            "type": "create_log_semantic_rule",
            "rule": normalized,
            "open_settings": bool(payload.get("open_settings", True)),
        },
    }


def bulk_generate_log_rules(payload: dict[str, Any]) -> dict[str, Any]:
    """批量生成日志**语义规则 / 标签规则**（用户可选），返回一次性的前端应用动作。

    两条分组路线（用户原话）：按**函数方法**批量建，或把**同类特征**的日志挑出来一起建。
    分组与候选配置是确定性的（``log_rule_batch``）；模型只负责把每组写成中文语义/标签，
    模型失败就用函数名/同类特征兜底 —— 批量生成不允许因为模型抽风而整批失败。

    这里**不直接写库**：返回 ``apply_log_display_rules`` 动作，由前端按原生 DisplayRule
    校验、去重后保存并立刻渲染（和单条 ``create_log_semantic_rule`` 同一条路径）。
    """
    from apps.tooling import log_rule_batch
    from apps.tooling.rule_autoconfig import autoconfigure_rule_batch

    mode = str(payload.get("mode") or "").strip().lower()
    if mode not in {"semantic", "label", "both"}:
        raise ToolInputError("请先确定要生成「语义」还是「标签」（mode 只能是 semantic / label / both）。")
    raw_samples = payload.get("samples")
    if isinstance(raw_samples, str):
        raw_samples = [line for line in raw_samples.splitlines() if line.strip()]
    if not isinstance(raw_samples, list) or not raw_samples:
        raise ToolInputError("samples 需要给出参与分析的日志行（来自本轮真实日志证据）。")

    samples = [item for item in (log_rule_batch.parse_sample(entry) for entry in raw_samples[:400]) if item is not None]
    if not samples:
        raise ToolInputError("没有从 samples 里认出任何日志行，请传原始日志文本。")

    group_by = str(payload.get("group_by") or "similar").strip().lower()
    groups = log_rule_batch.group_samples(samples, group_by="function" if group_by == "function" else "similar")
    if not groups:
        raise ToolInputError("这批日志没有形成可用的分组。")

    warnings: list[str] = []
    suggestions, llm_warnings = autoconfigure_rule_batch(
        [
            {
                "label": group.label,
                "function_name": group.function_name,
                "component": group.component,
                "count": group.count,
                "parameters": group.parameters,
                "members": group.members,
            }
            for group in groups
        ],
        mode=mode,
    )
    warnings.extend(llm_warnings)

    candidates = log_rule_batch.build_candidates(groups, mode=mode, suggestions=suggestions)
    existing = [str(item).strip() for item in (payload.get("existing_keywords") or []) if str(item).strip()]
    candidates, dropped = log_rule_batch.dedupe_candidates(candidates, existing)
    limit = max(1, min(int(payload.get("limit") or 12), log_rule_batch.MAX_GROUPS))
    if len(candidates) > limit:
        warnings.append(f"分组较多，本次先给前 {limit} 条规则（共识别 {len(candidates)} 组）。")
        candidates = candidates[:limit]
    if dropped:
        warnings.append(f"已跳过 {dropped} 条与现有规则重复的候选。")
    if not candidates:
        raise ToolInputError("这批日志与现有规则重复，没有新规则可建。")

    semantic_count = sum(1 for item in candidates if item.get("display_template"))
    label_count = sum(1 for item in candidates if item.get("custom_label_template"))
    group_label = "按函数方法" if group_by == "function" else "按同类特征"
    mode_label = {"semantic": "语义说明", "label": "标签", "both": "语义说明 + 标签"}[mode]
    summary = (
        f"{group_label}识别出 {len(groups)} 组日志，已生成 {len(candidates)} 条{mode_label}规则"
        f"（{semantic_count} 条含语义、{label_count} 条含标签），保存后立刻生效。"
    )
    if warnings:
        summary += " 注意：" + "；".join(warnings[:2])
    return {
        "status": "candidates_ready",
        "mode": mode,
        "group_by": group_by,
        "group_count": len(groups),
        "rule_count": len(candidates),
        "summary": summary,
        "warnings": warnings[:6],
        "rules": [
            {
                "name": item["name"],
                "kind": item["kind"],
                "scope": item["scope"],
                "keyword": item["keyword"],
                "display_mode": item["display_mode"],
                "display_template": item["display_template"],
                "custom_label_template": item["custom_label_template"],
                "custom_label_color": item["custom_label_color"],
                "sample_message": item["sample_message"],
                "parameters": item["parameters"],
                "group": item["group"],
            }
            for item in candidates
        ],
        "ui_action": {
            "type": "apply_log_display_rules",
            "mode": mode,
            "rules": candidates,
            "open_settings": bool(payload.get("open_settings", False)),
            "summary": summary,
        },
    }


def create_log_anomaly_rule(payload: dict[str, Any]) -> dict[str, Any]:
    keywords = [str(item).strip() for item in (payload.get("keywords") or []) if str(item).strip()]
    if not keywords:
        one = str(payload.get("keyword") or "").strip()
        if one:
            keywords = [one]
    keywords = list(dict.fromkeys(keywords))[:32]
    if not keywords:
        raise ToolInputError("请至少提供一个来自真实日志证据的异常关键字。")
    return {
        "status": "candidate_ready",
        "keywords": keywords,
        "ui_action": {
            "type": "create_log_anomaly_rule",
            "keywords": keywords,
            "case_sensitive": bool(payload.get("case_sensitive", False)),
            "whole_word": bool(payload.get("whole_word", False)),
            "enabled": bool(payload.get("enabled", True)),
            "open_settings": bool(payload.get("open_settings", True)),
        },
    }


def open_log_rule_settings(payload: dict[str, Any]) -> dict[str, Any]:
    tab = str(payload.get("tab") or "semantic").strip().lower()
    if tab not in {"semantic", "anomaly", "data", "query-skill"}:
        raise ToolInputError("tab 仅支持 semantic、anomaly、data 或 query-skill。")
    return {"ui_action": {"type": "open_log_rule_settings", "tab": tab}}

def open_workspace_page(payload: dict[str, Any]) -> dict[str, Any]:
    page = str(payload.get("page") or "").strip()
    allowed = {"resources", "logs", "atlog", "data", "knowledge", "audit", "platform-settings", "tools"}
    if page not in allowed:
        raise ToolInputError("page 不支持。")
    return {"ui_action": {"type": "open_workspace_page", "page": page}}


def open_environment_page(payload: dict[str, Any]) -> dict[str, Any]:
    environment = _environment(payload.get("environment_id"))
    return {
        "ui_action": {
            "type": "open_environment_page",
            "environment_id": environment.id,
            "environment_name": environment.name,
            "open_deployment": bool(payload.get("open_deployment", False)),
        }
    }


def open_log_locator(payload: dict[str, Any]) -> dict[str, Any]:
    environment = _environment(payload.get("environment_id"))
    action: dict[str, Any] = {
        "type": "open_log_locator",
        "environment_id": environment.id,
        "environment_name": environment.name,
    }
    for key in ("start_time", "end_time", "keyword", "source_categories", "fm_targets"):
        if payload.get(key) not in (None, "", []):
            action[key] = payload.get(key)

    # 用户直接点名的组件（“看下 cpfr 的日志”）：页面只认 (subsystem, fm) 这种机器可读目标，
    # 而模型通常只知道用户说的名字，所以这里先按组件名解析成目标再下发 —— 否则页面打开后
    # 组件选择框还是空的，用户还得自己再选一次，等于 AI 并没有真的「操作」页面。
    component_name = str(payload.get("component_name") or "").strip()
    if component_name:
        resolution = _component_target_rows(component_name)
        targets = [
            {"subsystem": item.get("subsystem"), "fm": item.get("fm"), "kind": item.get("kind") or "normal"}
            for item in (resolution.get("targets") or [])
            if item.get("subsystem") and item.get("fm")
        ]
        action["component_name"] = component_name
        if not targets:
            return {
                "component_name": component_name,
                "environment_id": environment.id,
                "found": False,
                "selection_required": False,
                "message": f"组件「{component_name}」没有配置任何日志目标，无法自动选择；请让用户确认组件名。",
            }
        if resolution.get("selection_required") or resolution.get("ambiguous"):
            # 多候选时绝不替用户决定：把候选交回模型，由模型问用户选哪个。
            return {
                "component_name": component_name,
                "environment_id": environment.id,
                "found": True,
                "ambiguous": True,
                "selection_required": True,
                "candidates": targets[:8],
                "message": f"组件「{component_name}」匹配到多个日志目标，需要用户选择其中一个。",
            }
        action["fm_targets"] = targets
        action["resolved_targets"] = targets
    return {"ui_action": action}

