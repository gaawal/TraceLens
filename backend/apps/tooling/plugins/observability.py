"""Read-only observability capabilities for TracePilot.

These close the gap between "diagnose a failure someone already noticed" and
"observe the system": clock skew, storage headroom, index freshness, and the audit
trail. Every one of them is deliberately read-only so the agent can call them freely
without a confirmation card.

They are registered through the plugin catalog rather than appended to the legacy
``TOOLS`` tuple, which is the supported way to add a capability without touching Core
Kernel.
"""
from __future__ import annotations

import logging
from typing import Any

from apps.tooling.plugins.catalog import register_function_tool
from apps.tooling.registry import ToolDefinition

logger = logging.getLogger("tracelens.tooling.plugins.observability")

_ENV_ID = {"type": ["integer", "string"], "description": "TraceLens 环境 ID"}


def _environment(environment_id: Any):
    from apps.tooling.services import _environment as resolve

    return resolve(environment_id)


def _failure(message: str) -> dict[str, Any]:
    return {"ok": False, "error": str(message)[:600]}


# --------------------------------------------------------------- clock skew
@register_function_tool(ToolDefinition(
    id="check_log_time_skew",
    name="检查上下位机时钟偏差",
    description=(
        "只读检查环境内上位机/下位机的系统时间偏差，并给出它对日志时间线的实际影响。"
        "当用户报告“查不到日志”“最近 N 小时是空的”“日志时间对不上”时，先调用本工具排除时钟问题。"
    ),
    category="环境操作",
    handler=None,
    agent_exposed=True,
    read_only=True,
    transport="function",
    domain="environment",
    kind="query",
    skills=("logs", "environment"),
    input_schema={
        "type": "object",
        "properties": {"environment_id": _ENV_ID, "refresh": {"type": "boolean", "default": False}},
        "required": ["environment_id"],
    },
    use_when=(
        "用户的相对时间窗口（最近 1/3/24 小时）查不到日志，或怀疑上下位机日志时间对不上时。"
        "这通常不是没有日志，而是时间锚点偏了。"
    ),
    do_not_use_when="用户只是问某个绝对时间窗内的日志内容，且已知时间同步正常。",
    implementation="apps.tooling.plugins.observability.check_log_time_skew",
))
def check_log_time_skew(payload: dict) -> dict[str, Any]:
    from apps.tooling.services import get_environment_runtime_status

    environment = _environment(payload.get("environment_id"))
    status = get_environment_runtime_status({
        "environment_id": environment.id,
        "refresh": bool(payload.get("refresh", False)),
    })

    upper = status.get("upper") or {}
    machines: list[dict[str, Any]] = []
    for item in status.get("lowers") or []:
        delta = item.get("time_delta_seconds")
        machines.append({
            "machine": str(item.get("name") or item.get("host") or "lower"),
            "role": "lower",
            "online": bool(item.get("online", True)),
            "remote_date": item.get("remote_date") or "",
            "delta_seconds": delta,
            "state": item.get("time_sync_state") or "",
            "sync_required": bool(item.get("time_sync_required", False)),
        })

    skews = [abs(int(m["delta_seconds"])) for m in machines if isinstance(m.get("delta_seconds"), int)]
    worst = max(skews) if skews else 0
    from django.conf import settings

    tolerance = int(getattr(settings, "TRACELENS_LOWER_TIME_SYNC_TOLERANCE_SECONDS", 60) or 60)

    if not machines:
        verdict = "unknown"
        impact = "环境里没有可比较的下位机，无法判断时钟偏差。"
    elif worst <= tolerance:
        verdict = "synced"
        impact = f"偏差最大 {worst} 秒，在容差 {tolerance} 秒内，日志时间线可直接按本地时间对齐。"
    elif worst <= 300:
        verdict = "skewed"
        impact = (
            f"偏差最大 {worst} 秒，已超过容差 {tolerance} 秒。按“最近 N 分钟”检索时，"
            "靠近窗口边缘的日志会被漏掉或错配；把窗口放宽到偏差的 2 倍以上再重查。"
        )
    else:
        verdict = "badly_skewed"
        impact = (
            f"偏差最大 {worst} 秒（约 {worst / 3600:.1f} 小时），时间线已严重错位。"
            "此时“查不到日志”几乎一定是时钟问题而不是没有日志；"
            "应先同步时间（sync_deployment_time，需用户确认）再检索。"
        )

    return {
        "ok": True,
        "environment_id": environment.id,
        "verdict": verdict,
        "tolerance_seconds": tolerance,
        "worst_delta_seconds": worst,
        "upper": {"machine": str(upper.get("name") or "upper"), "remote_date": upper.get("remote_date") or ""},
        "machines": machines,
        "timeline_impact": impact,
        "checked_at": status.get("checked_at") or "",
    }


# ---------------------------------------------------------- storage pressure
@register_function_tool(ToolDefinition(
    id="check_log_storage_pressure",
    name="检查日志存储水位",
    description=(
        "只读检查上下位机上日志根目录所在文件系统的剩余空间和日志占用。"
        "日志写满磁盘会直接断流——这是“日志突然没有了”最常见的真实原因。"
    ),
    category="环境操作",
    handler=None,
    agent_exposed=True,
    read_only=True,
    transport="function",
    domain="environment",
    kind="query",
    skills=("logs", "environment"),
    input_schema={
        "type": "object",
        "properties": {
            "environment_id": _ENV_ID,
            "include_usage": {"type": "boolean", "default": True, "description": "是否统计日志目录实际占用（较慢）"},
        },
        "required": ["environment_id"],
    },
    use_when="日志在某时刻之后完全没有新增，或怀疑日志写入中断时。",
    do_not_use_when="只是查询某个已知时间窗内的日志内容。",
    implementation="apps.tooling.plugins.observability.check_log_storage_pressure",
))
def check_log_storage_pressure(payload: dict) -> dict[str, Any]:
    from apps.common.services.ssh import execute
    from apps.logsources.services.remote_logs import expand_log_roots

    environment = _environment(payload.get("environment_id"))
    include_usage = bool(payload.get("include_usage", True))

    try:
        targets = expand_log_roots(environment)
    except Exception as exc:  # noqa: BLE001
        return _failure(f"无法解析日志根目录：{exc}")
    if not targets:
        return _failure("该环境没有启用中的日志路径规则，无法检查存储水位。")

    # One probe per (machine, filesystem), not per log root.
    by_machine: dict[Any, set[str]] = {}
    for target in targets:
        if target.machine is not None and target.root:
            by_machine.setdefault(target.machine, set()).add(target.root)

    filesystems: list[dict[str, Any]] = []
    for machine, roots in by_machine.items():
        root = sorted(roots)[0]
        command = f"df -Pk {root} 2>/dev/null | tail -n 1"
        try:
            result = execute(machine, command)
            fields = str(result.stdout or "").strip().split()
        except Exception as exc:  # noqa: BLE001
            filesystems.append({"machine": str(getattr(machine, "name", machine)), "error": str(exc)[:200]})
            continue
        if len(fields) < 6:
            filesystems.append({"machine": str(getattr(machine, "name", machine)), "error": "df 未返回可用数据"})
            continue
        try:
            total_kb, used_kb, avail_kb = int(fields[1]), int(fields[2]), int(fields[3])
            used_pct = int(str(fields[4]).rstrip("%"))
        except (ValueError, IndexError):
            filesystems.append({"machine": str(getattr(machine, "name", machine)), "error": "df 输出无法解析"})
            continue
        entry: dict[str, Any] = {
            "machine": str(getattr(machine, "name", machine)),
            "filesystem": fields[0],
            "mounted_on": fields[5],
            "probe_path": root,
            "total_gb": round(total_kb / 1024 / 1024, 2),
            "used_gb": round(used_kb / 1024 / 1024, 2),
            "available_gb": round(avail_kb / 1024 / 1024, 2),
            "used_percent": used_pct,
            "log_roots": sorted(roots),
            "verdict": "critical" if used_pct >= 95 else "warning" if used_pct >= 85 else "healthy",
        }
        if include_usage:
            try:
                usage = execute(machine, f"du -sk {root} 2>/dev/null | tail -n 1")
                parts = str(usage.stdout or "").strip().split()
                if parts and parts[0].isdigit():
                    entry["log_usage_gb"] = round(int(parts[0]) / 1024 / 1024, 2)
            except Exception:  # noqa: BLE001
                pass
        filesystems.append(entry)

    worst = max((f.get("used_percent", 0) for f in filesystems if "used_percent" in f), default=0)
    if not filesystems or worst == 0:
        verdict = "unknown"
    elif worst >= 95:
        verdict = "critical"
    elif worst >= 85:
        verdict = "warning"
    else:
        verdict = "healthy"

    advice = {
        "healthy": "存储水位正常。",
        "warning": "磁盘使用率已超过 85%，日志轮转或归档可能随时失败，建议尽快清理历史归档。",
        "critical": "磁盘使用率已超过 95%，日志很可能已经停止写入——这通常就是“日志突然没了”的原因。",
        "unknown": "未能取得磁盘信息，请检查该环境 SSH 是否可用。",
    }[verdict]

    return {
        "ok": True,
        "environment_id": environment.id,
        "verdict": verdict,
        "worst_used_percent": worst,
        "filesystems": filesystems,
        "advice": advice,
    }


# --------------------------------------------------------------- index health
@register_function_tool(ToolDefinition(
    id="inspect_log_index_health",
    name="检查日志索引与缓存健康",
    description=(
        "只读检查 Redis 日志指纹索引/正文缓存的连接状态、键数量与配置。"
        "用于区分“环境真的没有日志”和“本地索引脏了/缓存不可用导致查不到”。"
    ),
    category="日志检索",
    handler=None,
    agent_exposed=True,
    read_only=True,
    transport="function",
    domain="log",
    kind="query",
    skills=("logs",),
    input_schema={"type": "object", "properties": {}, "required": []},
    use_when="日志检索结果与预期不符、检索明显变慢，或怀疑本地索引/缓存异常时。",
    do_not_use_when="怀疑远端日志本身缺失；那应该先看存储水位和时钟偏差。",
    implementation="apps.tooling.plugins.observability.inspect_log_index_health",
))
def inspect_log_index_health(payload: dict) -> dict[str, Any]:
    from apps.logsources.services.redis_store import RedisLogStore

    status = RedisLogStore.status()
    connected = bool(status.get("connected"))
    enabled = bool(status.get("enabled"))
    if not enabled:
        verdict, advice = "disabled", "Redis 索引未启用；检索会直接走远端 SSH，功能可用但更慢。"
    elif not connected:
        verdict, advice = "degraded", "Redis 不可用，系统会自动回退远端读取。若检索异常变慢，这就是原因。"
    else:
        verdict, advice = "healthy", "索引与缓存工作正常。"
    return {
        "ok": True,
        "verdict": verdict,
        "redis_enabled": enabled,
        "redis_connected": connected,
        "endpoint": status.get("endpoint") or "",
        "database": status.get("database"),
        "cache_schema": status.get("cache_schema") or "",
        "tracelens_keys": status.get("tracelens_keys"),
        "database_keys": status.get("database_keys"),
        "last_error": status.get("last_error") or "",
        "advice": advice,
    }


# ------------------------------------------------------------------ audit trail
@register_function_tool(ToolDefinition(
    id="query_log_search_audit",
    name="查询日志检索审计",
    description=(
        "只读查询最近的日志检索审计记录：谁在什么时候、用哪些参数、检索了哪个环境、"
        "命中多少文件、结果如何。用于回答“上次查的是什么”“为什么这次结果不一样”。"
    ),
    category="界面操作",
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
            "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
            "result": {"type": "string", "description": "可选，按结果过滤：success/no_result/failed/cancelled/running"},
        },
        "required": [],
    },
    use_when="需要复现或解释一次历史检索，或对比“上次能查到、这次查不到”时。",
    do_not_use_when="用户想直接看某一个时间窗的日志内容。",
    implementation="apps.tooling.plugins.observability.query_log_search_audit",
))
def query_log_search_audit(payload: dict) -> dict[str, Any]:
    from apps.audits.models import LogSearchAudit

    limit = max(1, min(50, int(payload.get("limit") or 10)))
    queryset = LogSearchAudit.objects.all().order_by("-created_at")
    if payload.get("environment_id") not in (None, ""):
        queryset = queryset.filter(environment_id=int(str(payload["environment_id"])))
    if str(payload.get("result") or "").strip():
        queryset = queryset.filter(result=str(payload["result"]).strip())

    rows = []
    for record in queryset[:limit]:
        rows.append({
            "operation_id": record.operation_id,
            "created_at": record.created_at.isoformat() if record.created_at else "",
            "environment": record.environment_name or (record.environment_id and str(record.environment_id)) or "",
            "operator": record.operator_username or "",
            "client_ip": record.client_ip or "",
            "start_time": record.start_time.isoformat() if record.start_time else "",
            "end_time": record.end_time.isoformat() if record.end_time else "",
            "source_categories": list(record.source_categories or []),
            "keyword": (record.keyword or "")[:200],
            "result": record.result,
            "error_message": (record.error_message or "")[:400],
            "artifact_count": record.artifact_count,
            "matched_files": list(record.matched_files or [])[:12],
        })
    return {"ok": True, "count": len(rows), "searches": rows}


# -------------------------------------------------------------------- CPD reports
@register_function_tool(ToolDefinition(
    id="list_environment_reports",
    name="列出环境 CPD 报告",
    description="只读列出环境日志目录中的 CPD .rpt 报告文件，可按子系统/模块过滤，最新在前。",
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
            "subsystem": {"type": "string", "default": ""},
            "module": {"type": "string", "default": ""},
            "page": {"type": "integer", "default": 1, "minimum": 1},
            "page_size": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
        },
        "required": ["environment_id"],
    },
    use_when="需要先知道有哪些 CPD 报告可读，或在报告目录里按子系统/模块定位文件时。",
    do_not_use_when="只是想看普通 debug/run 日志。",
    implementation="apps.tooling.plugins.observability.list_environment_reports",
))
def list_environment_reports(payload: dict) -> dict[str, Any]:
    from apps.reports.services import list_all_reports, list_reports

    environment = _environment(payload.get("environment_id"))
    subsystem = str(payload.get("subsystem") or "").strip()
    module = str(payload.get("module") or "").strip()
    page = max(1, int(payload.get("page") or 1))
    page_size = max(1, min(200, int(payload.get("page_size") or 50)))
    if subsystem and module:
        return {"ok": True, **(list_reports(environment, subsystem, module, page=page, page_size=page_size) or {})}
    return {"ok": True, **(list_all_reports(environment, page=page, page_size=page_size) or {})}


@register_function_tool(ToolDefinition(
    id="read_environment_report",
    name="读取 CPD 报告内容",
    description="只读读取一个 CPD .rpt 报告文件的内容（截断到合理长度），用于分析报告里的结论与指标。",
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
            "subsystem": {"type": "string"},
            "module": {"type": "string"},
            "file_name": {"type": "string", "description": "以 .rpt 结尾的报告文件名"},
        },
        "required": ["environment_id", "subsystem", "module", "file_name"],
    },
    use_when="已经用 list_environment_reports 找到了具体报告文件，需要读内容时。",
    do_not_use_when="还没有确定具体报告文件名。",
    implementation="apps.tooling.plugins.observability.read_environment_report",
))
def read_environment_report(payload: dict) -> dict[str, Any]:
    from apps.reports.services import read_report_content

    environment = _environment(payload.get("environment_id"))
    result = read_report_content(
        environment,
        str(payload.get("subsystem") or ""),
        str(payload.get("module") or ""),
        str(payload.get("file_name") or ""),
    )
    data = dict(result or {})
    content = str(data.get("content") or "")
    if len(content) > 20000:
        data["content"] = content[:20000] + f"\n…（已截断，原文 {len(content)} 字符）"
    data["ok"] = True
    return data
