from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolRequest:
    tool_id: str
    arguments: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolExecution:
    tool_id: str
    arguments: dict[str, Any]
    data: Any

    def as_dict(self) -> dict[str, Any]:
        return {"tool_id": self.tool_id, "arguments": self.arguments, "data": self.data}
