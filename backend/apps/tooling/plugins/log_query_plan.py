from __future__ import annotations

from apps.tooling.plugins.catalog import register_function_tool
from apps.tooling.registry import ToolDefinition


@register_function_tool(ToolDefinition(
    id="create_log_query_plan",
    name="生成日志查询计划",
    description="根据组件归属生成需要查询的子系统模块和时间范围",
    category="自动化用例",
    handler=None,
    agent_exposed=True,
    read_only=True,
    transport="function",
    domain="atlog",
    kind="query",
    skills=("atlog",),
    input_schema={
        "type": "object",
        "properties": {
            "modules": {"type": "array"},
            "start_time": {"type": "string"},
            "end_time": {"type": "string"}
        }
    }
))
def create_log_query_plan(payload: dict):
    return {
        "action": "search_logs",
        "modules": payload.get("modules", []),
        "time_range": {
            "start": payload.get("start_time", ""),
            "end": payload.get("end_time", "")
        }
    }
