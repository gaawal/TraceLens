from __future__ import annotations

"""Token-bounded Tool result projections for the TracePilot Agent.

Business tools return rich payloads for the UI.  The model should not receive
those payloads verbatim.  This module uses a registry of semantic compactors so
new tools can add a projection without growing one monolithic ``if/elif`` block.
"""

from collections.abc import Callable
from typing import Any

from apps.tooling.registry import ToolDefinition

DEFAULT_TOOL_RESULT_CHARS = 12_000
Compactor = Callable[[Any], str]
_TOOL_COMPACTORS: dict[str, Compactor] = {}


def register_compactor(*tool_ids: str):
    def decorate(fn: Compactor) -> Compactor:
        for tool_id in tool_ids:
            _TOOL_COMPACTORS[str(tool_id)] = fn
        return fn
    return decorate


def shrink_for_llm(value: Any, *, depth: int = 0) -> Any:
    if depth >= 5:
        return "<nested data omitted>"
    if isinstance(value, str):
        return value if len(value) <= 3000 else value[:3000] + "…<truncated>"
    if isinstance(value, list):
        kept = [shrink_for_llm(item, depth=depth + 1) for item in value[:40]]
        if len(value) > 40:
            kept.append({"omitted_items": len(value) - 40})
        return kept
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 60:
                result["_omitted_fields"] = len(value) - 60
                break
            result[str(key)] = shrink_for_llm(item, depth=depth + 1)
        return result
    return value


def compact_scalar(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "是" if value else "否"
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text if len(text) <= 1200 else text[:1200] + "…"


def compact_semantic_lines(
    value: Any,
    *,
    prefix: str = "",
    depth: int = 0,
    lines: list[str] | None = None,
) -> list[str]:
    lines = lines if lines is not None else []
    if depth > 4 or len(lines) >= 120:
        return lines
    if isinstance(value, dict):
        for key, item in value.items():
            if len(lines) >= 120:
                break
            name = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(item, (dict, list)):
                compact_semantic_lines(item, prefix=name, depth=depth + 1, lines=lines)
            else:
                lines.append(f"{name}={compact_scalar(item)}")
        return lines
    if isinstance(value, list):
        if not value:
            if prefix:
                lines.append(f"{prefix}=-")
            return lines
        if all(not isinstance(item, (dict, list)) for item in value):
            joined = " | ".join(compact_scalar(item) for item in value[:30])
            if len(value) > 30:
                joined += f" | …另{len(value) - 30}项"
            lines.append(f"{prefix}={joined}" if prefix else joined)
            return lines
        for index, item in enumerate(value[:20], start=1):
            compact_semantic_lines(
                item,
                prefix=f"{prefix}[{index}]" if prefix else f"item[{index}]",
                depth=depth + 1,
                lines=lines,
            )
        if len(value) > 20:
            lines.append(f"{prefix}.省略={len(value) - 20}项")
        return lines
    lines.append(f"{prefix}={compact_scalar(value)}" if prefix else compact_scalar(value))
    return lines


def dense_yaml_lines(
    value: Any,
    *,
    indent: int = 0,
    depth: int = 0,
    lines: list[str] | None = None,
) -> list[str]:
    lines = lines if lines is not None else []
    if depth > 4 or len(lines) >= 120:
        return lines
    pad = "  " * indent
    if isinstance(value, dict):
        for index, (key, item) in enumerate(value.items()):
            if index >= 60 or len(lines) >= 120:
                remaining = max(0, len(value) - index)
                if remaining:
                    lines.append(f"{pad}省略字段: {remaining}")
                break
            key_text = str(key)
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}{key_text}:")
                dense_yaml_lines(item, indent=indent + 1, depth=depth + 1, lines=lines)
            else:
                lines.append(f"{pad}{key_text}: {compact_scalar(item)}")
        return lines
    if isinstance(value, list):
        if not value:
            lines.append(f"{pad}-")
            return lines
        for item in value[:24]:
            if len(lines) >= 120:
                break
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}-")
                dense_yaml_lines(item, indent=indent + 1, depth=depth + 1, lines=lines)
            else:
                lines.append(f"{pad}- {compact_scalar(item)}")
        if len(value) > 24:
            lines.append(f"{pad}省略项: {len(value) - 24}")
        return lines
    lines.append(f"{pad}{compact_scalar(value)}")
    return lines


def _machine_summary(machine: Any) -> str:
    if not isinstance(machine, dict):
        return "-"
    station = machine.get("station") if isinstance(machine.get("station"), dict) else {}
    name = machine.get("station_name") or station.get("station_name") or machine.get("name") or "-"
    host = machine.get("host") or "-"
    status = machine.get("connection_status") or "-"
    version = machine.get("software_version") or "-"
    return f"{name}@{host};状态={status};版本={version}"


def compact_environment_info(data: dict[str, Any]) -> str:
    lines = [
        f"环境={compact_scalar(data.get('name'))}",
        f"状态={compact_scalar(data.get('status'))};版本={compact_scalar(data.get('software_version'))};版本不一致={compact_scalar(data.get('version_mismatch'))}",
        f"上位机={_machine_summary(data.get('upper_machine'))}",
    ]
    lowers = [item for item in list(data.get("lower_machines") or []) if isinstance(item, dict)]
    if lowers:
        lines.append("下位机=" + " | ".join(_machine_summary(item) for item in lowers[:12]))
        if len(lowers) > 12:
            lines.append(f"下位机省略={len(lowers) - 12}")
    dhh = data.get("dhh_machine")
    if isinstance(dhh, dict):
        lines.append(f"DHH={_machine_summary(dhh)}")
    mismatch_hosts = [str(item) for item in list(data.get("version_mismatch_hosts") or []) if str(item).strip()]
    if mismatch_hosts:
        lines.append("版本不一致主机=" + " | ".join(mismatch_hosts[:12]))
    if data.get("description"):
        lines.append(f"说明={compact_scalar(data.get('description'))}")
    return "\n".join(lines)


def compact_find_environment(data: dict[str, Any]) -> str:
    lines = [f"找到={compact_scalar(data.get('found'))};精确={compact_scalar(data.get('exact'))}"]
    env = data.get("environment")
    if isinstance(env, dict):
        lines.append(compact_environment_info(env))
    candidates = [item for item in list(data.get("candidates") or []) if isinstance(item, dict)]
    if candidates:
        lines.append("候选:")
        for item in candidates[:10]:
            upper = item.get("upper_machine") if isinstance(item.get("upper_machine"), dict) else {}
            lines.append(
                f"- {compact_scalar(upper.get('host'))} / {compact_scalar(item.get('name'))};状态={compact_scalar(item.get('status'))}"
            )
        if len(candidates) > 10:
            lines.append(f"候选省略={len(candidates) - 10}")
    return "\n".join(lines)


def compact_runtime_status(data: dict[str, Any]) -> str:
    lines = [f"检查时间={compact_scalar(data.get('checked_at'))};缓存={compact_scalar(data.get('cache_status'))}"]
    upper = data.get("upper") if isinstance(data.get("upper"), dict) else {}
    if upper:
        lines.append(
            f"上位机={compact_scalar(upper.get('host'))};在线={compact_scalar(upper.get('online'))};时间={compact_scalar(upper.get('remote_date'))};信息={compact_scalar(upper.get('message'))}"
        )
    lowers = [item for item in list(data.get("lowers") or []) if isinstance(item, dict)]
    for item in lowers[:16]:
        lines.append(
            "下位机="
            f"{compact_scalar(item.get('host'))};在线={compact_scalar(item.get('online'))};"
            f"TB={compact_scalar(item.get('tb_simulator_running'))};时间偏差秒={compact_scalar(item.get('time_offset_seconds'))};"
            f"需同步={compact_scalar(item.get('time_sync_required'))};信息={compact_scalar(item.get('message'))}"
        )
    if len(lowers) > 16:
        lines.append(f"下位机省略={len(lowers) - 16}")
    dhh = data.get("dhh") if isinstance(data.get("dhh"), dict) else {}
    if dhh:
        lines.append(
            f"DHH={compact_scalar(dhh.get('host'))};在线={compact_scalar(dhh.get('online'))};服务运行={compact_scalar(dhh.get('service_running'))};信息={compact_scalar(dhh.get('message'))}"
        )
    return "\n".join(lines)


def compact_environment_versions(data: dict[str, Any]) -> str:
    lines = [
        f"上位机版本={compact_scalar(data.get('version'))};版本不一致={compact_scalar(data.get('version_mismatch'))};检查时间={compact_scalar(data.get('checked_at'))}"
    ]
    lowers = [item for item in list(data.get("lower_versions") or []) if isinstance(item, dict)]
    for item in lowers[:20]:
        lines.append(
            f"下位机={compact_scalar(item.get('host'))};版本={compact_scalar(item.get('version'))};不一致={compact_scalar(item.get('mismatch'))};跳过={compact_scalar(item.get('skipped'))};信息={compact_scalar(item.get('message'))}"
        )
    if len(lowers) > 20:
        lines.append(f"下位机省略={len(lowers) - 20}")
    return "\n".join(lines)


def _environment_user_label(environment_id: Any) -> str:
    try:
        env_id = int(environment_id)
    except (TypeError, ValueError):
        return ""
    try:
        from apps.environments.models import Environment

        environment = Environment.objects.select_related("upper_machine").get(pk=env_id)
    except Exception:
        return ""
    machine = getattr(environment, "upper_machine", None)
    host = str(getattr(machine, "host", "") or "").strip()
    station = str(getattr(machine, "station_name", "") or "").strip()
    name = str(getattr(environment, "name", "") or "").strip()
    secondary = station or name
    if host and secondary:
        return host if secondary == host else f"{host} / {secondary}"
    return host or secondary


def compact_deployment_rows(data: Any) -> str:
    if isinstance(data, dict) and "deployments" in data:
        rows = list(data.get("deployments") or [])
        prefix = f"部署数={compact_scalar(data.get('count'))}"
    elif isinstance(data, dict) and "deployment" in data:
        rows = [data.get("deployment")] if isinstance(data.get("deployment"), dict) else []
        prefix = "最新部署=" + ("有" if rows else "无")
    elif isinstance(data, dict):
        rows = [data]
        prefix = "部署详情"
    else:
        return "\n".join(dense_yaml_lines(data))
    lines = [prefix]
    for item in rows[:12]:
        if not isinstance(item, dict):
            continue
        lines.append(
            f"- 任务={compact_scalar(item.get('id'))};环境={compact_scalar(_environment_user_label(item.get('environment') or item.get('environment_id')) or '-')};"
            f"状态={compact_scalar(item.get('status'))};阶段={compact_scalar(item.get('current_step'))};"
            f"版本={compact_scalar(item.get('target_version'))};模式={compact_scalar(item.get('simulation_mode'))};"
            f"开始={compact_scalar(item.get('started_at') or item.get('created_at'))};结束={compact_scalar(item.get('finished_at'))}"
        )
        steps = [step for step in list(item.get("steps") or []) if isinstance(step, dict)]
        for step in steps[:12]:
            lines.append(
                f"  步骤={compact_scalar(step.get('key') or step.get('step_key') or step.get('name'))};状态={compact_scalar(step.get('status'))};信息={compact_scalar(step.get('message'))}"
            )
    if len(rows) > 12:
        lines.append(f"部署省略={len(rows) - 12}")
    return "\n".join(lines)


@register_compactor("resolve_environment_component")
def _component_result(data: Any) -> str:
    if not isinstance(data, dict):
        return "\n".join(dense_yaml_lines(data))
    targets = list(data.get("targets") or [])
    lines = [
        f"匹配={'精确' if data.get('exact_match') else '歧义' if data.get('ambiguous') else '未匹配'}",
        f"组件={compact_scalar(data.get('component_name'))}",
        f"来源={compact_scalar(data.get('source'))}",
    ]
    for index, item in enumerate(targets[:8], start=1):
        if isinstance(item, dict):
            lines.append(
                f"目标{index}={compact_scalar(item.get('subsystem'))}/{compact_scalar(item.get('fm') or item.get('module'))};类型={compact_scalar(item.get('kind') or 'normal')}"
            )
    if data.get("reason"):
        lines.append(f"说明={compact_scalar(data.get('reason'))}")
    return "\n".join(lines)


@register_compactor("query_environment_logs")
def _environment_logs(data: Any) -> str:
    if not isinstance(data, dict):
        return "\n".join(dense_yaml_lines(data))
    targets = ", ".join(
        f"{item.get('subsystem')}/{item.get('fm') or item.get('module')}"
        for item in list(data.get("targets") or [])[:8]
        if isinstance(item, dict)
    )
    header = [
        f"环境={compact_scalar(data.get('environment_name') or '-')}",
        f"组件={compact_scalar(data.get('component_name'))}",
        f"目标={targets or '-'}",
        f"日志类型={'/'.join(str(x) for x in (data.get('source_categories') or [])) or 'debug'}",
        f"时间={compact_scalar(data.get('start_time'))} ~ {compact_scalar(data.get('end_time'))}",
        f"过滤={compact_scalar(data.get('filter_mode'))}",
        f"命中行={compact_scalar(data.get('evidence_line_count'))};扫描行={compact_scalar(data.get('scanned_line_count'))};文件={compact_scalar(data.get('artifact_count'))}",
        f"压缩=函数折叠;函数组={compact_scalar(data.get('included_function_count'))}/{compact_scalar(data.get('function_count'))};上下文={compact_scalar(data.get('context_char_count'))}/{compact_scalar(data.get('context_char_budget'))}字符",
        f"扫描文本={compact_scalar(data.get('scanned_bytes'))}/{compact_scalar(data.get('scan_byte_budget'))}字节;截断={compact_scalar(data.get('truncated'))}",
    ]
    if data.get("keyword"):
        header.append(f"关键字={compact_scalar(data.get('keyword'))}")
    folded = str(data.get("folded_context") or "").strip()
    return "\n".join(header + (["函数折叠证据:", folded] if folded else ["证据=无匹配日志"]))


def _compact_atlog_rows(rows: Any, *, limit: int = 24) -> list[str]:
    values = [item for item in list(rows or []) if isinstance(item, dict)]
    priority = [
        item for item in values
        if str(item.get("level") or "").upper() in {"ERROR", "FATAL", "CRITICAL", "ALARM", "WARN", "WARNING"}
    ]
    ordered = [*priority, *values[-max(limit * 2, 24):]]
    lines: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for item in ordered:
        source = str(item.get("source_path") or item.get("source") or "")
        line_no = str(item.get("line_number") or item.get("line") or "")
        message = str(item.get("message") or item.get("raw") or "").replace("\n", " ").strip()
        key = (source, line_no, message[:400])
        if key in seen or not message:
            continue
        seen.add(key)
        lines.append(
            f"- {compact_scalar(item.get('time'))} | {compact_scalar(item.get('level'))} | "
            f"{compact_scalar(item.get('component'))} | {source or '-'}"
            + (f":{line_no}" if line_no else "")
            + f" | {message[:900]}"
        )
        if len(lines) >= limit:
            break
    return lines


@register_compactor("analyze_atlog_case")
def _atlog_case_analysis(data: Any) -> str:
    if not isinstance(data, dict):
        return "\n".join(dense_yaml_lines(data))
    links = data.get("links") if isinstance(data.get("links"), dict) else {}
    location = data.get("failure_location") if isinstance(data.get("failure_location"), dict) else {}
    lines = [
        f"用例={compact_scalar(data.get('case_id') or data.get('case_name'))};状态={compact_scalar(data.get('status'))}",
        f"时间={compact_scalar(data.get('start_time'))} ~ {compact_scalar(data.get('end_time'))};失败点={compact_scalar(data.get('failure_time'))}",
        f"event窗口={compact_scalar(data.get('event_start_time'))} ~ {compact_scalar(data.get('event_end_time'))}",
        f"失败断言={compact_scalar(data.get('assertion_summary') or data.get('assertion') or data.get('conclusion'))}",
    ]
    if location:
        lines.append(
            f"失败位置={compact_scalar(location.get('file'))}:{compact_scalar(location.get('line'))} {compact_scalar(location.get('function'))}"
        )
    failure_text = str(data.get("failure_text") or "").strip()
    if failure_text:
        lines.append("pytest/XML失败正文=" + failure_text[-2200:].replace("\n", " "))
    report_excerpt = str(data.get("report_excerpt") or "").strip()
    if report_excerpt:
        lines.append("pytest HTML摘录=" + report_excerpt[-2400:].replace("\n", " "))
    xy_errors = [item for item in list(data.get("xytest_errors") or []) if isinstance(item, dict)]
    if xy_errors:
        lines.append("xytest关键异常:")
        for item in xy_errors[-6:]:
            lines.append(
                f"- {compact_scalar(item.get('time'))} | {compact_scalar(item.get('level'))} | "
                f"{str(item.get('raw') or item.get('message') or '')[:900]}"
            )
    if links:
        useful = {key: value for key, value in links.items() if key in {"pytest_xml", "test_html", "xytest.log", "summary_report.xml", "summary_report"} and value}
        if useful:
            lines.append("原始证据链接=" + " | ".join(f"{key}:{value}" for key, value in useful.items()))
    return "\n".join(lines)


@register_compactor("query_atlog_event")
def _atlog_event(data: Any) -> str:
    if not isinstance(data, dict):
        return "\n".join(dense_yaml_lines(data))
    lines = [
        f"event时间={compact_scalar(data.get('start_time'))} ~ {compact_scalar(data.get('end_time'))}",
        f"event行数={compact_scalar(data.get('count'))};截断={compact_scalar(data.get('truncated'))};读取模式={compact_scalar(data.get('read_mode'))}",
        "组件=" + ("、".join(str(item) for item in list(data.get("components") or [])[:20]) or "-"),
        "DisplayCode=" + ("、".join(str(item) for item in list(data.get("display_codes") or [])[:20]) or "-"),
    ]
    evidence = _compact_atlog_rows(data.get("rows"), limit=20)
    if evidence:
        lines.append("event关键证据:")
        lines.extend(evidence)
    return "\n".join(lines)


@register_compactor("query_atlog_logs")
def _atlog_logs(data: Any) -> str:
    if not isinstance(data, dict):
        return "\n".join(dense_yaml_lines(data))
    lines = [
        f"ATLog时间={compact_scalar(data.get('start_time'))} ~ {compact_scalar(data.get('end_time'))}",
        f"命中行={compact_scalar(data.get('count'))};截断={compact_scalar(data.get('truncated'))};目标数={compact_scalar(data.get('matched_target_count'))};文件数={compact_scalar(data.get('matched_file_count'))}",
        f"异常规则数={compact_scalar(data.get('anomaly_rule_count'))};搜索引擎={compact_scalar(data.get('search_engine'))}",
    ]
    files = [str(item) for item in list(data.get("matched_files") or []) if str(item).strip()]
    if files:
        lines.append("命中文件=" + " | ".join(files[:12]))
    ai_context = str(data.get("ai_context") or "").strip()
    if ai_context:
        lines.append(
            f"模型上下文={compact_scalar(data.get('ai_context_char_count'))}/{compact_scalar(data.get('ai_context_char_budget'))}字符;"
            f"证据节点={compact_scalar(data.get('evidence_node_count'))};省略原始行={compact_scalar(data.get('omitted_row_count'))}"
        )
        lines.append("受控日志证据:")
        lines.append(ai_context[:12000])
    else:
        evidence = _compact_atlog_rows(data.get("evidence_rows") or data.get("rows"), limit=28)
        if evidence:
            lines.append("运行关键证据:")
            lines.extend(evidence)
        else:
            lines.append("运行关键证据=当前筛选无匹配")
    return "\n".join(lines)


@register_compactor("list_log_semantic_rules")
def _semantic_rules(data: Any) -> str:
    if not isinstance(data, dict):
        return "\n".join(dense_yaml_lines(data))
    lines = [f"规则数={compact_scalar(data.get('count'))}"]
    for item in list(data.get("rules") or [])[:30]:
        if isinstance(item, dict):
            lines.append(
                f"- {compact_scalar(item.get('name'))};范围={compact_scalar(item.get('scope'))};模式={compact_scalar(item.get('mode'))};关键字={compact_scalar(item.get('keyword'))};语义={compact_scalar(item.get('semantic'))};标签={compact_scalar(item.get('label'))}"
            )
    return "\n".join(lines)


@register_compactor("create_log_semantic_rule")
def _semantic_rule_created(data: Any) -> str:
    if not isinstance(data, dict):
        return "\n".join(dense_yaml_lines(data))
    return "\n".join([
        f"语义规则候选={compact_scalar(data.get('rule_name'))}",
        f"类型={compact_scalar(data.get('kind'))};范围={compact_scalar(data.get('scope'))}",
        "说明=前端会转换成 TraceLens 原生语义规则并校验保存。",
    ])


@register_compactor("create_log_anomaly_rule")
def _anomaly_rule_created(data: Any) -> str:
    if not isinstance(data, dict):
        return "\n".join(dense_yaml_lines(data))
    return "\n".join([
        f"异常规则候选={','.join(str(item) for item in list(data.get('keywords') or [])[:32]) or '-'}",
        "说明=只保存真实日志中的稳定异常关键字。",
    ])


@register_compactor("create_data_extraction_capability")
def _data_extraction_capability(data: Any) -> str:
    if not isinstance(data, dict):
        return "\n".join(dense_yaml_lines(data))
    fields = ",".join(str(item) for item in list(data.get("fields") or [])[:24])
    return "\n".join([
        f"候选能力={compact_scalar(data.get('status'))}",
        f"字段={fields or '-'};数量={compact_scalar(data.get('field_count'))}",
        f"验证后保存={compact_scalar(data.get('persist_rule'))};自动采集={compact_scalar(data.get('auto_start'))};打开数据页={compact_scalar(data.get('open_data_page'))};打开规则页={compact_scalar(data.get('open_rule_settings'))}",
        "说明=前端会先在当前已加载日志上验证命中；未命中不会保存规则。",
    ])


@register_compactor("list_data_extraction_rules")
def _data_extraction_rules(data: Any) -> str:
    if not isinstance(data, dict):
        return "\n".join(dense_yaml_lines(data))
    lines = [f"提取器数={compact_scalar(data.get('count'))}"]
    for item in list(data.get("rules") or [])[:24]:
        if not isinstance(item, dict):
            continue
        fields = ",".join(
            str(field.get("name") or field.get("key") or "")
            for field in list(item.get("fields") or [])[:24]
            if isinstance(field, dict)
        )
        modules = ",".join(str(x) for x in list(item.get("modules") or [])[:8])
        lines.append(
            f"- id={compact_scalar(item.get('id'))};名称={compact_scalar(item.get('name'))};模块={modules or '-'};字段={fields or '-'}"
        )
    return "\n".join(lines)


@register_compactor("get_environment_info")
def _environment_info(data: Any) -> str:
    return compact_environment_info(data) if isinstance(data, dict) else "\n".join(dense_yaml_lines(data))


@register_compactor("find_environment")
def _find_environment(data: Any) -> str:
    return compact_find_environment(data) if isinstance(data, dict) else "\n".join(dense_yaml_lines(data))


@register_compactor("get_environment_runtime_status")
def _runtime_status(data: Any) -> str:
    return compact_runtime_status(data) if isinstance(data, dict) else "\n".join(dense_yaml_lines(data))


@register_compactor("query_environment_version")
def _environment_version(data: Any) -> str:
    return compact_environment_versions(data) if isinstance(data, dict) else "\n".join(dense_yaml_lines(data))


@register_compactor(
    "list_environment_deployments",
    "get_latest_deployment",
    "get_deployment_detail",
    "get_active_deployments",
    "start_environment_deployment",
    "stop_environment_deployment",
    "retry_environment_deployment_step",
)
def _deployments(data: Any) -> str:
    return compact_deployment_rows(data)


def compact_result(value: Any, tool: ToolDefinition | None = None) -> str:
    budget = max(
        1200,
        min(
            int(getattr(tool, "max_llm_output_chars", DEFAULT_TOOL_RESULT_CHARS) or DEFAULT_TOOL_RESULT_CHARS),
            50_000,
        ),
    )
    data = value.get("data") if isinstance(value, dict) and value.get("status") == "ok" and "data" in value else value

    compactor = _TOOL_COMPACTORS.get(tool.id) if tool is not None else None
    if compactor is not None:
        text = compactor(data)
    elif isinstance(value, dict) and value.get("status") == "error":
        text = "\n".join([
            "状态=失败",
            f"可重试={compact_scalar(value.get('recoverable'))}",
            f"原因={compact_scalar(value.get('reason'))}",
            f"修正建议={compact_scalar(value.get('hint'))}",
        ])
    else:
        text = "\n".join(dense_yaml_lines(data))

    if len(text) <= budget:
        return text
    return text[:budget] + "\n…结果已按 Token 预算截断，请缩小查询范围后再取更多证据。"
