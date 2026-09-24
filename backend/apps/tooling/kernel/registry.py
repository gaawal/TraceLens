from __future__ import annotations

from collections import defaultdict
from threading import RLock
from typing import Any, Iterable

from apps.tooling.plugins.base import AtomicToolPlugin
from apps.tooling.plugins.catalog import iter_plugins
from apps.tooling.registry import ToolDefinition, normalize_skill_id


class ToolRegistry:
    """Stable Core Kernel registry over existing and future atomic tools."""

    def __init__(self, plugins: Iterable[AtomicToolPlugin] | None = None) -> None:
        source = list(plugins if plugins is not None else iter_plugins())
        self._lock = RLock()
        self._by_id: dict[str, AtomicToolPlugin] = {}
        self._by_atomic_id: dict[str, AtomicToolPlugin] = {}
        self._by_domain: dict[str, list[str]] = defaultdict(list)
        for plugin in source:
            self.register(plugin)

    def register(self, plugin: AtomicToolPlugin) -> None:
        definition = plugin.definition
        tool_id = str(definition.id or "").strip()
        if not tool_id:
            raise ValueError("ToolDefinition.id 不能为空")
        atomic_id = definition.capability_id()
        domain = definition.capability_domain()
        with self._lock:
            old = self._by_id.get(tool_id)
            if old is not None:
                old_domain = old.definition.capability_domain()
                self._by_domain[old_domain] = [item for item in self._by_domain[old_domain] if item != tool_id]
            self._by_id[tool_id] = plugin
            self._by_atomic_id[atomic_id] = plugin
            if tool_id not in self._by_domain[domain]:
                self._by_domain[domain].append(tool_id)

    def get_plugin(self, tool_id: str) -> AtomicToolPlugin | None:
        key = str(tool_id or "").strip()
        return self._by_id.get(key) or self._by_atomic_id.get(key)

    def get(self, tool_id: str) -> ToolDefinition | None:
        plugin = self.get_plugin(tool_id)
        return plugin.definition if plugin is not None else None

    def all_definitions(self) -> list[ToolDefinition]:
        return [plugin.definition for plugin in self._by_id.values()]

    def domains(self) -> dict[str, list[str]]:
        return {key: list(value) for key, value in self._by_domain.items()}

    def tools_for_domain(self, domain: str) -> list[ToolDefinition]:
        return [self._by_id[item].definition for item in self._by_domain.get(str(domain or ""), [])]

    def tools_for_skill(self, skill_id: str | None) -> list[ToolDefinition]:
        normalized = normalize_skill_id(skill_id)
        values = self.all_definitions()
        if normalized == "auto":
            return values
        return [tool for tool in values if normalized in tool.capability_skills()]

    def is_invokable(self, tool: ToolDefinition) -> bool:
        """Can this tool actually be executed?

        Legacy adapters mirror ``ToolDefinition.handler``; ``FunctionToolPlugin`` keeps its
        callable on the adapter instead, so a bare ``tool.handler is None`` test wrongly
        rejects every plugin-registered tool.
        """
        plugin = self.get_plugin(tool.id)
        if plugin is None:
            return False
        return tool.handler is not None or plugin.__class__.__name__ == "FunctionToolPlugin"

    # Kept for existing callers inside this module.
    _is_invokable = is_invokable

    def agent_candidates(self, skill_id: str | None = None) -> list[ToolDefinition]:
        return [
            tool for tool in self.tools_for_skill(skill_id)
            if tool.agent_exposed and self._is_invokable(tool)
        ]

    def as_dicts(self) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        for tool in self.all_definitions():
            item = tool.as_dict()
            invokable = self._is_invokable(tool)
            item["invokable"] = invokable
            # "Available to the agent" must mean "the LLM can actually call it":
            # exposed AND callable. The previous `invokable or transport == "stream"`
            # counted stream-transport UI endpoints that no agent can ever route to,
            # which inflated the reported agent toolset.
            item["agent_available"] = bool(tool.agent_exposed and invokable)
            values.append(item)
        return values

    @staticmethod
    def _compact_description(tool: ToolDefinition, max_chars: int = 1000) -> str:
        text = tool.agent_description().strip()
        return text if len(text) <= max_chars else text[:max_chars].rstrip() + "…"

    def llm_specs(self, tool_ids: set[str] | list[str] | tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        allowed = set(tool_ids) if tool_ids is not None else None
        specs: list[dict[str, Any]] = []
        for tool in self.agent_candidates("auto"):
            if allowed is not None and tool.id not in allowed and tool.capability_id() not in allowed:
                continue
            specs.append({
                "type": "function",
                "function": {
                    "name": tool.id,
                    "description": self._compact_description(tool),
                    "parameters": tool.input_schema,
                },
            })
        return specs

    def domain_catalog(self, skill_id: str | None = None) -> tuple[list[str], str]:
        grouped: dict[str, list[ToolDefinition]] = defaultdict(list)
        for tool in self.agent_candidates(skill_id):
            grouped[tool.capability_domain()].append(tool)
        domains = sorted(grouped)
        rows: list[str] = []
        for domain in domains:
            tools = grouped[domain]
            examples = "、".join(tool.name for tool in tools[:5])
            mutating = sum(1 for tool in tools if not tool.read_only)
            rows.append(
                f"- {domain} | tools={len(tools)} | write={mutating} | examples={examples}"
            )
        return domains, "\n".join(rows)

    def routing_catalog(self, skill_id: str | None = None, domains: set[str] | None = None) -> tuple[list[str], str]:
        ids: list[str] = []
        rows: list[str] = []
        for tool in self.agent_candidates(skill_id):
            if domains is not None and tool.capability_domain() not in domains:
                continue
            ids.append(tool.id)
            rows.append(
                f"- {tool.id} | {tool.name} | domain={tool.capability_domain()} "
                f"| kind={tool.capability_kind()} | read_only={tool.read_only} "
                f"| {tool.description.strip()[:220]}"
                + (f" | use_when={tool.use_when.strip()[:160]}" if tool.use_when else "")
                + (f" | avoid={tool.do_not_use_when.strip()[:120]}" if tool.do_not_use_when else "")
            )
        return ids, "\n".join(rows)
