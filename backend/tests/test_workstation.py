from __future__ import annotations

import json
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

from apps.tooling.assistant_runtime.state import RunStateStore, RUN_STATE
from apps.tooling import diagnostic_retrieval as retrieval
from apps.tooling.workstation import bound_query, public_snapshot, retrieval_plan, control_log_view


def query_args(**extra):
    return {"url": "http://10.0.0.1/case/", "start_time": "2026-09-23 10:00:00", "end_time": "2026-09-23 10:01:00",
            "targets": [{"subsystem": "spm", "module": "api"}], **extra}


@pytest.mark.parametrize("tier,seconds", [("narrow", 60), ("standard", 240), ("wide", 1200)])
def test_plan_preserves_anchor_and_timezone(tier, seconds):
    plan = retrieval_plan({"anchor_time": "2026-09-23T10:00:00+08:00", "reason": "缺失上游上下文", "tier": tier})
    assert (datetime.fromisoformat(plan["end_time"]) - datetime.fromisoformat(plan["start_time"])).total_seconds() == seconds
    assert plan["start_time"].endswith("+08:00")
    assert plan["correlation"] == "candidate"


@pytest.mark.parametrize("changes", [{"start_time": ""}, {"end_time": "2026-09-23 11:00:00"}, {"end_time": "2026-09-23 09:00:00"}, {"targets": []}, {"targets": [{"module": "x"}]}, {"targets": [{"subsystem": "s", "module": "m"}] * 5}])
def test_unsafe_query_rejected_before_io(changes):
    with pytest.raises(ValueError):
        bound_query(query_args(**changes), debug=True)


def test_budget_and_boolean_validation():
    assert bound_query(query_args(max_lines=50000), debug=True)["max_lines"] == 500
    with pytest.raises(ValueError):
        control_log_view({"task_id": "x", "errors_only": "false"})


def test_receipts_reject_duplicates_and_late_results():
    state = RunStateStore()
    state.register("run")
    state.expect_ui("run", "one")
    receipt = {"status": "success", "context": {"page": "logs"}}
    assert not state.acknowledge_ui("other", "one", receipt)
    assert state.acknowledge_ui("run", "one", receipt)
    assert not state.acknowledge_ui("run", "one", receipt)
    assert state.wait_ui("run", "one") == receipt
    assert not state.acknowledge_ui("run", "one", receipt)


def test_cancellation_and_missing_receipt_fail_closed():
    state = RunStateStore()
    state.register("run")
    state.expect_ui("run", "one")
    state.cancel("run")
    assert state.wait_ui("run", "one")["status"] == "failed"
    state.register("run")
    state.expect_ui("run", "two")
    assert state.wait_ui("run", "two", timeout=0)["status"] == "failed"
    assert not state._receipts


def test_public_records_redact_nested_credentials_and_bound_output():
    result = public_snapshot({"password": "sensitive", "items": [{"token": "private", "raw": "token=hidden hello"}]})
    text = json.dumps(result)
    assert not any(word in text for word in ("sensitive", "private", "hidden"))
    assert len(json.dumps(public_snapshot({"rows": ["x" * 5000] * 500}))) < 20000


def test_normalize_structured_and_unstructured_correlation():
    row = retrieval.correlate_row({"raw": '{"traceId":"abc", "errorCode":"E42", "component":"api", "timestamp":"2026-09-23T10:00:00Z"}'}, "case-a", "event")
    assert (row["trace_id"], row["error_code"], row["component"]) == ("abc", "E42", "api")
    other = retrieval.correlate_row({"raw": "errorCode=E42 traceId=abc"}, "case-b", "debug")
    assert other["trace_id"] == "abc"
    assert row["record_id"] != other["record_id"]
    assert retrieval.correlate_row({"raw": "failed"}, "case", "debug")["correlation_kind"] == "candidate"


def test_parallel_components_and_scoped_cache(monkeypatch):
    from apps.atlog import services
    retrieval._CACHE.clear()
    barrier = threading.Barrier(2, timeout=3)
    calls = []
    def read(payload):
        calls.append(payload)
        if len(calls) <= 2:
            barrier.wait()
        target = payload["targets"][0]["module"]
        return {"base_url": payload["url"], "rows": [{"raw": target, "time": payload["start_time"], "source_path": target}], "truncated": False}
    monkeypatch.setattr(services, "query_case_logs_tool", read)
    payload = query_args(targets=[{"subsystem": "s", "module": m} for m in ("api", "db")])
    result = retrieval.query_debug(payload)
    assert result["count"] == 2 and not result["cache_hit"]
    assert all(c["max_lines"] == 250 and c["include_event"] is False for c in calls)
    assert retrieval.query_debug(payload)["cache_hit"]
    retrieval.query_debug({**payload, "url": "http://10.0.0.1/other/"})
    assert len(calls) == 4


def test_partial_failure_is_visible_and_not_cached(monkeypatch):
    from apps.atlog import services
    retrieval._CACHE.clear()
    def read(payload):
        if payload["targets"][0]["module"] == "db":
            raise RuntimeError("component unavailable")
        return {"rows": [{"raw": "ERROR"}], "truncated": False}
    monkeypatch.setattr(services, "query_case_logs_tool", read)
    result = retrieval.query_debug(query_args(targets=[{"subsystem": "s", "module": m} for m in ("api", "db")]))
    assert result["partial_failure"] and result["component_errors"]
    assert not retrieval._CACHE


@pytest.mark.parametrize("receipt_status", ["success", "failed"])
def test_graph_waits_for_ui_and_refreshes_context(monkeypatch, receipt_status):
    from apps.tooling.ai_engine import graph
    from apps.tooling import assistant, registry
    events, calls = [], []
    RUN_STATE.register("test-ui-run")
    def emit(event):
        events.append(event)
        if event["type"] == "ui_action":
            assert not any(e["type"] == "tool_result" for e in events)
            RUN_STATE.acknowledge_ui("test-ui-run", event["action_id"], {"status": receipt_status, "detail": "receipt", "context": {"page": "logs"}})
    def execute(tool, args, **kwargs):
        calls.append(tool)
        return {"ui_action": {"type": "open_workspace_page", "page": "logs"}}
    # The fake kernel must mirror the real registry surface: `_execute` asks the registry
    # whether a tool is invokable rather than inspecting `tool.handler`.
    kernel = SimpleNamespace(
        registry=SimpleNamespace(
            get=lambda key: registry.get_tool(key),
            is_invokable=lambda tool: tool is not None and tool.handler is not None,
        ),
        context=SimpleNamespace(inject=lambda key, args, ctx: args),
        execute=execute,
    )
    state = {"run_id": "test-ui-run", "streaming": True, "context": {"ui_receipts": True}, "event_sink": emit,
             "kernel": kernel, "round_index": 1, "max_rounds": 5, "messages": [],
             "tool_calls": [{"id": "call", "function": {"name": "open_workspace_page", "arguments": '{"page":"logs"}'}}]}
    try:
        if receipt_status == "failed":
            with pytest.raises(assistant.AssistantError):
                graph._execute(state)
        else:
            result = graph._execute(state)
            assert state["context"]["page"] == "logs"
            assert any("最新页面状态" in m["content"] for m in result["messages"])
    finally:
        RUN_STATE.finish("test-ui-run")
