from __future__ import annotations

"""Deterministic log-to-LLM context compaction.

The UI/search layer may keep complete rows for rendering and evidence back-links,
but model-facing code should consume the bounded ``ai_context``/``evidence_rows``
produced here.  This mirrors mature agent runtimes: filter first, then provide a
small controlled window instead of dumping a whole log file into the prompt.
"""

import re
from collections import OrderedDict
from typing import Any, Iterable

_ERROR_LEVELS = {"ERROR", "FATAL", "CRITICAL", "ALARM"}
_WARN_LEVELS = {"WARN", "WARNING"}


def _text(value: Any, limit: int = 900) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit]


def _signature(value: str) -> str:
    value = re.sub(r"\b0x[0-9a-fA-F]+\b", "0x#", value)
    value = re.sub(r"\b[0-9a-fA-F]{16,}\b", "#HEX", value)
    value = re.sub(r"\b\d{6,}\b", "#N", value)
    return value[:420]


def _priority(row: dict[str, Any]) -> int:
    level = str(row.get("level") or "").upper()
    matched_rules = list(row.get("matched_anomaly_rules") or [])
    score = 0
    if matched_rules:
        score += 5000
    if level in _ERROR_LEVELS:
        score += 4000
    elif level in _WARN_LEVELS:
        score += 1600
    if row.get("display_code"):
        score += 900
    if row.get("function_name") or row.get("function"):
        score += 120
    return score


def compact_log_rows_for_ai(
    rows: Iterable[dict[str, Any]] | None,
    *,
    max_chars: int = 12_000,
    max_nodes: int = 40,
    max_groups: int = 24,
) -> dict[str, Any]:
    """Return a bounded, high-density context while preserving evidence links.

    Rows are grouped by source/function (or component fallback).  High-signal
    ERROR/anomaly rows win the budget.  A small temporal representative sample is
    retained so the model still sees execution order without receiving raw logs.
    """
    safe_chars = max(2_000, min(int(max_chars or 12_000), 24_000))
    safe_nodes = max(8, min(int(max_nodes or 40), 80))
    safe_groups = max(4, min(int(max_groups or 24), 40))
    values = [dict(item) for item in (rows or []) if isinstance(item, dict)]

    groups: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    for idx, row in enumerate(values):
        source = _text(row.get("source_path") or row.get("source") or "日志", 300)
        function_name = _text(row.get("function_name") or row.get("function") or "", 180)
        component = _text(row.get("component") or row.get("module") or "", 120)
        owner = function_name or component or "未归属函数"
        key = f"{source}|{owner}"
        bucket = groups.setdefault(key, {
            "source": source,
            "owner": owner,
            "first_index": idx,
            "line_count": 0,
            "error_count": 0,
            "warning_count": 0,
            "anomaly_count": 0,
            "rows": [],
            "seen": set(),
        })
        bucket["line_count"] += 1
        level = str(row.get("level") or "").upper()
        if level in _ERROR_LEVELS:
            bucket["error_count"] += 1
        elif level in _WARN_LEVELS:
            bucket["warning_count"] += 1
        if row.get("matched_anomaly_rules"):
            bucket["anomaly_count"] += 1

        message = _text(row.get("message") or row.get("raw") or "")
        if not message:
            continue
        sig = _signature(message)
        high_signal = _priority(row) > 0
        # Keep every distinct high-signal row up to a tiny per-group cap; keep
        # only one ordinary representative row for context.
        per_group_cap = 6 if high_signal else 1
        if sig in bucket["seen"] or len(bucket["rows"]) >= per_group_cap:
            continue
        bucket["seen"].add(sig)
        normalized = {
            "time": _text(row.get("time"), 80),
            "level": level,
            "component": component,
            "function_name": function_name,
            "source_path": source,
            "line_number": row.get("line_number") or row.get("line"),
            "message": message,
            "matched_anomaly_rules": list(row.get("matched_anomaly_rules") or [])[:8],
            "display_code": _text(row.get("display_code"), 120),
            "source_kind": _text(row.get("source_kind"), 80),
            "_priority": _priority(row),
            "_index": idx,
        }
        bucket["rows"].append(normalized)

    ranked = sorted(
        groups.values(),
        key=lambda g: (
            -(g["anomaly_count"] * 5000 + g["error_count"] * 4000 + g["warning_count"] * 1200),
            g["first_index"],
        ),
    )[:safe_groups]

    evidence_pool: list[dict[str, Any]] = []
    for group in ranked:
        evidence_pool.extend(group["rows"])
    evidence_pool.sort(key=lambda r: (-int(r.get("_priority") or 0), int(r.get("_index") or 0)))
    evidence = evidence_pool[:safe_nodes]
    evidence.sort(key=lambda r: int(r.get("_index") or 0))

    selected_keys = {
        f"{row.get('source_path')}|{row.get('function_name') or row.get('component') or '未归属函数'}"
        for row in evidence
    }
    selected_groups = [g for g in ranked if f"{g['source']}|{g['owner']}" in selected_keys]
    selected_groups.sort(key=lambda g: g["first_index"])

    parts = [
        f"日志压缩摘要：原始匹配 {len(values)} 行；模型证据 {len(evidence)} 条；函数/来源组 {len(selected_groups)} 个。"
    ]
    for group in selected_groups:
        block = (
            f"[组 {group['owner']}] source={group['source']} | 行{group['line_count']} | "
            f"ERROR {group['error_count']} | WARN {group['warning_count']} | 异常规则命中 {group['anomaly_count']}"
        )
        candidates = [row for row in evidence if f"{row.get('source_path')}|{row.get('function_name') or row.get('component') or '未归属函数'}" == f"{group['source']}|{group['owner']}"]
        lines = [block]
        for row in candidates[:6]:
            location = f":{row.get('line_number')}" if row.get("line_number") else ""
            rules = ",".join(str(x) for x in row.get("matched_anomaly_rules") or [])
            meta = " ".join(x for x in [str(row.get("time") or ""), str(row.get("level") or ""), str(row.get("component") or "")] if x)
            lines.append(f"  · {meta} {row.get('source_path')}{location} | {row.get('message')}" + (f" | rule={rules}" if rules else ""))
        candidate = "\n".join(lines)
        if len("\n".join(parts)) + len(candidate) + 1 > safe_chars:
            break
        parts.append(candidate)

    context = "\n".join(parts)[:safe_chars]
    public_evidence = []
    for row in evidence:
        clean = dict(row)
        clean.pop("_priority", None)
        clean.pop("_index", None)
        public_evidence.append(clean)

    omitted = max(0, len(values) - len(public_evidence))
    from apps.tooling.evidence import build_evidence_pack
    return {
        "evidence_pack": build_evidence_pack(values, max_chars=safe_chars),
        "ai_context": context,
        "evidence_rows": public_evidence,
        "ai_context_char_count": len(context),
        "ai_context_char_budget": safe_chars,
        "evidence_node_count": len(public_evidence),
        "source_row_count": len(values),
        "omitted_row_count": omitted,
        "ai_context_truncated": omitted > 0 or len(context) >= safe_chars,
        "next_strategy": (
            "证据不足时继续按明确 subsystem/module、异常规则或更窄证据条件补取；不要把剩余原始日志整体送给模型。"
            if omitted else "当前受控证据窗口已覆盖全部匹配行。"
        ),
    }
