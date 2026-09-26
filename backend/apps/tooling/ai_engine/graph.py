from __future__ import annotations

"""LangGraph execution engine for TracePilot.

The graph owns *orchestration only*.  Every action is still an atomic capability
adapted through ``apps.tooling.kernel``.  The graph routes, plans, invokes one or
more atomic capabilities, reviews deterministic results, and either loops for
more evidence or finalizes the answer.

This module intentionally does not know individual business implementations
(SSH, deployment, logs, ATLog, data extraction, navigation).  Those stay behind
Core Kernel and the Tool registry so the Agent can compose them without
coupling the control plane to feature code.
"""

import contextvars
import json
import logging
import queue
import re
import threading
import uuid
from apps.tooling.workstation import bound_query, evidence_action, public_snapshot
from apps.tooling.assistant_runtime.state import RUN_STATE
from typing import Any, Callable, Iterator, Literal

from langgraph.graph import END, START, StateGraph

from apps.tooling.llm import LLMClientError, get_llm_client
from apps.tooling.ai_engine.review import review_tracepilot_tool_result
from apps.tooling.ai_engine.atlog_flow import run_atlog_workflow, should_run_atlog_workflow
from apps.tooling.ai_engine.router import SemanticRouter
from apps.tooling.ai_engine.state import ExecutionState
from apps.tooling.kernel import ToolRequest, get_default_kernel

logger = logging.getLogger("tracelens.assistant.graph")

EventSink = Callable[[dict[str, Any]], None]


class GraphExecutionError(RuntimeError):
    pass


def _assistant():
    # Deferred import avoids a module cycle: apps.tooling.assistant is the public
    # compatibility facade and imports this engine only when chat/chat_stream runs.
    from apps.tooling import assistant as assistant_module

    return assistant_module


def _emit(state: ExecutionState, event: dict[str, Any]) -> None:
    if event.get("type") == "task":
        state["stage"] = str(event.get("phase") or state.get("stage") or "")
        state["progress"] = max(0,min(100,int(event.get("progress") or state.get("progress") or 0)))
    if event.get("type") == "tool_call": state["current_tool"] = str(event.get("tool_id") or "")
    from datetime import datetime, timezone
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    sink = state.get("event_sink")
    if callable(sink):
        sink(event)


def _consume_generator(generator: Iterator[dict[str, Any]], state: ExecutionState) -> Any:
    while True:
        try:
            _emit(state, next(generator))
        except StopIteration as stop:
            return stop.value


def _prepare(state: ExecutionState) -> dict[str, Any]:
    a = _assistant()
    message = str(state.get("message") or "").strip()
    if not message:
        raise a.AssistantError("请输入任务内容。")

    context = state.get("context") if isinstance(state.get("context"), dict) else {}
    context = dict(context)
    history = state.get("history") if isinstance(state.get("history"), list) else []
    memory = str(state.get("memory") or "")
    run_id = str(state.get("run_id") or "").strip()[:96]
    a.register_run(run_id)

    selected_log_target = a._selected_log_target_from_message(message)
    if selected_log_target:
        context["_assistant_selected_log_target"] = selected_log_target

    client = get_llm_client(session_id=state.get("session_id", ""))
    kernel = get_default_kernel()
    requested_skill = a._normalize_skill_id(state.get("requested_skill"))
    # Stream a useful event *before* the semantic-router LLM call.  Without this
    # the UI sits on a generic placeholder while the first model request is in
    # flight, which feels stalled even though work has already started.
    _emit(state, {
        "type": "task", "status": "running", "phase": "routing",
        "title": "正在理解任务", "detail": "结合当前页面上下文选择最合适的能力", "progress": 3,
    })
    _emit(state, {
        "type": "thinking", "id": "semantic-route", "stage": "routing",
        "title": "正在理解任务", "detail": "结合当前页面上下文选择最合适的能力",
        "status": "running", "percentage": 3,
    })
    semantic_route = SemanticRouter(kernel.registry, event_sink=lambda event: _emit(state, event)).route(
        client,
        message=message,
        context=context,
        skill_id=requested_skill,
    )
    normalized_skill = a._normalize_skill_id(str(semantic_route.get("skill_id") or requested_skill))
    route_reason = str(semantic_route.get("reason") or "").strip()
    route_detail = f"已选择 {normalized_skill} 能力" + (f"：{route_reason[:180]}" if route_reason else "")
    _emit(state, {
        "type": "thinking", "id": "semantic-route", "stage": "routing",
        "title": "任务理解完成", "detail": route_detail, "status": "success", "percentage": 8,
    })
    special_workflow = ""
    if should_run_atlog_workflow(semantic_route, context, normalized_skill):
        special_workflow = "atlog_diagnosis"

    # Semantic Agent may choose the deterministic one-shot deployment workflow.
    if str(semantic_route.get("workflow_hint") or "") == "deployment_reuse_last_success":
        confirmation = a._last_success_deployment_confirmation(message, context, semantic_trigger=True)
        if confirmation is not None:
            final_text = a._confirmation_pending_text([confirmation])
            turn_memory = a._build_turn_memory(
                previous_memory=memory,
                user_message=message,
                skill_id="deployment",
                context=context,
                tool_facts=["部署参数来源=最近一次成功部署；已应用本轮用户覆盖并完成预览校验"],
                final_text=final_text,
            )
            _emit(state, {"type": "token_usage", "usage": a._usage_payload(0, 0, estimated=False)})
            _emit(state, {"type": "confirmation", "confirmation": confirmation})
            _emit(state, {"type": "token_reset", "text": final_text})
            return {
                "context": context,
                "history": history,
                "client": client,
                "kernel": kernel,
                "semantic_route": semantic_route,
                "normalized_skill": "deployment",
                "confirmations": [confirmation],
                "result_cards": [],
                "selection_pending": False,
                "tool_memory_facts": ["部署参数来源=最近一次成功部署；已应用本轮用户覆盖并完成预览校验"],
                "final_text": final_text,
                "turn_memory": turn_memory,
                "steps": [],
                "ui_actions": [],
                "suggested_actions": [],
                "fast_path": True,
            }

    selected_tool_ids = set(str(item).strip() for item in list(semantic_route.get("tool_ids") or []) if str(item).strip())
    if selected_tool_ids & {"query_atlog_event", "query_atlog_logs"}:
        selected_tool_ids.add("plan_log_retrieval")
    if isinstance(context.get("log_locator"), dict) and context["log_locator"].get("task_id"):
        selected_tool_ids.add("control_log_view")
    # 「让用户选择」是**通用交互**，不属于某个领域：不论本轮路由选了哪个领域都要留给 Agent，
    # 否则模型只能把选项写进正文让用户手打（用户明确要求改成可点选的固定选择组件）。
    if kernel.registry.get("ask_user_choice") is not None:
        selected_tool_ids.add("ask_user_choice")
    tools = kernel.registry.llm_specs(selected_tool_ids)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": a._system_prompt(context, normalized_skill, selected_tool_ids)},
        *a._memory_message(memory),
        *([] if memory.strip() else a._history_messages(history)),
        {"role": "user", "content": message[:12_000]},
    ]
    max_rounds = int(a._semantic_route_round_budget(semantic_route))
    data_extraction_requested = bool(
        {"list_data_extraction_rules", "create_data_extraction_capability", "run_data_extraction"}
        & selected_tool_ids
    )

    logger.info(
        "assistant.graph.prepare run=%s skill=%s mode=%s rounds=%s tools=%s reason=%s",
        run_id,
        normalized_skill,
        semantic_route.get("mode"),
        max_rounds,
        ",".join(sorted(selected_tool_ids)),
        semantic_route.get("reason"),
    )

    page_title, page_detail = a._page_evidence_trace(context)
    _emit(state, {"type": "task", "status": "running", "phase": "evidence", "title": page_title, "detail": page_detail})
    _emit(state, a._trace_event("page-evidence", "evidence", page_title, status="success", detail=page_detail))

    return {
        "context": context,
        "history": history,
        "client": client,
        "kernel": kernel,
        "semantic_route": semantic_route,
        "normalized_skill": normalized_skill,
        "selected_tool_ids": selected_tool_ids,
        "tools": tools,
        "messages": messages,
        "max_rounds": max_rounds,
        "round_index": 1,
        "forced_tool_id": "",
        "data_extraction_requested": data_extraction_requested,
        "confirmations": [],
        "result_cards": [],
        "selection_pending": False,
        "tool_memory_facts": [],
        "steps": [],
        "ui_actions": [],
        "suggested_actions": [],
        "fast_path": False,
        "special_workflow": special_workflow,
        "final_text": "",
    }


def _route_after_prepare(state: ExecutionState) -> Literal["plan", "atlog", "finalize"]:
    if state.get("fast_path"):
        return "finalize"
    if state.get("special_workflow") == "atlog_diagnosis":
        return "atlog"
    return "plan"


def _atlog_diagnosis(state: ExecutionState) -> dict[str, Any]:
    a = _assistant()
    run_id = str(state.get("run_id") or "")
    if a._run_cancelled(run_id):
        raise a.AssistantCancelled("用户已终止当前分析。")

    outcome = run_atlog_workflow(
        message=str(state.get("message") or ""),
        context=state.get("context") if isinstance(state.get("context"), dict) else {},
        emit=lambda event: _emit(state, event),
    )
    final_text = str(outcome.get("final_text") or "").strip()
    result = outcome.get("result") if isinstance(outcome.get("result"), dict) else {}
    report = result.get("report") if isinstance(result.get("report"), dict) else {}
    tool_facts = [
        f"ATLog 根因={str(report.get('root_cause') or report.get('summary') or '')[:700]}",
        f"ATLog 置信度={report.get('confidence', 0)}",
    ]
    turn_memory = a._build_turn_memory(
        previous_memory=state.get("memory"),
        user_message=str(state.get("message") or ""),
        skill_id="atlog",
        context=state.get("context") or {},
        tool_facts=tool_facts,
        final_text=final_text,
    )
    return {
        "final_text": final_text,
        "atlog_result": result,
        "tool_memory_facts": tool_facts,
        "turn_memory": turn_memory,
        "steps": list(state.get("steps") or []),
        "confirmations": [],
        "selection_pending": False,
        "result_cards": list(state.get("result_cards") or []),
    }


def _finish_from_existing_evidence(state: ExecutionState, *, reason: str) -> str:
    """Stop tool expansion and synthesize the best answer from collected facts."""
    a = _assistant()
    facts = list(state.get("tool_memory_facts") or [])
    _emit(state, {
        "type": "task", "status": "running", "phase": "answer",
        "title": "正在整理现有证据", "detail": reason,
    })
    try:
        content, _ = _consume_generator(
            a._stream_completion(
                state["client"],
                a._compact_retry_messages(
                    user_message=str(state.get("message") or ""),
                    context=state.get("context") or {},
                    tool_memory_facts=facts,
                ),
                tools=None,
                tool_choice="none",
                round_index=int(state.get("round_index") or 1) + 900,
                run_id=str(state.get("run_id") or ""),
                emit_tokens=True,
            ),
            state,
        )
        final_text = str(content or "").strip()
        if not final_text or a._model_output_looks_like_control_text(final_text):
            final_text = a._fallback_answer_from_facts(facts)
    except Exception:  # noqa: BLE001 - final synthesis must not lose gathered evidence
        logger.exception("assistant.graph.force_finish.failed")
        final_text = a._fallback_answer_from_facts(facts)
    _emit(state, {"type": "token_reset", "text": final_text})
    return final_text


def _plan(state: ExecutionState) -> dict[str, Any]:
    a = _assistant()
    run_id = str(state.get("run_id") or "")
    if a._run_cancelled(run_id):
        raise a.AssistantCancelled("用户已终止当前分析。")

    round_index = int(state.get("round_index") or 1)
    max_rounds = int(state.get("max_rounds") or 1)
    if round_index > max_rounds:
        final_text = _finish_from_existing_evidence(
            state,
            reason="已达到本轮安全取证上限，停止继续调用工具并基于现有证据形成结论。",
        )
        return {"final_text": final_text, "content": "", "tool_calls": [], "redirected": False}

    messages = list(state.get("messages") or [])
    pending_guidance = a._drain_run_guidance(run_id)
    if pending_guidance:
        guidance_text = "；".join(pending_guidance)
        messages.append({
            "role": "user",
            "content": "执行过程中用户补充了新的引导，请作为当前最高优先级约束继续：" + guidance_text,
        })
        _emit(state, {"type": "task", "status": "running", "phase": "guidance", "title": "已接收新的分析引导", "detail": guidance_text[:240]})
        _emit(state, a._trace_event(f"guidance-{round_index}", "guidance", "已应用用户引导", status="success", detail=guidance_text[:320]))

    forced_tool_id = str(state.get("forced_tool_id") or "").strip()
    tool_choice: Any = "auto"
    if forced_tool_id:
        tool_choice = {"type": "function", "function": {"name": forced_tool_id}}
        logger.info("assistant.graph.force_tool round=%s tool=%s", round_index, forced_tool_id)

    description = "选择最少必要工具" if round_index == 1 else "根据已取得证据继续执行或形成结论"
    title = "正在判断下一步" if round_index == 1 else "正在根据新证据继续判断"
    trace_id = f"plan-{round_index}"
    _emit(state, {"type": "task", "status": "running", "phase": "planning", "title": title, "detail": description})
    _emit(state, {"type": "thinking", "id": trace_id, "stage": "planning", "title": title, "detail": description, "status": "running"})
    _emit(state, a._trace_event(trace_id, "thinking", title, status="running", detail=description))

    try:
        content, tool_calls = _consume_generator(
            a._stream_completion(
                state["client"],
                messages,
                tools=state.get("tools") or [],
                tool_choice=tool_choice,
                round_index=round_index,
                run_id=run_id,
                emit_tokens=True,
                progress_trace_id=trace_id,
                progress_title=title,
            ),
            state,
        )
    except LLMClientError:
        logger.exception("assistant.graph.llm.failed round=%s", round_index)
        raise

    if a._run_cancelled(run_id):
        raise a.AssistantCancelled("用户已终止当前分析。")

    tool_memory_facts = list(state.get("tool_memory_facts") or [])
    if a._model_output_looks_like_control_text(content):
        logger.warning(
            "assistant.graph.control_text round=%s has_tools=%s sample=%s",
            round_index,
            bool(tool_calls),
            re.sub(r"\s+", " ", content)[:800],
        )
        if tool_calls:
            content = ""
        else:
            _emit(state, {
                "type": "task",
                "status": "running",
                "phase": "answer",
                "title": "正在整理有效回答",
                "detail": "模型返回了无效控制文本，正在使用精简上下文重试一次",
            })
            retry_content, _ignored = _consume_generator(
                a._stream_completion(
                    state["client"],
                    a._compact_retry_messages(
                        user_message=state["message"],
                        context=state["context"],
                        tool_memory_facts=tool_memory_facts,
                    ),
                    tools=None,
                    tool_choice="none",
                    round_index=round_index + 500,
                    run_id=run_id,
                    emit_tokens=True,
                ),
                state,
            )
            content = (
                retry_content.strip()
                if retry_content and not a._model_output_looks_like_control_text(retry_content)
                else a._fallback_answer_from_facts(tool_memory_facts)
            )

    redirect_guidance = a._drain_run_guidance(run_id)
    if redirect_guidance:
        guidance_text = "；".join(redirect_guidance)
        _emit(state, {"type": "token_reset", "text": ""})
        if content:
            messages.append({"role": "assistant", "content": content})
        messages.append({
            "role": "user",
            "content": "用户刚刚改变了分析方向。停止执行刚才尚未开始的计划，按新引导重新规划：" + guidance_text,
        })
        _emit(state, a._trace_event(trace_id, "planning", "本轮规划已被用户调整", status="interrupted", detail="停止执行尚未开始的计划，按新的用户引导重新规划"))
        _emit(state, {"type": "task", "status": "running", "phase": "guidance", "title": "正在按新方向重新规划", "detail": guidance_text[:240]})
        return {
            "messages": messages,
            "forced_tool_id": "",
            "content": "",
            "tool_calls": [],
            "redirected": True,
            "round_index": round_index + 1,
        }

    assistant_payload: dict[str, Any] = {"role": "assistant", "content": content or ""}
    if tool_calls:
        assistant_payload["tool_calls"] = tool_calls
    messages.append(assistant_payload)

    if tool_calls:
        planned_names: list[str] = []
        planned_ids: list[str] = []
        for call in tool_calls:
            fn = call.get("function") if isinstance(call, dict) else {}
            fn = fn if isinstance(fn, dict) else {}
            tool_id = str(fn.get("name") or "").strip()
            if tool_id:
                planned_ids.append(tool_id)
            tool = state["kernel"].registry.get(tool_id) if tool_id else None
            planned_names.append(tool.name if tool is not None else tool_id or "工具")
        quiet_only = bool(planned_ids) and all(a._quiet_tool(item) for item in planned_ids)
        if quiet_only:
            _emit(state, a._trace_event(trace_id, "planning", "已完成当前判断", status="success", detail="继续处理当前请求"))
        else:
            copy = "、".join(planned_names[:3])
            _emit(state, a._trace_event(trace_id, "planning", "已确定下一步操作", status="success", detail="准备执行：" + copy))
            _emit(state, {"type": "task", "status": "running", "phase": "tool", "title": "下一步：" + copy, "detail": "等待工具返回实际数据"})
        return {
            "messages": messages,
            "forced_tool_id": "",
            "content": content,
            "tool_calls": tool_calls,
            "redirected": False,
        }

    _emit(state, a._trace_event(trace_id, "planning", "已完成当前判断", status="success", detail="现有证据足够，进入结论整理"))
    if a._answer_has_substance(content) and not a._model_output_looks_like_control_text(content):
        final_text = content.strip()
    else:
        _emit(state, {"type": "task", "status": "running", "phase": "answer", "title": "正在形成结论", "detail": "正在根据已取得的实际结果整理回答"})
        retry_text, _ignored = _consume_generator(
            a._stream_completion(
                state["client"],
                a._compact_retry_messages(
                    user_message=state["message"],
                    context=state["context"],
                    tool_memory_facts=tool_memory_facts,
                ),
                tools=None,
                tool_choice="none",
                round_index=round_index + 300,
                run_id=run_id,
                emit_tokens=True,
            ),
            state,
        )
        final_text = (
            retry_text.strip()
            if retry_text and not a._model_output_looks_like_control_text(retry_text)
            else a._fallback_answer_from_facts(tool_memory_facts)
        )
    _emit(state, {"type": "token_reset", "text": final_text})
    return {
        "messages": messages,
        "content": content,
        "tool_calls": [],
        "redirected": False,
        "final_text": final_text,
    }


def _route_after_plan(state: ExecutionState) -> Literal["plan", "execute", "finalize"]:
    if state.get("final_text"):
        return "finalize"
    if state.get("redirected"):
        if int(state.get("round_index") or 1) > int(state.get("max_rounds") or 1):
            return "finalize"
        return "plan"
    if state.get("tool_calls"):
        return "execute"
    return "finalize"


def _execute(state: ExecutionState) -> dict[str, Any]:
    a = _assistant()
    run_id = str(state.get("run_id") or "")
    if a._run_cancelled(run_id):
        raise a.AssistantCancelled("用户已终止当前分析。")

    round_index = int(state.get("round_index") or 1)
    messages = list(state.get("messages") or [])
    confirmations = list(state.get("confirmations") or [])
    result_cards = list(state.get("result_cards") or [])
    tool_memory_facts = list(state.get("tool_memory_facts") or [])
    steps = list(state.get("steps") or [])
    ui_actions = list(state.get("ui_actions") or [])
    selection_pending = bool(state.get("selection_pending"))
    forced_tool_id = ""
    # Notes that must not be interleaved with the assistant's tool_calls / tool replies.
    # An OpenAI-compatible gateway requires every `tool` message to directly follow the
    # assistant message that requested it, so a `system` note appended mid-loop makes the
    # whole next request fail with HTTP 400.
    deferred_system_notes: list[str] = []

    for call_index, call in enumerate(list(state.get("tool_calls") or []), start=1):
        if a._run_cancelled(run_id):
            raise a.AssistantCancelled("用户已终止当前分析。")

        fn = call.get("function") if isinstance(call, dict) else {}
        fn = fn if isinstance(fn, dict) else {}
        tool_id = str(fn.get("name") or "").strip()
        call_id = str(call.get("id") or f"call-{round_index}-{call_index}")
        tool = state["kernel"].registry.get(tool_id)
        trace_id = f"tool-{round_index}-{call_index}"

        # Use the registry's invokability rule, not `tool.handler is None`: plugin tools
        # (FunctionToolPlugin) keep their callable on the adapter, so the raw check rejected
        # every plugin tool with "工具不可调用" even though the router had just offered it.
        if tool is None or not state["kernel"].registry.is_invokable(tool):
            result: Any = {"success": False, "error": "工具不可调用。"}
            steps.append({"tool_id": tool_id, "name": tool_id, "status": "failed", "message": "工具不可调用"})
            _emit(state, a._trace_event(trace_id, "tool", tool_id or "未知工具", status="failed", detail="工具不可调用"))
        else:
            try:
                arguments = a._parse_arguments(fn.get("arguments"))
                arguments = state["kernel"].context.inject(tool.id, arguments, state["context"])
                if tool.id in {"query_atlog_event", "query_atlog_logs"}:
                    arguments = bound_query(arguments, debug=tool.id == "query_atlog_logs")
                    consumed = int(state["context"].get("_retrieval_reserved_lines") or 0)
                    if consumed + arguments["max_lines"] > 3000:
                        raise ValueError("本轮检索已达到 3000 行预算，请基于已有证据回答或开启新的诊断")
                    state["context"]["_retrieval_reserved_lines"] = consumed + arguments["max_lines"]
                trace_title, trace_detail = a._tool_trace_copy(tool, arguments)
                quiet = a._quiet_tool(tool.id)
                logger.info("assistant.graph.tool tool=%s arguments=%s", tool.id, a._json(arguments)[:30_000])

                if not quiet:
                    _emit(state, {"type": "task", "status": "running", "phase": "tool", "title": trace_title, "detail": trace_detail})
                    _emit(state, {"type": "tool_call", "id": trace_id, "tool_id": tool.id, "title": trace_title, "detail": trace_detail, "status": "running", "input": public_snapshot(arguments)})
                    _emit(state, a._trace_event(trace_id, "tool", trace_title, detail=trace_detail))

                interactive_choice = False
                if tool.id == "ask_user_choice":
                    # 「让用户选择」：把选项发成结构化事件，前端渲染固定选择组件；
                    # 本轮到此为止，用户点选后选项内容会作为下一条消息回来，再继续。
                    choice = state["kernel"].execute(tool.id, arguments, context=state["context"])
                    choice_payload = choice.get("data") if isinstance(choice, dict) and isinstance(choice.get("data"), dict) else choice
                    _emit(state, {"type": "choices", "choices": choice_payload})
                    interactive_choice = True
                    options_text = "、".join(str(item.get("label") or "") for item in (choice_payload or {}).get("options") or [])
                    result = {
                        "success": True,
                        "waiting_for_user_choice": True,
                        "message": (
                            "已把选项交给用户（" + options_text + "）。"
                            "本轮请**立即结束**：不要再调用工具、不要再复述选项、只回一句「请选择」即可。"
                        ),
                    }
                elif a._requires_confirmation(tool):
                    pending = a._pending_action(tool, arguments)
                    confirmations.append(pending)
                    result = {
                        "success": False,
                        "requires_confirmation": True,
                        "message": f"{tool.name} 需要用户确认，尚未执行。",
                        "summary": pending["summary"],
                    }
                    if not quiet:
                        steps.append({"tool_id": tool.id, "name": tool.name, "status": "confirm", "message": "等待用户确认"})
                        _emit(state, a._trace_event(trace_id, "tool", tool.name, status="confirm", detail="该操作需要用户确认后才能继续"))
                    _emit(state, {"type": "confirmation", "confirmation": pending})
                else:
                    if tool.read_only:
                        value = (
                            state["kernel"].execute(tool.id, arguments, context=state["context"])
                            if quiet
                            else _consume_generator(
                                a._invoke_read_only_tool_with_progress(
                                    tool,
                                    arguments,
                                    trace_id=trace_id,
                                    trace_title=trace_title,
                                    trace_detail=trace_detail,
                                    run_id=run_id,
                                    execute=lambda tool_id=tool.id, args=dict(arguments): state["kernel"].execute(
                                        tool_id, args, context=state["context"]
                                    ),
                                ),
                                state,
                            )
                        )
                    else:
                        value = state["kernel"].execute(tool.id, arguments, context=state["context"])

                    if isinstance(value, dict) and value.get("selection_required"):
                        selection_pending = True
                    if a._run_cancelled(run_id):
                        raise a.AssistantCancelled("用户已终止当前分析。")

                    result = {"status": "ok", "data": value}
                    # 「让用户选择」只发 choices 事件，**绝不能**再发 ui_action：
                    # 前端不会给一个不存在的页面动作回执，发了就会让整轮一直等回执（卡在"运行中"）。
                    action = None if interactive_choice else (a._extract_ui_action(value) or evidence_action(tool.id, arguments, value))
                    if action:
                        ui_actions.append(action)
                        action_id = uuid.uuid4().hex
                        needs_receipt = bool(state.get("streaming") and run_id and state["context"].get("ui_receipts"))
                        if needs_receipt:
                            RUN_STATE.expect_ui(run_id, action_id)
                        _emit(state, {"type": "ui_action", "action": action, "action_id": action_id, "run_id": run_id})
                        if needs_receipt:
                            receipt = RUN_STATE.wait_ui(run_id, action_id)
                            if a._run_cancelled(run_id):
                                raise a.AssistantCancelled("用户已接管页面")
                            result["ui_receipt"] = {k: v for k, v in receipt.items() if k != "context"}
                            if receipt.get("status") != "success":
                                # Do not execute dependent actions against a stale page.
                                _emit(state, {"type": "tool_result", "id": trace_id, "tool_id": tool.id,
                                    "title": "页面操作未完成", "status": "failed", "detail": receipt.get("detail"),
                                    "output": public_snapshot(result)})
                                raise a.AssistantError(str(receipt.get("detail") or "页面未确认完成"))
                            fresh = receipt.get("context") or {}
                            for key in ("page", "page_label", "environment_id", "environment_name", "log_locator", "selected_atlog_case", "atlog_page"):
                                if key in fresh:
                                    state["context"][key] = fresh[key]
                            deferred_system_notes.append(
                                "页面操作回执成功，后续操作使用此最新页面状态：" + a._json(public_snapshot(fresh))
                            )
                    card = a._result_card_from_tool(tool, arguments, value)
                    if card:
                        card["id"] = f"{tool.id}-{round_index}-{call_index}"
                        result_cards = [item for item in result_cards if item.get("kind") != card.get("kind")]
                        result_cards.append(card)
                        result_cards = result_cards[-6:]
                    success_title, success_detail = a._tool_trace_copy(tool, arguments, result)
                    if success_title.startswith("正在"):
                        success_title = success_title.replace("正在", "已", 1)
                    if not quiet:
                        steps.append({"tool_id": tool.id, "name": tool.name, "status": "success", "message": "已完成"})
                        _emit(state, {"type": "tool_result", "id": trace_id, "tool_id": tool.id, "title": success_title, "detail": success_detail, "status": "success", "output": public_snapshot(result)})
                        _emit(state, a._trace_event(trace_id, "tool", success_title, status="success", detail=success_detail))

                    if isinstance(value, dict):
                        review = review_tracepilot_tool_result(
                            tool_id=tool.id,
                            tool_result=value,
                            selected_tool_ids=set(state.get("selected_tool_ids") or set()),
                            data_extraction_requested=bool(state.get("data_extraction_requested")),
                            has_page_log_evidence=bool(
                                a._displayed_log_evidence(state["context"]) or a._data_pattern_evidence(state["context"])
                            ),
                        )
                        next_tool = str(review.get("forced_tool_id") or "").strip()
                        instruction = str(review.get("instruction") or "").strip()
                        if next_tool:
                            forced_tool_id = next_tool
                            if instruction:
                                deferred_system_notes.append(instruction)
                            logger.info(
                                "assistant.graph.review tool=%s workflow=%s forced_tool=%s",
                                tool.id,
                                review.get("workflow"),
                                next_tool,
                            )
            except (a.AssistantCancelled, a.AssistantError):
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("assistant.graph.tool.failed tool=%s", tool_id)
                result = a._recoverable_error(exc)
                if tool is None or not a._quiet_tool(tool.id):
                    steps.append({
                        "tool_id": tool_id,
                        "name": tool.name if tool else tool_id,
                        "status": "failed",
                        "message": str(result.get("reason") or "工具执行失败")[:500],
                    })
                    failed_detail = str(result.get("reason") or "工具执行失败")[:500]
                    _emit(state, {"type": "tool_result", "id": trace_id, "tool_id": tool_id, "title": tool.name if tool else tool_id, "detail": failed_detail, "status": "failed"})
                    _emit(state, a._trace_event(trace_id, "tool", tool.name if tool else tool_id, status="failed", detail=failed_detail))

        llm_tool_content = a._compact_result(result, tool)
        if tool is not None:
            compact_fact = re.sub(r"\s+", " ", llm_tool_content).strip()
            if compact_fact:
                tool_memory_facts.append(f"{tool.name}:{compact_fact[:650]}")
        messages.append({"role": "tool", "tool_call_id": call_id, "content": llm_tool_content})

    final_text = ""
    if selection_pending:
        final_text = a._selection_pending_text()
        _emit(state, {"type": "token_reset", "text": final_text})
    elif confirmations:
        final_text = a._confirmation_pending_text(confirmations)
        _emit(state, {"type": "token_reset", "text": final_text})

    # Only now, with every tool_call answered, is it safe to add system notes.
    for note in deferred_system_notes:
        messages.append({"role": "system", "content": note})

    # Invariant: every tool_call in the assistant turn must be answered by a `tool`
    # message, or the next request is rejected with HTTP 400
    # ("An assistant message with 'tool_calls' must be followed by tool messages...").
    # The model can emit several calls in one turn, and a branch that skips one (budget
    # guard, cancelled action, unexpected shape) used to leave the history malformed and
    # kill the whole run. Backfill a placeholder rather than let that happen.
    answered = {
        str(item.get("tool_call_id") or "")
        for item in messages
        if isinstance(item, dict) and item.get("role") == "tool"
    }
    for call in list(state.get("tool_calls") or []):
        call_id = str((call or {}).get("id") or "").strip()
        if not call_id or call_id in answered:
            continue
        name = str(((call or {}).get("function") or {}).get("name") or "").strip()
        logger.warning("assistant.graph.tool_call_unanswered tool=%s call=%s", name, call_id)
        messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(
                {"success": False, "error": "该工具调用未产生结果（可能被预算或确认流程跳过）。"},
                ensure_ascii=False,
            ),
        })

    next_round = round_index + 1
    if not final_text and next_round > int(state.get("max_rounds") or 1):
        final_text = _finish_from_existing_evidence(
            {**state, "tool_memory_facts": tool_memory_facts, "round_index": next_round},
            reason="已达到本轮安全取证上限，停止扩展工具调用并整理已取得的结果。",
        )

    return {
        "messages": messages,
        "confirmations": confirmations,
        "result_cards": result_cards,
        "selection_pending": selection_pending,
        "tool_memory_facts": tool_memory_facts,
        "steps": steps,
        "ui_actions": ui_actions,
        "forced_tool_id": forced_tool_id,
        "tool_calls": [],
        "round_index": next_round,
        "final_text": final_text,
    }


def _route_after_execute(state: ExecutionState) -> Literal["plan", "finalize"]:
    return "finalize" if state.get("final_text") else "plan"


def _finalize(state: ExecutionState) -> dict[str, Any]:
    a = _assistant()
    final_text = str(state.get("final_text") or "").strip()
    confirmations = list(state.get("confirmations") or [])
    selection_pending = bool(state.get("selection_pending"))
    result_cards = list(state.get("result_cards") or [])

    if not final_text:
        final_text = a._fallback_answer_from_facts(list(state.get("tool_memory_facts") or []))
        _emit(state, {"type": "token_reset", "text": final_text})

    turn_memory = str(state.get("turn_memory") or "")
    if not turn_memory:
        turn_memory = a._build_turn_memory(
            previous_memory=state.get("memory"),
            user_message=state["message"],
            skill_id=str(state.get("normalized_skill") or "auto"),
            context=state.get("context") or {},
            tool_facts=list(state.get("tool_memory_facts") or []),
            final_text=final_text,
        )

    if state.get("fast_path"):
        _emit(state, {
            "type": "task",
            "status": "confirm",
            "phase": "done",
            "title": "等待确认",
            "detail": "部署参数已一次性准备完成",
        })
    else:
        _emit(state, {
            "type": "task",
            "status": "selection" if selection_pending else "confirm" if confirmations else "done",
            "phase": "done",
            "title": "等待选择日志" if selection_pending else "等待确认" if confirmations else "任务完成",
            "detail": (
                "请选择一个日志目标后继续"
                if selection_pending
                else "需要确认后继续"
                if confirmations
                else (final_text[:180] if final_text else "本次任务已执行完成")
            ),
        })

    _emit(state, {
        "type": "done",
        "message": final_text,
        "confirmation_count": len(confirmations),
        "memory": turn_memory,
        "result_cards": result_cards,
        "suggested_actions": [],
    })
    return {"final_text": final_text, "turn_memory": turn_memory, "suggested_actions": []}


def _build_graph():
    graph = StateGraph(ExecutionState)
    graph.add_node("prepare", _prepare)
    graph.add_node("plan", _plan)
    graph.add_node("atlog", _atlog_diagnosis)
    graph.add_node("execute", _execute)
    graph.add_node("finalize", _finalize)
    graph.add_edge(START, "prepare")
    graph.add_conditional_edges("prepare", _route_after_prepare, {"plan": "plan", "atlog": "atlog", "finalize": "finalize"})
    graph.add_edge("atlog", "finalize")
    graph.add_conditional_edges(
        "plan",
        _route_after_plan,
        {"plan": "plan", "execute": "execute", "finalize": "finalize"},
    )
    graph.add_conditional_edges("execute", _route_after_execute, {"plan": "plan", "finalize": "finalize"})
    graph.add_edge("finalize", END)
    return graph.compile()


TRACEPILOT_EXECUTION_GRAPH = _build_graph()


def _initial_state(
    *,
    message: str,
    history: list[dict[str, Any]] | None,
    context: dict[str, Any] | None,
    skill_id: str | None,
    memory: str | None,
    run_id: str,
    event_sink: EventSink,
    streaming: bool,
) -> ExecutionState:
    return {
        "session_id": str((context or {}).get("session_id") or ""),
        "task_id": str((context or {}).get("task_id") or run_id),
        "context_snapshot": dict(context or {}),
        "stage": "prepare", "progress": 0, "current_tool": "",
        "message": str(message or "").strip(),
        "history": history if isinstance(history, list) else [],
        "context": context if isinstance(context, dict) else {},
        "requested_skill": str(skill_id or "auto"),
        "memory": str(memory or ""),
        "run_id": str(run_id or "").strip()[:96],
        "event_sink": event_sink,
        "streaming": bool(streaming),
    }


def run_sync(
    *,
    message: str,
    history: list[dict[str, Any]] | None = None,
    context: dict[str, Any] | None = None,
    skill_id: str | None = None,
    memory: str | None = None,
) -> dict[str, Any]:
    """Run the same LangGraph used by streaming chat and return REST payload."""
    a = _assistant()
    events: list[dict[str, Any]] = []
    state = _initial_state(
        message=message,
        history=history,
        context=context,
        skill_id=skill_id,
        memory=memory,
        run_id="",
        event_sink=events.append,
        streaming=False,
    )
    try:
        result = TRACEPILOT_EXECUTION_GRAPH.invoke(state)
    finally:
        a.finish_run("")

    confirmations = list(result.get("confirmations") or [])
    return {
        "message": str(result.get("final_text") or ""),
        "steps": list(result.get("steps") or []),
        "ui_actions": list(result.get("ui_actions") or []),
        "confirmations": confirmations,
        "result_cards": list(result.get("result_cards") or []),
        "suggested_actions": list(result.get("suggested_actions") or []),
        "memory": str(result.get("turn_memory") or ""),
    }


def run_stream(
    *,
    message: str,
    history: list[dict[str, Any]] | None = None,
    context: dict[str, Any] | None = None,
    skill_id: str | None = None,
    memory: str | None = None,
    run_id: str = "",
) -> Iterator[dict[str, Any]]:
    """Execute LangGraph on a worker and forward node events immediately.

    Nodes emit progress through an event sink. Running the graph on its own worker
    lets the outer generator deliver those events while a model/tool node is still
    executing, preserving the current SSE responsiveness.
    """
    a = _assistant()
    event_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
    run_id = str(run_id or "").strip()[:96]

    def emit(event: dict[str, Any]) -> None:
        event_queue.put(("event", event))

    # The graph runs on a worker thread, and contextvars do NOT cross thread boundaries.
    # Without this snapshot the run silently loses its session id and the composer's
    # model / reasoning-effort selection, because those are carried as contextvars.
    run_context = contextvars.copy_context()

    def invoke_graph() -> None:
        TRACEPILOT_EXECUTION_GRAPH.invoke(
            _initial_state(
                message=message,
                history=history,
                context=context,
                skill_id=skill_id,
                memory=memory,
                run_id=run_id,
                event_sink=emit,
                streaming=True,
            )
        )

    def worker() -> None:
        try:
            run_context.run(invoke_graph)
        except a.AssistantCancelled:
            logger.info("assistant.graph.cancelled run=%s", run_id)
            emit({"type": "task", "status": "stopped", "phase": "stopped", "title": "已终止当前分析", "detail": "已保留完成的步骤与证据，不再启动后续原子能力"})
            emit({
                "type": "done",
                "message": "当前分析已按你的要求终止。已完成的只读结果仍保留，可以随时从这里换方向继续。",
                "confirmation_count": 0,
                "memory": str(memory or "")[: a._MAX_MEMORY_CHARS],
                "result_cards": [],
            })
        except BaseException as exc:  # noqa: BLE001
            event_queue.put(("error", exc))
        finally:
            a.finish_run(run_id)
            event_queue.put(("done", None))

    threading.Thread(target=worker, name=f"tracepilot-graph-{run_id or 'sync'}", daemon=True).start()

    while True:
        kind, value = event_queue.get()
        if kind == "done":
            break
        if kind == "error":
            raise value
        if isinstance(value, dict):
            yield value
