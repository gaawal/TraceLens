from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from apps.tooling.kernel import get_default_kernel
from apps.tooling.llm import get_llm_client
from apps.tooling.skills.loader import load_skill

ProgressCallback = Callable[[dict[str, Any]], None]
ToolCompactor = Callable[[str, dict[str, Any], Any], Any]
ToolObserver = Callable[[str, dict[str, Any], Any], None]

logger = logging.getLogger("tracelens.tooling.skill")

_SENSITIVE_LOG_KEYS = {"password", "passwd", "token", "secret", "api_key", "apikey", "authorization", "cookie", "credential", "credentials"}

def _safe_log_value(value: Any, *, depth: int = 0) -> Any:
    """Bound debug logging without leaking credentials or dumping huge logs."""
    if depth > 4:
        return "<max-depth>"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in list(value.items())[:80]:
            name = str(key)
            lowered = name.casefold()
            if any(secret in lowered for secret in _SENSITIVE_LOG_KEYS):
                out[name] = "***"
            else:
                out[name] = _safe_log_value(item, depth=depth + 1)
        if len(value) > 80:
            out["__truncated_keys__"] = len(value) - 80
        return out
    if isinstance(value, (list, tuple)):
        items = [_safe_log_value(item, depth=depth + 1) for item in list(value)[:30]]
        if len(value) > 30:
            items.append(f"<+{len(value)-30} items>")
        return items
    if isinstance(value, str):
        text = value.replace("\r", " ")
        return text[:3000] + (f"...<+{len(text)-3000} chars>" if len(text) > 3000 else "")
    return value

def _json_for_log(value: Any, limit: int = 12000) -> str:
    try:
        encoded = json.dumps(_safe_log_value(value), ensure_ascii=False, default=str, sort_keys=True)
    except Exception:
        encoded = str(value)
    return encoded[:limit] + (f"...<+{len(encoded)-limit} chars>" if len(encoded) > limit else "")

_SKILL_AGENT_LABELS = {
    "atlog-analysis": "ATLog 分析",
    "logs": "日志分析",
    "deployment": "环境部署",
    "data": "数据提取",
}


def _skill_agent_label(skill_id: str) -> str:
    return _SKILL_AGENT_LABELS.get(str(skill_id or "").strip(), str(skill_id or "Skill").strip() or "Skill")



def _usage(response: Any) -> dict[str, int]:
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


def _merge_usage(total: dict[str, int], current: dict[str, int]) -> dict[str, int]:
    return {
        "input_tokens": int(total.get("input_tokens") or 0) + int(current.get("input_tokens") or 0),
        "output_tokens": int(total.get("output_tokens") or 0) + int(current.get("output_tokens") or 0),
        "total_tokens": int(total.get("total_tokens") or 0) + int(current.get("total_tokens") or 0),
    }


def _tool_calls(message: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for call in list(getattr(message, "tool_calls", None) or []):
        fn = getattr(call, "function", None)
        name = str(getattr(fn, "name", "") or "").strip()
        if not name:
            continue
        raw = getattr(fn, "arguments", "") or "{}"
        try:
            arguments = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        except (TypeError, ValueError, json.JSONDecodeError):
            arguments = {}
        result.append({
            "id": str(getattr(call, "id", "") or f"call-{len(result)+1}"),
            "name": name,
            "arguments": arguments if isinstance(arguments, dict) else {},
        })
    return result


def _spec_for(tool: Any) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.id,
            "description": tool.agent_description().strip()[:1200],
            "parameters": tool.input_schema,
        },
    }


def _signature(name: str, arguments: dict[str, Any]) -> str:
    try:
        encoded = json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:  # noqa: BLE001
        encoded = str(arguments)
    return f"{name}:{encoded}"


def _emit(callback: ProgressCallback | None, event: dict[str, Any]) -> None:
    if callback is None:
        return
    try:
        callback(event)
    except Exception:  # noqa: BLE001 - UI progress must never break execution
        return


def run_skill_agent(
    *,
    skill_id: str,
    task_input: str,
    context: dict[str, Any],
    tool_ids: list[str],
    progress_callback: ProgressCallback | None = None,
    compact_tool_result: ToolCompactor | None = None,
    observe_tool_result: ToolObserver | None = None,
    max_iterations: int = 7,
    single_use_tools: set[str] | None = None,
) -> dict[str, Any]:
    """Run a small generic Skill-driven tool loop.

    The Skill defines strategy; this runtime only loads metadata, asks the model
    what capability to call next, dispatches through Core Kernel, feeds bounded
    tool results back, and forces a final answer at the safety limit.
    """
    skill_text = load_skill(skill_id)
    skill_label = _skill_agent_label(skill_id)
    agent_label = f"{skill_label} Skill Agent"
    kernel = get_default_kernel()
    client = get_llm_client()
    logger.info(
        "skill.runtime.start skill=%s task_chars=%s context=%s requested_tools=%s max_iterations=%s",
        skill_id,
        len(str(task_input or "")),
        _json_for_log(context, 8000),
        list(tool_ids),
        max_iterations,
    )
    definitions = []
    for tool_id in tool_ids:
        tool = kernel.registry.get(tool_id)
        if tool is not None:
            definitions.append(tool)
    specs = [_spec_for(tool) for tool in definitions]
    allowed = {tool.id for tool in definitions}
    max_iterations = max(1, min(int(max_iterations or 7), 12))
    single_use_tools = set(single_use_tools or set())

    _emit(progress_callback, {
        "type": "stage",
        "stage": {
            "name": "skill_load", "label": f"加载 {skill_label} Skill", "status": "running",
            "agent": "Skill Runtime", "action": f"load_skill({skill_id})",
            "detail": "正在加载诊断策略和可用原子能力。", "progress": 4,
        },
        "timestamp": time.time(),
    })
    _emit(progress_callback, {
        "type": "stage",
        "stage": {
            "name": "skill_load", "label": f"加载 {skill_label} Skill", "status": "completed",
            "agent": "Skill Runtime", "action": f"load_skill({skill_id})",
            "detail": f"已加载 Skill，开放 {len(specs)} 个已有原子能力。", "progress": 8,
        },
        "timestamp": time.time(),
    })

    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "你是 TraceLens 的 Skill Agent Runtime。下面的 SKILL.md 是本任务唯一的领域策略。"
                "不要机械执行全部工具；每轮只根据当前证据决定是否需要工具。"
                "工具结果已经做上下文压缩，禁止要求整份日志。\n\n"
                + skill_text
            ),
        },
        {"role": "user", "content": str(task_input or "")[:18000]},
    ]
    usage_total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    records: list[dict[str, Any]] = []
    executed: dict[str, Any] = {}
    used_single: set[str] = set()
    final_content = ""

    for iteration in range(1, max_iterations + 1):
        pct = min(82, 10 + iteration * 10)
        _emit(progress_callback, {
            "type": "thinking",
            "stage_name": "agent_decision",
            "agent": agent_label,
            "text": (
                "正在基于当前证据判断是否需要继续取证。"
                if iteration == 1 else
                "已收到上一轮原子能力结果，正在判断证据是否足够或需要更精确的下一步。"
            ),
            "percentage": pct,
            "timestamp": time.time(),
        })
        logger.info(
            "skill.runtime.llm_decision skill=%s iteration=%s/%s message_count=%s tool_specs=%s task_tail=%s",
            skill_id, iteration, max_iterations, len(messages),
            [spec.get("function", {}).get("name") for spec in specs],
            str(task_input or "")[-2500:],
        )
        response = client.chat(messages, tools=specs, tool_choice="auto")
        current_usage = _usage(response)
        usage_total = _merge_usage(usage_total, current_usage)
        _emit(progress_callback, {"type": "token_usage", "usage": current_usage, "timestamp": time.time()})
        message = response.choices[0].message
        calls = _tool_calls(message)
        assistant_payload: dict[str, Any] = {
            "role": "assistant",
            "content": str(getattr(message, "content", "") or ""),
        }
        if calls:
            assistant_payload["tool_calls"] = [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call["arguments"], ensure_ascii=False, default=str),
                    },
                }
                for call in calls
            ]
        messages.append(assistant_payload)

        logger.info(
            "skill.runtime.llm_decision_result skill=%s iteration=%s calls=%s content=%s",
            skill_id, iteration,
            _json_for_log([{"name": c.get("name"), "arguments": c.get("arguments")} for c in calls], 10000),
            str(getattr(message, "content", "") or "")[:3000],
        )
        if not calls:
            final_content = str(getattr(message, "content", "") or "").strip()
            break

        for call in calls:
            name = call["name"]
            arguments = dict(call["arguments"] or {})
            if name not in allowed:
                compact = {"ok": False, "error": f"Skill 未授权原子能力: {name}"}
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(compact, ensure_ascii=False)})
                continue
            if name in single_use_tools:
                if name in used_single:
                    compact = {"ok": True, "skipped": True, "reason": f"{name} 本次分析只允许执行一次，已有结果请直接复用。"}
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(compact, ensure_ascii=False)})
                    continue
                used_single.add(name)
            if name == "match_cases":
                arguments["min_score"] = max(70.0, float(arguments.get("min_score") or 70.0))
                arguments["limit"] = min(3, max(1, int(arguments.get("limit") or 3)))

            sig = _signature(name, arguments)
            if sig in executed:
                compact = {
                    "ok": True,
                    "skipped": True,
                    "reason": "同一原子能力与同一参数已经执行过，请复用已有结果并继续判断。",
                    "cached_result": executed[sig],
                }
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(compact, ensure_ascii=False, default=str)[:12000]})
                continue

            tool = kernel.registry.get(name)
            label = tool.name if tool is not None else name
            logger.info(
                "skill.runtime.tool_selected skill=%s iteration=%s tool=%s model_arguments=%s",
                skill_id, iteration, name, _json_for_log(arguments),
            )
            started = time.perf_counter()
            _emit(progress_callback, {
                "type": "stage",
                "stage": {
                    "name": f"tool:{name}", "label": label, "status": "running",
                    "agent": agent_label, "action": name,
                    "detail": "Agent 已根据当前证据选择该原子能力，正在获取必要信息。",
                    "progress": pct,
                },
                "timestamp": time.time(),
            })
            try:
                value = kernel.execute(name, arguments, context=context)
                logger.info(
                    "skill.runtime.tool_result skill=%s iteration=%s tool=%s elapsed_ms=%s raw_result=%s",
                    skill_id, iteration, name,
                    max(0, int((time.perf_counter() - started) * 1000)),
                    _json_for_log(value, 12000),
                )
                if observe_tool_result is not None:
                    observe_tool_result(name, arguments, value)
                compact = compact_tool_result(name, arguments, value) if compact_tool_result is not None else value
                executed[sig] = compact
                status = "completed"
                detail = "原子能力执行完成，结果已压缩回填 Agent。"
                records.append({
                    "tool_id": name,
                    "name": label,
                    "arguments": arguments,
                    "status": "success",
                    "duration_ms": max(0, int((time.perf_counter() - started) * 1000)),
                })
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "skill.runtime.tool_failed skill=%s iteration=%s tool=%s model_arguments=%s",
                    skill_id, iteration, name, _json_for_log(arguments),
                )
                compact = {"ok": False, "error": str(exc)}
                status = "error"
                detail = str(exc)[:600]
                records.append({
                    "tool_id": name,
                    "name": label,
                    "arguments": arguments,
                    "status": "failed",
                    "message": detail,
                    "duration_ms": max(0, int((time.perf_counter() - started) * 1000)),
                })
            _emit(progress_callback, {
                "type": "stage",
                "stage": {
                    "name": f"tool:{name}", "label": label, "status": status,
                    "agent": agent_label, "action": name,
                    "detail": detail, "progress": min(88, pct + 4),
                },
                "timestamp": time.time(),
            })
            encoded = json.dumps(compact, ensure_ascii=False, default=str)
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": encoded[:16000] + ("\n[tool result truncated]" if len(encoded) > 16000 else ""),
            })

    if not final_content:
        _emit(progress_callback, {
            "type": "thinking", "stage_name": "synthesis", "agent": agent_label,
            "text": "已达到本次取证安全上限，停止继续扩大日志范围，正在基于现有证据形成当前最佳结论。",
            "percentage": 92, "timestamp": time.time(),
        })
        response = client.chat(
            messages + [{
                "role": "system",
                "content": "现在停止调用工具。严格按 SKILL.md 的最终 JSON 格式，基于已有证据输出当前最佳结论；证据不足时降低置信度并明确缺失事实。",
            }],
            tools=None,
        )
        current_usage = _usage(response)
        usage_total = _merge_usage(usage_total, current_usage)
        _emit(progress_callback, {"type": "token_usage", "usage": current_usage, "timestamp": time.time()})
        final_content = str(response.choices[0].message.content or "").strip()

    logger.info(
        "skill.runtime.finish skill=%s records=%s usage=%s final=%s",
        skill_id, len(records), usage_total, final_content[:4000],
    )
    return {
        "content": final_content,
        "tool_records": records,
        "usage": usage_total,
        "iterations": min(max_iterations, max(1, len(records) + 1)),
    }
