from __future__ import annotations

import importlib
import logging
import pkgutil
from threading import RLock
from typing import Iterable

from apps.tooling.registry import TOOLS, ToolDefinition
from apps.tooling.plugins.base import AtomicToolPlugin, FunctionToolPlugin, LegacyToolPlugin

logger = logging.getLogger("tracelens.tooling.plugins")

_LOCK = RLock()
_EXTRA_PLUGINS: dict[str, AtomicToolPlugin] = {}
_DISCOVERED = False
# Modules that raised on import. A plugin that cannot import is silently absent from
# the agent's toolset, which is indistinguishable from "the capability does not exist"
# unless we keep the evidence around.
_DISCOVERY_FAILURES: dict[str, str] = {}


def plugin_discovery_failures() -> dict[str, str]:
    """{module_name: error} for plugins that failed to import during discovery."""
    _discover_modules()
    with _LOCK:
        return dict(_DISCOVERY_FAILURES)


def register_plugin(plugin: AtomicToolPlugin) -> AtomicToolPlugin:
    """Register a future plugin without editing Core Kernel.

    Existing tools do not need to call this: they are adapted automatically from
    ``apps.tooling.registry.TOOLS``. New plugin modules may call this at import time.
    """
    tool_id = str(plugin.definition.id or "").strip()
    if not tool_id:
        raise ValueError("plugin.definition.id 不能为空")
    with _LOCK:
        _EXTRA_PLUGINS[tool_id] = plugin
    return plugin


def register_function_tool(definition: ToolDefinition):
    """Decorator for metadata-driven future atomic tools."""

    def decorator(handler):
        register_plugin(FunctionToolPlugin(definition=definition, handler=handler))
        return handler

    return decorator


def _discover_modules() -> None:
    global _DISCOVERED
    with _LOCK:
        if _DISCOVERED:
            return
        _DISCOVERED = True
    package_name = "apps.tooling.plugins"
    try:
        package = importlib.import_module(package_name)
        for module in pkgutil.iter_modules(package.__path__, package_name + "."):
            short = module.name.rsplit(".", 1)[-1]
            if short in {"base", "catalog", "__init__"} or short.startswith("_"):
                continue
            try:
                importlib.import_module(module.name)
            except Exception as exc:  # noqa: BLE001
                logger.exception("tool_plugin.discovery_failed module=%s", module.name)
                with _LOCK:
                    _DISCOVERY_FAILURES[module.name] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.exception("tool_plugin.discovery_failed package=%s", package_name)
        with _LOCK:
            _DISCOVERY_FAILURES[package_name] = f"{type(exc).__name__}: {exc}"
    with _LOCK:
        if _DISCOVERY_FAILURES:
            # One aggregated, greppable line: otherwise a broken plugin only shows up as
            # a missing tool, which nobody notices until an agent silently degrades.
            logger.error(
                "tool_plugin.discovery_incomplete failed=%s modules=%s",
                len(_DISCOVERY_FAILURES), ",".join(sorted(_DISCOVERY_FAILURES)),
            )


def iter_plugins() -> Iterable[AtomicToolPlugin]:
    """Yield legacy adapters first, then dynamically discovered plugins.

    An explicit plugin with the same ID overrides the legacy adapter. This allows
    gradual migration one capability at a time without rewriting all tools.
    """
    _discover_modules()
    extras = dict(_EXTRA_PLUGINS)
    for definition in TOOLS:
        if definition.id in extras:
            continue
        yield LegacyToolPlugin(definition=definition)
    yield from extras.values()
