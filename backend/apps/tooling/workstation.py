"""Public action records and bounded log plans. No model reasoning is exposed."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

WINDOW_TIERS = {"narrow": 30, "standard": 120, "wide": 600}
MAX_QUERY_LINES = 500
MAX_COMPONENTS = 4


def public_snapshot(value: Any) -> Any:
    """Bound trace details and redact credential fields (including nested values)."""
    def clean(item: Any, depth: int = 0) -> Any:
        if depth > 6:
            return "[深层数据已省略]"
        if isinstance(item, dict):
            return {str(k): "[已隐藏]" if re.search(r"password|passwd|secret|token|authorization|cookie|credential|api.?key", str(k), re.I) else clean(v, depth + 1)
                    for k, v in list(item.items())[:40] if not str(k).startswith("_")}
        if isinstance(item, (list, tuple)):
            return [clean(v, depth + 1) for v in item[:12]]
        if isinstance(item, str):
            item = re.sub(r"(?i)(password|token|secret|api_key|authorization)([\s:=]+)[^\s,;&]+", r"\1\2[已隐藏]", item)
            return item[:1200]
        return item
    result = clean(value)
    encoded = json.dumps(result, ensure_ascii=False, default=str)
    return result if len(encoded) <= 10000 else {"excerpt": encoded[:10000], "truncated": True}


def retrieval_plan(payload: dict[str, Any]) -> dict[str, Any]:
    from apps.tooling.services import ToolInputError
    tier = str(payload.get("tier") or "narrow")
    if tier not in WINDOW_TIERS:
        raise ToolInputError("扩窗档位必须是 narrow / standard / wide")
    reason = str(payload.get("reason") or "").strip()
    if not reason:
        raise ToolInputError("检索计划必须说明依据；扩窗需解释上一档缺失的证据")
    try:
        anchor = datetime.fromisoformat(str(payload.get("anchor_time") or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolInputError("anchor_time 必须来自失败断言或 event 的真实时间") from exc
    targets = payload.get("targets") or []
    if not isinstance(targets, list) or len(targets) > MAX_COMPONENTS or any(not isinstance(t, dict) or not t.get("subsystem") or not t.get("module") for t in targets):
        raise ToolInputError("最多选择 4 个明确的 subsystem/module 目标")
    seconds = WINDOW_TIERS[tier]
    return {
        "stage": "retrieval_plan", "tier": tier, "anchor_time": anchor.isoformat(),
        "start_time": (anchor - timedelta(seconds=seconds)).isoformat(),
        "end_time": (anchor + timedelta(seconds=seconds)).isoformat(),
        "targets": targets, "reason": reason[:800],
        "trace_id": str(payload.get("trace_id") or "")[:128],
        "error_code": str(payload.get("error_code") or "")[:128],
        "max_lines": MAX_QUERY_LINES, "max_components": MAX_COMPONENTS,
        "correlation": "trace" if payload.get("trace_id") else "candidate",
        "warning": "时间邻近或相同错误码仅表示候选关联，不足以证明因果关系",
    }


def bound_query(payload: dict[str, Any], *, debug: bool) -> dict[str, Any]:
    from apps.tooling.services import ToolInputError
    result = dict(payload)
    try:
        start = datetime.fromisoformat(str(result.get("start_time") or "").replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(result.get("end_time") or "").replace("Z", "+00:00"))
        seconds = (end - start).total_seconds()
        if seconds <= 0 or seconds > 1200:
            raise ValueError()
        result["max_lines"] = max(100, min(int(result.get("max_lines") or MAX_QUERY_LINES), MAX_QUERY_LINES))
    except (ValueError, TypeError, OverflowError) as exc:
        raise ToolInputError("AI 检索需有效且不超过 20 分钟的时间窗；先以失败时间生成三档检索计划") from exc
    targets = result.get("targets") or []
    if debug and (not isinstance(targets, list) or not 1 <= len(targets) <= MAX_COMPONENTS or any(not isinstance(t, dict) or not t.get("subsystem") or not t.get("module") for t in targets)):
        raise ToolInputError("debug 检索必须限定 1–4 个真实 subsystem/module，禁止全组件扫描")
    if debug:
        result["max_lines"] = max(result["max_lines"], 100 * len(targets))
    return result


def control_log_view(payload: dict[str, Any]) -> dict[str, Any]:
    from apps.tooling.services import ToolInputError
    allowed = {"task_id", "entry_id", "query", "errors_only", "components", "start_time", "end_time", "folding_enabled"}
    action = {k: v for k, v in payload.items() if k in allowed}
    if not action.get("task_id"):
        raise ToolInputError("当前页面没有可操作的日志任务")
    for key in ("errors_only", "folding_enabled"):
        if key in action and not isinstance(action[key], bool):
            raise ToolInputError(f"{key} 必须是 boolean")
    if "components" in action and (not isinstance(action["components"], list) or len(action["components"]) > 16):
        raise ToolInputError("components 必须是最多 16 项的数组")
    return {"ui_action": {"type": "control_log_view", **action}}


def evidence_action(tool_id: str, arguments: dict[str, Any], value: Any) -> dict[str, Any] | None:
    if tool_id not in {"query_atlog_event", "query_atlog_logs"} or not isinstance(value, dict):
        return None
    rows = value.get("rows")
    if not isinstance(rows, list):
        return None
    # UI receives actual rows; model context is independently compacted by the kernel.
    result = {k: v for k, v in value.items() if k in {"base_url", "start_time", "end_time", "count", "truncated", "components", "levels", "sources", "log_catalog", "event_raw_text", "event_query", "partial_failure", "component_errors", "cache_hit"}}
    result["rows"] = rows[:MAX_QUERY_LINES]
    if tool_id == "query_atlog_event":
        result["sources"] = ["event.log"]
        result["event_raw_text"] = value.get("raw_text", "")
        result["event_query"] = {"requested": True, "found": bool(rows)}
    return {"type": "show_atlog_evidence", "case_url": value.get("base_url") or arguments.get("url"),
            "source_kind": "event" if tool_id == "query_atlog_event" else "debug", "targets": arguments.get("targets") or [], "result": result}
