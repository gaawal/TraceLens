from __future__ import annotations

"""ATLog diagnosis workflow adapter for the TracePilot orchestration graph.

The domain-specific diagnosis remains implemented in ``apps.atlog.ai_agent``.
This module only adapts the selected browser case context and the ATLog graph's
public progress events to TracePilot's SSE protocol.  No ATLog business logic is
reimplemented here.
"""

import json
import logging
from typing import Any, Callable

from apps.atlog.ai_agent import diagnose_case_with_ai

EventSink = Callable[[dict[str, Any]], None]

logger = logging.getLogger("tracelens.tooling.atlog_flow")

_DIAG_TOOL_IDS = {"analyze_atlog_case", "query_atlog_event", "query_atlog_logs"}
_STAGE_LABELS = {
    "skill_load": "加载 ATLog 分析 Skill",
    "agent_decision": "判断下一步证据",
    "tool": "执行原子能力",
    "synthesis": "收敛当前证据",
    "final": "生成根因结论",
    # Legacy fixed-graph names are retained only for compatibility fallback.
    "context": "汇聚当前用例证据",
    "component_map": "定位目标组件",
    "triage": "选择定向日志目标",
    "retrieve": "提取目标日志证据",
    "review": "检查证据完整性",
    "history": "匹配高相似历史案例",
    "report": "生成根因结论",
}
_STAGE_PROGRESS = {
    "skill_load": 8, "agent_decision": 18, "tool": 48, "synthesis": 92, "final": 100,
    "context": 12, "component_map": 28, "triage": 42, "retrieve": 60,
    "review": 74, "history": 86, "report": 96,
}


def selected_case(context: dict[str, Any]) -> dict[str, Any]:
    runtime = context if isinstance(context, dict) else {}
    scope = runtime.get("assistant_scope") if isinstance(runtime.get("assistant_scope"), dict) else {}
    scoped = scope.get("atlog_case") if isinstance(scope.get("atlog_case"), dict) else {}
    selected = runtime.get("selected_atlog_case") if isinstance(runtime.get("selected_atlog_case"), dict) else {}
    page = runtime.get("atlog_page") if isinstance(runtime.get("atlog_page"), dict) else {}
    expanded = page.get("expanded_case") if isinstance(page.get("expanded_case"), dict) else {}
    merged: dict[str, Any] = {}
    for item in (scoped, selected, expanded):
        if isinstance(item, dict):
            merged.update(item)
    return merged


def _valid_url(value: Any) -> str:
    text = str(value or "").strip()
    return text if text.casefold().startswith(("http://", "https://")) else ""


def should_run_atlog_workflow(route: dict[str, Any], context: dict[str, Any], skill_id: str) -> bool:
    """Use AI semantic routing output, never user-keyword matching, to enter ATLog."""
    if str(skill_id or "").strip() != "atlog":
        return False
    case = selected_case(context)
    url = _valid_url(case.get("case_url") or case.get("source_case_url") or case.get("url"))
    if not url:
        return False
    mode = str(route.get("mode") or "").strip().lower()
    if mode not in {"answer", "query", "workflow", "action"}:
        return False
    tool_ids = {str(item).strip() for item in list(route.get("tool_ids") or []) if str(item).strip()}
    # The current ATLog path is a page-only analyst. Retrieval/navigation requests
    # must stay in the general graph, where the selected capabilities can execute.
    return bool(route.get("use_page_evidence")) and not tool_ids


def _case_messages(case: dict[str, Any]) -> list[dict[str, Any]]:
    """Build a small page-evidence window; never dump the rendered log table."""
    rows: list[dict[str, Any]] = []
    for source_bucket in ("event_evidence", "runtime_evidence", "loaded_evidence"):
        for item in list(case.get(source_bucket) or []):
            if isinstance(item, dict):
                row = dict(item)
                row.setdefault("source_kind", "event" if source_bucket == "event_evidence" else "current_view")
                rows.append(row)

    def score(row: dict[str, Any]) -> int:
        level = str(row.get("level") or "").upper()
        value = 5000 if row.get("matched_anomaly_rules") else 0
        if level in {"ERROR", "FATAL", "CRITICAL", "ALARM"}:
            value += 4000
        elif level in {"WARN", "WARNING"}:
            value += 1200
        if str(row.get("source_kind") or "") == "event":
            value += 300
        return value

    # High-signal rows get the budget first; a few recent rows preserve local
    # sequence.  Raw page rows remain in the UI and are not model context.
    indexed = list(enumerate(rows))
    ranked = sorted(indexed, key=lambda pair: (-score(pair[1]), pair[0]))
    chosen_indexes = {idx for idx, _ in ranked[:36]}
    chosen_indexes.update(idx for idx, _ in indexed[-8:])

    seen: set[tuple[str, str, str]] = set()
    result: list[dict[str, Any]] = []
    for idx, row in indexed:
        if idx not in chosen_indexes:
            continue
        source = str(row.get("source_path") or row.get("source") or "")
        line = str(row.get("line_number") or row.get("line") or "")
        raw = str(row.get("raw") or row.get("message") or "").replace("\r", " ").replace("\n", " ").strip()[:1000]
        key = (source, line, raw[:420])
        if not raw or key in seen:
            continue
        seen.add(key)
        result.append({
            "time": row.get("time") or "",
            "component": row.get("component") or "",
            "level": row.get("level") or "",
            "source": source,
            "source_path": source,
            "line_number": row.get("line_number") or row.get("line"),
            "message": raw,
            "raw": raw,
            "source_kind": row.get("source_kind") or "current_view",
            "matched_anomaly_rules": list(row.get("matched_anomaly_rules") or [])[:8],
        })
        if len(result) >= 44:
            break
    return result


def _case_context(case: dict[str, Any], user_message: str) -> dict[str, Any]:
    request = case.get("current_log_request") if isinstance(case.get("current_log_request"), dict) else {}
    return {
        "case_id": str(case.get("case_id") or "")[:255],
        "case_name": str(case.get("case_name") or "")[:255],
        "case_description": str(case.get("case_description") or case.get("description") or "")[:4000],
        "user_request": str(user_message or "")[:4000],
        "report_facts": case.get("report_facts") if isinstance(case.get("report_facts"), dict) else {},
        "start_time": case.get("start_time") or request.get("start_time") or "",
        "end_time": case.get("end_time") or request.get("end_time") or "",
        "selected_targets": list(case.get("selected_targets") or request.get("fm_targets") or [])[:8],
    }


def _events_from_progress(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Map ATLog public progress to the shared TracePilot SSE event contract.

    We intentionally stream auditable execution summaries only (stage/tool/result),
    never hidden model chain-of-thought.  ``trace`` is kept for backwards
    compatibility while the explicit events make the frontend experience closer to
    mature agent runtimes such as TroubleShooter.
    """
    event_type = str(event.get("type") or "")
    if event_type == "answer_snapshot":
        return [{"type": "token_reset", "text": str(event.get("text") or "")}]
    if event_type == "stage":
        stage = event.get("stage") if isinstance(event.get("stage"), dict) else {}
        name = str(stage.get("name") or "atlog")
        raw_status = str(stage.get("status") or "running")
        status = "success" if raw_status == "completed" else raw_status
        title = str(stage.get("label") or _STAGE_LABELS.get(name) or ("执行原子能力" if name.startswith("tool:") else "用例分析"))
        detail = str(stage.get("detail") or "")[:900]
        try:
            progress = int(stage.get("progress") or _STAGE_PROGRESS.get(name, 0))
        except (TypeError, ValueError):
            progress = int(_STAGE_PROGRESS.get(name, 0))
        if status == "success" and name != "final":
            progress = min(99, progress + 2)
        trace = {
            "type": "trace",
            "trace": {
                "id": f"atlog-{name}",
                "stage": "tool" if name == "retrieve" else "evidence",
                "title": title,
                "status": status,
                "detail": detail,
            },
        }
        progress_event = {
            "type": "progress",
            "id": f"atlog-{name}",
            "stage": name,
            "title": title,
            "detail": detail,
            "status": status,
            "percentage": progress,
            "current_step": list(_STAGE_LABELS).index(name) + 1 if name in _STAGE_LABELS else 0,
            "total_steps": len(_STAGE_LABELS),
        }
        if name == "retrieve" or name.startswith("tool:"):
            tool_id = str(stage.get("action") or ("query_atlog_logs" if name == "retrieve" else name.split(":", 1)[-1]))
            tool_event = {
                "type": "tool_result" if status == "success" else "tool_call",
                "id": f"atlog-{name}-{tool_id or 'action'}",
                "tool_id": tool_id,
                "title": title,
                "detail": detail,
                "status": status,
            }
            return [progress_event, trace, tool_event]
        return [progress_event, trace]
    if event_type == "thinking":
        name = str(event.get("stage_name") or "atlog")
        title = _STAGE_LABELS.get(name, str(event.get("agent") or "正在分析"))
        detail = str(event.get("text") or "")[:900]
        return [
            {
                "type": "thinking",
                "id": f"atlog-{name}",
                "stage": name,
                "title": title,
                "detail": detail,
                "status": "running",
                "percentage": int(_STAGE_PROGRESS.get(name, 0)),
            },
            {
                "type": "trace",
                "trace": {
                    "id": f"atlog-{name}",
                    "stage": "thinking",
                    "title": title,
                    "status": "running",
                    "detail": detail,
                },
            },
        ]
    if event_type == "token_usage":
        return [{"type": "token_usage", "usage": event.get("usage") or {}}]
    return []


def format_result(result: dict[str, Any]) -> str:
    report = result.get("report") if isinstance(result.get("report"), dict) else {}
    summary = str(report.get("summary") or "").strip()
    root = str(report.get("root_cause") or "").strip()
    confidence = report.get("confidence")
    level = str(report.get("confidence_level") or "").strip()
    lines: list[str] = []
    if root:
        lines.append(f"**根因结论**：{root}")
    elif summary:
        lines.append(f"**分析结论**：{summary}")
    if summary and summary != root:
        lines.append(f"\n{summary}")
    if confidence not in (None, ""):
        lines.append(f"\n**置信度**：{confidence}%{f'（{level}）' if level else ''}")
    evidence = [item for item in list(report.get("evidence") or []) if isinstance(item, dict)][:6]
    if evidence:
        lines.append("\n**关键证据**")
        for item in evidence:
            prefix = " ".join(part for part in [str(item.get("time") or "").strip(), str(item.get("component") or "").strip()] if part)
            source = str(item.get("source") or "").strip()
            message = str(item.get("message") or "").strip().replace("\n", " ")[:500]
            where = " · ".join(part for part in [prefix, source] if part)
            lines.append(f"- {where + '：' if where else ''}{message}")
    recs = [str(item).strip() for item in list(report.get("recommendations") or []) if str(item).strip()][:4]
    if recs:
        lines.append("\n**建议**")
        lines.extend(f"- {item}" for item in recs)
    if not lines:
        lines.append("当前已完成用例证据分析，但没有形成可验证的根因结论。")
    return "\n".join(lines)


def run_atlog_workflow(
    *,
    message: str,
    context: dict[str, Any],
    emit: EventSink,
) -> dict[str, Any]:
    case = selected_case(context)
    url = _valid_url(case.get("case_url") or case.get("source_case_url") or case.get("url"))
    logger.info(
        "atlog.flow.start message=%s selected_case=%s resolved_case_url=%s",
        str(message or "")[:3000],
        json.dumps(case, ensure_ascii=False, default=str)[:14000],
        url[:1000],
    )
    if not url:
        raise ValueError("当前选中用例缺少有效 ATLog URL。")

    emit({
        "type": "agent_start",
        "title": "正在分析当前用例",
        "detail": "优先复用页面报告、event 和当前目标组件日志，缺什么再补什么",
        "percentage": 4,
    })
    emit({
        "type": "task", "status": "running", "phase": "atlog",
        "title": "正在分析当前用例", "detail": "优先复用页面报告、event 和当前目标组件日志，缺什么再补什么",
        "progress": 4,
    })

    def progress(event: dict[str, Any]) -> None:
        for mapped in _events_from_progress(event):
            emit(mapped)

    result = diagnose_case_with_ai(
        url,
        current_messages=_case_messages(case),
        anomaly_rules=list(case.get("anomaly_rules") or []),
        case_context={**_case_context(case, message), "session_id": str(context.get("session_id") or "")},
        progress_callback=progress,
    )
    text = format_result(result)
    emit({
        "type": "progress", "id": "atlog-final", "stage": "final",
        "title": "根因分析完成", "detail": "已基于当前可回链证据形成结论",
        "status": "success", "percentage": 100, "current_step": len(_STAGE_LABELS), "total_steps": len(_STAGE_LABELS),
    })
    emit({"type": "token_reset", "text": text})
    return {"result": result, "final_text": text}
