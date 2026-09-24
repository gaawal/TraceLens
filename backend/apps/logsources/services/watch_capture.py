"""实时监听 / 实时采集 — the one place that decides what the server watches.

Two kinds of watch share every mechanical part (targets, hit recording, SSE, leases) and
differ in exactly two ways: which rule list they read, and how a rule compiles into a matcher.

* ``semantic``   — a log semantics rule (``ResourceSettings.display_rules``) drives the timeline
                   ribbon: "tell me when this symptom appears".
* ``extraction`` — a data extractor (``ResourceSettings.data_extraction_rules``) drives the
                   collector panel: "stream this number for me".

Both the workspace toggle and the AI assistant go through this module, so ticking a checkbox
and saying "实时采集下 xxx 数据" produce exactly the same server state, and neither kind can
drift into the other's surface.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from apps.environments.models import Environment, ResourceSettings
from apps.logsources.models import LogWatch, LogWatchLevel, LogWatchTriggerKind
from apps.logsources.services.watch_rules import compile_display_rule, compile_extraction_rule

logger = logging.getLogger("tracelens.logsources.watch_capture")


@dataclass(frozen=True)
class WatchSource:
    """How one kind of watch reads and writes its rules."""

    kind: str
    settings_field: str
    watch_field: str
    opt_in_field: str
    name_prefix: str
    compile: Callable[[Any], tuple[Any, str]]
    label: str


SOURCES: dict[str, WatchSource] = {
    "semantic": WatchSource(
        kind="semantic",
        settings_field="display_rules",
        watch_field="source_rule_id",
        opt_in_field="liveWatch",
        name_prefix="实时监听 · ",
        compile=compile_display_rule,
        label="语义规则",
    ),
    "extraction": WatchSource(
        kind="extraction",
        settings_field="data_extraction_rules",
        watch_field="extraction_rule_id",
        opt_in_field="liveCapture",
        name_prefix="实时采集 · ",
        compile=compile_extraction_rule,
        label="数据提取器",
    ),
}


def source_of(kind: str) -> WatchSource:
    return SOURCES.get(str(kind or "").strip()) or SOURCES["extraction"]


# ------------------------------------------------------------------ rule access
def rules_of(kind: str) -> list[dict[str, Any]]:
    """The shared rule list for one kind, filtered to objects with an id."""
    source = source_of(kind)
    raw = getattr(ResourceSettings.get_solo(), source.settings_field, None) or []
    return [
        item for item in raw
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]


# Kept for the existing extraction-only callers and tests.
def extraction_rules() -> list[dict[str, Any]]:
    return rules_of("extraction")


def live_rule_ids(kind: str) -> list[str]:
    source = source_of(kind)
    return [
        str(rule["id"]) for rule in rules_of(kind)
        if rule.get("enabled", True) is not False and rule.get(source.opt_in_field) is True
    ]


def live_capture_rule_ids() -> list[str]:
    return live_rule_ids("extraction")


def live_semantic_rule_ids() -> list[str]:
    return live_rule_ids("semantic")


def find_rules(kind: str, selectors: Iterable[str]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Resolve ids, names or keywords to rules.

    Accepts a name fragment so "实时采集下光源功率数据" works without the model knowing an id —
    but an unmatched fragment is reported rather than silently picking something.
    """
    wanted = [str(item).strip() for item in (selectors or []) if str(item).strip()]
    if not wanted:
        return [], []
    candidates = rules_of(kind)
    matched: list[dict[str, Any]] = []
    problems: list[dict[str, str]] = []
    for selector in wanted:
        needle = selector.casefold()
        exact = [
            rule for rule in candidates
            if str(rule.get("id") or "").casefold() == needle or str(rule.get("name") or "").casefold() == needle
        ]
        if exact:
            for rule in exact:
                if rule not in matched:
                    matched.append(rule)
            continue
        partial = [
            rule for rule in candidates
            if needle in str(rule.get("name") or "").casefold()
            or needle in str(rule.get("keyword") or rule.get("matchKeyword") or "").casefold()
        ]
        if partial:
            for rule in partial:
                if rule not in matched:
                    matched.append(rule)
            continue
        problems.append({"selector": selector, "reason": f"没有匹配的{source_of(kind).label}"})
    return matched, problems


def find_extraction_rules(selectors: Iterable[str]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    return find_rules("extraction", selectors)


def set_opt_in(kind: str, rule_ids: Iterable[str], enabled: bool) -> list[str]:
    """Flip the opt-in flag on the shared rule list. Returns the ids that changed."""
    source = source_of(kind)
    wanted = {str(item).strip() for item in (rule_ids or []) if str(item).strip()}
    if not wanted:
        return []
    settings_obj = ResourceSettings.get_solo()
    changed: list[str] = []
    next_rules: list[Any] = []
    for rule in (getattr(settings_obj, source.settings_field, None) or []):
        if not isinstance(rule, dict):
            next_rules.append(rule)
            continue
        rule_id = str(rule.get("id") or "")
        if rule_id in wanted and bool(rule.get(source.opt_in_field) is True) is not enabled:
            rule = {**rule, source.opt_in_field: enabled}
            changed.append(rule_id)
        next_rules.append(rule)
    if changed:
        setattr(settings_obj, source.settings_field, next_rules)
        settings_obj.save(update_fields=[source.settings_field, "updated_at"])
    return changed


def set_live_capture(rule_ids: Iterable[str], enabled: bool) -> list[str]:
    return set_opt_in("extraction", rule_ids, enabled)


def set_live_watch(rule_ids: Iterable[str], enabled: bool) -> list[str]:
    return set_opt_in("semantic", rule_ids, enabled)


# ------------------------------------------------------------------ watch lifecycle
def _upsert_watch(
    environment: Environment,
    source: WatchSource,
    rule: dict[str, Any],
    compiled: Any,
    *,
    existing: LogWatch | None,
    names_in_use: set[str],
    targets: list[dict[str, Any]],
    source_categories: list[str],
    target_rows: int,
    stop_with_monitoring: bool,
) -> LogWatch:
    name = f"{source.name_prefix}{compiled.name}"
    if existing is None and name in names_in_use:
        # (environment, name) is unique. Two rules can legitimately share a display name, and
        # crashing the whole sync with an IntegrityError is not an acceptable answer — and
        # silently adopting the other rule's watch would be worse. Disambiguate instead.
        name = f"{name} · {str(rule['id'])[:8]}"
    watch = existing or LogWatch(environment=environment, name=name)
    setattr(watch, source.watch_field, str(rule["id"]))
    watch.name = name
    watch.level = LogWatchLevel.OBSERVE
    previous = dict(existing.capture_config or {}) if existing else {}
    if existing is None:
        # Only the first creation picks a trigger; editing targets must not silently reset a
        # trigger the user configured on the watch (burst/silence thresholds).
        watch.trigger_kind = LogWatchTriggerKind.APPEAR
        watch.trigger_config = {}
    watch.targets = targets
    watch.source_categories = source_categories
    watch.capture_config = {
        "mode": source.kind,
        source.watch_field: str(rule["id"]),
        "target_rows": target_rows,
        "stop_with_monitoring": stop_with_monitoring,
        # A semantic rule may be configured to keep surrounding lines; extraction works off
        # the single matched line. Preserving the existing values keeps a re-sync additive.
        "before_lines": int(previous.get("before_lines") or 0),
        "cooldown_seconds": int(previous.get("cooldown_seconds") or 0),
        "hourly_quota": int(previous.get("hourly_quota") or 0),
    }
    watch.enabled = True
    watch.save()
    return watch


def sync_watches(
    environment: Environment,
    *,
    kind: str = "extraction",
    rule_ids: Iterable[str],
    targets: list[dict[str, Any]],
    source_categories: list[str] | None = None,
    enable: bool = True,
    target_rows: int = 0,
    stop_with_monitoring: bool = True,
) -> dict[str, Any]:
    """Create/update the watches for the opted-in rules, disable the rest.

    Disabling matters as much as enabling: an orphaned watcher keeps an SSH tail open for
    data nobody asked for. Only watches of this ``kind`` are touched, so turning extraction
    off never stops the timeline ribbon (and vice versa).
    """
    source = source_of(kind)
    requested = [str(item).strip() for item in (rule_ids or []) if str(item).strip()]
    clean_targets = [
        {
            "subsystem": str(item.get("subsystem") or "").strip(),
            "fm": str(item.get("fm") or "").strip(),
            "kind": str(item.get("kind") or "normal"),
        }
        for item in (targets or [])
        if isinstance(item, dict) and str(item.get("subsystem") or "").strip() and str(item.get("fm") or "").strip()
    ]
    clean_categories = [str(item).strip() for item in (source_categories or []) if str(item).strip()]
    target_rows = max(0, int(target_rows or 0))

    managed = LogWatch.objects.filter(environment=environment)
    existing = {getattr(watch, source.watch_field): watch for watch in managed if getattr(watch, source.watch_field)}
    # Names already taken in this environment, so a same-named rule gets a distinct watch
    # instead of a UNIQUE violation.
    names_in_use = {watch.name for watch in managed}
    problems: list[dict[str, str]] = []
    active_ids: list[str] = []
    stopped: list[str] = []

    if enable:
        if not clean_targets:
            return {
                "enabled_rule_ids": [], "stopped_rule_ids": [],
                "problems": [{"selector": "-", "reason": "至少需要一个监听目标（子系统/模块）才能实时监听"}],
                "watches": managed,
            }
        by_id = {str(rule["id"]): rule for rule in rules_of(kind)}
        for rule_id in requested:
            rule = by_id.get(rule_id)
            if rule is None:
                problems.append({"selector": rule_id, "reason": f"{source.label}不存在"})
                continue
            compiled, reason = source.compile(rule)
            if compiled is None:
                problems.append({"selector": rule.get("name") or rule_id, "reason": reason})
                continue
            existing_watch = existing.get(rule_id)
            _upsert_watch(
                environment, source, rule, compiled,
                existing=existing_watch,
                names_in_use=names_in_use - ({existing_watch.name} if existing_watch else set()),
                targets=clean_targets,
                source_categories=clean_categories,
                target_rows=target_rows,
                stop_with_monitoring=stop_with_monitoring,
            )
            active_ids.append(rule_id)
            names_in_use.add(f"{source.name_prefix}{compiled.name}")

    for rule_id, watch in existing.items():
        if watch.enabled and (not enable or rule_id not in active_ids):
            watch.enabled = False
            watch.save(update_fields=["enabled", "updated_at"])
            stopped.append(rule_id)

    return {
        "enabled_rule_ids": active_ids,
        "stopped_rule_ids": stopped,
        "problems": problems,
        "watches": LogWatch.objects.filter(environment=environment).exclude(**{source.watch_field: ""}),
    }


def sync_capture_watches(environment: Environment, **kwargs: Any) -> dict[str, Any]:
    """Extraction-rule flavour of :func:`sync_watches`."""
    kwargs.pop("kind", None)
    return sync_watches(environment, kind="extraction", **kwargs)


def stop_watches(
    *,
    kind: str | None = None,
    rule_ids: Iterable[str] | None = None,
    environment_id: int | None = None,
) -> list[str]:
    """Stop watches — one kind, specific rules, or everything in an environment.

    Three callers with genuinely different meanings: un-ticking one rule must not stop the
    others, 实时监听 turning off stops both kinds, and the assistant stopping one extractor
    must leave the rest running. All land here so none of them can forget to release the
    SSH tail.
    """
    if kind:
        source = source_of(kind)
        queryset = LogWatch.objects.filter(enabled=True).exclude(**{source.watch_field: ""})
        if environment_id:
            queryset = queryset.filter(environment_id=environment_id)
        if rule_ids is not None:
            wanted = {str(item).strip() for item in (rule_ids or []) if str(item).strip()}
            queryset = queryset.filter(**{f"{source.watch_field}__in": wanted})
        stopped: list[str] = []
        for watch in queryset:
            watch.enabled = False
            watch.save(update_fields=["enabled", "updated_at"])
            stopped.append(str(getattr(watch, source.watch_field)))
        return stopped

    from django.db.models import Q

    queryset = LogWatch.objects.filter(enabled=True).exclude(extraction_rule_id="").exclude(source_rule_id="")
    if environment_id:
        queryset = queryset.filter(environment_id=environment_id)
    if rule_ids is not None:
        wanted = {str(item).strip() for item in (rule_ids or []) if str(item).strip()}
        queryset = queryset.filter(Q(extraction_rule_id__in=wanted) | Q(source_rule_id__in=wanted))
    stopped = []
    for watch in queryset:
        watch.enabled = False
        watch.save(update_fields=["enabled", "updated_at"])
        stopped.append(str(watch.extraction_rule_id or watch.source_rule_id))
    return stopped


def stop_capture_watches(
    *,
    rule_ids: Iterable[str] | None = None,
    environment_id: int | None = None,
) -> list[str]:
    """Extraction-only stop, kept for callers that predate the ``kind`` parameter."""
    return stop_watches(kind="extraction", rule_ids=rule_ids, environment_id=environment_id)
