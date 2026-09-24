from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from apps.atlog.anomaly_rules import compile_anomaly_rules, matching_compiled_anomaly_rules, normalize_anomaly_rules
from apps.atlog.services import _case_log_hierarchy, analyze_case, discover_case_log_files, query_case_logs
from apps.knowledge.models import AbnormalCase
from apps.knowledge.serializers import AbnormalCaseSerializer
from apps.logsources.models import LogFmDefinition

_TOKEN_RE = re.compile(r"[0-9A-Za-z_:.+\-/\u4e00-\u9fff]{2,}")
_ERROR_CODE_RE = re.compile(
    r"\b(error\s*code|error[_-]?code|err\s*code|err[_-]?code|errno|error\s*no|err\s*no)\b\s*(?:=|:|：)?\s*[\"']?(0x[0-9a-fA-F]+)\b",
    re.I,
)
_SIGNAL_WORDS = ("fatal", "critical", "exception", "assertionerror", "assert", "timeout", "failed", "failure", "error", "alarm")
_STOP = {"error", "failed", "failure", "the", "and", "with", "from", "this", "that", "true", "false"}


def _norm_entity(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _tokens(text: str) -> set[str]:
    result: set[str] = set()
    for raw in _TOKEN_RE.findall(str(text or "").lower()):
        token = raw.strip("._:/-+")
        if len(token) < 2 or token in _STOP or token.isdigit():
            continue
        result.add(token)
        if len(result) >= 240:
            break
    return result


def _current_signature(
    analysis: dict[str, Any],
    rows: list[dict[str, Any]],
    anomaly_rules: list[dict[str, Any]] | None = None,
) -> tuple[set[str], set[str]]:
    parts = [
        str(analysis.get("reason_category") or ""),
        str(analysis.get("reason_detail") or ""),
        str(analysis.get("assertion_summary") or ""),
        str(analysis.get("assertion") or ""),
    ]
    modules: set[str] = set()
    compiled_rules = compile_anomaly_rules(anomaly_rules or [])
    for row in rows:
        level = str(row.get("level") or "").lower()
        text = str(row.get("message") or row.get("raw") or "")
        rule_hit = bool(compiled_rules and matching_compiled_anomaly_rules(text, compiled_rules))
        if rule_hit or (not compiled_rules and (level in {"error", "fatal", "critical"} or any(word in text.lower() for word in _SIGNAL_WORDS))):
            parts.append(text)
            module = str(row.get("component") or "").strip().lower()
            if module:
                modules.add(module)
        if len(parts) >= 80:
            break
    return _tokens(" ".join(parts)), modules


def _case_signature(case: AbnormalCase) -> tuple[set[str], set[str]]:
    parts = [case.name, case.category, case.symptom, case.root_cause, case.solution, case.description, " ".join(case.tags or [])]
    modules: set[str] = set()
    evidences: list[Any] = list(case.evidences or [])
    for group in case.feature_groups or []:
        if not isinstance(group, dict) or group.get("enabled", True) is False:
            continue
        evidences.extend(group.get("evidences") or [])
    for evidence in evidences[:600]:
        if not isinstance(evidence, dict):
            continue
        parts.extend([
            str(evidence.get("template") or ""),
            str(evidence.get("message") or ""),
            " ".join(str(item) for item in (evidence.get("tokens") or [])),
        ])
        module = str(evidence.get("module") or evidence.get("component") or "").strip().lower()
        if module:
            modules.add(module)
    return _tokens(" ".join(parts)), modules


def _evidence_preview(case: AbnormalCase, limit: int = 5) -> list[dict[str, Any]]:
    """Return a small, UI-safe sample of the *real* evidence stored in a case.

    Historical ATLog matches should be able to show the previously accepted AI
    conclusion together with the logs that justified it, without another model
    call.  Never synthesize evidence here: only project fields from the stored
    case evidence/feature groups.
    """
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(item: Any) -> None:
        if not isinstance(item, dict) or len(rows) >= max(1, min(int(limit or 5), 8)):
            return
        message = str(item.get("message") or item.get("raw") or "").strip()
        if not message:
            return
        timestamp = str(item.get("timestamp") or item.get("time") or "")[:80]
        module = str(item.get("module") or item.get("component") or "")[:120]
        source = str(item.get("source_file") or item.get("source") or "")[:240]
        key = (module.casefold(), source.casefold(), message[:400].casefold())
        if key in seen:
            return
        seen.add(key)
        rows.append({
            "time": timestamp,
            "component": module,
            "source": source,
            "message": message[:1800],
        })

    for evidence in case.evidences or []:
        add(evidence)
        if len(rows) >= limit:
            return rows
    for group in case.feature_groups or []:
        if not isinstance(group, dict) or group.get("enabled", True) is False:
            continue
        for evidence in group.get("evidences") or []:
            add(evidence)
            if len(rows) >= limit:
                return rows
    return rows


def score_cases_against(
    current_tokens: set[str],
    current_modules: set[str],
    *,
    category: str = "",
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Rank stored cases against a token/module signature.

    Single implementation on purpose: the ATLog pre-check and a plain log-context search must
    agree on what "80% match" means, or the same incident scores differently depending on
    which entry point you used.
    """
    results: list[dict[str, Any]] = []
    for case in AbnormalCase.objects.filter(enabled=True).order_by("-matched_count", "-updated_at")[:500]:
        case_tokens, case_modules = _case_signature(case)
        if not current_tokens or not case_tokens:
            continue
        overlap = current_tokens & case_tokens
        if len(overlap) < 2:
            continue
        coverage = len(overlap) / max(1, min(len(current_tokens), 40))
        precision = len(overlap) / max(1, min(len(case_tokens), 80))
        module_overlap = bool(current_modules & case_modules)
        category_bonus = 0.0
        wanted_category = str(category or "").strip().lower()
        if wanted_category and wanted_category in f"{case.category} {case.symptom} {case.root_cause}".lower():
            category_bonus = 10.0
        score = min(100.0, coverage * 65 + precision * 15 + (15.0 if module_overlap else 0.0) + category_bonus)
        if score < 28:
            continue
        results.append({
            "case_id": case.id,
            "name": case.name,
            "category": case.category,
            "symptom": case.symptom,
            "root_cause": case.root_cause,
            "solution": case.solution,
            "score": round(score, 1),
            "matched_tokens": sorted(overlap)[:16],
            "matched_modules": sorted(current_modules & case_modules)[:12],
            "evidence_count": case.evidence_count,
            "evidence_preview": _evidence_preview(case),
            "matched_count": case.matched_count,
        })
    results.sort(key=lambda item: (item["score"], item["matched_count"]), reverse=True)
    return results[: max(1, min(int(limit or 5), 10))]


def match_cases_from_log_context(
    text: str,
    *,
    component: str = "",
    subsystem: str = "",
    category: str = "",
    limit: int = 5,
) -> dict[str, Any]:
    """Search historical cases from *any* log evidence, not only an ATLog case URL.

    This is what lets the analysis flow ask "have I seen this before?" straight after the user
    points at a log window, instead of first requiring a test-case URL.
    """
    tokens = _tokens(text)
    modules = {
        str(value).strip().lower()
        for value in (component, subsystem)
        if str(value).strip()
    }
    matches = score_cases_against(tokens, modules, category=category, limit=limit)
    top_score = float(matches[0]["score"]) if matches else 0.0
    return {
        "matches": matches,
        "strong_match": top_score >= 80.0,
        "top_score": top_score,
        "token_count": len(tokens),
        "recommendation": (
            "优先复核历史案例，现场证据一致时可直接复用结论。"
            if top_score >= 80
            else "没有高相似历史案例，建议继续 AI 深度诊断。"
            if top_score < 45
            else "存在中等相似案例，建议先比对证据差异再决定是否复用。"
        ),
    }


def match_case_knowledge(url: str, *, limit: int = 5, anomaly_rules: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Deterministic knowledge pre-check performed before the user spends an AI call."""
    analysis = analyze_case(url)
    start_time = str(analysis.get("event_start_time") or analysis.get("start_time") or "")
    end_time = str(analysis.get("event_end_time") or analysis.get("end_time") or "")
    active_rules = normalize_anomaly_rules(anomaly_rules or [])
    log_result = query_case_logs(
        str(analysis.get("base_url") or url),
        start_time=start_time,
        end_time=end_time,
        max_lines=4000,
    )
    signature_rows = list(log_result.get("rows") or [])

    # If the human-confirmed root component table can deterministically map an
    # event component to a concrete debug target, also collect that target's
    # anomaly-rule hits for knowledge matching.  This keeps the pre-check free
    # of LLM calls while letting "same key log -> previous AI conclusion" work
    # on the strongest real evidence rather than only on event.log vocabulary.
    if active_rules:
        event_components = {
            _norm_entity(row.get("component")): str(row.get("component") or "").strip()
            for row in signature_rows
            if str(row.get("source_kind") or "") == "event" and str(row.get("component") or "").strip()
        }
        if event_components:
            discovered = discover_case_log_files(str(analysis.get("base_url") or url))
            available = {
                (_norm_entity(item.get("subsystem")), _norm_entity(item.get("module"))): {
                    "subsystem": str(item.get("subsystem") or ""),
                    "module": str(item.get("module") or ""),
                }
                for item in discovered
                if str(item.get("subsystem") or "").strip() and str(item.get("module") or "").strip()
            }
            targets: list[dict[str, str]] = []
            seen_targets: set[tuple[str, str]] = set()
            for source_module in (
                LogFmDefinition.objects.filter(enabled=True, event_component=True)
                .select_related("subsystem")
                .prefetch_related("target_modules__subsystem")
            ):
                if _norm_entity(source_module.name) not in event_components:
                    continue
                target_modules = list(source_module.target_modules.filter(enabled=True, subsystem__enabled=True))
                target_modules.sort(key=lambda item: (int(item.query_priority or 100), item.subsystem.name.casefold(), item.name.casefold()))
                for target_module in target_modules:
                    key = (_norm_entity(target_module.subsystem.name), _norm_entity(target_module.name))
                    actual = available.get(key)
                    if actual and key not in seen_targets:
                        seen_targets.add(key)
                        targets.append(actual)
                    if len(targets) >= 8:
                        break
                if len(targets) >= 8:
                    break
            if targets:
                debug_result = query_case_logs(
                    str(analysis.get("base_url") or url),
                    start_time=start_time,
                    end_time=end_time,
                    targets=targets,
                    anomaly_rules=active_rules,
                    include_event=False,
                    max_lines=800,
                )
                signature_rows.extend(list(debug_result.get("rows") or []))
    current_tokens, current_modules = _current_signature(
        analysis,
        signature_rows,
        active_rules,
    )
    matches = score_cases_against(
        current_tokens,
        current_modules,
        category=str(analysis.get("reason_category") or ""),
        limit=limit,
    )
    top_score = float(matches[0]["score"]) if matches else 0.0
    return {
        "case_id": analysis.get("case_id") or "",
        "case_name": analysis.get("case_name") or analysis.get("case_id") or "",
        "matches": matches,
        "strong_match": top_score >= 80.0,
        "top_score": top_score,
        "recommendation": "优先复核历史案例，若现场证据一致可直接复用结论。" if top_score >= 80 else "没有高相似历史案例，建议继续 AI 深度诊断。",
    }


def _normalize_template(text: str) -> str:
    value = str(text or "").strip()
    value = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "<IP>", value)
    value = re.sub(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}\b", "<UUID>", value)
    value = re.sub(r"\b0x[0-9a-fA-F]{4,}\b", "<HEX>", value)
    value = re.sub(r"(?<![\w.])[-+]?(?:\d+\.\d+|\d+)(?![\w.])", "<N>", value)
    return re.sub(r"\s+", " ", value).strip()


def _anomaly_rules(row: dict[str, Any]) -> list[dict[str, Any]]:
    # AI evidence rows carry the exact user-managed anomaly rules that admitted
    # the log line. Preserve those rules when the diagnosis is accepted into the
    # knowledge base; do not silently replace them with built-in ERROR words.
    configured = row.get("anomaly_rules")
    if isinstance(configured, list):
        result: list[dict[str, Any]] = []
        for index, item in enumerate(configured):
            if not isinstance(item, dict):
                continue
            keyword = str(item.get("keyword") or "").strip()
            if not keyword:
                continue
            result.append({
                "id": str(item.get("id") or f"atlog-rule-{index}"),
                "keyword": keyword,
                "case_sensitive": bool(item.get("case_sensitive", item.get("caseSensitive", False))),
                "whole_word": bool(item.get("whole_word", item.get("wholeWord", False))),
            })
        if result:
            return result[:12]

    # Compatibility fallback for older non-AI evidence created before rule
    # metadata was attached to rows. New AI diagnosis paths should not use this.
    level = str(row.get("level") or "").strip().upper()
    text = f"{level} {row.get('message') or ''} {row.get('raw') or ''}".lower()
    keywords: list[str] = []
    if level in {"ERROR", "FATAL", "CRITICAL", "ALARM"}:
        keywords.append(level)
    for word in _SIGNAL_WORDS:
        if word in text and word.upper() not in keywords:
            keywords.append(word)
        if len(keywords) >= 4:
            break
    return [
        {"id": f"atlog-ai-{keyword.lower()}", "keyword": keyword, "case_sensitive": False, "whole_word": False}
        for keyword in keywords
    ]


def _knowledge_evidence(row: dict[str, Any]) -> dict[str, Any] | None:
    rules = _anomaly_rules(row)
    source_kind = str(row.get("source_kind") or row.get("source_category") or "").strip().lower()
    report_evidence = source_kind in {"case_report", "pytest", "xytest", "case-metadata"}
    if not rules and not report_evidence:
        return None
    raw = str(row.get("raw") or row.get("message") or "").strip()
    message = str(row.get("message") or raw).strip()
    if not raw:
        return None
    source_path = str(row.get("source_path") or row.get("source") or "")
    subsystem, module = _case_log_hierarchy(source_path, str(row.get("component") or ""))
    template = _normalize_template(message)
    codes = []
    for match in _ERROR_CODE_RE.finditer(f"{message} {raw}"):
        codes.append({"key": re.sub(r"[\s_-]+", "", match.group(1).lower()), "value": match.group(2).lower(), "raw": match.group(0)})
    return {
        "source_entry_id": f"atlog-ai-{uuid4().hex}",
        "timestamp": str(row.get("time") or ""),
        "raw": raw,
        "message": message,
        "level": str(row.get("level") or ""),
        "severity": "error" if str(row.get("level") or "").upper() in {"ERROR", "FATAL", "CRITICAL"} else "warning",
        "subsystem": subsystem,
        "module": module or str(row.get("component") or ""),
        "component": str(row.get("component") or module or ""),
        "function_name": "",
        "source_file": source_path,
        "source_line": row.get("line_number"),
        "source_category": "case_report" if report_evidence else "debug" if "full_logs/log/debug/" in source_path else "run",
        "process_id": "",
        "thread_id": "",
        "trace_id": "",
        "anomaly_rules": rules,
        "template": template,
        "tokens": sorted(_tokens(template))[:120],
        "error_codes": codes[:12],
    }


def build_ai_case_evidences(rows: list[dict[str, Any]], *, require_runtime: bool = True) -> list[dict[str, Any]]:
    evidences = [item for row in rows if (item := _knowledge_evidence(row)) is not None]
    if require_runtime:
        runtime = [item for item in evidences if item.get("anomaly_rules") and item.get("source_category") != "case_report"]
        if not runtime:
            raise ValueError("AI 报告缺少可回链的运行日志异常证据，不能保存为案例。")
    # Prefer one coherent evidence chain: test/report evidence first, then runtime evidence.
    report = [item for item in evidences if item.get("source_category") == "case_report"]
    runtime = [item for item in evidences if item.get("source_category") != "case_report"]
    return [*report[:12], *runtime[:88]][:100]


def accept_ai_diagnosis(result: dict[str, Any], *, target_case_id: int | None = None) -> dict[str, Any]:
    report = result.get("report") if isinstance(result.get("report"), dict) else {}
    analysis = result.get("_deterministic_analysis") if isinstance(result.get("_deterministic_analysis"), dict) else {}
    verified_rows = result.get("_verified_evidence_rows") if isinstance(result.get("_verified_evidence_rows"), list) else []
    evidences = build_ai_case_evidences(verified_rows, require_runtime=True)

    now = datetime.now(timezone.utc).isoformat()
    case_name = str(result.get("case_name") or result.get("case_id") or "自动化用例异常").strip()

    # When a high-confidence historical case has already been identified, append this
    # accepted incident as another *real-evidence* feature group instead of creating a
    # duplicate case.  Established root-cause text is preserved unless it was blank.
    if target_case_id is not None:
        try:
            target_id = int(target_case_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("目标案例 ID 无效。") from exc
        instance = AbnormalCase.objects.filter(pk=target_id, enabled=True).first()
        if instance is None:
            raise ValueError("目标历史案例不存在或已停用。")
        groups = [dict(item) for item in (instance.feature_groups or []) if isinstance(item, dict)]
        if not groups and instance.evidences:
            groups = [{
                "id": uuid4().hex,
                "title": "历史现场",
                "enabled": True,
                "created_at": now,
                "source_operation_id": instance.source_operation_id or "",
                "source_task_name": instance.source_task_name or "",
                "environment_name": instance.environment_name or "",
                "note": "由旧版扁平举证迁移。",
                "evidences": list(instance.evidences or []),
            }]
        if len(groups) >= 30:
            raise ValueError("该案例已达到 30 组现场特征上限，请先在案例库治理旧现场。")
        environment_name = str((analysis.get("environment") or {}).get("environment_name") or "")[:128] if isinstance(analysis.get("environment"), dict) else ""
        groups.append({
            "id": uuid4().hex,
            "title": f"AI采纳现场 · {case_name}"[:128],
            "enabled": True,
            "created_at": now,
            "source_operation_id": "",
            "source_task_name": case_name[:255],
            "environment_name": environment_name,
            "note": (
                "AI 辅助诊断后由用户主动采纳；仅保存可回链真实日志。\n"
                f"诊断结论：{str(report.get('root_cause') or '')}\n"
                f"摘要：{str(report.get('summary') or '')}"
            )[:1800],
            "ai_conclusion": str(report.get("root_cause") or "")[:4000],
            "evidences": evidences[:100],
        })
        snapshot = dict(instance.query_snapshot or {})
        history = [item for item in (snapshot.get("ai_accept_history") or []) if isinstance(item, dict)][-19:]
        history.append({
            "accepted_at": now,
            "base_url": result.get("base_url") or "",
            "case_id": result.get("case_id") or "",
            "diagnosis": {key: report.get(key) for key in ("schema_version", "root_cause", "subsystem", "module", "evidence", "timeline", "confidence", "suggestion")},
            "confidence": report.get("confidence") or 0,
            "root_cause": str(report.get("root_cause") or "")[:4000],
            "token_usage": result.get("token_usage") or {},
        })
        snapshot["ai_accept_history"] = history
        tags = list(instance.tags or [])
        for tag in ("ATLog", "AI补充现场"):
            if tag not in tags:
                tags.append(tag)
        patch = {
            "feature_groups": groups,
            "query_snapshot": snapshot,
            "tags": tags[:30],
        }
        if not str(instance.root_cause or "").strip() and report.get("root_cause"):
            patch["root_cause"] = str(report.get("root_cause") or "")[:8000]
        serializer = AbnormalCaseSerializer(instance, data=patch, partial=True)
        serializer.is_valid(raise_exception=True)
        instance = serializer.save()
        return AbnormalCaseSerializer(instance).data

    payload = {
        "name": f"{case_name} · {str(report.get('root_cause_category') or 'AI诊断')[:80]}",
        "category": str(report.get("root_cause_category") or analysis.get("reason_category") or "")[:128],
        "symptom": str(analysis.get("assertion_summary") or analysis.get("conclusion") or report.get("summary") or "")[:4000],
        "root_cause": str(report.get("root_cause") or "")[:8000],
        "solution": "",
        "description": "AI 辅助诊断结论已由用户主动采纳；案例证据仅保存可回链的真实日志。\n" + str(report.get("summary") or "")[:4000],
        "tags": ["ATLog", "AI采纳", str(result.get("case_id") or "")][:30],
        "enabled": True,
        "source_operation_id": "",
        "source_task_name": case_name[:255],
        "environment": (analysis.get("environment") or {}).get("environment_id") if isinstance(analysis.get("environment"), dict) else None,
        "environment_name": str((analysis.get("environment") or {}).get("environment_name") or "")[:128] if isinstance(analysis.get("environment"), dict) else "",
        "query_snapshot": {
            "source": "atlog_ai_diagnosis",
            "accepted_at": now,
            "base_url": result.get("base_url") or "",
            "diagnosis": {key: report.get(key) for key in ("schema_version", "root_cause", "subsystem", "module", "evidence", "timeline", "confidence", "suggestion")},
            "confidence": report.get("confidence") or 0,
            "token_usage": result.get("token_usage") or {},
            "steps": result.get("steps") or [],
        },
        "evidences": evidences[:100],
        "fingerprint_version": 4,
    }
    serializer = AbnormalCaseSerializer(data=payload)
    serializer.is_valid(raise_exception=True)
    instance = serializer.save()
    return AbnormalCaseSerializer(instance).data
