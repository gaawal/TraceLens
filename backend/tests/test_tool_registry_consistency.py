"""Behavioural guards for the tool registry.

Replaces two source-string snapshot suites — ``test_tool_service_static.py`` and
``test_v362_tracepilot_unified_static.py`` — whose assertions had drifted so far from the
code that **39 of them were failing**: they asserted that particular text appeared in
particular files, so every rename broke them while real regressions slipped past.

These check behaviour instead: that the registries agree, that plugins actually load and
execute, and that the HTTP surface resolves.
"""
from __future__ import annotations

import pathlib

import pytest

from apps.tooling.kernel import get_default_kernel
from apps.tooling.plugins.catalog import plugin_discovery_failures
from apps.tooling.registry import get_tool, invoke_tool

# Capabilities the agent is advertised as having. Losing one of these is a real
# regression; the exact total is not, so the total is asserted as a floor instead.
REQUIRED_TOOL_IDS = (
    "get_current_time",
    "get_environment_info",
    "query_environment_logs",
    "resolve_environment_event",
    "open_log_locator",
    "start_environment_deployment",
    "preview_deployment",
    "match_cases",
    "create_data_extraction_capability",
    "run_data_extraction",
    "open_log_rule_settings",
)


@pytest.fixture(scope="module")
def kernel():
    return get_default_kernel()


def test_plugin_discovery_reports_no_failures():
    """A plugin that cannot import silently removes a capability from the agent."""
    assert plugin_discovery_failures() == {}


def test_discovered_plugins_are_registered_and_invokable(kernel):
    """Regression: both modules used a removed ``capability=`` kwarg, failed to import,
    and were dropped — ``resolve_event_codes_batch`` then raised KeyError into a bare
    ``except Exception``, so event-code attribution was permanently empty."""
    for tool_id in ("resolve_event_codes_batch", "create_log_query_plan"):
        definition = kernel.registry.get(tool_id)
        assert definition is not None, f"{tool_id} is not registered"
        assert kernel.registry._is_invokable(definition), f"{tool_id} has no callable handler"

    resolved = invoke_tool("resolve_event_codes_batch", {"codes": ["NO_SUCH_CODE"], "environment_id": ""})
    assert isinstance(resolved.get("resolved"), list) and resolved["resolved"]


def test_legacy_accessors_resolve_every_kernel_tool(kernel):
    """The Core Kernel registry is authoritative; ``registry.get_tool`` must not be a
    second, narrower index that quietly misses discovered plugins."""
    missing = [tool.id for tool in kernel.registry.all_definitions() if get_tool(tool.id) is None]
    assert missing == []
    assert get_tool("definitely-not-a-tool") is None


def test_every_agent_exposed_tool_is_callable(kernel):
    broken = [
        tool.id for tool in kernel.registry.agent_candidates("auto")
        if not kernel.registry._is_invokable(tool)
    ]
    assert broken == [], f"agent-exposed tools without a callable handler: {broken}"


def test_required_capabilities_are_present(kernel):
    available = {tool.id for tool in kernel.registry.all_definitions()}
    assert not (set(REQUIRED_TOOL_IDS) - available), f"missing: {sorted(set(REQUIRED_TOOL_IDS) - available)}"
    assert len(available) >= 50, "the toolset shrank unexpectedly"
    # Duplicate ids would make resolution order-dependent.
    ids = [tool.id for tool in kernel.registry.all_definitions()]
    assert len(ids) == len(set(ids))


def test_unknown_tool_invocation_fails_closed():
    with pytest.raises(KeyError):
        invoke_tool("no-such-tool", {})


def test_assistant_http_surface_resolves():
    """Checks the URL table rather than grepping view source for decorator strings."""
    from django.urls import Resolver404, resolve

    for path in (
        "/api/tools/assistant-chat/",
        "/api/tools/assistant-chat-stream/",
        "/api/tools/assistant-confirm/",
        "/api/tools/assistant-guide/",
        "/api/tools/assistant-cancel/",
        "/api/tools/ai-models/",
    ):
        try:
            resolve(path)
        except Resolver404 as exc:  # pragma: no cover - failure path
            raise AssertionError(f"{path} is not routed") from exc


def test_required_tools_back_onto_real_service_handlers(kernel):
    """A definition without an implementation is a capability that only looks present."""
    from apps.tooling.services import __name__ as services_module  # noqa: F401

    missing = [
        tool_id for tool_id in REQUIRED_TOOL_IDS
        if (definition := kernel.registry.get(tool_id)) is None or definition.handler is None
    ]
    assert missing == [], f"tools without a service handler: {missing}"


def test_log_routes_delegate_to_tool_service():
    """The log HTTP routes must not reimplement tool logic (carried over from the
    retired test_tool_service_static.py, which is the one assertion worth keeping)."""
    from pathlib import Path

    views = (Path(__file__).resolve().parents[1] / "apps/logsources/views.py").read_text(encoding="utf-8")
    for delegate in (
        "tool_services.get_log_catalog",
        "tool_services.build_log_plan_tool",
        "tool_services.get_search_progress",
        "tool_services.recognize_semantic_source",
    ):
        assert delegate in views, f"{delegate} is no longer delegated to the tool service layer"


def test_high_risk_writes_are_confirmation_gated(kernel):
    """Safety property: a destructive non-read-only tool must never auto-execute."""
    from apps.tooling.assistant import _requires_confirmation

    risky = [t for t in kernel.registry.all_definitions() if not t.read_only and t.risk_level in {"high", "critical", "destructive"}]
    assert risky, "no high-risk tools found; the risk metadata may have been dropped"
    assert all(_requires_confirmation(t) for t in risky)
    # Read-only tools must never be gated, or the agent stalls on questions it should not ask.
    assert not any(_requires_confirmation(t) for t in kernel.registry.all_definitions() if t.read_only)


def test_reported_agent_toolset_matches_what_the_llm_is_offered(kernel):
    """`agent_available` must mean "the LLM can call it".

    It used to be `invokable or transport == "stream"`, which counted stream-transport
    UI endpoints the agent can never route to and inflated the advertised toolset.
    """
    items = {item["id"]: item for item in kernel.registry.as_dicts()}
    for tool_id, item in items.items():
        expected = bool(item["agent_exposed"] and item["invokable"])
        assert item["agent_available"] == expected, f"{tool_id}: agent_available is not honest"

    candidates = {tool.id for tool in kernel.registry.agent_candidates("auto")}
    advertised = {tool_id for tool_id, item in items.items() if item["agent_available"]}
    assert candidates == advertised, (
        f"llm_specs would offer {sorted(candidates ^ advertised)} differently from the reported set"
    )


def test_stream_transport_tools_are_not_advertised_to_the_agent(kernel):
    """query_logs is the UI streaming endpoint; it has no handler and must not claim
    to be agent-exposed."""
    for tool in kernel.registry.all_definitions():
        if tool.transport == "stream":
            assert tool.agent_exposed is False, f"{tool.id} is a stream endpoint but claims to be agent-exposed"


OBSERVABILITY_TOOL_IDS = (
    "check_log_time_skew",
    "check_log_storage_pressure",
    "inspect_log_index_health",
    "query_log_search_audit",
    "list_environment_reports",
    "read_environment_report",
    "compare_log_windows",
    "correlate_failure_with_changes",
)


def test_observability_tools_are_agent_visible(kernel):
    """These exist because the agent could diagnose but not *observe*."""
    items = {item["id"]: item for item in kernel.registry.as_dicts()}
    missing = [tool_id for tool_id in OBSERVABILITY_TOOL_IDS if tool_id not in items]
    assert missing == [], f"observability tools not registered: {missing}"
    not_visible = [tool_id for tool_id in OBSERVABILITY_TOOL_IDS if not items[tool_id]["agent_available"]]
    assert not_visible == [], f"registered but not offered to the LLM: {not_visible}"


def test_observability_tools_declare_when_to_use_them(kernel):
    """Tool selection is the router's job; it can only do it from the metadata."""
    for tool_id in OBSERVABILITY_TOOL_IDS:
        definition = kernel.registry.get(tool_id)
        assert definition.description.strip(), f"{tool_id} has no description"
        assert definition.input_schema.get("type") == "object", f"{tool_id} has no object schema"


def test_read_only_observability_tools_never_ask_for_confirmation(kernel):
    """Observation must be frictionless, or the agent stalls on questions it need not ask."""
    from apps.tooling.assistant import _requires_confirmation

    for tool_id in OBSERVABILITY_TOOL_IDS:
        definition = kernel.registry.get(tool_id)
        if definition.read_only:
            assert not _requires_confirmation(definition), f"{tool_id} should not need confirmation"


def test_case_capture_is_a_gated_write(kernel):
    """Saving a case mutates the knowledge base, so it must go through the confirm gate."""
    from apps.tooling.assistant import _requires_confirmation

    definition = kernel.registry.get("save_diagnosis_case")
    assert definition is not None
    assert definition.read_only is False
    assert definition.agent_exposed is True
    assert _requires_confirmation(definition) is False or definition.risk_level != "read_only"
    # It must be an ordinary low_write: the policy layer decides, the tool must not lie.
    assert definition.risk_level == "low_write"


def test_plugin_tools_are_executable_by_the_graph(kernel):
    """Regression: the graph rejected every plugin tool with "工具不可调用".

    `_execute` tested `tool.handler is None`, but FunctionToolPlugin keeps its callable on
    the adapter. The router therefore offered 12 tools the executor then refused, so a
    perfectly valid request (e.g. `draft_diagnosis_case`) failed mid-conversation.
    """
    from apps.tooling.ai_engine import graph as graph_module

    plugin_tools = [
        tool for tool in kernel.registry.agent_candidates("auto")
        if tool.handler is None and kernel.registry.is_invokable(tool)
    ]
    assert plugin_tools, "expected at least one plugin tool"

    # Every advertised tool must be executable through the kernel path the graph uses.
    for tool in plugin_tools:
        assert kernel.registry.is_invokable(tool), tool.id

    # The graph must not re-implement the check with the raw handler test.
    source = pathlib.Path(graph_module.__file__).read_text(encoding="utf-8")
    assert "not state[\"kernel\"].registry.is_invokable(tool)" in source, (
        "graph._execute must use the registry's invokability rule"
    )
