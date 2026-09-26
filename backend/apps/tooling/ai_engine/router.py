from __future__ import annotations

import json
import logging
import time
from typing import Any

from apps.tooling.kernel.registry import ToolRegistry
from apps.tooling.registry import list_skills, normalize_skill_id

logger = logging.getLogger("tracelens.ai_engine.router")

_DOMAIN_TOOL = "select_tracepilot_domain"
_ROUTE_TOOL = "select_tracepilot_route"
_MAX_ROUTE_TOOLS = 10
_MAX_DOMAINS = 3


def _json_context(context: dict[str, Any]) -> str:
    runtime = context if isinstance(context, dict) else {}
    locator = runtime.get("log_locator") if isinstance(runtime.get("log_locator"), dict) else {}
    scope = runtime.get("assistant_scope") if isinstance(runtime.get("assistant_scope"), dict) else {}
    scoped_case = scope.get("atlog_case") if isinstance(scope.get("atlog_case"), dict) else {}
    selected_case = runtime.get("selected_atlog_case") if isinstance(runtime.get("selected_atlog_case"), dict) else {}
    atlog_page = runtime.get("atlog_page") if isinstance(runtime.get("atlog_page"), dict) else {}
    expanded_case = atlog_page.get("expanded_case") if isinstance(atlog_page.get("expanded_case"), dict) else {}
    atlog_case = {**scoped_case, **selected_case, **expanded_case}
    report_facts = atlog_case.get("report_facts") if isinstance(atlog_case.get("report_facts"), dict) else {}
    current_log_request = atlog_case.get("current_log_request") if isinstance(atlog_case.get("current_log_request"), dict) else {}
    event_evidence = list(atlog_case.get("event_evidence") or [])[:6]
    runtime_evidence = list(atlog_case.get("runtime_evidence") or [])[:8]
    snapshot = {
        "page": runtime.get("page"),
        "environment_id": runtime.get("environment_id"),
        "environment_name": runtime.get("environment_name") or runtime.get("environment_label"),
        "client_local_time": runtime.get("client_local_time"),
        "client_timezone": runtime.get("client_timezone"),
        "log_locator": {
            "environment_id": locator.get("environment_id"),
            "component_name": locator.get("component_name"),
            "fm_targets": list(locator.get("fm_targets") or [])[:8],
            "source_categories": list(locator.get("source_categories") or [])[:8],
            "keyword": locator.get("keyword"),
            "errors_only": locator.get("errors_only"),
            "view_time_range": locator.get("view_time_range"),
            "query_time_range": locator.get("query_time_range"),
            "function_fold_summary": list(locator.get("function_fold_summary") or [])[:16],
        },
        "atlog_case": {
            "case_url": atlog_case.get("case_url") or atlog_case.get("url"),
            "case_id": atlog_case.get("case_id"),
            "case_name": atlog_case.get("case_name"),
            "status": atlog_case.get("status"),
            "assertion_summary": atlog_case.get("assertion_summary") or report_facts.get("assertion_summary"),
            "failure_time": atlog_case.get("failure_time"),
            "start_time": atlog_case.get("start_time") or current_log_request.get("start_time"),
            "end_time": atlog_case.get("end_time") or current_log_request.get("end_time"),
            "fm_targets": list(current_log_request.get("fm_targets") or [])[:8],
            "event_evidence": event_evidence,
            "runtime_evidence": runtime_evidence,
            "report_fact_keys": list(report_facts.keys())[:12],
        },
    }
    return json.dumps(snapshot, ensure_ascii=False, default=str)[:4200]


def _extract_arguments(response: Any) -> dict[str, Any]:
    try:
        message = response.choices[0].message
    except Exception:  # noqa: BLE001
        return {}
    calls = getattr(message, "tool_calls", None) or []
    if calls:
        fn = getattr(calls[0], "function", None)
        raw = getattr(fn, "arguments", "") if fn is not None else ""
        try:
            value = json.loads(str(raw or "{}"))
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}
    content = str(getattr(message, "content", "") or "").strip()
    if content:
        try:
            value = json.loads(content)
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _has_page_log_evidence(context: dict[str, Any]) -> bool:
    locator = context.get("log_locator") if isinstance(context.get("log_locator"), dict) else {}
    if bool(
        locator.get("function_fold_context")
        or locator.get("function_fold_summary")
        or locator.get("displayed_evidence")
        or locator.get("displayed_logs")
    ):
        return True
    scope = context.get("assistant_scope") if isinstance(context.get("assistant_scope"), dict) else {}
    scoped_case = scope.get("atlog_case") if isinstance(scope.get("atlog_case"), dict) else {}
    selected_case = context.get("selected_atlog_case") if isinstance(context.get("selected_atlog_case"), dict) else {}
    atlog_page = context.get("atlog_page") if isinstance(context.get("atlog_page"), dict) else {}
    expanded_case = atlog_page.get("expanded_case") if isinstance(atlog_page.get("expanded_case"), dict) else {}
    atlog_case = {**scoped_case, **selected_case, **expanded_case}
    return bool(
        atlog_case.get("report_facts")
        or atlog_case.get("event_evidence")
        or atlog_case.get("runtime_evidence")
        or atlog_case.get("loaded_evidence")
    )


class SemanticRouter:
    """One routing call for small catalogs; hierarchical routing for large ones."""

    def __init__(self, registry: ToolRegistry, event_sink=None) -> None:
        self.registry = registry
        self.event_sink = event_sink

    def _chat(self, client, messages, **kwargs):
        from apps.tooling.assistant import _provider_usage, _usage_payload, _estimate_token_count

        started = time.perf_counter()
        response = client.chat(messages, **kwargs)
        if callable(self.event_sink):
            usage = _provider_usage(response) or _usage_payload(
                _estimate_token_count(json.dumps([messages, kwargs.get("tools")], ensure_ascii=False)),
                _estimate_token_count(str(response.choices[0].message)), estimated=True,
            )
            self.event_sink({"type": "token_usage", "usage": usage})
        logger.info("ai_engine.route_call duration_ms=%s", int((time.perf_counter() - started) * 1000))
        return response

    def route(self, client: Any, *, message: str, context: dict[str, Any], skill_id: str | None) -> dict[str, Any]:
        normalized = normalize_skill_id(skill_id)
        try:
            candidate_ids, catalog = self.registry.routing_catalog(normalized)
            if len(candidate_ids) <= 48 and len(catalog) <= 18000:
                return self._select_tools(client, message=message, context=context,
                                          skill_id=normalized, domain_route=None)
            domain_route = self._select_domains(client, message=message, context=context, skill_id=normalized)
            if str(domain_route.get("mode") or "") == "answer" or not domain_route.get("domains"):
                return {
                    "skill_id": str(domain_route.get("skill_id") or (normalized if normalized != "auto" else "environment")),
                    "domain": "general",
                    "domains": [],
                    "mode": "answer",
                    "tool_ids": [],
                    "navigation_needed": False,
                    "needs_current_time": False,
                    "use_page_evidence": False,
                    "workflow_hint": "none",
                    "reason": str(domain_route.get("reason") or "direct_answer")[:240],
                }
            route = self._select_tools(
                client,
                message=message,
                context=context,
                skill_id=normalized,
                domain_route=domain_route,
            )
            logger.info(
                "ai_engine.route skill=%s domains=%s mode=%s tools=%s nav=%s reason=%s",
                route["skill_id"], ",".join(route.get("domains") or []), route["mode"],
                ",".join(route["tool_ids"]), route["navigation_needed"], route["reason"],
            )
            return route
        except Exception as exc:  # noqa: BLE001
            logger.warning("ai_engine.route.failed fallback=safe reason=%s", exc, exc_info=True)
            return self._safe_fallback(normalized)

    def _select_domains(self, client: Any, *, message: str, context: dict[str, Any], skill_id: str) -> dict[str, Any]:
        domain_ids, catalog = self.registry.domain_catalog(skill_id)
        skill_catalog = "\n".join(
            f"- {item['id']}: {item['name']} — {item['description']}"
            for item in list_skills() if str(item.get("id") or "") != "auto"
        )
        system = (
            "你是 TracePilot AI Engine 的领域路由节点。只判断任务领域，不执行 Tool、不回答业务结果。\n"
            "必须按语义判断，禁止关键词/正则硬编码。简单查询只选一个最相关领域；跨域任务最多 3 个。\n"
            "只有用户明确要求页面切换时才选择 navigation；普通知识无需系统数据时 mode=answer 且 domains=[]。"
        )
        tool = {
            "type": "function",
            "function": {
                "name": _DOMAIN_TOOL,
                "description": "选择本轮需要加载的业务领域。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill_id": {"type": "string", "enum": ["logs", "deployment", "environment", "atlog", "data"]},
                        "mode": {"type": "string", "enum": ["answer", "query", "workflow", "action"]},
                        "domains": {"type": "array", "items": {"type": "string", "enum": domain_ids}, "maxItems": _MAX_DOMAINS},
                        "reason": {"type": "string", "maxLength": 240},
                    },
                    "required": ["skill_id", "mode", "domains", "reason"],
                    "additionalProperties": False,
                },
            },
        }
        user = (
            f"用户请求：\n{str(message or '')[:8000]}\n\n"
            f"指定 Skill：{skill_id}\n运行上下文：{_json_context(context)}\n\n"
            f"可选 Skill：\n{skill_catalog}\n\n领域摘要：\n{catalog}"
        )
        response = self._chat(client,
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            tools=[tool],
            tool_choice={"type": "function", "function": {"name": _DOMAIN_TOOL}},
        )
        raw = _extract_arguments(response)
        allowed = set(domain_ids)
        domains = [str(item).strip() for item in list(raw.get("domains") or []) if str(item).strip() in allowed][:_MAX_DOMAINS]
        mode = str(raw.get("mode") or ("query" if domains else "answer")).strip().lower()
        routed_skill = normalize_skill_id(str(raw.get("skill_id") or skill_id))
        if routed_skill == "auto":
            routed_skill = skill_id if skill_id != "auto" else "environment"
        return {
            "skill_id": routed_skill,
            "mode": mode if mode in {"answer", "query", "workflow", "action"} else "query",
            "domains": domains,
            "reason": str(raw.get("reason") or "").strip()[:240],
        }

    def _select_tools(self, client: Any, *, message: str, context: dict[str, Any], skill_id: str, domain_route: dict[str, Any] | None) -> dict[str, Any]:
        combined = domain_route is None
        domain_route = domain_route or {}
        domains = set(self.registry.domain_catalog(skill_id)[0]) if combined else set(str(item) for item in list(domain_route.get("domains") or []))
        candidate_ids, catalog = self.registry.routing_catalog(skill_id, domains=domains)
        system = (
            "你是 TracePilot 的路由节点。根据用户请求从开放 Tool 选择最小必要集合，不执行工具、不生成业务结论。\n"
            "规则：查询问题不选写操作；除非用户明确要求打开/切换页面，否则 navigation_needed=false；"
            "页面已有充分日志证据时优先 use_page_evidence=true；相对时间且上下文无时间时才 needs_current_time=true；"
            "沿用最近成功部署参数并覆盖少量字段时 workflow_hint=deployment_reuse_last_success。不要回答用户。"
            "已有证据只够部分分析时仍保留补取工具；用户明确要求继续查日志时 use_page_evidence=false。"
            # 「让用户选择」只在**确实还没选过**时才挡：用户已经在选项组件里点过（消息形如
            # 「回答上面的选择——「…」：…」）就是选定了，必须放行执行工具，否则会陷入"反复问同一个问题"。
            "如果用户要求批量生成/新增日志语义或标签规则，但确实**还没有**做过这个选择，"
            "本轮 tool_ids 只放 ask_user_choice，不要放 bulk_generate_log_rules；"
            "而当下这一轮是对上一轮选项的回答（消息里带「回答上面的选择——」），或答案里已经给出"
            "「语义」「标签」「两者都要」等选择时，**必须**放行 bulk_generate_log_rules（mode 按答案取），不要再放 ask_user_choice。"
        )
        route_tool = {
            "type": "function",
            "function": {
                "name": _ROUTE_TOOL,
                "description": "从已开放领域中选择本轮最小原子 Tool 集。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tool_ids": {"type": "array", "items": {"type": "string", "enum": candidate_ids}, "maxItems": _MAX_ROUTE_TOOLS},
                        "navigation_needed": {"type": "boolean"},
                        "needs_current_time": {"type": "boolean"},
                        "use_page_evidence": {"type": "boolean"},
                        "workflow_hint": {"type": "string", "enum": ["none", "deployment_reuse_last_success"]},
                        "reason": {"type": "string", "maxLength": 240},
                    },
                    "required": ["tool_ids", "navigation_needed", "needs_current_time", "use_page_evidence", "workflow_hint", "reason"],
                    "additionalProperties": False,
                },
            },
        }
        if combined:
            parameters = route_tool["function"]["parameters"]
            parameters["properties"].update({
                "skill_id": {"type": "string", "enum": ["logs", "deployment", "environment", "atlog", "data"]},
                "mode": {"type": "string", "enum": ["answer", "query", "workflow", "action"]},
                "domains": {"type": "array", "items": {"type": "string", "enum": sorted(domains)}, "maxItems": _MAX_DOMAINS},
            })
            parameters["required"].extend(["skill_id", "mode", "domains"])
        user = (
            f"用户请求：\n{str(message or '')[:8000]}\n\n"
            f"已选领域：{', '.join(sorted(domains))}\n任务模式：{domain_route.get('mode')}\n"
            f"运行上下文：{_json_context(context)}\n\n开放 Tool：\n{catalog}"
        )
        response = self._chat(client,
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            tools=[route_tool],
            tool_choice={"type": "function", "function": {"name": _ROUTE_TOOL}},
        )
        raw = _extract_arguments(response)
        if combined:
            domains = {str(item) for item in list(raw.get("domains") or []) if str(item) in domains}
            mode = str(raw.get("mode") or "query")
            domain_route = {
                "skill_id": normalize_skill_id(raw.get("skill_id") or skill_id),
                "mode": mode if mode in {"answer", "query", "workflow", "action"} else "query",
                "reason": raw.get("reason"),
            }
        return self._validate(raw, skill_id, context, domains, domain_route)

    def _validate(self, raw: dict[str, Any], requested_skill: str, context: dict[str, Any], domains: set[str], domain_route: dict[str, Any]) -> dict[str, Any]:
        candidate_ids = set(self.registry.routing_catalog(requested_skill, domains=domains)[0])
        mode = str(domain_route.get("mode") or "query").strip().lower()
        navigation_needed = bool(raw.get("navigation_needed"))
        needs_current_time = bool(raw.get("needs_current_time"))
        use_page_evidence = bool(raw.get("use_page_evidence")) and _has_page_log_evidence(context)
        scope = context.get("assistant_scope") if isinstance(context.get("assistant_scope"), dict) else {}
        scoped_case = scope.get("atlog_case") if isinstance(scope.get("atlog_case"), dict) else {}
        selected_case = context.get("selected_atlog_case") if isinstance(context.get("selected_atlog_case"), dict) else {}
        atlog_page = context.get("atlog_page") if isinstance(context.get("atlog_page"), dict) else {}
        expanded_case = atlog_page.get("expanded_case") if isinstance(atlog_page.get("expanded_case"), dict) else {}
        atlog_case = {**scoped_case, **selected_case, **expanded_case}
        report_facts = atlog_case.get("report_facts") if isinstance(atlog_case.get("report_facts"), dict) else {}
        page_has_report = bool(report_facts)
        page_has_event = bool(atlog_case.get("event_evidence"))
        page_has_runtime = bool(atlog_case.get("runtime_evidence") or atlog_case.get("loaded_evidence"))

        selected: list[str] = []
        for value in list(raw.get("tool_ids") or []):
            tool_id = str(value or "").strip()
            if not tool_id or tool_id not in candidate_ids:
                continue
            tool = self.registry.get(tool_id)
            if tool is None:
                continue
            if not navigation_needed and tool.capability_kind() == "navigation":
                continue
            if mode in {"answer", "query"} and not tool.read_only:
                continue
            if tool_id == "get_current_time" and not needs_current_time:
                continue
            if use_page_evidence and tool_id == "query_environment_logs":
                continue
            # When the routing Agent explicitly declares that current page evidence
            # is sufficient, do not rediscover evidence already present in the ATLog
            # working context.  Missing evidence types remain available as Tools.
            if use_page_evidence and tool_id == "analyze_atlog_case" and page_has_report:
                continue
            if use_page_evidence and tool_id == "query_atlog_event" and page_has_event:
                continue
            if use_page_evidence and tool_id == "query_atlog_logs" and page_has_runtime:
                continue
            selected.append(tool_id)
            if len(selected) >= _MAX_ROUTE_TOOLS:
                break
        skill = normalize_skill_id(str(domain_route.get("skill_id") or requested_skill))
        if skill == "auto":
            skill = requested_skill if requested_skill != "auto" else "environment"
        return {
            "skill_id": skill,
            "domain": next(iter(domains), "general"),
            "domains": sorted(domains),
            "mode": mode,
            "tool_ids": selected,
            "navigation_needed": navigation_needed,
            "needs_current_time": needs_current_time,
            "use_page_evidence": use_page_evidence,
            "workflow_hint": str(raw.get("workflow_hint") or "none").strip(),
            "reason": str(raw.get("reason") or domain_route.get("reason") or "").strip()[:240],
        }

    def _safe_fallback(self, normalized: str) -> dict[str, Any]:
        if normalized != "auto":
            candidates = [
                tool.id for tool in self.registry.agent_candidates(normalized)
                if tool.read_only and tool.capability_kind() != "navigation"
            ][:_MAX_ROUTE_TOOLS]
        else:
            candidates = []
        return {
            "skill_id": normalized if normalized != "auto" else "environment",
            "domain": "general",
            "domains": [],
            "mode": "query" if candidates else "answer",
            "tool_ids": candidates,
            "navigation_needed": False,
            "needs_current_time": False,
            "use_page_evidence": False,
            "workflow_hint": "none",
            "reason": "semantic_router_fallback",
        }
