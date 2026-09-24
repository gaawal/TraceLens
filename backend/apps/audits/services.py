from __future__ import annotations

import ipaddress
import re
import time
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.audits.models import AuditResult, LogSearchAudit, LogSearchAuditTarget


def _normalize_ip(value: str) -> str:
    """Normalize a proxy/client address into a plain IPv4/IPv6 string when possible."""
    text = str(value or "").strip().strip('"')
    if not text:
        return ""

    # RFC 7239 may wrap IPv6 in brackets and may append a port.
    if text.startswith("[") and "]" in text:
        end = text.find("]")
        host = text[1:end]
    else:
        host = text
        # IPv4:port is safe to split; raw IPv6 contains multiple colons and must stay intact.
        if host.count(":") == 1:
            maybe_host, maybe_port = host.rsplit(":", 1)
            if maybe_port.isdigit():
                host = maybe_host

    # RFC 7239 permits obfuscated identifiers such as for=_hidden; they are not usable IPs.
    if host.lower() == "unknown" or host.startswith("_"):
        return ""
    try:
        parsed = ipaddress.ip_address(host)
        if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
            return str(parsed.ipv4_mapped)
        return str(parsed)
    except ValueError:
        return host.strip()


def _forwarded_for_ip(value: str) -> str:
    """Return the first usable RFC 7239 Forwarded `for=` value."""
    for hop in str(value or "").split(","):
        match = re.search(r"(?:^|;)\s*for\s*=\s*(\"[^\"]+\"|[^;\s]+)", hop, flags=re.IGNORECASE)
        if not match:
            continue
        ip = _normalize_ip(match.group(1))
        if ip:
            return ip
    return ""


def request_client_ip(request) -> str:
    """
    Resolve the original browser/client IP through the local reverse proxy.

    TraceLens is normally accessed through the Vite/Nginx proxy, so REMOTE_ADDR is often
    127.0.0.1 or a container bridge address. Prefer standard proxy headers and only fall
    back to the direct peer address when no proxy supplied an original client address.
    """
    forwarded = _forwarded_for_ip(request.META.get("HTTP_FORWARDED", ""))
    if forwarded:
        return forwarded

    x_forwarded_for = str(request.META.get("HTTP_X_FORWARDED_FOR", "") or "").strip()
    if x_forwarded_for:
        for item in x_forwarded_for.split(","):
            ip = _normalize_ip(item)
            if ip:
                return ip

    x_real_ip = _normalize_ip(request.META.get("HTTP_X_REAL_IP", ""))
    if x_real_ip:
        return x_real_ip

    return _normalize_ip(request.META.get("REMOTE_ADDR", ""))


def request_operator_username(request) -> str:
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return str(user.get_username() or "")
    return ""


def normalized_targets(payload: dict[str, Any]) -> list[dict[str, str]]:
    targets: list[dict[str, str]] = []
    for item in payload.get("fm_targets") or []:
        subsystem = str(item.get("subsystem") or "").strip()
        module = str(item.get("fm") or item.get("module") or "").strip()
        if not subsystem or not module:
            continue
        targets.append({"subsystem": subsystem, "module": module, "kind": str(item.get("kind") or "normal")})
    return targets


def _is_dhh_relation(relation) -> bool:
    machine = relation.target_machine
    metadata = relation.metadata or {}
    station_name = str(metadata.get("station_name") or machine.station_name or machine.name or "").strip().lower()
    station_type = str(metadata.get("station_type") or machine.station_type or "").strip().upper()
    return station_name == "dhh" or station_type == "DHH"


def _environment_strategy(environment) -> dict[str, Any]:
    dhh = None
    for relation in environment.machine_relations.select_related("target_machine").filter(is_active=True, target_machine__is_active=True):
        if _is_dhh_relation(relation):
            dhh = relation.target_machine
            break
    upper = environment.upper_machine
    if dhh is None:
        return {
            "mode": "direct",
            "label": "按环境日志路径直接查询",
            "upper": {"id": upper.id, "name": upper.name, "host": upper.host},
        }
    return {
        "mode": "upper_then_dhh",
        "label": "DHH 环境：调试日志上位机优先，未命中时回退 DHH",
        "upper": {"id": upper.id, "name": upper.name, "host": upper.host},
        "dhh": {"id": dhh.id, "name": dhh.name, "host": dhh.host},
    }


def _machine_role_map(audit: LogSearchAudit) -> dict[int, str]:
    environment = audit.environment
    if environment is None:
        return {}
    roles: dict[int, str] = {environment.upper_machine_id: "upper"}
    for relation in environment.machine_relations.select_related("target_machine").filter(is_active=True):
        roles[relation.target_machine_id] = "dhh" if _is_dhh_relation(relation) else "lower"
    return roles


def _diagnostic_conclusion(*, result: str, selected_files: int, result_count: int, error_message: str = "") -> tuple[str, str]:
    if result == AuditResult.SUCCESS:
        return "success", f"已从 {selected_files} 个候选文件中命中 {result_count} 条日志。"
    if result == AuditResult.NO_RESULT:
        if selected_files > 0:
            return "file_selected_no_rows", "候选日志文件已找到，但指定时间范围/过滤条件内没有命中日志行；建议检查日志时间格式、文件实际时间范围和关键字条件。"
        return "no_candidate_file", "未选中与查询条件及时间范围重叠的候选日志文件；建议检查模块路径、归档边界、文件时间索引和 DHH 回退来源。"
    if result == AuditResult.CANCELLED:
        return "cancelled", error_message or "日志检索已停止。"
    if result == AuditResult.FAILED:
        return "failed", error_message or "日志检索失败，请查看错误信息。"
    return "running", "日志检索正在执行。"


def record_log_search_cache_hit(audit: LogSearchAudit | None, *, artifact_count: int, result_count: int, output_bytes: int = 0) -> None:
    if audit is None:
        return
    diagnostics = dict(audit.diagnostics or {})
    diagnostics["cache"] = {
        "hit": True,
        "message": "本次查询从最终结果缓存恢复，未重新执行远程目录扫描。",
    }
    diagnostics["plan"] = {
        "status": "cached",
        "selected_file_count": max(0, int(artifact_count)),
        "message": f"缓存记录包含 {max(0, int(artifact_count))} 个候选文件。",
    }
    code, message = _diagnostic_conclusion(
        result=AuditResult.SUCCESS if result_count > 0 else AuditResult.NO_RESULT,
        selected_files=max(0, int(artifact_count)),
        result_count=max(0, int(result_count)),
    )
    diagnostics["time_filter"] = {
        "status": "complete",
        "requested_start": audit.start_time.isoformat(),
        "requested_end": audit.end_time.isoformat(),
        "matched_lines": max(0, int(result_count)),
        "output_bytes": max(0, int(output_bytes)),
    }
    diagnostics["conclusion"] = {"code": code, "message": message}
    LogSearchAudit.objects.filter(pk=audit.pk).update(diagnostics=diagnostics, updated_at=timezone.now())
    audit.diagnostics = diagnostics


def record_log_search_client_result(audit: LogSearchAudit, result_count: int) -> None:
    diagnostics = dict(audit.diagnostics or {})
    selected_files = max(0, int(audit.artifact_count or 0))
    final_result = AuditResult.SUCCESS if result_count > 0 else AuditResult.NO_RESULT
    code, message = _diagnostic_conclusion(
        result=final_result, selected_files=selected_files, result_count=max(0, int(result_count))
    )
    time_filter = dict(diagnostics.get("time_filter") or {})
    time_filter.update({
        "status": "complete",
        "requested_start": audit.start_time.isoformat(),
        "requested_end": audit.end_time.isoformat(),
        "matched_lines": max(0, int(result_count)),
        "client_result": True,
    })
    diagnostics["time_filter"] = time_filter
    diagnostics["conclusion"] = {"code": code, "message": message}
    audit.diagnostics = diagnostics


def create_log_search_audit(*, request, environment, operation_id: str, payload: dict[str, Any], start_time, end_time) -> LogSearchAudit:
    upper = environment.upper_machine
    request_payload = {
        "start_time": payload.get("start_time"),
        "end_time": payload.get("end_time"),
        "source_categories": list(payload.get("source_categories") or []),
        "subsystems": list(payload.get("subsystems") or []),
        "fms": list(payload.get("fms") or []),
        "fm_targets": list(payload.get("fm_targets") or []),
        "keyword": str(payload.get("keyword") or ""),
    }
    targets_snapshot = normalized_targets(request_payload)
    diagnostics = {
        "version": 1,
        "query": {
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "source_categories": list(request_payload["source_categories"]),
            "targets": targets_snapshot,
            "keyword": request_payload["keyword"],
        },
        "strategy": _environment_strategy(environment),
        "plan": {"status": "pending", "selected_file_count": 0, "message": "等待扫描候选日志文件。"},
        "time_filter": {
            "status": "pending",
            "requested_start": start_time.isoformat(),
            "requested_end": end_time.isoformat(),
            "matched_lines": 0,
        },
        "conclusion": {"code": "running", "message": "日志检索正在执行。"},
    }
    with transaction.atomic():
        audit = LogSearchAudit.objects.create(
            operation_id=operation_id,
            operator_username=request_operator_username(request),
            client_ip=request_client_ip(request),
            environment=environment,
            environment_name=environment.name,
            target_host=upper.host,
            target_username=upper.username,
            start_time=start_time,
            end_time=end_time,
            source_categories=request_payload["source_categories"],
            source_categories_text="|" + "|".join(str(item).strip() for item in request_payload["source_categories"] if str(item).strip()) + "|",
            keyword=request_payload["keyword"],
            request_payload=request_payload,
            diagnostics=diagnostics,
        )
        LogSearchAuditTarget.objects.bulk_create(
            [LogSearchAuditTarget(audit=audit, **item) for item in targets_snapshot],
            ignore_conflicts=True,
        )
    audit._started_monotonic = time.monotonic()  # transient helper, never persisted
    return audit



def record_log_search_artifacts(audit: LogSearchAudit | None, artifacts) -> None:
    """Persist selected files plus the planning stage used by the audit diagnostic panel."""
    if audit is None:
        return
    matched_files: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    role_map = _machine_role_map(audit)
    role_counts = {"upper": 0, "lower": 0, "dhh": 0, "unknown": 0}
    for artifact in artifacts or []:
        machine_id = int(getattr(artifact, "machine_id", 0) or 0)
        machine_role = role_map.get(machine_id, "unknown")
        item: dict[str, Any] = {
            "machine_id": machine_id,
            "machine": str(getattr(artifact, "machine_name", "") or ""),
            "machine_role": machine_role,
            "subsystem": str(getattr(artifact, "subsystem", "") or ""),
            "module": str(getattr(artifact, "fm", "") or ""),
            "source_category": str(getattr(artifact, "source_category", "") or ""),
            "path": str(getattr(artifact, "path", "") or ""),
            "member": str(getattr(artifact, "member_name", "") or ""),
            "kind": str(getattr(artifact, "kind", "") or ""),
            "size": max(0, int(getattr(artifact, "size", 0) or 0)),
            "indexed_start": getattr(artifact, "start_time", None).isoformat() if getattr(artifact, "start_time", None) else "",
            "indexed_end": getattr(artifact, "end_time", None).isoformat() if getattr(artifact, "end_time", None) else "",
            "boundary_time": getattr(artifact, "boundary_time", None).isoformat() if getattr(artifact, "boundary_time", None) else "",
        }
        key = tuple(str(item[name]) for name in ("machine", "subsystem", "module", "source_category", "path", "member"))
        if key in seen:
            continue
        seen.add(key)
        matched_files.append(item)
        role_counts[machine_role] = role_counts.get(machine_role, 0) + 1

    diagnostics = dict(audit.diagnostics or {})
    strategy = dict(diagnostics.get("strategy") or {})
    dhh_decision = "not_applicable"
    if strategy.get("mode") == "upper_then_dhh":
        if role_counts.get("dhh", 0) > 0:
            dhh_decision = "fallback_used"
        elif role_counts.get("upper", 0) > 0:
            dhh_decision = "skipped_after_upper_hit"
        else:
            dhh_decision = "no_selected_source"
    diagnostics["plan"] = {
        "status": "complete",
        "selected_file_count": len(matched_files),
        "role_counts": role_counts,
        "dhh_decision": dhh_decision,
        "message": (
            f"已选中 {len(matched_files)} 个与查询条件/时间范围重叠的候选文件，进入日志内容时间过滤。"
            if matched_files
            else "候选文件选择结果为 0；模块路径、归档边界或文件时间索引可能未覆盖本次查询范围。"
        ),
    }
    LogSearchAudit.objects.filter(pk=audit.pk).update(
        artifact_count=len(matched_files),
        matched_files=matched_files,
        diagnostics=diagnostics,
        updated_at=timezone.now(),
    )
    audit.artifact_count = len(matched_files)
    audit.matched_files = matched_files
    audit.diagnostics = diagnostics


def cancel_running_log_search_audits(
    *,
    operation_id: str,
    environment_id: int | None = None,
    result_count: int | None = None,
    artifact_count: int | None = None,
    message: str = "日志检索已停止。",
) -> int:
    """Immediately mark matching running audit rows as cancelled.

    The cancel API is user-visible and should not wait for the streaming generator to
    unwind before the audit page reflects the stop request.  The streaming finalizer
    will confirm the same terminal state later.
    """
    op = str(operation_id or "").strip()
    if not op:
        return 0
    # A fetch AbortController can tear down the streaming response a fraction before
    # the explicit cancel request reaches Django.  In that race the generator may
    # have written FAILED first; the user's explicit stop action must win.
    queryset = LogSearchAudit.objects.filter(
        operation_id=op,
        result__in=[AuditResult.RUNNING, AuditResult.FAILED],
    )
    if environment_id is not None:
        queryset = queryset.filter(environment_id=environment_id)
    finished = timezone.now()
    count = 0
    for audit in queryset.only("id", "created_at", "artifact_count", "result_count", "diagnostics", "start_time", "end_time", "output_bytes"):
        duration_ms = max(0, int((finished - audit.created_at).total_seconds() * 1000))
        diagnostics = dict(audit.diagnostics or {})
        code, diagnostic_message = _diagnostic_conclusion(
            result=AuditResult.CANCELLED,
            selected_files=max(0, int(artifact_count if artifact_count is not None else audit.artifact_count or 0)),
            result_count=max(0, int(result_count if result_count is not None else audit.result_count or 0)),
            error_message=str(message or "日志检索已停止。"),
        )
        diagnostics["conclusion"] = {"code": code, "message": diagnostic_message}
        updates: dict[str, Any] = {
            "result": AuditResult.CANCELLED,
            "error_message": str(message or "日志检索已停止。")[:12000],
            "finished_at": finished,
            "duration_ms": duration_ms,
            "diagnostics": diagnostics,
            "updated_at": finished,
        }
        if result_count is not None:
            updates["result_count"] = max(int(audit.result_count or 0), max(0, int(result_count)))
        if artifact_count is not None:
            updates["artifact_count"] = max(int(audit.artifact_count or 0), max(0, int(artifact_count)))
        count += LogSearchAudit.objects.filter(
            pk=audit.pk,
            result__in=[AuditResult.RUNNING, AuditResult.FAILED],
        ).update(**updates)
    return count


def finish_log_search_audit(
    audit: LogSearchAudit | None,
    *,
    result: str,
    error_message: str = "",
    artifact_count: int | None = None,
    result_count: int | None = None,
    output_bytes: int | None = None,
) -> None:
    if audit is None:
        return
    finished = timezone.now()
    started_monotonic = getattr(audit, "_started_monotonic", None)
    duration_ms = int((time.monotonic() - started_monotonic) * 1000) if started_monotonic is not None else max(0, int((finished - audit.created_at).total_seconds() * 1000))
    updates: dict[str, Any] = {
        "result": result,
        "error_message": str(error_message or "")[:12000],
        "finished_at": finished,
        "duration_ms": duration_ms,
        "updated_at": finished,
    }
    if artifact_count is not None:
        updates["artifact_count"] = max(0, int(artifact_count))
    if result_count is not None:
        updates["result_count"] = max(0, int(result_count))
    if output_bytes is not None:
        updates["output_bytes"] = max(0, int(output_bytes))

    selected_files = max(0, int(artifact_count if artifact_count is not None else audit.artifact_count or 0))
    matched_lines = max(0, int(result_count if result_count is not None else audit.result_count or 0))
    diagnostics = dict(audit.diagnostics or {})
    time_filter = dict(diagnostics.get("time_filter") or {})
    time_filter.update({
        "status": "complete" if result in [AuditResult.SUCCESS, AuditResult.NO_RESULT] else result,
        "requested_start": audit.start_time.isoformat(),
        "requested_end": audit.end_time.isoformat(),
        "selected_file_count": selected_files,
        "matched_lines": matched_lines,
        "output_bytes": max(0, int(output_bytes if output_bytes is not None else audit.output_bytes or 0)),
    })
    diagnostics["time_filter"] = time_filter
    plan_diagnostic = dict(diagnostics.get("plan") or {})
    if result in [AuditResult.FAILED, AuditResult.CANCELLED] and plan_diagnostic.get("status") == "pending":
        plan_diagnostic.update({
            "status": result,
            "message": str(error_message or ("日志检索已停止。" if result == AuditResult.CANCELLED else "候选文件扫描阶段失败。")),
        })
        diagnostics["plan"] = plan_diagnostic
    code, message = _diagnostic_conclusion(
        result=result, selected_files=selected_files, result_count=matched_lines, error_message=str(error_message or "")
    )
    diagnostics["conclusion"] = {"code": code, "message": message}
    updates["diagnostics"] = diagnostics
    audit.diagnostics = diagnostics

    queryset = LogSearchAudit.objects.filter(pk=audit.pk)
    # Once the user has explicitly stopped an operation, later generator cleanup
    # (GeneratorExit/network teardown/success finalization) must never rewrite it.
    if result != AuditResult.CANCELLED:
        queryset = queryset.exclude(result=AuditResult.CANCELLED)
    queryset.update(**updates)
