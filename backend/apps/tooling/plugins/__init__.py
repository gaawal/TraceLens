"""Atomic Tool plugin package.

Current TraceLens tools are auto-adapted from the legacy registry. New capabilities
can be added as independent plugin modules and registered through ``register_plugin``
or ``register_function_tool`` without modifying Core Kernel.
"""

from apps.tooling.plugins.catalog import register_function_tool, register_plugin

__all__ = ["register_plugin", "register_function_tool"]
