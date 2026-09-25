from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable

from django.conf import settings

from apps.atlog.anomaly_rules import normalize_anomaly_rules
from apps.atlog.knowledge_bridge import build_ai_case_evidences
from apps.atlog.services import case_id_from_url
from apps.tooling.log_context import compact_log_rows_for_ai
from apps.tooling.llm import get_llm_client
from apps.tooling.skills.loader import load_skill
from apps.tooling.skills.runtime import run_skill_agent

ProgressCallback = Callable[[dict[str, Any]], None]

logger = logging.getLogger("tracelens.atlog.skill")

_ALLOWED_TOOLS = [
    "analyze_atlog_case",
    "query_atlog_event",
    "resolve_log_components",
    "query_atlog_logs",
    "match_cases",
]


def _json_object(text: str) -> dict[str, Any]:
    value = str(text or "").strip()
    if not value:
        return {}
    candidates = [value]
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", value, re.S | re.I)
    if fenced:
        candidates.insert(0, fenced.group(1))
    first, last = value.find("{"), value.rfind("}")
    if 0 <= first < last:
        candidates.append(value[first:last + 1])
    for item in candidates:
        try:
            parsed = json.loads(item)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _safe_text(value: Any, limit: int) -> str:
    return str(value or "").replace("\r", " ").strip()[:limit]


def _page_report_rows(case_context: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    report = case_context.get("report_facts") if isinstance(case_context.get("report_facts"), dict) else {}
    case_id = _safe_text(case_context.get("case_id") or report.get("case_id"), 255)
    case_name = _safe_text(case_context.get("case_name") or report.get("case_name") or case_id, 255)
    description = _safe_text(case_context.get("case_description") or case_context.get("description") or report.get("case_description"), 1800)
    if case_id or case_name or description:
        rows.append({
            "time": "", "level": "INFO", "component": "", "source_path": "case-metadata",
            "source_kind": "report", "line_number": None,
            "message": " | ".join(part for part in [f"用例={case_name or case_id}" if (case_name or case_id) else "", f"描述={description}" if description else ""] if part),
        })
    fields = [
        ("pytest/summary", report.get("assertion_summary") or report.get("assertion") or case_context.get("assertion_summary")),
        ("pytest/failure", report.get("failure_text") or case_context.get("failure_text")),
        ("pytest/location", report.get("failure_location") or case_context.get("failure_location")),
        ("xytest", report.get("xytest_error") or report.get("xytest_errors") or case_context.get("xytest_error")),
        ("pytest-html", report.get("report_excerpt") or case_context.get("report_excerpt")),
    ]
    for source, raw in fields:
        if isinstance(raw, list):
            raw = "\n".join(str(item) for item in raw[-8:])
        text = _safe_text(raw, 2200)
        if text:
            rows.append({
                "time": "", "level": "ERROR" if source != "case-metadata" else "INFO",
                "component": "", "source_path": source, "source_kind": "report",
                "line_number": None, "message": text,
            })
    return rows


def _context_evidence_rows(case_context: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    # 用户当前日志工作台选择的内容优先进入 AI 证据上下文。
    # 这里不能只传模块/时间范围等元数据，必须把用户实际看到的日志行作为分析输入。
    selected_context = case_context.get("selected_log_context")
    if isinstance(selected_context, dict):
        for item in list(selected_context.get("rows") or [])[:240]:
            if not isinstance(item, dict):
                continue
            message = _safe_text(item.get("message") or item.get("raw"), 2200)
            if not message:
                continue
            rows.append({
                "time": _safe_text(item.get("time"), 80),
                "level": _safe_text(item.get("level"), 32),
                "component": _safe_text(item.get("component") or item.get("module"), 120),
                "source_path": _safe_text(item.get("source_path") or item.get("source"), 320),
                "source_kind": "selected_user_log",
                "line_number": item.get("line_number") if item.get("line_number") is not None else item.get("line"),
                "message": message,
                "raw": message,
            })
    for bucket, source_kind in (("event_evidence", "event"), ("runtime_evidence", "runtime"), ("loaded_evidence", "runtime")):
        for item in list(case_context.get(bucket) or [])[:48]:
            if not isinstance(item, dict):
                continue
            message = _safe_text(item.get("message") or item.get("raw"), 2200)
            if not message:
                continue
            rows.append({
                "time": _safe_text(item.get("time"), 80),
                "level": _safe_text(item.get("level"), 32),
                "component": _safe_text(item.get("component"), 120),
                "source_path": _safe_text(item.get("source_path") or item.get("source"), 320),
                "source_kind": source_kind,
                "line_number": item.get("line_number") if item.get("line_number") is not None else item.get("line"),
                "message": message,
                "raw": message,
            })
    # Keep stable order while removing the duplicate runtime/loaded aliases.
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        key = "|".join([str(row.get("source_path") or ""), str(row.get("line_number") or ""), str(row.get("message") or "")])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _compact_analysis(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": value.get("case_id"),
        "case_name": value.get("case_name"),
        "status": value.get("status"),
        "conclusion": value.get("conclusion"),
        "assertion": _safe_text(value.get("assertion"), 3000),
        "assertion_summary": _safe_text(value.get("assertion_summary"), 1600),
        "reason_category": value.get("reason_category"),
        "reason_detail": _safe_text(value.get("reason_detail"), 1800),
        "failure_location": value.get("failure_location"),
        "failure_time": value.get("failure_time"),
        "event_start_time": value.get("event_start_time"),
        "event_end_time": value.get("event_end_time"),
        "xytest_errors": list(value.get("xytest_errors") or [])[-8:],
        "report_excerpt": _safe_text(value.get("report_excerpt"), 3000),
        "failure_text": _safe_text(value.get("failure_text"), 4000),
        "log_catalog": [
            {"subsystem": item.get("subsystem"), "module": item.get("module") or item.get("component"), "kind": item.get("kind")}
            for item in list(value.get("log_catalog") or [])[:80]
            if isinstance(item, dict)
        ],
        "warnings": list(value.get("warnings") or [])[:10],
    }


def _normalize_report(value: dict[str, Any], raw_text: str) -> dict[str, Any]:
    report = value.get("report") if isinstance(value.get("report"), dict) else value
    if not isinstance(report, dict) or not report:
        report = {"summary": _safe_text(raw_text, 2400), "root_cause": _safe_text(raw_text, 3200)}
    try:
        confidence = max(0, min(100, int(float(report.get("confidence") or 0))))
    except (TypeError, ValueError):
        confidence = 0
    level = str(report.get("confidence_level") or "").strip()
    if level not in {"高", "中", "低"}:
        level = "高" if confidence >= 80 else "中" if confidence >= 55 else "低"
    recommendations = [str(item)[:500] for item in list(report.get("recommendations") or []) if str(item).strip()][:5]
    evidence: list[dict[str, Any]] = []
    for item in list(report.get("evidence") or [])[:10]:
        if not isinstance(item, dict):
            continue
        evidence.append({
            "time": _safe_text(item.get("time"), 80),
            "component": _safe_text(item.get("component"), 120),
            "source": _safe_text(item.get("source"), 240),
            "message": _safe_text(item.get("message"), 1800),
            "why": _safe_text(item.get("why"), 600),
        })
    return {
        "summary": _safe_text(report.get("summary"), 3000),
        "root_cause": _safe_text(report.get("root_cause"), 4000),
        "root_cause_category": _safe_text(report.get("root_cause_category") or "未归类", 160),
        "confidence": confidence,
        "confidence_level": level,
        "recommendations": recommendations,
        "evidence": evidence,
        "causal_chain": [], "excluded_causes": [], "next_checks": [], "limitations": [],
    }


def _llm_usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    def value(*names: str) -> int:
        for name in names:
            raw = getattr(usage, name, None)
            if raw is None and isinstance(usage, dict):
                raw = usage.get(name)
            if raw not in (None, ""):
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    pass
        return 0

    input_tokens = value("prompt_tokens", "input_tokens")
    output_tokens = value("completion_tokens", "output_tokens")
    total_tokens = value("total_tokens") or input_tokens + output_tokens
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total_tokens}


def _merge_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    return {
        "input_tokens": int(left.get("input_tokens") or 0) + int(right.get("input_tokens") or 0),
        "output_tokens": int(left.get("output_tokens") or 0) + int(right.get("output_tokens") or 0),
        "total_tokens": int(left.get("total_tokens") or 0) + int(right.get("total_tokens") or 0),
    }


def _evidence_inventory(
    report_rows: list[dict[str, Any]],
    page_rows: list[dict[str, Any]],
    compact_rows: list[dict[str, Any]],
    report_facts: dict[str, Any],
    selected_targets: list[Any],
) -> dict[str, Any]:
    rows = [*page_rows, *compact_rows]
    event_rows = 0
    runtime_rows = 0
    error_rows = 0
    sources: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        source = str(row.get("source_path") or row.get("source") or "").strip()
        kind = str(row.get("source_kind") or "").strip().casefold()
        level = str(row.get("level") or "").strip().upper()
        lowered = source.casefold()
        if source and source not in sources:
            sources.append(source)
        if kind == "event" or "event.log" in lowered:
            event_rows += 1
        elif kind in {"debug", "executor", "runtime", "log"} or any(token in lowered for token in ("/debug/", "debug/", "executor", ".log")):
            runtime_rows += 1
        if level in {"ERROR", "FATAL", "CRITICAL"}:
            error_rows += 1
    return {
        "report_available": bool(report_rows or report_facts),
        "report_evidence_count": len(report_rows),
        "event_evidence_count": event_rows,
        "runtime_evidence_count": runtime_rows,
        "error_evidence_count": error_rows,
        "selected_target_count": len([item for item in selected_targets if item]),
        "source_count": len(sources),
        "sources": sources[:16],
    }


def _verify_evidence(report: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    verified: list[dict[str, Any]] = []
    used: set[int] = set()
    for cited in list(report.get("evidence") or [])[:10]:
        message = str(cited.get("message") or "").strip()
        source = str(cited.get("source") or "").strip()
        component = str(cited.get("component") or "").strip()
        if not message:
            continue
        for idx, row in enumerate(rows):
            if idx in used:
                continue
            row_message = str(row.get("message") or row.get("raw") or "").strip()
            row_source = str(row.get("source_path") or row.get("source") or "").strip()
            row_component = str(row.get("component") or "").strip()
            source_ok = not source or source == row_source
            component_ok = not component or component.casefold() == row_component.casefold()
            message_ok = message == row_message or message in row_message or row_message in message
            if source_ok and component_ok and message_ok:
                used.add(idx)
                verified.append(dict(row))
                break
    return verified


def diagnose_case_with_skill(
    url: str,
    *,
    current_messages: list[dict[str, Any]] | None = None,
    anomaly_rules: list[dict[str, Any]] | None = None,
    case_context: dict[str, Any] | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    base_url = str(url or "").strip()
    if not base_url:
        raise ValueError("用例日志 URL 不能为空。")
    started = time.perf_counter()
    rules = normalize_anomaly_rules(anomaly_rules or [])
    context = dict(case_context or {})
    explicit_rows = [dict(item) for item in (current_messages or []) if isinstance(item, dict)]
    context_rows = _context_evidence_rows(context)
    page_rows: list[dict[str, Any]] = []
    seen_page_rows: set[str] = set()
    for item in [*explicit_rows, *context_rows]:
        key = "|".join([str(item.get("source_path") or item.get("source") or ""), str(item.get("line_number") or item.get("line") or ""), str(item.get("message") or item.get("raw") or "")])
        if key in seen_page_rows:
            continue
        seen_page_rows.add(key)
        page_rows.append(item)
    # 先量后压：异常/页面上下文的原文没超过长度上限就**原样直送**（模型看到真实日志行，
    # 没有模板占位符），超过上限才走函数归属压缩。上限跟日志分析那一档共用。
    from apps.tooling.log_context import evidence_max_chars, raw_log_rows_context

    evidence_budget = evidence_max_chars(context)
    page_compact = raw_log_rows_context(page_rows, max_chars=evidence_budget)
    if page_compact is None:
        page_compact = compact_log_rows_for_ai(page_rows, max_chars=10000, max_nodes=36, max_groups=22)
    report_rows = _page_report_rows(context)
    from apps.tooling.evidence import build_evidence_pack, structured_diagnosis
    evidence_pack = build_evidence_pack([*report_rows, *page_rows], max_chars=12000)
    report_facts = context.get("report_facts") if isinstance(context.get("report_facts"), dict) else {}
    request = context.get("current_log_request") if isinstance(context.get("current_log_request"), dict) else {}
    selected_targets = list(context.get("selected_targets") or request.get("fm_targets") or [])[:8]
    case_id = _safe_text(context.get("case_id") or report_facts.get("case_id") or case_id_from_url(base_url), 255)
    case_name = _safe_text(context.get("case_name") or report_facts.get("case_name") or case_id, 255)

    logger.info(
        "atlog.skill.evidence mode=%s rows=%s chars=%s budget=%s",
        page_compact.get("mode") or "compressed",
        page_compact.get("source_row_count"),
        page_compact.get("raw_chars") or len(str(page_compact.get("ai_context") or "")),
        evidence_budget,
    )
    logger.info(
        "atlog.skill.start case_url=%s case_id=%s case_name=%s page_rows=%s report_rows=%s selected_targets=%s start_time=%s end_time=%s",
        base_url[:1000], case_id, case_name, len(page_rows), len(report_rows),
        json.dumps(selected_targets, ensure_ascii=False, default=str)[:5000],
        context.get("start_time") or request.get("start_time") or report_facts.get("start_time") or "",
        context.get("end_time") or request.get("end_time") or report_facts.get("end_time") or "",
    )
    logger.info(
        "atlog.skill.context report_facts=%s current_log_request=%s page_ai_context=%s",
        json.dumps(report_facts, ensure_ascii=False, default=str)[:10000],
        json.dumps(request, ensure_ascii=False, default=str)[:8000],
        str(page_compact.get("ai_context") or "")[:8000],
    )

    runtime_context = {
        "selected_atlog_case": {
            **context,
            "case_id": case_id,
            "case_name": case_name,
            "case_url": base_url,
            "source_case_url": base_url,
            "url": base_url,
            "start_time": context.get("start_time") or request.get("start_time") or report_facts.get("start_time") or "",
            "end_time": context.get("end_time") or request.get("end_time") or report_facts.get("end_time") or "",
            "selected_targets": selected_targets,
            "current_log_request": request,
        },
        "assistant_scope": {"atlog_case": {"case_url": base_url, "case_id": case_id, "case_name": case_name}},
    }

    task_payload = {
        "task": "分析当前自动化用例失败原因，给出可回链原始证据的根因结论。",
        "case": {
            "case_id": case_id,
            "case_name": case_name,
            "case_description": _safe_text(context.get("case_description") or context.get("description") or report_facts.get("case_description"), 3200),
            "case_url": base_url,
            "start_time": runtime_context["selected_atlog_case"].get("start_time"),
            "end_time": runtime_context["selected_atlog_case"].get("end_time"),
            "selected_targets": selected_targets,
        },
        "page_report_facts": report_facts,
        "page_log_context": {
            "ai_context": page_compact.get("ai_context"),
            "evidence_rows": list(page_compact.get("evidence_rows") or [])[:36],
            "source_row_count": page_compact.get("source_row_count"),
        },
        "anomaly_rules": [
            {"keyword": item.get("keyword"), "case_sensitive": bool(item.get("case_sensitive")), "whole_word": bool(item.get("whole_word"))}
            for item in rules[:80]
        ],
        "instruction": "先使用已有上下文；只有缺证据时才调用工具。不要机械执行固定顺序。",
    }

    internal: dict[str, Any] = {
        "analysis": {},
        "rows": [*report_rows, *page_rows],
        "evidence_rows": [*report_rows, *list(page_compact.get("evidence_rows") or [])],
        "selected_targets": selected_targets,
        "component_candidates": [],
        "history_matches": [],
        "event_components": [],
        "warnings": [],
    }

    def observe(tool_id: str, arguments: dict[str, Any], value: Any) -> None:
        if not isinstance(value, dict):
            return
        if tool_id == "analyze_atlog_case":
            internal["analysis"] = dict(value)
            if value.get("warnings"):
                internal["warnings"].extend(list(value.get("warnings") or []))
        elif tool_id in {"query_atlog_event", "query_atlog_logs"}:
            rows = [dict(item) for item in list(value.get("rows") or []) if isinstance(item, dict)]
            internal["rows"].extend(rows)
            internal["evidence_rows"].extend([dict(item) for item in list(value.get("evidence_rows") or []) if isinstance(item, dict)])
            if tool_id == "query_atlog_event":
                internal["event_components"] = list(dict.fromkeys([*internal["event_components"], *list(value.get("components") or [])]))
            if tool_id == "query_atlog_logs":
                for item in list(arguments.get("targets") or []):
                    if isinstance(item, dict) and item not in internal["selected_targets"]:
                        internal["selected_targets"].append(dict(item))
        elif tool_id == "resolve_log_components":
            internal["component_candidates"] = list(value.get("candidates") or value.get("targets") or [])[:40]
        elif tool_id == "match_cases":
            internal["history_matches"] = [
                dict(item) for item in list(value.get("matches") or [])
                if isinstance(item, dict) and float(item.get("score") or 0) >= 70.0
            ][:3]

    inventory = _evidence_inventory(
        report_rows,
        page_rows,
        [dict(item) for item in list(page_compact.get("evidence_rows") or []) if isinstance(item, dict)],
        report_facts,
        selected_targets,
    )
    task_payload["evidence_inventory"] = inventory

    # Intelligent Analysis is intentionally page-context driven.  Do not enter
    # an autonomous tool loop here: the user has already selected the case,
    # report, time window and visible logs in the UI.  Additional retrieval is
    # reserved for an explicit follow-up in TracePilot (e.g. "继续查 event" or
    # "扩大时间窗").
    if progress_callback is not None:
        progress_callback({
            "type": "stage",
            "stage": {
                "name": "page_context_analysis", "label": "分析当前页面证据", "status": "running",
                "agent": "ATLog Context Analyst", "action": "analyze_selected_context",
                "detail": "正在统一分析当前选中的测试报告、pytest/HTML/xytest、event、日志和时间线证据。",
                "progress": 35,
            },
            "timestamp": time.time(),
        })

    skill_text = load_skill("atlog-analysis")
    direct_payload = {
        "case": task_payload.get("case"),
        "report_facts": report_facts,
        "evidence_pack": evidence_pack,
        "report_evidence": report_rows,
        "selected_log_request": request,
        "selected_targets": selected_targets,
        "page_log_context": task_payload.get("page_log_context"),
        "evidence_inventory": inventory,
        "anomaly_rules": task_payload.get("anomaly_rules"),
    }
    client = get_llm_client(session_id=str(context.get("session_id") or ""))
    response = client.chat([
        {
            "role": "system",
            "content": (
                "你是 TraceLens ATLog 用例上下文分析器。当前任务禁止调用任何工具，也不要要求自动继续取证。"
                "用户已经在页面上选择了用例、报告、时间范围、组件和可见日志；你必须只根据这些当前上下文给出一次性结论。"
                "优先级：测试报告/pytest HTML/xytest/summary 的失败事实 > event 证据 > 当前选中组件日志/时间线。"
                "如果报告本身已经能直接解释失败，就直接给出原因，不要因为存在可查询日志就人为降低结论。"
                "如果证据存在冲突，指出冲突并给出最可能解释；如果证据不足，也必须给当前最佳结论并明确缺口，"
                "不要返回 need_more、不要规划工具、不要让用户等待下一轮。"
                "证据引用必须来自输入里的 report_evidence 或 page_log_context.evidence_rows，不能编造。"
                "输出严格 JSON："
                '{"report":{"summary":"","root_cause":"","root_cause_category":"","confidence":0,'
                '"confidence_level":"高|中|低","recommendations":[],"evidence":[]}}.'
                "\n\nATLog 分析策略参考（仅作为判断原则，不代表固定执行步骤）：\n" + skill_text
            ),
        },
        {
            "role": "user",
            "content": json.dumps(direct_payload, ensure_ascii=False, default=str)[:32000],
        },
    ], tools=None)
    direct_usage = _llm_usage(response)
    direct_text = str(response.choices[0].message.content or "").strip()
    direct_parsed = _json_object(direct_text)
    direct_report = direct_parsed.get("report") if isinstance(direct_parsed.get("report"), dict) else direct_parsed
    if not isinstance(direct_report, dict) or not (direct_report.get("summary") or direct_report.get("root_cause")):
        direct_report = {
            "summary": _safe_text(direct_text, 2400),
            "root_cause": _safe_text(direct_text, 3200),
            "root_cause_category": "上下文分析",
            "confidence": 40,
            "confidence_level": "低",
            "recommendations": [],
            "evidence": [],
        }
    runtime = {
        "content": json.dumps(direct_report, ensure_ascii=False, default=str),
        "tool_records": [],
        "usage": direct_usage,
        "iterations": 1,
        "fast_path": True,
        "mode": "page_context_only",
    }
    if progress_callback is not None:
        progress_callback({
            "type": "stage",
            "stage": {
                "name": "page_context_analysis", "label": "分析当前页面证据", "status": "completed",
                "agent": "ATLog Context Analyst", "action": "analyze_selected_context",
                "detail": "已基于当前页面上下文形成结论；未自动扩展日志范围或调用额外工具。",
                "progress": 82,
            },
            "timestamp": time.time(),
        })
        progress_callback({"type": "token_usage", "usage": direct_usage, "timestamp": time.time()})

    parsed = _json_object(runtime.get("content") or "")
    report = _normalize_report(parsed, str(runtime.get("content") or ""))
    all_rows = [dict(item) for item in internal["rows"] if isinstance(item, dict)]
    verified_rows = _verify_evidence(report, all_rows)
    if not verified_rows and internal["evidence_rows"]:
        verified_rows = [dict(item) for item in internal["evidence_rows"][:6] if isinstance(item, dict)]
        if not report.get("evidence"):
            report["evidence"] = [
                {
                    "time": _safe_text(row.get("time"), 80),
                    "component": _safe_text(row.get("component"), 120),
                    "source": _safe_text(row.get("source_path") or row.get("source"), 240),
                    "message": _safe_text(row.get("message") or row.get("raw"), 1800),
                    "why": "当前 Skill Agent 取证过程中保留的高价值原始证据。",
                }
                for row in verified_rows[:6]
                if _safe_text(row.get("message") or row.get("raw"), 1800)
            ]

    report = structured_diagnosis(report, all_rows, selected_targets)
    report["evidence_pack"] = evidence_pack
    analysis = dict(internal.get("analysis") or {})
    analysis.setdefault("case_id", case_id)
    analysis.setdefault("case_name", case_name)
    analysis.setdefault("base_url", base_url)
    analysis.setdefault("case_description", _safe_text(context.get("case_description") or context.get("description") or report_facts.get("case_description"), 4000))
    if not analysis.get("assertion_summary"):
        analysis["assertion_summary"] = _safe_text(report_facts.get("assertion_summary") or report_facts.get("assertion"), 1800)

    case_evidence_rows = [*report_rows, *internal["evidence_rows"], *verified_rows]
    case_evidences = build_ai_case_evidences(case_evidence_rows, require_runtime=False)
    selected_targets_clean = [dict(item) for item in internal["selected_targets"] if isinstance(item, dict)][:12]
    usage = runtime.get("usage") if isinstance(runtime.get("usage"), dict) else {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    if progress_callback is not None:
        progress_callback({
            "type": "stage",
            "stage": {
                "name": "final", "label": "根因分析完成", "status": "completed",
                "agent": "ATLog Skill Agent", "action": "finalize",
                "detail": "Skill Agent 已基于当前证据收敛结论。", "progress": 100,
            },
            "timestamp": time.time(),
        })
        progress_callback({"type": "report_preview", "text": "\n".join(filter(None, [report.get("summary"), report.get("root_cause")]))[:7000], "timestamp": time.time()})

    logger.info(
        "atlog.skill.finish case_url=%s case_id=%s selected_targets=%s event_components=%s history_matches=%s usage=%s report=%s",
        base_url[:1000], case_id,
        json.dumps(selected_targets_clean, ensure_ascii=False, default=str)[:5000],
        json.dumps(internal["event_components"], ensure_ascii=False, default=str)[:3000],
        json.dumps(internal["history_matches"], ensure_ascii=False, default=str)[:5000],
        usage,
        json.dumps(report, ensure_ascii=False, default=str)[:12000],
    )
    return {
        "case_id": analysis.get("case_id") or case_id,
        "case_name": analysis.get("case_name") or case_name,
        "base_url": analysis.get("base_url") or base_url,
        "model": str(getattr(settings, "TRACELENS_AI_MODEL", "GLM-4.7-XS")),
        "provider": str(getattr(settings, "TRACELENS_AI_PROVIDER", "my-llm")),
        "report": report,
        "steps": list(runtime.get("tool_records") or []),
        "retrieval_rounds": sum(1 for item in list(runtime.get("tool_records") or []) if item.get("tool_id") in {"query_atlog_event", "query_atlog_logs"}),
        "event_components": internal["event_components"],
        "selected_targets": selected_targets_clean,
        "component_candidates": internal["component_candidates"],
        "evidence_message_count": len(internal["evidence_rows"]),
        "collected_message_count": len(all_rows),
        "current_message_count": len(page_rows),
        "warnings": list(dict.fromkeys(str(item) for item in internal["warnings"] if str(item).strip()))[:20],
        "history_matches": internal["history_matches"],
        "anomaly_rules": [
            {"keyword": rule.get("keyword"), "case_sensitive": bool(rule.get("case_sensitive")), "whole_word": bool(rule.get("whole_word"))}
            for rule in rules
        ],
        "token_usage": usage,
        "duration_ms": max(0, int((time.perf_counter() - started) * 1000)),
        "case_description": _safe_text(analysis.get("case_description"), 4000),
        "case_evidences": case_evidences,
        "case_draft": {
            "name": _safe_text(analysis.get("case_name") or analysis.get("case_id") or "自动化用例异常", 255),
            "category": _safe_text(report.get("root_cause_category") or analysis.get("reason_category"), 120),
            "symptom": _safe_text(analysis.get("assertion_summary") or analysis.get("conclusion"), 4000),
            "root_cause": _safe_text(report.get("root_cause"), 4000),
            "solution": "\n".join(report.get("recommendations") or [])[:6000],
            "description": "\n".join(filter(None, [
                f"用例描述：{_safe_text(analysis.get('case_description'), 3000)}" if analysis.get("case_description") else "",
                f"AI诊断摘要：{_safe_text(report.get('summary'), 3000)}" if report.get("summary") else "",
            ]))[:6000],
            "tags": list(dict.fromkeys([_safe_text(report.get("root_cause_category"), 120), *[_safe_text(item.get("module"), 120) for item in selected_targets_clean if item.get("module")]]))[:12],
        },
        "skill_id": "atlog-analysis",
        "skill_iterations": int(runtime.get("iterations") or 0),
        "_verified_evidence_rows": verified_rows,
        "_deterministic_analysis": analysis,
    }
