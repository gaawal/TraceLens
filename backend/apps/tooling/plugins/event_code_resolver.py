from __future__ import annotations

from apps.tooling.plugins.catalog import register_function_tool
from apps.tooling.registry import ToolDefinition
from apps.logsources.models import EventCodeDefinition


@register_function_tool(ToolDefinition(
    id="resolve_event_codes_batch",
    name="批量解析事件码归属",
    description="一次解析多个事件码，返回组件、子系统、模块归属信息",
    category="事件关联",
    handler=None,
    agent_exposed=True,
    read_only=True,
    transport="function",
    domain="event",
    kind="query",
    skills=("atlog", "logs"),
    input_schema={
        "type": "object",
        "properties": {"codes": {"type": "array", "items": {"type": "string"}}, "environment_id": {"type": ["integer", "string"]}},
        "required": ["codes"]
    }
))
def resolve_event_codes_batch(payload: dict):
    codes = list(dict.fromkeys([str(x).strip() for x in payload.get("codes", []) if str(x).strip()]))
    environment_id = payload.get("environment_id")
    result = []
    for code in codes:
        qs = EventCodeDefinition.objects.filter(active=True).filter(code__iexact=code)
        if environment_id not in (None, ""):
            qs = qs.filter(environment_id=int(environment_id))
        rows = list(qs.select_related("subsystem")[:10])
        if not rows:
            qs = EventCodeDefinition.objects.filter(active=True, display_code__iexact=code)
            if environment_id not in (None, ""):
                qs = qs.filter(environment_id=int(environment_id))
            rows = list(qs.select_related("subsystem")[:10])
        for row in rows:
            result.append({
                "code": code,
                "display_code": row.display_code,
                "component": row.component_code,
                "subsystem": row.subsystem.name,
                "module": row.component_code,
                "description": row.code_string or "",
            })
        if not rows:
            result.append({"code": code, "component": "", "subsystem": "", "module": "", "description": "未找到事件配置"})
    return {"resolved": result, "count": len(result)}
