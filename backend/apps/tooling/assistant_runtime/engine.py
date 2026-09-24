"""Compatibility facade for the TracePilot AI Engine.

Public callers historically imported ``apps.tooling.assistant_runtime.engine``.
The actual LangGraph orchestration now lives in ``apps.tooling.ai_engine.graph``.
"""

from apps.tooling.ai_engine.graph import GraphExecutionError, run_stream, run_sync

__all__ = ["GraphExecutionError", "run_stream", "run_sync"]
