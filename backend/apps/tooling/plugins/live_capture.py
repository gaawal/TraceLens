"""实时采集 tools for TracePilot.

The user's phrasing is "实时采集下 xxx 数据" — one sentence, and the whole chain has to happen:
opt the extractor in, start the server-side watcher, and start the page's live stream. These
two tools exist so the assistant drives *the same* code path the 实时监听 switch does, instead
of the assistant having its own idea of what is being collected.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.utils import timezone

from apps.tooling.plugins.catalog import register_function_tool
from apps.tooling.registry import ToolDefinition

logger = logging.getLogger("tracelens.tooling.plugins.live_capture")

_ENV_ID = {"type": ["integer", "string"], "description": "TraceLens 环境 ID"}


def _environment(environment_id: Any):
    from apps.tooling.services import _environment as resolve

    return resolve(environment_id)


def _catalog(kind: str) -> list[dict[str, Any]]:
    """The rules of one kind, in the shape the model needs to choose between them."""
    from apps.logsources.services.watch_capture import rules_of, source_of

    source = source_of(kind)
    rows: list[dict[str, Any]] = []
    for rule in rules_of(kind):
        rows.append({
            "id": str(rule["id"]),
            "kind": source.kind,
            "name": str(rule.get("name") or ""),
            # Both rule models call their matching text something different.
            "match": str(rule.get("matchKeyword") or rule.get("keyword") or ""),
            "scope": "/".join(
                [item for item in (
                    ",".join(str(x) for x in (rule.get("subsystems") or [])),
                    ",".join(str(x) for x in (rule.get("modules") or [])),
                ) if item]
            ),
            "enabled": rule.get("enabled", True) is not False,
            "live": rule.get(source.opt_in_field) is True,
            "fields": [str(field.get("name") or field.get("key") or "") for field in (rule.get("fields") or []) if isinstance(field, dict)],
        })
    return rows


@register_function_tool(ToolDefinition(
    id="configure_live_watch",
    name="开启/关闭实时监听（时间线标签流或数据采集）",
    description=(
        "把指定的规则加入（或移出）实时监听，同步服务端监视器，并可选直接开始实时监听。"
        "kind=extraction（默认）作用于数据提取器，命中进入数据采集面板；"
        "kind=semantic 作用于日志语义规则，命中进入时间线实时标签流。"
        "用户说“实时采集下 xxx 数据 / 开始采集 xxx / 别采集 xxx 了”用 extraction；"
        "说“实时监听这个错误 / 这条日志一出现就告诉我”用 semantic。"
        "不传 rules 时返回该类型当前可用的规则清单，用于先确认再执行。"
    ),
    category="数据采集",
    handler=None,
    agent_exposed=True,
    read_only=False,
    risk_level="low_write",
    transport="function",
    domain="logs",
    kind="mutation",
    skills=("logs", "data"),
    input_schema={
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["extraction", "semantic"],
                "default": "extraction",
                "description": "extraction=数据提取器（进数据采集面板）；semantic=日志语义规则（进时间线标签流）。默认 extraction。",
            },
            "rules": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
                "description": "规则名称、名称片段、匹配关键字或 ID。留空则只返回该类型可用清单，不做修改。",
            },
            "extractors": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
                "description": "兼容别名，等同 rules。",
            },
            "action": {
                "type": "string",
                "enum": ["enable", "disable"],
                "default": "enable",
                "description": "enable=加入实时监听；disable=移出并停止对应监视器。",
            },
            "environment_id": _ENV_ID,
            "subsystem": {"type": "string", "default": "", "description": "要监听的子系统，例如 cpfr"},
            "fm": {"type": "string", "default": "", "description": "要监听的模块/组件，例如 cpdisp"},
            "source_categories": {
                "type": "array",
                "items": {"type": "string"},
                "default": ["debug"],
                "description": "日志类型，默认 debug。",
            },
            "start_monitoring": {
                "type": "boolean",
                "default": True,
                "description": "是否同时打开页面的实时监听（需要 subsystem 和 fm）。",
            },
            "target_rows": {
                "type": ["integer", "string"],
                "default": 0,
                "description": "采集到多少行后停止；0 表示持续采集。",
            },
        },
        "required": [],
    },
    use_when="用户要求对某些数据开始/停止实时采集，或想先看看有哪些可实时采集的数据提取器时。",
    do_not_use_when=(
        "用户只是想查询一段历史日志（用 query_environment_logs）；"
        "或想新建一条规则 / 数据提取器（先去日志规则页面创建，再实时监听）。"
    ),
    implementation="apps.tooling.plugins.live_capture.configure_live_watch",
))
def configure_live_watch(payload: dict) -> dict[str, Any]:
    from apps.logsources.models import LogWatch
    from apps.logsources.services.watch_capture import (
        find_rules,
        set_opt_in,
        source_of,
        stop_watches,
        sync_watches,
    )

    kind = str(payload.get("kind") or "extraction").strip()
    source = source_of(kind)
    kind = source.kind
    # `extractors` stays accepted: the phrasing "实时采集下 xxx 数据" is what the user says, and
    # a model that already learned the old argument name should not be punished for it.
    selectors = [
        str(item) for item in ((payload.get("rules") or []) + (payload.get("extractors") or []))
        if str(item).strip()
    ]
    action = str(payload.get("action") or "enable").strip().lower()
    surface = "数据采集面板" if kind == "extraction" else "时间线标签流"
    catalog = _catalog(kind)

    if not selectors:
        return {
            "ok": True,
            "changed": False,
            "kind": kind,
            "rules": catalog,
            "live_rule_ids": [item["id"] for item in catalog if item["live"]],
            "note": f"未指定 rules，仅返回当前可用的{source.label}清单（命中会进入{surface}）。请确认要监听哪一个。",
        }

    matched, unresolved = find_rules(kind, selectors)
    if not matched:
        return {
            "ok": False,
            "changed": False,
            "kind": kind,
            "rules": catalog,
            "problems": unresolved,
            "note": f"没有匹配到任何{source.label}。",
        }

    enable = action != "disable"
    matched_ids = [str(rule["id"]) for rule in matched]
    # A rule that cannot match a log line can never fire; say so instead of enabling a
    # watcher that silently watches nothing.
    blocked = []
    for rule in matched:
        compiled, reason = source.compile(rule)
        if compiled is None:
            blocked.append({"id": str(rule["id"]), "name": str(rule.get("name") or ""), "reason": reason})
    usable_ids = [rule_id for rule_id in matched_ids if rule_id not in {item["id"] for item in blocked}]

    # Which watchers were actually running for these rules *before* the flag flips. The
    # pruning receiver may stop them first, and reporting "stopped nothing" would be a lie
    # about an SSH tail that really did close.
    running_before = set(
        LogWatch.objects.filter(enabled=True, **{f"{source.watch_field}__in": usable_ids})
        .values_list(source.watch_field, flat=True)
    ) if usable_ids else set()
    changed = set_opt_in(kind, matched_ids, enable) if usable_ids else []
    environment_id = payload.get("environment_id")
    subsystem = str(payload.get("subsystem") or "").strip()
    fm = str(payload.get("fm") or "").strip()
    source_categories = [str(item) for item in (payload.get("source_categories") or ["debug"]) if str(item).strip()]

    outcome: dict[str, Any] = {}
    if action == "disable" or not usable_ids:
        # Stop exactly the rules that were named — another watcher in the same environment may
        # still be one the user wants running.
        try:
            stopped_by_us = stop_watches(
                kind=kind,
                rule_ids=usable_ids,
                environment_id=int(environment_id) if environment_id and str(environment_id).isdigit() else None,
            )
            outcome = {
                "stopped_rule_ids": sorted(running_before | set(stopped_by_us)),
                "enabled_rule_ids": [],
                "problems": [],
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("live_watch.stop_failed error=%s", exc)
    elif not environment_id or not subsystem or not fm:
        return {
            "ok": False,
            "changed": bool(changed),
            "kind": kind,
            "enabled_rule_ids": [],
            "stopped_rule_ids": [],
            "rules": catalog,
            "problems": blocked + [{"id": "-", "name": "-", "reason": "开启实时监听需要 environment_id / subsystem / fm"}],
            "note": f"已更新{source.label}的实时监听开关，但缺少环境或监听目标，监视器未启动。请补充后重试。",
        }
    else:
        outcome = sync_watches(
            _environment(environment_id),
            kind=kind,
            rule_ids=usable_ids,
            targets=[{"subsystem": subsystem, "fm": fm, "kind": "normal"}],
            source_categories=source_categories,
            enable=True,
            target_rows=int(payload.get("target_rows") or 0),
        )

    problems = list(unresolved) + blocked + list(outcome.get("problems") or [])
    # Re-read the list so the echoed catalog matches the state the caller just caused —
    # returning the pre-change snapshot made the model think its own write did not land.
    catalog = _catalog(kind)
    enabled_names = [str(rule.get("name") or rule["id"]) for rule in matched if str(rule["id"]) in set(usable_ids)]
    start_monitoring = payload.get("start_monitoring", True) is not False
    result: dict[str, Any] = {
        "ok": not problems or bool(usable_ids),
        "changed": True,
        "kind": kind,
        "action": action,
        "rules": catalog,
        "enabled_rule_ids": outcome.get("enabled_rule_ids", []),
        "stopped_rule_ids": outcome.get("stopped_rule_ids", []),
        "live_rule_ids": [item["id"] for item in catalog if item["live"] or item["id"] in set(usable_ids)],
        "problems": problems,
        "note": (
            f"已{'开启' if enable else '停止'}实时监听（{source.label} → {surface}）：{'、'.join(enabled_names) or '（无）'}。"
            + (f"监听目标 {subsystem}/{fm}。" if (enable and subsystem and fm) else "未指定监听目标，未启动监视器。")
        ),
    }
    if enable and start_monitoring and usable_ids and environment_id and subsystem and fm:
        # The page owns the actual stream; this is the same action the toolbar switch runs.
        result["ui_action"] = {
            "type": "start_live_monitoring",
            "environment_id": int(environment_id) if str(environment_id).isdigit() else environment_id,
            "subsystem": subsystem,
            "fm": fm,
            "source_categories": source_categories,
        }
    return result


@register_function_tool(ToolDefinition(
    id="read_live_capture",
    name="读取实时采集到的数据",
    description=(
        "读取某个数据提取器在实时采集中已经采集到的数据行（原始日志行 + 时间 + 模块），"
        "以及该提取器配置的字段，便于直接分析这些数据。"
    ),
    category="数据采集",
    handler=None,
    agent_exposed=True,
    read_only=True,
    risk_level="read",
    transport="function",
    domain="logs",
    kind="query",
    skills=("logs", "data"),
    input_schema={
        "type": "object",
        "properties": {
            "extractor": {"type": "string", "default": "", "description": "数据提取器名称、名称片段或 ID；留空则读取全部实时采集提取器。"},
            "environment_id": _ENV_ID,
            "limit": {"type": ["integer", "string"], "default": 50, "description": "最多读取多少行，最大 200。"},
            "since_minutes": {"type": ["integer", "string"], "default": 0, "description": "只看最近多少分钟的采集数据；0 表示不限制时间。"},
        },
        "required": [],
    },
    use_when="用户想分析实时采集到的数据，例如“实时采集下光源功率数据并分析趋势”。先 configure_live_watch（kind=extraction）再用它读数据。",
    do_not_use_when="还没有开启实时采集时；那时应先用 configure_live_watch 开启。",
    implementation="apps.tooling.plugins.live_capture.read_live_capture",
))
def read_live_capture(payload: dict) -> dict[str, Any]:
    from apps.logsources.models import LogWatch, LogWatchHit
    from apps.logsources.services.watch_capture import find_extraction_rules, live_capture_rule_ids

    limit = max(1, min(200, int(payload.get("limit") or 50)))
    since_minutes = max(0, int(payload.get("since_minutes") or 0))
    selector = str(payload.get("extractor") or "").strip()
    environment_id = payload.get("environment_id")

    if selector:
        matched, problems = find_extraction_rules([selector])
    else:
        # Default to the opted-in collectors: "read what is being captured" needs no argument.
        matched, problems = find_extraction_rules(live_capture_rule_ids()), []
    rule_ids = [str(rule["id"]) for rule in matched]
    if not rule_ids:
        return {
            "ok": False,
            "rows": [],
            "problems": problems,
            "note": "没有找到对应的数据提取器，也没有已开启实时采集的提取器。",
        }

    watches = LogWatch.objects.filter(extraction_rule_id__in=rule_ids)
    if environment_id:
        watches = watches.filter(environment_id=environment_id)
    watches = list(watches.select_related("environment"))
    if not watches:
        return {
            "ok": False,
            "rows": [],
            "rules": [{"id": str(rule["id"]), "name": rule.get("name"), "fields": [f.get("name") or f.get("key") for f in (rule.get("fields") or []) if isinstance(f, dict)]} for rule in matched],
            "note": "这些提取器还没有对应的实时采集监视器（可能还没开始实时监听）。请先用 configure_live_watch 开启。",
        }

    hits = LogWatchHit.objects.filter(watch__in=watches, watch__enabled=True)
    if since_minutes:
        hits = hits.filter(matched_at__gte=timezone.now() - timedelta(minutes=since_minutes))
    rows = list(hits.order_by("-matched_at")[:limit])
    by_rule = {str(watch.extraction_rule_id): watch for watch in watches}
    rule_by_id = {str(rule["id"]): rule for rule in matched}
    return {
        "ok": True,
        "count": len(rows),
        "rules": [
            {
                "id": rule_id,
                "name": str((rule_by_id.get(rule_id) or {}).get("name") or ""),
                "match": str((rule_by_id.get(rule_id) or {}).get("matchKeyword") or ""),
                "fields": [
                    str(field.get("name") or field.get("key") or "")
                    for field in ((rule_by_id.get(rule_id) or {}).get("fields") or [])
                    if isinstance(field, dict)
                ],
                "watch_id": by_rule[rule_id].pk,
                "hit_count": by_rule[rule_id].hit_count,
                "collecting": by_rule[rule_id].enabled,
            }
            for rule_id in rule_ids if rule_id in by_rule
        ],
        # Raw lines on purpose: the field extraction engine lives in the browser, and a second
        # Python implementation would drift from it. The model reads the same text the UI does.
        "rows": [
            {
                "at": hit.matched_at.isoformat() if hit.matched_at else "",
                "extractor_id": str(hit.watch.extraction_rule_id),
                "subsystem": hit.subsystem,
                "fm": hit.fm,
                "line": hit.line_text,
            }
            for hit in rows
        ],
        "note": (
            f"读取到 {len(rows)} 条实时采集数据（最新在前）。字段定义随 rules 返回，"
            "如需绘图或统计，请说明数据和指标。"
            if rows else
            "监视器已在运行，但还没有采集到匹配的行。可能是目标模块暂时没有输出该日志。"
        ),
    }
