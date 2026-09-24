from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from apps.tooling.registry import ToolDefinition


class AtomicToolPlugin(Protocol):
    """Uniform adapter contract used by Core Kernel.

    Existing TraceLens tools and future plugins both implement this tiny surface.
    Business logic remains inside the original service/handler.
    """

    @property
    def definition(self) -> ToolDefinition: ...

    def invoke(self, payload: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class LegacyToolPlugin:
    """Adapter for the already-existing TraceLens ToolDefinition registry."""

    definition: ToolDefinition

    def invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        handler = self.definition.handler
        if handler is None:
            raise RuntimeError(f"工具 {self.definition.id} 不支持通用 JSON 调用。")
        return handler(payload)


@dataclass(frozen=True)
class FunctionToolPlugin:
    """Metadata-driven adapter for new atomic capabilities.

    Future capabilities can be added under ``apps.tooling.plugins`` without
    modifying Core Kernel. They only need a ToolDefinition + handler.
    """

    definition: ToolDefinition
    handler: Callable[[dict[str, Any]], dict[str, Any]]

    def invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.handler(payload)
