from __future__ import annotations

import json
import logging
from typing import Any

from apps.tooling.kernel.context import EnvironmentContextManager
from apps.tooling.kernel.protocol import ToolExecution, ToolRequest
from apps.tooling.kernel.registry import ToolRegistry

logger = logging.getLogger("tracelens.tooling.kernel")

_SENSITIVE = ("password", "passwd", "token", "secret", "api_key", "authorization", "cookie", "credential")

def _safe(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "<max-depth>"
    if isinstance(value, dict):
        out = {}
        for key, item in list(value.items())[:80]:
            name = str(key)
            out[name] = "***" if any(part in name.casefold() for part in _SENSITIVE) else _safe(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_safe(item, depth + 1) for item in list(value)[:30]]
    if isinstance(value, str):
        return value[:3000] + ("..." if len(value) > 3000 else "")
    return value

def _log_json(value: Any, limit: int = 10000) -> str:
    try:
        text = json.dumps(_safe(value), ensure_ascii=False, default=str, sort_keys=True)
    except Exception:
        text = str(value)
    return text[:limit]


class CoreKernel:
    """Stable routing/execution kernel.

    The kernel contains no domain implementation. It resolves Tool metadata,
    injects runtime context, and dispatches to the adapter that owns the existing
    handler.
    """

    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self.registry = registry or ToolRegistry()
        self.context = EnvironmentContextManager(self.registry)

    def prepare(self, request: ToolRequest) -> tuple[Any, dict[str, Any]]:
        plugin = self.registry.get_plugin(request.tool_id)
        if plugin is None:
            raise KeyError(request.tool_id)
        logger.info(
            "kernel.prepare tool=%s model_arguments=%s context=%s",
            request.tool_id, _log_json(request.arguments), _log_json(request.context, 8000),
        )
        arguments = self.context.inject(plugin.definition.id, request.arguments, request.context)
        logger.info(
            "kernel.prepared tool=%s injected_arguments=%s",
            plugin.definition.id, _log_json(arguments),
        )
        return plugin, arguments

    def execute_request(self, request: ToolRequest) -> ToolExecution:
        plugin, arguments = self.prepare(request)
        try:
            data = plugin.invoke(arguments)
        except Exception:
            logger.exception(
                "kernel.execute_failed tool=%s injected_arguments=%s",
                plugin.definition.id, _log_json(arguments),
            )
            raise
        logger.info(
            "kernel.executed tool=%s result=%s",
            plugin.definition.id, _log_json(data, 12000),
        )
        self.context.absorb_result(plugin.definition.id, data, request.context)
        return ToolExecution(tool_id=plugin.definition.id, arguments=arguments, data=data)

    def dispatch(self, packet: dict[str, Any]) -> dict[str, Any]:
        """JSON-compatible AI Engine -> Core Kernel protocol boundary."""
        if not isinstance(packet, dict):
            raise TypeError("Kernel packet 必须是 JSON object")
        request = ToolRequest(
            tool_id=str(packet.get("tool_id") or packet.get("tool") or "").strip(),
            arguments=packet.get("arguments") if isinstance(packet.get("arguments"), dict) else {},
            context=packet.get("context") if isinstance(packet.get("context"), dict) else {},
        )
        if not request.tool_id:
            raise ValueError("Kernel packet 缺少 tool_id")
        result = self.execute_request(request)
        return {"ok": True, **result.as_dict()}

    def execute(self, tool_id: str, arguments: dict[str, Any] | None = None, *, context: dict[str, Any] | None = None) -> Any:
        runtime_context = context if isinstance(context, dict) else {}
        packet = self.dispatch({"tool_id": tool_id, "arguments": arguments or {}, "context": runtime_context})
        return packet.get("data")


_DEFAULT_KERNEL: CoreKernel | None = None


def get_default_kernel() -> CoreKernel:
    global _DEFAULT_KERNEL
    if _DEFAULT_KERNEL is None:
        _DEFAULT_KERNEL = CoreKernel()
    return _DEFAULT_KERNEL
