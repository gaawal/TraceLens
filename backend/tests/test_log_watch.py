"""Tests for realtime log watches.

The watch feature exists because the browser's live view stops when the tab closes, so the
pieces that must not silently regress are: the server-side rule matcher (it has to agree with
the UI, or a tag lights up but never fires), the idempotency key, and the per-(environment,
target) tail coalescing that keeps SSH sessions from multiplying.
"""
from __future__ import annotations

import pytest

from apps.logsources.models import LogWatchHit
from apps.logsources.services.watch_events import WatchEventBus
from apps.logsources.services.watch_rules import (
    compile_display_rules,
    extract_display_rule_message,
    normalize_signature,
    rules_for_watch,
)

# Exactly eight leading [...] fields, as the builtin log format produces.
PREFIX = "[2026-09-23 23:28:00.000] [DEBUG] [cpfr] [cpfr] [T-1] [FN] [pid] [tag] "

KEYWORD_RULE = {
    "id": "r-kw",
    "name": "光源功率告警",
    "enabled": True,
    "kind": "keyword",
    "keyword": "illumination source power",
    "displayTemplate": "光源功率异常",
    "customLabelTemplate": "光源功率",
    "customLabelColor": "#e11d48",
    "showLabelOnTimeline": True,
}
TEMPLATE_RULE = {
    "id": "r-tpl",
    "name": "偏移量",
    "enabled": True,
    "kind": "template",
    "displayTemplate": "偏移 {offset}",
    "customLabelTemplate": "偏移 {offset}",
    "patternTokens": [
        {"kind": "text", "value": "alignment mark detected, offset compensation applied "},
        {"kind": "parameter", "parameterId": "p1"},
    ],
    "parameters": [{"id": "p1", "label": "offset", "sampleValue": "0x32f93"}],
}


def test_message_extraction_matches_the_frontend_contract():
    assert extract_display_rule_message(PREFIX + "hello world") == "hello world"
    # Seven fields: the eight-field strip must not apply.
    seven = "[a] [b] [c] [d] [e] [f] [g] body text"
    assert extract_display_rule_message(seven) == seven
    # Multi-line input uses the first non-empty line.
    assert extract_display_rule_message("\n\n" + PREFIX + "first\nsecond") == "first"


def test_keyword_rule_matches_case_sensitively_on_the_message_body():
    compiled, problems = compile_display_rules([KEYWORD_RULE])
    assert problems == []
    rule = compiled[0]
    assert rule.matches(PREFIX + "illumination source power stabilised") is True
    # The component field contains "cpfr", not the keyword: matching must use the body only.
    assert rule.matches(PREFIX + "unrelated message") is False
    assert rule.matches(PREFIX + "ILLUMINATION SOURCE POWER stabilised") is False


def test_template_rule_captures_the_whole_parameter():
    compiled, _ = compile_display_rules([TEMPLATE_RULE])
    match = compiled[0].matches(PREFIX + "alignment mark detected, offset compensation applied 0x32f93")
    assert match
    # The end anchor is what stops a non-greedy capture at one character.
    assert match.group(1) == "0x32f93"


def test_malformed_rules_are_reported_not_swallowed():
    """The frontend drops bad rules silently; a watcher must say why it observes nothing."""
    compiled, problems = compile_display_rules([
        {"id": "ok", "name": "ok", "enabled": True, "kind": "keyword", "keyword": "x"},
        {"id": "no-keyword", "name": "bad", "enabled": True, "kind": "keyword", "keyword": ""},
        {"id": "", "name": "no-id", "enabled": True, "kind": "keyword", "keyword": "y"},
        {"id": "disabled", "name": "off", "enabled": False, "kind": "keyword", "keyword": "z"},
    ])
    assert [rule.rule_id for rule in compiled] == ["ok"]
    # Disabled rules are inactive, not broken; an id-less rule is reported by position.
    reasons = {item["rule_id"] for item in problems}
    assert reasons == {"no-keyword", "<第 3 条规则>"}


def test_watch_selects_only_its_own_rule():
    rules = [KEYWORD_RULE, TEMPLATE_RULE]
    assert [rule.rule_id for rule in rules_for_watch(rules, "r-tpl")] == ["r-tpl"]
    assert len(rules_for_watch(rules, "")) == 2


def test_signature_ignores_prefix_and_volatile_numbers():
    a = normalize_signature(PREFIX + "retry 3 failed for lot 12345")
    b = normalize_signature("[2026-09-24 00:00:01.000] [ERROR] [hmi] [hmi] [T-9] [G] [pid] [tag] retry 9 failed for lot 99999")
    assert a == b == "retry # failed for lot #"


def test_dedup_key_is_stable_within_a_second_and_differs_across_seconds():
    from datetime import datetime, timezone

    base = datetime(2026, 9, 23, 12, 0, 0, 500000, tzinfo=timezone.utc)
    later = datetime(2026, 9, 23, 12, 0, 0, 900000, tzinfo=timezone.utc)
    next_second = datetime(2026, 9, 23, 12, 0, 1, tzinfo=timezone.utc)

    assert LogWatchHit.build_dedup_key(1, "sig", base) == LogWatchHit.build_dedup_key(1, "sig", later)
    assert LogWatchHit.build_dedup_key(1, "sig", base) != LogWatchHit.build_dedup_key(1, "sig", next_second)
    assert LogWatchHit.build_dedup_key(1, "sig", base) != LogWatchHit.build_dedup_key(2, "sig", base)


def test_tail_coalescing_key_covers_everything_that_changes_the_stream():
    """One SSH tail serves all watches on the same (environment, target, categories)."""
    from apps.logsources.services.watch_worker import TargetHub

    target = {"subsystem": "cpfr", "fm": "cpfr", "kind": "normal"}
    base = TargetHub.key_for(1, target, ["debug"])
    assert base == TargetHub.key_for(1, dict(target), ["debug"])
    assert base != TargetHub.key_for(2, target, ["debug"]), "different environments must not share a tail"
    assert base != TargetHub.key_for(1, {"subsystem": "hmi", "fm": "hmi"}, ["debug"])
    assert base != TargetHub.key_for(1, target, ["run"]), "different log categories need different tails"


@pytest.mark.skipif(not WatchEventBus.enabled(), reason="Redis is required for watch sequencing")
def test_sequence_is_monotonic_and_leases_are_exclusive():
    watch_id = 9900001
    first = WatchEventBus.next_seq(watch_id)
    second = WatchEventBus.next_seq(watch_id)
    assert second == first + 1

    try:
        assert WatchEventBus.try_claim_watch(watch_id, "worker-a") is True
        assert WatchEventBus.try_claim_watch(watch_id, "worker-b") is False
        assert WatchEventBus.lease_holder(watch_id) == "worker-a"
        # The loser must not be able to release someone else's lease.
        WatchEventBus.release_lease(watch_id, "worker-b")
        assert WatchEventBus.lease_holder(watch_id) == "worker-a"
        WatchEventBus.release_lease(watch_id, "worker-a")
        assert WatchEventBus.lease_holder(watch_id) == ""
    finally:
        WatchEventBus.release_lease(watch_id, "worker-a")


@pytest.mark.skipif(not WatchEventBus.enabled(), reason="Redis is required for the replay ring")
def test_replay_ring_resumes_from_a_sequence_number():
    watch_id = 9900002
    # The ring is real Redis state that outlives a test run, so clear it first — otherwise a
    # previous run's entries look like a replay bug.
    client = WatchEventBus._client()
    client.delete(f"tracelens:v3:watch:{watch_id}:ring")
    try:
        WatchEventBus.publish_hit({"watch_id": watch_id, "seq": 11, "signature": "a"})
        WatchEventBus.publish_hit({"watch_id": watch_id, "seq": 12, "signature": "b"})
        assert [item["seq"] for item in WatchEventBus.recent_hits(watch_id, 0)] == [11, 12]
        assert [item["seq"] for item in WatchEventBus.recent_hits(watch_id, 11)] == [12]
        assert WatchEventBus.recent_hits(watch_id, 12) == []
    finally:
        client.delete(f"tracelens:v3:watch:{watch_id}:ring")
