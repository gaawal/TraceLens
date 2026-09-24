"""Knowledge-loop and cross-signal analysis capabilities for TracePilot.

``save_diagnosis_case`` closes the loop that was previously open: the agent could *read*
historical cases (``match_cases``) and the REST API could *write* one
(``/atlog-analysis/ai-accept/``), but the agent itself had no way to record what it just
concluded. Everything it learned died with the conversation.

The two comparison tools exist because the agent could already answer "what is broken"
but not "what changed" or "what is new" — the two questions that actually drive
observability work.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from apps.tooling.plugins.catalog import register_function_tool
from apps.tooling.registry import ToolDefinition

logger = logging.getLogger("tracelens.tooling.plugins.analysis")

_ENV_ID = {"type": ["integer", "string"], "description": "TraceLens 环境 ID"}


def _environment(environment_id: Any):
    from apps.tooling.services import _environment as resolve

    return resolve(environment_id)


# ------------------------------------------------------------ knowledge capture
@register_function_tool(ToolDefinition(
    id="save_diagnosis_case",
    name="保存诊断结论为知识案例",
    description=(
        "把一次已经成立的诊断结论连同它的真实日志证据保存进案例库，供以后 match_cases 复用。"
        "这是写操作，必须经用户确认。"
    ),
    category="自动化用例",
    handler=None,
    agent_exposed=True,
    read_only=False,
    risk_level="low_write",
    transport="function",
    domain="case",
    kind="mutation",
    skills=("atlog", "logs"),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "案例名称（简短、可检索）"},
            "symptom": {"type": "string", "description": "故障现象：用户能看到的表现"},
            "root_cause": {"type": "string", "description": "已确认的根因"},
            "solution": {"type": "string", "default": "", "description": "处理建议"},
            "category": {"type": "string", "default": "", "description": "故障分类"},
            "tags": {"type": "array", "items": {"type": "string"}, "default": []},
            "environment_id": _ENV_ID,
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
                "description": "支撑结论的真实日志原文（必须来自实际读取到的日志，不能编造）",
            },
            "link_to_case_id": {
                "type": "integer",
                "description": "可选：并入一个已存在的历史案例，而不是新建",
            },
        },
        "required": ["name", "symptom", "root_cause"],
    },
    use_when=(
        "用户明确要求“记下来 / 存成案例 / 下次遇到直接匹配”，且根因已经由真实日志证据支撑时。"
        "必须先向用户展示将要保存的内容并取得确认。"
    ),
    do_not_use_when=(
        "结论还没有证据支撑、只是猜测；或用户没有要求沉淀知识。"
        "不要为了“留个记录”而自动保存。"
    ),
    validation_rules=(
        "evidence 必须来自真实读取到的日志原文；没有证据时不要调用本工具",
        "写操作，必须先生成确认卡并取得用户明确确认",
    ),
    implementation="apps.tooling.plugins.analysis.save_diagnosis_case",
))
def save_diagnosis_case(payload: dict) -> dict[str, Any]:
    from datetime import datetime, timezone
    from uuid import uuid4

    from django.utils import timezone as dj_timezone

    from apps.knowledge.models import AbnormalCase

    name = str(payload.get("name") or "").strip()[:255]
    symptom = str(payload.get("symptom") or "").strip()
    root_cause = str(payload.get("root_cause") or "").strip()
    if not name or not symptom or not root_cause:
        raise ValueError("name / symptom / root_cause 不能为空。")

    evidence = [str(item).strip() for item in (payload.get("evidence") or []) if str(item).strip()]
    tags = [str(item).strip()[:64] for item in (payload.get("tags") or []) if str(item).strip()][:12]
    category = str(payload.get("category") or "").strip()[:128]
    solution = str(payload.get("solution") or "").strip()
    environment = None
    environment_name = ""
    if payload.get("environment_id") not in (None, ""):
        environment = _environment(payload.get("environment_id"))
        environment_name = str(getattr(environment, "name", "") or "")[:128]

    now = datetime.now(timezone.utc).isoformat()
    group = {
        "id": uuid4().hex,
        "title": f"TracePilot 诊断现场 · {name}"[:128],
        "enabled": True,
        "created_at": now,
        "source_operation_id": "",
        "source_task_name": name[:255],
        "environment_name": environment_name,
        "note": f"由 TracePilot 对话诊断后保存。\n现象：{symptom}\n根因：{root_cause}",
        "evidences": [{"raw": line[:4000], "source": "tracepilot"} for line in evidence[:40]],
    }

    target_id = payload.get("link_to_case_id")
    if target_id not in (None, ""):
        instance = AbnormalCase.objects.filter(pk=int(target_id), enabled=True).first()
        if instance is None:
            raise ValueError("目标历史案例不存在或已停用。")
        groups = [dict(item) for item in (instance.feature_groups or []) if isinstance(item, dict)]
        if len(groups) >= 30:
            raise ValueError("该案例已达到 30 组现场特征上限，请先治理旧现场。")
        groups.append(group)
        instance.feature_groups = groups
        instance.evidence_count = int(instance.evidence_count or 0) + len(evidence)
        # Preserve established knowledge; only fill genuine blanks.
        if not instance.root_cause:
            instance.root_cause = root_cause
        if not instance.symptom:
            instance.symptom = symptom
        instance.tags = list(dict.fromkeys([*(instance.tags or []), *tags]))[:24]
        instance.save(update_fields=["feature_groups", "evidence_count", "root_cause", "symptom", "tags", "updated_at"])
        return {
            "ok": True,
            "action": "appended",
            "case_id": instance.pk,
            "case_name": instance.name,
            "feature_group_count": len(groups),
            "evidence_count": len(evidence),
        }

    instance = AbnormalCase.objects.create(
        name=name,
        category=category,
        symptom=symptom,
        root_cause=root_cause,
        solution=solution,
        tags=tags,
        environment=environment,
        environment_name=environment_name,
        evidences=[{"raw": line[:4000], "source": "tracepilot"} for line in evidence[:40]],
        feature_groups=[group],
        evidence_count=len(evidence),
        last_matched_at=None,
    )
    logger.info("tooling.case.saved case_id=%s evidence=%s", instance.pk, len(evidence))
    return {
        "ok": True,
        "action": "created",
        "case_id": instance.pk,
        "case_name": instance.name,
        "evidence_count": len(evidence),
        "saved_at": dj_timezone.now().isoformat(),
    }


# --------------------------------------------------------------- window compare
_SIGNATURE_RE = re.compile(r"\[(FATAL|ERROR|WARN|INFO|DEBUG)\]", re.IGNORECASE)


def _evidence_for_window(environment, payload: dict[str, Any], start: str, end: str) -> dict[str, Any]:
    """Run one directed log read and return the compacted result the tool layer produces."""
    from apps.tooling.services import query_environment_logs

    request = {
        "environment_id": environment.id,
        "start_time": start,
        "end_time": end,
        "component_name": payload.get("component_name") or "",
        "keyword": payload.get("keyword") or "",
        "errors_only": bool(payload.get("errors_only", True)),
        "source_categories": payload.get("source_categories") or [],
    }
    return query_environment_logs(request) or {}


def _normalize_signature(line: str) -> str:
    """Collapse volatile numbers so 'retry 3' and 'retry 7' are one signature."""
    text = str(line or "").strip()
    # Strip the leading timestamp first. Collapsing digits first would rewrite the date as
    # `#-#-#` and the timestamp pattern below would no longer match.
    text = re.sub(r"^\d{4}-\d{2}-\d{2}[ T][\d:.]+(?:[+-]\d{2}:?\d{2})?\s*", "", text)
    text = re.sub(r"\d+", "#", text)
    return text[:220]


def _window_profile(result: dict[str, Any]) -> dict[str, Any]:
    """Summarise one window using the fields the log tool actually returns.

    `query_environment_logs` deliberately folds raw lines into per-function groups, so a
    line-by-line diff is not available; per-function counts plus the sampled messages are.
    """
    summaries = result.get("function_summaries") or []
    functions: dict[str, dict[str, int]] = {}
    signatures: dict[str, int] = {}
    for item in summaries:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "未归属函数")[:160]
        entry = functions.setdefault(name, {"line_count": 0, "error_count": 0, "warning_count": 0})
        for key in entry:
            entry[key] += int(item.get(key) or 0)
        for sample in item.get("samples") or []:
            key = _normalize_signature(sample)
            if key:
                signatures[key] = signatures.get(key, 0) + 1
    return {
        "line_count": int(result.get("line_count") or 0),
        "scanned_line_count": int(result.get("scanned_line_count") or 0),
        "artifact_count": int(result.get("artifact_count") or 0),
        "error_count": sum(item["error_count"] for item in functions.values()),
        "warning_count": sum(item["warning_count"] for item in functions.values()),
        "functions": functions,
        "signatures": signatures,
        "truncated": bool(result.get("truncated") or result.get("scan_truncated")),
    }


@register_function_tool(ToolDefinition(
    id="compare_log_windows",
    name="对比两个时间窗的日志差异",
    description=(
        "把两个时间窗各自读取一次并做确定性差异对比，返回“新增/消失/激增”的日志签名。"
        "适合回答“这个小时和昨天同一小时比，多了什么”“是不是新问题”。"
    ),
    category="日志分析",
    handler=None,
    agent_exposed=True,
    read_only=True,
    transport="function",
    domain="log",
    kind="query",
    skills=("logs",),
    input_schema={
        "type": "object",
        "properties": {
            "environment_id": _ENV_ID,
            "baseline_start": {"type": "string", "description": "基线窗口开始，如 2026-09-22T10:00:00"},
            "baseline_end": {"type": "string", "description": "基线窗口结束"},
            "current_start": {"type": "string", "description": "当前窗口开始"},
            "current_end": {"type": "string", "description": "当前窗口结束"},
            "component_name": {"type": "string", "default": ""},
            "keyword": {"type": "string", "default": ""},
            "errors_only": {"type": "boolean", "default": True},
            "top": {"type": "integer", "default": 15, "minimum": 1, "maximum": 60},
        },
        "required": ["environment_id", "baseline_start", "baseline_end", "current_start", "current_end"],
    },
    use_when=(
        "用户问“和之前比有什么不同”“这是不是新出现的问题”“刚才还好好的，现在多了什么”。"
        "两个窗口建议长度相近，且处于同一业务周期（同一时段跨天对比）。"
    ),
    do_not_use_when="只关心某一个窗口内的具体日志内容，不需要对比。",
    implementation="apps.tooling.plugins.analysis.compare_log_windows",
))
def compare_log_windows(payload: dict) -> dict[str, Any]:
    environment = _environment(payload.get("environment_id"))
    required = ("baseline_start", "baseline_end", "current_start", "current_end")
    missing = [key for key in required if not str(payload.get(key) or "").strip()]
    if missing:
        raise ValueError("缺少时间窗参数：" + "、".join(missing))

    baseline = _window_profile(_evidence_for_window(environment, payload, str(payload["baseline_start"]), str(payload["baseline_end"])))
    current = _window_profile(_evidence_for_window(environment, payload, str(payload["current_start"]), str(payload["current_end"])))

    top = max(1, min(60, int(payload.get("top") or 15)))
    b_sig, c_sig = baseline["signatures"], current["signatures"]
    b_fun, c_fun = baseline["functions"], current["functions"]

    appeared = sorted(
        [{"signature": k, "current_count": v} for k, v in c_sig.items() if k not in b_sig],
        key=lambda item: -item["current_count"],
    )
    disappeared = sorted(
        [{"signature": k, "baseline_count": v} for k, v in b_sig.items() if k not in c_sig],
        key=lambda item: -item["baseline_count"],
    )
    new_functions = sorted([k for k in c_fun if k not in b_fun])
    absent_functions = sorted([k for k in b_fun if k not in c_fun])
    function_deltas = sorted(
        [
            {
                "function": k,
                "baseline_errors": b_fun[k]["error_count"],
                "current_errors": v["error_count"],
                "error_delta": v["error_count"] - b_fun[k]["error_count"],
                "baseline_lines": b_fun[k]["line_count"],
                "current_lines": v["line_count"],
            }
            for k, v in c_fun.items()
            if k in b_fun and v["error_count"] != b_fun[k]["error_count"]
        ],
        key=lambda item: -abs(item["error_delta"]),
    )

    error_delta = current["error_count"] - baseline["error_count"]
    if new_functions:
        verdict = f"当前窗口出现 {len(new_functions)} 个基线中不存在的函数组，很可能是新问题。"
    elif appeared and error_delta > 0:
        verdict = f"当前窗口出现 {len(appeared)} 条新日志签名，且 ERROR 增加 {error_delta} 条，倾向新问题。"
    elif error_delta > 0:
        verdict = f"没有新签名，但 ERROR 从 {baseline['error_count']} 增到 {current['error_count']}，是既有问题的加重。"
    elif error_delta < 0:
        verdict = f"ERROR 从 {baseline['error_count']} 降到 {current['error_count']}，当前窗口比基线更干净。"
    else:
        verdict = "两个窗口的错误量级基本一致，更像既有问题的重复出现。"

    return {
        "ok": True,
        "environment_id": environment.id,
        "baseline": {
            "window": [payload["baseline_start"], payload["baseline_end"]],
            "line_count": baseline["line_count"],
            "error_count": baseline["error_count"],
            "warning_count": baseline["warning_count"],
            "artifact_count": baseline["artifact_count"],
            "function_count": len(b_fun),
            "truncated": baseline["truncated"],
        },
        "current": {
            "window": [payload["current_start"], payload["current_end"]],
            "line_count": current["line_count"],
            "error_count": current["error_count"],
            "warning_count": current["warning_count"],
            "artifact_count": current["artifact_count"],
            "function_count": len(c_fun),
            "truncated": current["truncated"],
        },
        "error_delta": error_delta,
        "new_functions": new_functions[:top],
        "absent_functions": absent_functions[:top],
        "function_deltas": function_deltas[:top],
        "appeared_signatures": appeared[:top],
        "disappeared_signatures": disappeared[:top],
        "verdict": verdict,
    }


# ------------------------------------------------------- correlate with changes
@register_function_tool(ToolDefinition(
    id="correlate_failure_with_changes",
    name="关联故障时间窗与近期变更",
    description=(
        "把一次故障的时间窗与近期部署、版本、日志规则变更放在一起，返回“什么变了”的时间线。"
        "故障排查的第一个问题通常是“最近改了什么”。"
    ),
    category="环境操作",
    handler=None,
    agent_exposed=True,
    read_only=True,
    transport="function",
    domain="deployment",
    kind="query",
    skills=("logs", "deployment", "environment"),
    input_schema={
        "type": "object",
        "properties": {
            "environment_id": _ENV_ID,
            "failure_start": {"type": "string", "description": "故障时间窗开始"},
            "failure_end": {"type": "string", "description": "故障时间窗结束"},
            "lookback_hours": {"type": "integer", "default": 72, "minimum": 1, "maximum": 720},
        },
        "required": ["environment_id", "failure_start", "failure_end"],
    },
    use_when="用户问“为什么突然出问题”“是不是刚部署过”“最近有什么改动”时。",
    do_not_use_when="用户只想看故障日志本身，不关心变更来源。",
    implementation="apps.tooling.plugins.analysis.correlate_failure_with_changes",
))
def correlate_failure_with_changes(payload: dict) -> dict[str, Any]:
    from datetime import timedelta

    from django.utils.dateparse import parse_datetime

    from apps.tooling.services import list_environment_deployments, query_environment_version

    environment = _environment(payload.get("environment_id"))
    start = parse_datetime(str(payload.get("failure_start") or ""))
    end = parse_datetime(str(payload.get("failure_end") or ""))
    if start is None or end is None:
        raise ValueError("failure_start / failure_end 需要是 ISO 时间，例如 2026-09-23T10:00:00。")
    lookback = max(1, min(720, int(payload.get("lookback_hours") or 72)))
    since = start - timedelta(hours=lookback)

    timeline: list[dict[str, Any]] = []
    warnings: list[str] = []

    try:
        deployments = list_environment_deployments({"environment_id": environment.id}) or {}
        for item in (deployments.get("deployments") or deployments.get("results") or [])[:40]:
            created = str(item.get("created_at") or item.get("scheduled_at") or "")
            moment = parse_datetime(created)
            if moment is None or moment < since:
                continue
            timeline.append({
                "at": created,
                "kind": "deployment",
                "title": f"部署 {item.get('status') or ''}".strip(),
                "detail": f"版本={item.get('target_version') or '-'} 模式={item.get('simulation_mode') or '-'}",
                "inside_failure_window": start <= moment <= end,
            })
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"读取部署历史失败：{exc}")

    try:
        versions = query_environment_version({"environment_id": environment.id}) or {}
        timeline.append({
            "at": versions.get("checked_at") or "",
            "kind": "version",
            "title": "当前版本快照",
            "detail": str(versions.get("consistency") or versions.get("summary") or versions)[:300],
            "inside_failure_window": False,
        })
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"读取版本失败：{exc}")

    timeline.sort(key=lambda item: str(item.get("at") or ""))
    inside = [item for item in timeline if item.get("inside_failure_window")]
    before = [item for item in timeline if not item.get("inside_failure_window")]

    if inside:
        verdict = f"故障窗口内有 {len(inside)} 项变更，强烈怀疑是变更引入的。"
    elif before:
        verdict = f"故障窗口内没有变更，但窗口前 {lookback} 小时内有 {len(before)} 项，仍可能是延迟暴露。"
    else:
        verdict = f"故障窗口前后 {lookback} 小时内都没有记录到变更，优先怀疑环境/硬件或外部输入。"

    return {
        "ok": True,
        "environment_id": environment.id,
        "failure_window": [payload["failure_start"], payload["failure_end"]],
        "lookback_hours": lookback,
        "changes_inside_window": inside[:20],
        "changes_before_window": before[-20:],
        "warnings": warnings[:5],
        "verdict": verdict,
    }


@register_function_tool(ToolDefinition(
    id="draft_diagnosis_case",
    name="把诊断结论整理成案例草稿",
    description=(
        "把已经成立的诊断结论整理成可导入案例库的结构化草稿：一次调用同时产出面向人的 "
        "Markdown 结论、面向机器的案例字段，以及保存所需的证据列表。只读，不写库。"
        "调用后界面会给出一键导入入口，用户核对后才会真正入库。"
    ),
    category="自动化用例",
    handler=None,
    agent_exposed=True,
    read_only=True,
    transport="function",
    # Domain `log`, not `case`: the router narrows each turn to three domains, and a
    # log-analysis turn never includes `case`, so the drafting tool was never offered.
    # Turning an analysis into a case is the tail of the log-analysis job.
    domain="log",
    kind="query",
    skills=("atlog", "logs"),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "案例名称（简短可检索）"},
            "symptom": {"type": "string", "description": "故障现象"},
            "root_cause": {"type": "string", "description": "已确认根因；不确定时留空并在 open_questions 说明"},
            "solution": {"type": "string", "default": "", "description": "处理建议"},
            "category": {"type": "string", "default": ""},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"], "default": "medium"},
            "tags": {"type": "array", "items": {"type": "string"}, "default": []},
            "components": {"type": "array", "items": {"type": "string"}, "default": [], "description": "涉及的子系统/模块"},
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
                "description": "支撑结论的真实日志原文，必须来自实际读取到的日志",
            },
            "open_questions": {"type": "array", "items": {"type": "string"}, "default": []},
        },
        "required": ["name", "symptom"],
    },
    use_when=(
        "一次日志分析已经得出结论，且用户可能需要把它沉淀成案例时。"
        "结论必须有真实日志证据支撑；不确定的部分写进 open_questions，不要编造根因。"
    ),
    do_not_use_when="还在取证阶段、结论尚未成立时；先继续查日志。",
    validation_rules=(
        "root_cause 为空时必须用 open_questions 说明还缺什么证据，不要用猜测填充",
        "evidence 必须是真实读取到的日志原文",
    ),
    implementation="apps.tooling.plugins.analysis.draft_diagnosis_case",
))
def draft_diagnosis_case(payload: dict) -> dict[str, Any]:
    """Produce a human-readable Markdown conclusion plus a machine-usable case draft.

    One payload, two renderings. The Markdown is what the user reads in the chat; the
    `case_draft` is what the import button fills the case editor with. Keeping them in one
    call is what stops the conclusion from being retyped by hand — which is how a good
    analysis ends up never making it into the knowledge base.
    """
    name = str(payload.get("name") or "").strip()[:255]
    symptom = str(payload.get("symptom") or "").strip()
    root_cause = str(payload.get("root_cause") or "").strip()
    solution = str(payload.get("solution") or "").strip()
    category = str(payload.get("category") or "").strip()[:128]
    confidence = str(payload.get("confidence") or "medium").strip().lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "medium"
    tags = [str(item).strip()[:64] for item in (payload.get("tags") or []) if str(item).strip()][:12]
    components = [str(item).strip()[:128] for item in (payload.get("components") or []) if str(item).strip()][:12]
    evidence = [str(item).strip() for item in (payload.get("evidence") or []) if str(item).strip()][:40]
    open_questions = [str(item).strip() for item in (payload.get("open_questions") or []) if str(item).strip()][:10]

    if not name or not symptom:
        raise ValueError("name / symptom 不能为空。")

    # Say out loud what is missing rather than silently importing a half-filled case.
    # Two renderings on purpose: `missing` stays machine-checkable (callers gate on it),
    # `missing_labels` is what a human reads — deriving both here keeps them in step and
    # stops every consumer from inventing its own Chinese translation of a field name.
    missing: list[str] = []
    if not root_cause:
        missing.append("root_cause")
    if not evidence:
        missing.append("evidence")
    if open_questions:
        missing.append("open_questions")
    missing_labels = {
        "root_cause": "根因尚未确认",
        "evidence": "缺少真实日志证据",
        "open_questions": "仍有待确认的问题",
    }

    confidence_label = {"high": "高", "medium": "中", "low": "低"}.get(confidence, "中")

    lines = [f"## {name}", ""]
    lines.append(f"**置信度**：{confidence_label}" + (f" · **分类**：{category}" if category else ""))
    if components:
        lines.append(f"**涉及模块**：{'、'.join(components)}")
    lines.append("")
    lines.append("### 故障现象")
    lines.append(symptom)
    lines.append("")
    lines.append("### 根因")
    lines.append(root_cause or "（尚未确定，见下方待确认项）")
    if solution:
        lines.append("")
        lines.append("### 处理建议")
        lines.append(solution)
    if evidence:
        lines.append("")
        lines.append(f"### 关键证据（{len(evidence)} 条）")
        lines.extend(f"- `{item[:400]}`" for item in evidence[:12])
    if open_questions:
        lines.append("")
        lines.append("### 待确认")
        lines.extend(f"- {item}" for item in open_questions)
    if tags:
        lines.append("")
        lines.append("标签：" + "、".join(f"`{item}`" for item in tags))
    markdown = "\n".join(lines)

    return {
        "ok": True,
        "markdown": markdown,
        "case_draft": {
            "name": name,
            "category": category,
            "symptom": symptom,
            "root_cause": root_cause,
            "solution": solution,
            "description": markdown,
            "tags": tags,
        },
        "evidences": [
            {"raw": item[:4000], "component": "", "timestamp": "", "source": "tracepilot"}
            for item in evidence
        ],
        "confidence": confidence,
        "components": components,
        "missing": missing,
        "missing_labels": [missing_labels[item] for item in missing if item in missing_labels],
        "open_questions": open_questions,
        "importable": bool(root_cause),
    }
