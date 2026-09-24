from __future__ import annotations

from typing import Any

from apps.tooling.kernel.registry import ToolRegistry


class EnvironmentContextManager:
    """Inject deterministic runtime context into atomic Tool arguments.

    Runtime-owned internal identifiers always win after they have been resolved.
    The manager fills deterministic browser/runtime values so the AI never needs
    to invent database IDs, log targets, or time ranges.
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    @staticmethod
    def _runtime_log_targets(context: dict[str, Any]) -> list[dict[str, str]]:
        locator = context.get("log_locator") if isinstance(context.get("log_locator"), dict) else {}
        targets: list[dict[str, str]] = []
        for item in list(locator.get("fm_targets") or []):
            if not isinstance(item, dict):
                continue
            subsystem = str(item.get("subsystem") or "").strip()
            fm = str(item.get("fm") or item.get("module") or "").strip()
            kind = str(item.get("kind") or "normal").strip()
            if subsystem and fm:
                targets.append({"subsystem": subsystem, "fm": fm, "kind": kind if kind in {"normal", "executor"} else "normal"})
        if targets:
            return targets
        for raw in list(locator.get("targets") or []):
            text = str(raw or "").strip()
            if not text or "/" not in text:
                continue
            subsystem, fm = text.split("/", 1)
            kind = "executor" if fm.endswith(":executor") else "normal"
            if kind == "executor":
                fm = fm[:-9]
            subsystem, fm = subsystem.strip(), fm.strip()
            if subsystem and fm:
                targets.append({"subsystem": subsystem, "fm": fm, "kind": kind})
        return targets

    @staticmethod
    def _positive_int(value: Any) -> int | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _atlog_case_context(context: dict[str, Any]) -> dict[str, Any]:
        """Return the deterministic ATLog case snapshot visible to the browser.

        The current selected case is browser-owned state.  Merge every available
        representation so a stale conversation snapshot cannot hide fresher report
        or log evidence from the expanded case panel.
        """
        scope = context.get("assistant_scope") if isinstance(context.get("assistant_scope"), dict) else {}
        scoped_case = scope.get("atlog_case") if isinstance(scope.get("atlog_case"), dict) else {}
        selected_case = context.get("selected_atlog_case") if isinstance(context.get("selected_atlog_case"), dict) else {}
        page = context.get("atlog_page") if isinstance(context.get("atlog_page"), dict) else {}
        expanded = page.get("expanded_case") if isinstance(page.get("expanded_case"), dict) else {}
        merged: dict[str, Any] = {}
        # Scope is the conversation snapshot; live selected/expanded state wins.
        for value in (scoped_case, selected_case, expanded):
            if isinstance(value, dict):
                merged.update(value)
        return merged

    @staticmethod
    def _valid_atlog_url(value: Any) -> str:
        text = str(value or "").strip()
        lowered = text.casefold()
        return text if lowered.startswith("http://") or lowered.startswith("https://") else ""

    @classmethod
    def _atlog_targets(cls, context: dict[str, Any]) -> list[dict[str, str]]:
        case = cls._atlog_case_context(context)
        request = case.get("current_log_request") if isinstance(case.get("current_log_request"), dict) else {}
        values = list(request.get("fm_targets") or case.get("selected_targets") or [])
        targets: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in values:
            if not isinstance(item, dict):
                continue
            subsystem = str(item.get("subsystem") or "").strip()
            module = str(item.get("module") or item.get("fm") or "").strip()
            key = (subsystem.casefold(), module.casefold())
            if not subsystem or not module or key in seen:
                continue
            seen.add(key)
            targets.append({"subsystem": subsystem, "module": module})
        return targets

    @staticmethod
    def _case_evidence_entries(locator: dict[str, Any]) -> list[dict[str, Any]]:
        """把「用户当时正在看的那批日志」变成案例举证。

        浏览器已经把结构化条目放在 `displayed_evidence_entries` 里（含异常规则、来源文件、
        行号、级别、指纹）。这里只做搬运和裁剪，绝不重新解析字符串版本 ——
        重新解析是伪造来源的最短路径。
        """
        entries: list[dict[str, Any]] = []
        selected = locator.get("selected_entry") if isinstance(locator.get("selected_entry"), dict) else None
        candidates = list(locator.get("displayed_evidence_entries") or [])
        if selected:
            candidates.insert(0, selected)
        seen: set[str] = set()
        for item in candidates:
            if not isinstance(item, dict) or len(entries) >= 12:
                continue
            raw = str(item.get("raw") or item.get("message") or "").strip()
            if not raw:
                continue
            key = raw[:400]
            if key in seen:
                continue
            seen.add(key)
            entry = {key_name: item.get(key_name) for key_name in (
                "raw", "message", "level", "severity", "subsystem", "module", "component",
                "function_name", "source_file", "source_line", "source_category",
                "timestamp", "anomaly_rules", "template", "tokens", "error_codes",
            )}
            entry["raw"] = raw[:4000]
            entry["source"] = str(item.get("source_file") or "tracepilot")[:255]
            entry["evidence_kind"] = str(item.get("evidence_kind") or "").strip() or None
            entries.append({key_name: value for key_name, value in entry.items() if value not in (None, "")})
        return entries

    def _inject_case_binding(self, next_args: dict[str, Any], runtime: dict[str, Any]) -> None:
        """把案例草稿绑定到「本次分析的那份日志」。

        模型只决定「现在该沉淀案例了」；绑定到哪份日志、用哪些举证，是运行时已经
        掌握的确定性事实，不该让模型复述一遍（它抄错一次，案例就挂到别人身上了）。
        """
        locator = runtime.get("log_locator") if isinstance(runtime.get("log_locator"), dict) else {}
        if not locator:
            return
        if not str(next_args.get("source_task_name") or "").strip():
            next_args["source_task_name"] = str(locator.get("task_name") or "")[:255]
        if not next_args.get("environment_id"):
            environment_id = self._positive_int(locator.get("environment_id") or runtime.get("environment_id"))
            if environment_id is not None:
                next_args["environment_id"] = environment_id
        if not str(next_args.get("environment_name") or "").strip():
            next_args["environment_name"] = str(
                runtime.get("_assistant_resolved_environment_name") or locator.get("environment_name") or ""
            )[:128]
        if not isinstance(next_args.get("query_snapshot"), dict) or not next_args.get("query_snapshot"):
            request_range = locator.get("query_time_range") if isinstance(locator.get("query_time_range"), dict) else {}
            next_args["query_snapshot"] = {
                "task_id": str(locator.get("task_id") or ""),
                "task_name": str(locator.get("task_name") or "")[:255],
                "source": str(locator.get("source") or ""),
                "start_time": str(request_range.get("start") or ""),
                "end_time": str(request_range.get("end") or ""),
                "source_categories": list(locator.get("source_categories") or [])[:8],
                "fm_targets": list(locator.get("fm_targets") or [])[:12],
                "keyword": str(locator.get("keyword") or "")[:255],
                "errors_only": bool(locator.get("errors_only")),
                "result_count": locator.get("result_count"),
                "source_files": list(locator.get("source_files") or [])[:8],
            }
        # 模型自己传了结构化条目就优先用它的；否则用页面当时展示的那一批，
        # 保证「保存的案例」和「刚分析过的日志」是同一份证据。
        if not next_args.get("evidence_entries"):
            entries = self._case_evidence_entries(locator)
            if entries:
                next_args["evidence_entries"] = entries

    def absorb_result(self, tool_id: str, data: Any, context: dict[str, Any] | None) -> None:
        """Persist deterministic Tool facts back into the runtime context.

        Internal database identifiers must be carried by the Kernel rather than by
        the model.  In particular, ``find_environment`` intentionally hides the
        numeric ``environment_id`` from user-facing/LLM summaries, so the resolved
        id is bound here for every later environment Tool in the same run.
        """
        runtime = context if isinstance(context, dict) else None
        if runtime is None or not isinstance(data, dict):
            return

        if tool_id != "find_environment":
            return

        environment = data.get("environment") if isinstance(data.get("environment"), dict) else None
        environment_id = self._positive_int(environment.get("id") if environment else None)
        if bool(data.get("found")) and environment_id is not None:
            runtime["environment_id"] = environment_id
            runtime["_assistant_resolved_environment_id"] = environment_id
            runtime["_assistant_resolved_environment_name"] = str(
                environment.get("name") or environment.get("display_name") or ""
            ).strip()
            runtime.pop("_assistant_environment_resolution_failed", None)
            runtime.pop("_assistant_environment_query", None)

            locator = runtime.get("log_locator")
            if isinstance(locator, dict):
                locator["environment_id"] = environment_id
            return

        # A failed explicit resolver call must not silently fall back to the page's
        # previously selected environment.  Keep the page context intact for the UI,
        # but block environment actions until the Agent resolves a real target.
        runtime.pop("_assistant_resolved_environment_id", None)
        runtime["_assistant_environment_resolution_failed"] = True
        runtime["_assistant_environment_query"] = str(
            data.get("query") or data.get("requested_query") or ""
        ).strip()

    def inject(self, tool_id: str, arguments: dict[str, Any], context: dict[str, Any] | None) -> dict[str, Any]:
        tool = self.registry.get(tool_id)
        next_args = dict(arguments or {})
        runtime = context if isinstance(context, dict) else {}
        if tool is None:
            return next_args

        properties = tool.input_schema.get("properties") if isinstance(tool.input_schema, dict) else {}
        properties = properties if isinstance(properties, dict) else {}
        if tool_id == "control_log_view":
            locator = runtime.get("log_locator") if isinstance(runtime.get("log_locator"), dict) else {}
            next_args["task_id"] = locator.get("task_id") or ""

        # ATLog URL/time/target values are owned by the current page context.
        # Inject them in the Kernel instead of asking the LLM to reproduce them
        # across multiple tool rounds.
        atlog_case = self._atlog_case_context(runtime)
        if atlog_case and tool_id in {"analyze_atlog_case", "query_atlog_event", "query_atlog_logs"}:
            if "url" in properties:
                # The selected case URL is deterministic browser state and therefore
                # authoritative.  Never allow an LLM-generated case id/name/path to
                # overwrite it; the model decides *which* Tool to call, not how to
                # reconstruct the current case locator.
                case_url = self._valid_atlog_url(
                    atlog_case.get("case_url")
                    or atlog_case.get("source_case_url")
                    or atlog_case.get("url")
                )
                if case_url:
                    next_args["url"] = case_url
                elif not self._valid_atlog_url(next_args.get("url")):
                    next_args.pop("url", None)
            request = atlog_case.get("current_log_request") if isinstance(atlog_case.get("current_log_request"), dict) else {}
            for key in ("start_time", "end_time"):
                if key in properties and not str(next_args.get(key) or "").strip():
                    value = str(atlog_case.get(key) or request.get(key) or "").strip()
                    if value:
                        next_args[key] = value
            if tool_id == "query_atlog_logs" and not next_args.get("targets"):
                targets = self._atlog_targets(runtime)
                if targets:
                    next_args["targets"] = targets

        if tool_id == "start_environment_deployment":
            trace_context = runtime.get("_trace_context") if isinstance(runtime.get("_trace_context"), dict) else None
            if trace_context:
                next_args["_trace_context"] = {
                    "operator": str(trace_context.get("operator") or "TracePilot").strip(),
                    "client_ip": str(trace_context.get("client_ip") or "").strip(),
                    "source": str(trace_context.get("source") or "assistant").strip() or "assistant",
                }

        if tool_id == "draft_diagnosis_case":
            self._inject_case_binding(next_args, runtime)

        if "environment_id" in properties:
            # A resolver-produced id is authoritative.  Never let the model guess or
            # overwrite an internal database key after ``find_environment`` succeeded.
            resolved_id = self._positive_int(runtime.get("_assistant_resolved_environment_id"))
            if resolved_id is not None:
                next_args["environment_id"] = resolved_id
            elif runtime.get("_assistant_environment_resolution_failed"):
                raise ValueError("目标环境尚未解析成功，请先重新查找并确认具体环境。")
            elif not next_args.get("environment_id"):
                locator = runtime.get("log_locator") if isinstance(runtime.get("log_locator"), dict) else {}
                environment_id = locator.get("environment_id") or runtime.get("environment_id")
                if environment_id not in (None, ""):
                    next_args["environment_id"] = environment_id

        if tool.id != "query_environment_logs":
            return next_args

        locator = runtime.get("log_locator") if isinstance(runtime.get("log_locator"), dict) else {}
        chosen = runtime.get("_assistant_selected_log_target") if isinstance(runtime.get("_assistant_selected_log_target"), dict) else None
        if not next_args.get("fm_targets") and chosen:
            subsystem = str(chosen.get("subsystem") or "").strip()
            fm = str(chosen.get("fm") or "").strip()
            kind = str(chosen.get("kind") or "normal").strip() or "normal"
            if subsystem and fm:
                next_args["fm_targets"] = [{"subsystem": subsystem, "fm": fm, "kind": kind}]

        runtime_targets = self._runtime_log_targets(runtime)
        if not next_args.get("fm_targets") and runtime_targets:
            component_name = str(next_args.get("component_name") or "").strip()
            selected_names = {str(item.get("fm") or "").strip().casefold() for item in runtime_targets}
            context_component = str(locator.get("component_name") or "").strip().casefold()
            if not component_name or component_name.casefold() in selected_names or (context_component and component_name.casefold() == context_component):
                next_args["fm_targets"] = runtime_targets

        for key in ("source_categories", "keyword"):
            if next_args.get(key) in (None, "", []):
                value = locator.get(key)
                if value not in (None, "", []):
                    next_args[key] = value
        if "errors_only" not in next_args and locator.get("errors_only") is not None:
            next_args["errors_only"] = bool(locator.get("errors_only"))
        if not next_args.get("start_time") or not next_args.get("end_time"):
            for key in ("view_time_range", "query_time_range", "loaded_time_range"):
                time_range = locator.get(key) if isinstance(locator.get(key), dict) else {}
                start, end = time_range.get("start"), time_range.get("end")
                if start and end:
                    next_args.setdefault("start_time", start)
                    next_args.setdefault("end_time", end)
                    break
        return next_args
