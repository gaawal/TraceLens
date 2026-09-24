"""Regression guard for reasoning-gateway tool_choice compatibility.

Reasoning / "thinking" models (DeepSeek ``deepseek-flash`` among them) answer HTTP 400
``Thinking mode does not support this tool_choice`` for ``tool_choice: "required"`` and
for forced-function choices. TracePilot's semantic router *requires* a forced choice, so
without the compatibility path every routing call burned all retry attempts behind
exponential backoff and then silently fell back to an unfiltered tool set.

These tests exercise the pure adaptation logic without touching the network or Django
settings, by building an uninitialised client with ``object.__new__``.
"""
from __future__ import annotations

import httpx
from openai import APIStatusError

from apps.tooling.llm.client import LLMClient

FORCED = {"type": "function", "function": {"name": "select_tracepilot_route"}}
REJECTION = "Thinking mode does not support this tool_choice"


def _client() -> LLMClient:
    client = object.__new__(LLMClient)
    client.model = "deepseek-flash"
    client._thinking_disabled = False
    client._forced_tool_choice_unsupported = False
    return client


def _status_error(status: int, message: str) -> APIStatusError:
    request = httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions")
    response = httpx.Response(status, request=request, json={"error": {"message": message}})
    return APIStatusError(message, response=response, body=None)


def test_tool_choice_rejection_is_recognised():
    assert LLMClient._rejects_tool_choice(_status_error(400, REJECTION)) is True
    # Non-400s and unrelated 400s must not be mistaken for a tool_choice problem.
    assert LLMClient._rejects_tool_choice(_status_error(500, REJECTION)) is False
    assert LLMClient._rejects_tool_choice(_status_error(400, "invalid api key")) is False


def test_first_degradation_disables_thinking_and_keeps_the_forced_choice():
    client = _client()
    kwargs = {"tool_choice": FORCED, "tools": [{"type": "function"}]}

    assert client._degrade_tool_choice(kwargs, _status_error(400, REJECTION)) is True
    # Leaving thinking mode is preferred: the forced route survives.
    assert kwargs["tool_choice"] == FORCED
    assert kwargs["extra_body"]["thinking"] == {"type": "disabled"}
    assert client._thinking_disabled is True
    assert client._forced_tool_choice_unsupported is False


def test_second_degradation_falls_back_to_auto_and_stops_retrying():
    client = _client()
    kwargs = {"tool_choice": FORCED}
    assert client._degrade_tool_choice(kwargs, _status_error(400, REJECTION)) is True
    assert client._degrade_tool_choice(kwargs, _status_error(400, REJECTION)) is True
    assert kwargs["tool_choice"] == "auto"
    assert client._forced_tool_choice_unsupported is True
    # Third time there is nothing left to adapt: surface the real error.
    assert client._degrade_tool_choice(kwargs, _status_error(400, REJECTION)) is False


def test_learned_degradation_is_reused_without_renegotiating():
    """A client that already learned the constraint must not send the rejected shape."""
    client = _client()
    client.model = "deepseek-flash"
    client._thinking_disabled = True
    client._forced_tool_choice_unsupported = True
    kwargs = client._request_kwargs("deepseek-flash", [{"role": "user", "content": "hi"}], None, FORCED)

    assert kwargs["tool_choice"] == "auto"
    assert kwargs["extra_body"]["thinking"] == {"type": "disabled"}
    # The provider-specific field must ride in extra_body; the SDK rejects unknown kwargs.
    assert "thinking" not in kwargs


def test_plain_requests_are_left_untouched():
    client = _client()
    kwargs = client._request_kwargs("deepseek-flash", [{"role": "user", "content": "hi"}], None, None)

    assert "tool_choice" not in kwargs
    assert "extra_body" not in kwargs
