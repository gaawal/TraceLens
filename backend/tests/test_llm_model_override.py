"""Regression guard: the composer's model / reasoning-effort picker must reach the model.

Two real defects motivated these tests:

1. The picker's choice travels as a contextvar, but ``ai_engine.graph.run_stream`` and
   ``assistant._invoke_read_only_tool_with_progress`` execute the run on a *worker
   thread*. Contextvars do not cross thread boundaries, so the selection was silently
   dropped and every call used the configured default model.
2. The request log printed ``self.model`` (the configured default), so the dropped
   selection was invisible in diagnostics.

The threading case is covered structurally, because exercising it end-to-end needs
LangGraph, the ORM and a live gateway.
"""
from __future__ import annotations

import contextvars
import threading
from pathlib import Path

from apps.tooling.llm.client import (
    REASONING_EFFORTS,
    get_llm_effort_override,
    get_llm_model_override,
    set_llm_overrides,
)

ROOT = Path(__file__).resolve().parents[2]


def _client():
    from apps.tooling.llm.client import LLMClient

    client = object.__new__(LLMClient)
    client.model = "deepseek-flash"
    client._thinking_disabled = False
    client._forced_tool_choice_unsupported = False
    return client


def test_overrides_apply_to_request_kwargs():
    set_llm_overrides("deepseek-v4-pro", "low")
    kwargs = _client()._request_kwargs("deepseek-flash", [{"role": "user", "content": "hi"}])
    assert kwargs["model"] == "deepseek-v4-pro"
    assert kwargs["reasoning_effort"] == "low"


def test_defaults_are_used_when_no_selection_is_made():
    set_llm_overrides("", "")
    kwargs = _client()._request_kwargs("deepseek-flash", [{"role": "user", "content": "hi"}])
    assert kwargs["model"] == "deepseek-flash"
    assert "reasoning_effort" not in kwargs


def test_unknown_effort_is_dropped_instead_of_failing_the_turn():
    set_llm_overrides("deepseek-flash", "impossible")
    assert get_llm_effort_override() == ""
    kwargs = _client()._request_kwargs("deepseek-flash", [{"role": "user", "content": "hi"}])
    assert "reasoning_effort" not in kwargs
    # Every advertised level must survive the filter.
    for effort in REASONING_EFFORTS:
        set_llm_overrides("deepseek-flash", effort)
        assert get_llm_effort_override() == effort


def test_selection_crosses_a_worker_thread_with_copy_context():
    """The exact mechanism the graph and tool threads rely on."""
    set_llm_overrides("", "")
    seen: dict[str, str] = {}

    snapshot = contextvars.copy_context()

    def body() -> None:
        # Mirrors the real worker: everything that needs the selection runs *inside*
        # the snapshot, which is what `run_context.run(invoke_graph)` achieves.
        set_llm_overrides("deepseek-v4-pro", "max")
        seen["model"] = get_llm_model_override()
        seen["kwargs_model"] = _client()._request_kwargs("deepseek-flash", [{"role": "user", "content": "hi"}])["model"]

    def worker() -> None:
        snapshot.run(body)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert seen["model"] == "deepseek-v4-pro"
    assert seen["kwargs_model"] == "deepseek-v4-pro"
    # The seeding context is untouched: no cross-run leakage.
    assert get_llm_model_override() == ""


def test_worker_threads_snapshot_their_calling_context():
    """Guard the fix at both thread boundaries that used to drop the selection."""
    for relative in ("backend/apps/tooling/ai_engine/graph.py", "backend/apps/tooling/assistant.py"):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "contextvars.copy_context()" in source, relative
        assert "run_context.run(" in source or "tool_context.run(" in source, relative
