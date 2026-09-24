from __future__ import annotations

from typing import Any, Callable, TypedDict

EventSink = Callable[[dict[str, Any]], None]


class ExecutionState(TypedDict, total=False):
    session_id: str
    task_id: str
    context_snapshot: dict[str, Any]
    stage: str
    progress: int
    current_tool: str
    updated_at: str
    message: str
    history: list[dict[str, Any]]
    context: dict[str, Any]
    requested_skill: str
    memory: str
    run_id: str
    event_sink: EventSink
    streaming: bool
    client: Any
    kernel: Any
    semantic_route: dict[str, Any]
    normalized_skill: str
    selected_tool_ids: set[str]
    tools: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    max_rounds: int
    round_index: int
    forced_tool_id: str
    data_extraction_requested: bool
    content: str
    tool_calls: list[dict[str, Any]]
    redirected: bool
    confirmations: list[dict[str, Any]]
    result_cards: list[dict[str, Any]]
    selection_pending: bool
    tool_memory_facts: list[str]
    final_text: str
    turn_memory: str
    steps: list[dict[str, Any]]
    ui_actions: list[dict[str, Any]]
    suggested_actions: list[dict[str, Any]]
    fast_path: bool
    special_workflow: str
    atlog_result: dict[str, Any]

AgentState = ExecutionState
