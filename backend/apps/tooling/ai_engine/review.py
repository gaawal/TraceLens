from __future__ import annotations

"""LangGraph control plane for TracePilot.

The browser and normal TraceLens pages keep calling the existing deterministic
services directly.  This module is only the AI orchestration control layer: it
chooses a short/complex Agent budget and enforces bounded workflow transitions
(such as data-extraction creation and subsystem-scoped log-skill fallback)
without reimplementing any Tool handler.
"""

from typing import Any, Literal, TypedDict

try:  # Keep local static/test environments usable even before dependencies are installed.
    from langgraph.graph import END, START, StateGraph
except ModuleNotFoundError:  # pragma: no cover - production requirements include langgraph.
    END = START = None
    StateGraph = None


class TracePilotRouteState(TypedDict, total=False):
    message: str
    skill_id: str
    mode: Literal["fast", "agent"]
    max_rounds: int
    reason: str


class TracePilotReviewState(TypedDict, total=False):
    tool_id: str
    tool_result: dict[str, Any]
    selected_tool_ids: list[str]
    data_extraction_requested: bool
    has_page_log_evidence: bool
    workflow: str
    forced_tool_id: str
    instruction: str


def _route_node(state: TracePilotRouteState) -> TracePilotRouteState:
    """Legacy graph budget node; semantic intent routing lives in assistant.py.

    Do not infer capabilities from user keywords here. The LLM routing Agent selects
    Skill/Tool semantics before this graph is involved.
    """
    skill_id = str(state.get("skill_id") or "auto")
    return {"mode": "agent", "max_rounds": 5, "reason": f"semantic_router:{skill_id}"}


def _review_classify(state: TracePilotReviewState) -> TracePilotReviewState:
    tool_id = str(state.get("tool_id") or "")
    if tool_id == "list_data_extraction_rules" and state.get("data_extraction_requested"):
        return {"workflow": "data_extraction"}
    if tool_id == "query_environment_logs":
        return {"workflow": "log_evidence"}
    if tool_id == "match_log_query_skills":
        return {"workflow": "log_skill_match"}
    if tool_id == "query_log_query_skill_step":
        return {"workflow": "log_skill_step"}
    return {"workflow": "continue"}


def _route_review(state: TracePilotReviewState) -> str:
    return str(state.get("workflow") or "continue")


def _data_extraction_guard(state: TracePilotReviewState) -> TracePilotReviewState:
    value = state.get("tool_result") if isinstance(state.get("tool_result"), dict) else {}
    allowed = set(state.get("selected_tool_ids") or [])
    count = int(value.get("count") or 0)
    if count > 0 and "run_data_extraction" in allowed:
        return {
            "forced_tool_id": "run_data_extraction",
            "instruction": "用户明确要求数据提取，已有提取器可复用。下一轮必须调用 run_data_extraction 使用刚才返回的规则，不要提前结束任务。",
        }
    if count == 0 and state.get("has_page_log_evidence") and "create_data_extraction_capability" in allowed:
        return {
            "forced_tool_id": "create_data_extraction_capability",
            "instruction": "已有提取器匹配数为 0，但当前页面存在真实日志证据。下一轮必须比较重复日志族的稳定文本与变化字段，并基于真实样例创建候选提取规则；不要把 0 个提取器当作任务完成。",
        }
    return {}


def _log_evidence_guard(state: TracePilotReviewState) -> TracePilotReviewState:
    value = state.get("tool_result") if isinstance(state.get("tool_result"), dict) else {}
    allowed = set(state.get("selected_tool_ids") or [])
    status = str(value.get("evidence_status") or "")
    if status not in {"no_match", "source_empty", "source_not_found"} or "match_log_query_skills" not in allowed:
        return {}
    targets = [item for item in list(value.get("targets") or []) if isinstance(item, dict)]
    first = targets[0] if targets else {}
    subsystem = str(first.get("subsystem") or "").strip()
    module = str(first.get("fm") or first.get("module") or "").strip()
    if not subsystem:
        return {}
    return {
        "forced_tool_id": "match_log_query_skills",
        "instruction": (
            "标准固定路径日志查询没有取得有效证据。下一轮必须调用 match_log_query_skills，"
            f"subsystem={subsystem!r}、module={module!r}，并把刚才的真实工具结果作为 evidence_text。"
            "只能匹配这个子系统的 Skill；若没有匹配 Skill，再明确说明缺少补充检索策略。"
        ),
    }


def _log_skill_match_guard(state: TracePilotReviewState) -> TracePilotReviewState:
    value = state.get("tool_result") if isinstance(state.get("tool_result"), dict) else {}
    allowed = set(state.get("selected_tool_ids") or [])
    if int(value.get("count") or 0) <= 0 or "query_log_query_skill_step" not in allowed:
        return {}
    return {
        "forced_tool_id": "query_log_query_skill_step",
        "instruction": (
            "已经匹配到当前子系统的日志查询 Skill。下一轮调用 query_log_query_skill_step，"
            "优先选择 matches 中得分最高的 Skill，并执行满足当前证据状态的最前一个必要步骤。"
            "environment_id/start_time/end_time 必须沿用当前任务真实上下文或上一轮标准日志查询范围；不得构造新的路径。"
        ),
    }


def _log_skill_step_guard(state: TracePilotReviewState) -> TracePilotReviewState:
    value = state.get("tool_result") if isinstance(state.get("tool_result"), dict) else {}
    allowed = set(state.get("selected_tool_ids") or [])
    status = str(value.get("evidence_status") or value.get("status") or "").strip().lower()
    next_step = value.get("next_step") if isinstance(value.get("next_step"), dict) else {}
    if status not in {"no_match", "source_empty", "source_not_found"}:
        return {}
    if not next_step or "query_log_query_skill_step" not in allowed:
        return {}
    try:
        next_index = int(next_step.get("index"))
    except (TypeError, ValueError):
        return {}
    skill_id = value.get("skill_id")
    skill_name = str(value.get("skill_name") or "日志查询 Skill")
    step_name = str(next_step.get("name") or f"步骤 {next_index + 1}")
    return {
        "forced_tool_id": "query_log_query_skill_step",
        "instruction": (
            f"{skill_name} 的当前补充检索步骤仍未取得有效证据。下一轮继续调用 query_log_query_skill_step，"
            f"skill_id={skill_id!r}、step_index={next_index}（{step_name}）。"
            "environment_id/start_time/end_time 沿用当前任务真实范围；不得改到其他子系统，也不得自行构造远端路径。"
        ),
    }


def _continue_guard(state: TracePilotReviewState) -> TracePilotReviewState:
    return {}


def _build_route_graph():
    if StateGraph is None:
        return None
    graph = StateGraph(TracePilotRouteState)
    graph.add_node("route", _route_node)
    graph.add_edge(START, "route")
    graph.add_edge("route", END)
    return graph.compile()


def _build_review_graph():
    if StateGraph is None:
        return None
    graph = StateGraph(TracePilotReviewState)
    graph.add_node("classify", _review_classify)
    graph.add_node("data_extraction", _data_extraction_guard)
    graph.add_node("log_evidence", _log_evidence_guard)
    graph.add_node("log_skill_match", _log_skill_match_guard)
    graph.add_node("log_skill_step", _log_skill_step_guard)
    graph.add_node("continue", _continue_guard)
    graph.add_edge(START, "classify")
    graph.add_conditional_edges(
        "classify",
        _route_review,
        {
            "data_extraction": "data_extraction",
            "log_evidence": "log_evidence",
            "log_skill_match": "log_skill_match",
            "log_skill_step": "log_skill_step",
            "continue": "continue",
        },
    )
    for node in ("data_extraction", "log_evidence", "log_skill_match", "log_skill_step", "continue"):
        graph.add_edge(node, END)
    return graph.compile()


TRACEPILOT_ROUTE_GRAPH = _build_route_graph()
TRACEPILOT_REVIEW_GRAPH = _build_review_graph()


def route_tracepilot_request(message: str, skill_id: str) -> TracePilotRouteState:
    initial: TracePilotRouteState = {"message": str(message or ""), "skill_id": str(skill_id or "auto")}
    if TRACEPILOT_ROUTE_GRAPH is None:
        return {**initial, **_route_node(initial)}
    return TRACEPILOT_ROUTE_GRAPH.invoke(initial)


def review_tracepilot_tool_result(
    *,
    tool_id: str,
    tool_result: dict[str, Any],
    selected_tool_ids: set[str],
    data_extraction_requested: bool,
    has_page_log_evidence: bool,
) -> TracePilotReviewState:
    initial: TracePilotReviewState = {
        "tool_id": tool_id,
        "tool_result": tool_result,
        "selected_tool_ids": sorted(selected_tool_ids),
        "data_extraction_requested": bool(data_extraction_requested),
        "has_page_log_evidence": bool(has_page_log_evidence),
    }
    if TRACEPILOT_REVIEW_GRAPH is None:
        classified = {**initial, **_review_classify(initial)}
        workflow = _route_review(classified)
        node = {
            "data_extraction": _data_extraction_guard,
            "log_evidence": _log_evidence_guard,
            "log_skill_match": _log_skill_match_guard,
            "log_skill_step": _log_skill_step_guard,
        }.get(workflow, _continue_guard)
        return {**classified, **node(classified)}
    return TRACEPILOT_REVIEW_GRAPH.invoke(initial)
