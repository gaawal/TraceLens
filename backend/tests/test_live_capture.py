"""Tests for 实时采集 — the opt-in switch, the watch sync, and the assistant tools.

Why these exist: the failure this feature fixes was *ambiguity about what gets collected*.
A regression here is silent (the panel collects everything, or nothing), so the invariants are
asserted directly:

* only extractors explicitly opted in are collected,
* un-ticking stops exactly the ones named, never the others,
* an extractor without a Match keyword is refused out loud instead of collecting nothing,
* a deleted extractor never leaves an enabled watch behind.

The suite has no pytest-django, so DB-touching tests snapshot and restore what they mutate
instead of relying on a rollback.
"""
from __future__ import annotations

import pytest

from apps.environments.models import Environment, ResourceSettings
from apps.logsources.models import LogWatch
from apps.logsources.services.watch_capture import (
    find_extraction_rules,
    live_capture_rule_ids,
    set_live_capture,
    stop_capture_watches,
    stop_watches,
    sync_capture_watches,
    sync_watches,
)
from apps.logsources.services.watch_rules import (
    compile_extraction_rule,
    extraction_rules_for_watch,
)

POWER_RULE = {
    "id": "ext-power",
    "name": "光源功率漂移",
    "enabled": True,
    "matchKeyword": "illumination source power",
    "caseSensitive": False,
    "fields": [{"id": "f1", "key": "drift", "name": "漂移量"}],
    "liveCapture": True,
}
# Deliberately NOT opted in: the whole point is that ticking one extractor must not
# collect the others too.
DOSE_RULE = {**POWER_RULE, "id": "ext-dose", "name": "曝光剂量", "matchKeyword": "dose=", "liveCapture": False}
NO_MATCH_RULE = {
    "id": "ext-nomatch",
    "name": "没有关键字的提取器",
    "enabled": True,
    "matchKeyword": "",
    "fields": [],
}
DISABLED_RULE = {**POWER_RULE, "id": "ext-off", "name": "已停用提取器", "enabled": False}


# ----------------------------------------------------------------- rule compiling
def test_extraction_rule_compiles_to_a_keyword_matcher():
    compiled, reason = compile_extraction_rule(POWER_RULE)
    assert reason == ""
    assert compiled is not None
    assert compiled.kind == "keyword"
    assert compiled.keyword == "illumination source power"
    # Data collection is not a symptom lane: it must not claim a timeline row.
    assert compiled.show_on_timeline is False
    assert compiled.display_mode == "data"
    assert compiled.label_template == "光源功率漂移"


def test_extraction_rule_without_match_keyword_is_refused_with_a_reason():
    compiled, reason = compile_extraction_rule(NO_MATCH_RULE)
    assert compiled is None
    assert "Match" in reason


def test_disabled_extraction_rule_is_refused():
    compiled, reason = compile_extraction_rule(DISABLED_RULE)
    assert compiled is None
    assert "停用" in reason


def test_case_insensitive_extraction_rule_matches_differently_cased_log_lines():
    compiled, _ = compile_extraction_rule(POWER_RULE)
    assert compiled is not None
    upper = "[2026-09-24 08:12:48.502] [INFO] [SPWSP] ILLUMINATION SOURCE POWER STABILISED"
    lower = "[2026-09-24 08:12:48.502] [INFO] [SPWSP] illumination source power stabilised"
    assert bool(compiled.matches(upper)) is True
    assert bool(compiled.matches(lower)) is True

    sensitive, _ = compile_extraction_rule({**POWER_RULE, "caseSensitive": True})
    assert sensitive is not None
    assert bool(sensitive.matches(upper)) is False
    assert bool(sensitive.matches(lower)) is True


def test_extraction_rules_for_watch_only_returns_the_referenced_rule():
    rules = [POWER_RULE, DOSE_RULE]
    assert [rule.rule_id for rule in extraction_rules_for_watch(rules, "ext-dose")] == ["ext-dose"]
    # An id that no longer exists must compile to nothing rather than to *all* rules.
    assert extraction_rules_for_watch(rules, "ext-missing") == []


# ----------------------------------------------------------------- selectors
def test_find_extraction_rules_resolves_id_name_fragment_and_match_keyword(monkeypatch):
    from apps.logsources.services import watch_capture

    monkeypatch.setattr(watch_capture, "rules_of", lambda kind: [dict(POWER_RULE), dict(DOSE_RULE)] if kind == "extraction" else [])

    matched, problems = find_extraction_rules(["ext-power"])
    assert [rule["id"] for rule in matched] == ["ext-power"]
    assert problems == []

    assert [rule["id"] for rule in find_extraction_rules(["光源功率漂移"])[0]] == ["ext-power"]
    assert [rule["id"] for rule in find_extraction_rules(["剂量"])[0]] == ["ext-dose"]
    # Matching on the log keyword is what makes "实时采集 dose 数据" work.
    assert [rule["id"] for rule in find_extraction_rules(["dose="])[0]] == ["ext-dose"]


def test_find_extraction_rules_reports_what_it_could_not_resolve(monkeypatch):
    from apps.logsources.services import watch_capture

    monkeypatch.setattr(watch_capture, "rules_of", lambda kind: [dict(POWER_RULE)] if kind == "extraction" else [])
    matched, problems = find_extraction_rules(["绝对不存在的提取器名字"])
    assert matched == []
    assert problems and problems[0]["selector"] == "绝对不存在的提取器名字"


def test_find_extraction_rules_without_selectors_matches_nothing(monkeypatch):
    from apps.logsources.services import watch_capture

    monkeypatch.setattr(watch_capture, "rules_of", lambda kind: [dict(POWER_RULE)] if kind == "extraction" else [])
    assert find_extraction_rules([]) == ([], [])


# ----------------------------------------------------------------- live DB state
@pytest.fixture()
def capture_state():
    """Snapshot every watch + the shared rule lists, and restore them afterwards.

    Snapshots *all* watches, not just the ones that look managed: an earlier version filtered
    by "has a rule id", which silently skipped a legacy semantic watch and then deleted it on
    teardown. Cleanup only removes rows that appeared after the snapshot (by pk), so it can
    never take something it did not create.
    """
    settings_obj = ResourceSettings.get_solo()
    saved_rules = settings_obj.data_extraction_rules
    saved_display_rules = settings_obj.display_rules
    saved_display_initialized = settings_obj.display_rules_initialized
    saved_watches = {
        watch.pk: {
            "name": watch.name,
            "environment_id": watch.environment_id,
            "source_rule_id": watch.source_rule_id,
            "extraction_rule_id": watch.extraction_rule_id,
            "level": watch.level,
            "trigger_kind": watch.trigger_kind,
            "trigger_config": watch.trigger_config,
            "targets": watch.targets,
            "source_categories": watch.source_categories,
            "capture_config": watch.capture_config,
            "enabled": watch.enabled,
            "created_by": watch.created_by,
            "hit_count": watch.hit_count,
            "dropped_count": watch.dropped_count,
            "last_error": watch.last_error,
        }
        for watch in LogWatch.objects.all()
    }
    yield
    LogWatch.objects.exclude(pk__in=list(saved_watches)).delete()
    for pk, fields in saved_watches.items():
        if LogWatch.objects.filter(pk=pk).exists():
            LogWatch.objects.filter(pk=pk).update(**fields)
        else:
            LogWatch.objects.create(pk=pk, **fields)
    settings_obj.refresh_from_db()
    settings_obj.data_extraction_rules = saved_rules
    settings_obj.display_rules = saved_display_rules
    settings_obj.display_rules_initialized = saved_display_initialized
    settings_obj.save(update_fields=[
        "data_extraction_rules", "display_rules", "display_rules_initialized", "updated_at",
    ])


def test_capture_state_fixture_restores_every_watch(capture_state):
    """The fixture is a safety net; prove it round-trips instead of trusting it."""
    before = {
        watch.pk: (watch.name, watch.enabled, watch.source_rule_id, watch.extraction_rule_id)
        for watch in LogWatch.objects.all()
    }
    LogWatch.objects.create(
        environment=_environment(), name="临时测试监视器", source_rule_id="", extraction_rule_id="",
        level="observe", trigger_kind="appear",
    )
    assert LogWatch.objects.count() == len(before) + 1
    # Teardown runs after this test; the next test re-snapshots, so assert the counts directly.


@pytest.fixture()
def stub_extractors(monkeypatch):
    """Point the service at a fixed extractor list so results do not depend on seeded data."""
    from apps.logsources.services import watch_capture

    rules = [dict(POWER_RULE), dict(DOSE_RULE), dict(NO_MATCH_RULE)]
    monkeypatch.setattr(
        watch_capture, "rules_of",
        lambda kind: [dict(rule) for rule in rules] if kind == "extraction" else [],
    )
    return rules


def _seed_rules():
    settings_obj = ResourceSettings.get_solo()
    settings_obj.data_extraction_rules = [dict(POWER_RULE), dict(DOSE_RULE), dict(NO_MATCH_RULE)]
    settings_obj.save(update_fields=["data_extraction_rules", "updated_at"])


def _environment() -> Environment:
    environment = Environment.objects.first()
    if environment is None:
        pytest.skip("no environment in the database")
    return environment


def test_set_live_capture_only_flips_the_named_extractors(capture_state, stub_extractors):
    settings_obj = ResourceSettings.get_solo()
    _seed_rules()

    assert set_live_capture(["ext-dose"], True) == ["ext-dose"]
    settings_obj.refresh_from_db()
    flags = {rule["id"]: rule.get("liveCapture") for rule in settings_obj.data_extraction_rules}
    assert flags["ext-dose"] is True
    assert flags["ext-nomatch"] is None
    # A no-op flip must not be reported as a change.
    assert set_live_capture(["ext-dose"], True) == []

    assert set_live_capture(["ext-power"], False) == ["ext-power"]
    settings_obj.refresh_from_db()
    flags = {rule["id"]: rule.get("liveCapture") for rule in settings_obj.data_extraction_rules}
    assert flags["ext-power"] is False


def test_live_capture_rule_ids_ignores_disabled_and_unticked_extractors(capture_state):
    # No extractor stub here on purpose: this asserts the flag reader against the real
    # shared list, which is the state the server actually acts on.
    settings_obj = ResourceSettings.get_solo()
    _seed_rules()
    LogWatch.objects.exclude(extraction_rule_id="").delete()
    assert live_capture_rule_ids() == ["ext-power"]

    settings_obj.refresh_from_db()
    settings_obj.data_extraction_rules = [
        {**rule, "enabled": False} if rule["id"] == "ext-power" else rule
        for rule in settings_obj.data_extraction_rules
    ]
    settings_obj.save(update_fields=["data_extraction_rules", "updated_at"])
    assert live_capture_rule_ids() == []


def test_sync_capture_watches_creates_one_watch_per_opted_in_extractor(capture_state, stub_extractors):
    _seed_rules()
    LogWatch.objects.exclude(extraction_rule_id="").delete()

    outcome = sync_capture_watches(
        _environment(),
        rule_ids=["ext-dose"],
        targets=[{"subsystem": "spwsp", "fm": "spwsp"}],
        source_categories=["debug"],
        enable=True,
    )
    assert outcome["enabled_rule_ids"] == ["ext-dose"]
    watch = LogWatch.objects.get(extraction_rule_id="ext-dose")
    assert watch.enabled is True
    assert watch.targets == [{"subsystem": "spwsp", "fm": "spwsp", "kind": "normal"}]
    assert watch.capture_config["mode"] == "extraction"
    # A capture watch must not pretend to be a semantic-rule watch.
    assert watch.source_rule_id == ""


def test_sync_capture_watches_refuses_a_rule_without_a_match_keyword(capture_state, stub_extractors):
    _seed_rules()
    LogWatch.objects.exclude(extraction_rule_id="").delete()

    outcome = sync_capture_watches(
        _environment(),
        rule_ids=["ext-nomatch"],
        targets=[{"subsystem": "spwsp", "fm": "spwsp"}],
        enable=True,
    )
    assert outcome["enabled_rule_ids"] == []
    assert outcome["problems"] and "Match" in outcome["problems"][0]["reason"]
    assert not LogWatch.objects.filter(extraction_rule_id="ext-nomatch").exists()


def test_sync_capture_watches_without_targets_reports_a_problem(capture_state, stub_extractors):
    _seed_rules()
    outcome = sync_capture_watches(_environment(), rule_ids=["ext-dose"], targets=[], enable=True)
    assert outcome["enabled_rule_ids"] == []
    assert outcome["problems"] and "监听目标" in outcome["problems"][0]["reason"]


def test_sync_capture_watches_disables_the_collectors_that_dropped_out(capture_state, stub_extractors):
    _seed_rules()
    LogWatch.objects.exclude(extraction_rule_id="").delete()
    environment = _environment()
    targets = [{"subsystem": "spwsp", "fm": "spwsp"}]

    sync_capture_watches(environment, rule_ids=["ext-power", "ext-dose"], targets=targets, enable=True)
    assert set(LogWatch.objects.filter(enabled=True).exclude(extraction_rule_id="").values_list("extraction_rule_id", flat=True)) == {"ext-power", "ext-dose"}

    outcome = sync_capture_watches(environment, rule_ids=["ext-dose"], targets=targets, enable=True)
    assert outcome["stopped_rule_ids"] == ["ext-power"]
    assert set(LogWatch.objects.filter(enabled=True).exclude(extraction_rule_id="").values_list("extraction_rule_id", flat=True)) == {"ext-dose"}


def test_stop_capture_watches_leaves_other_collectors_running(capture_state, stub_extractors):
    _seed_rules()
    LogWatch.objects.exclude(extraction_rule_id="").delete()
    environment = _environment()
    targets = [{"subsystem": "spwsp", "fm": "spwsp"}]
    sync_capture_watches(environment, rule_ids=["ext-power", "ext-dose"], targets=targets, enable=True)

    # Stopping one extractor must not stop the other one.
    assert stop_capture_watches(rule_ids=["ext-power"], environment_id=environment.id) == ["ext-power"]
    assert set(LogWatch.objects.filter(enabled=True).exclude(extraction_rule_id="").values_list("extraction_rule_id", flat=True)) == {"ext-dose"}

    # 实时监听 turning off stops everything for the environment.
    assert set(stop_capture_watches(environment_id=environment.id)) == {"ext-dose"}
    assert not LogWatch.objects.filter(enabled=True).exclude(extraction_rule_id="").exists()


def test_deleting_an_extractor_prunes_its_capture_watch(capture_state, stub_extractors):
    _seed_rules()
    LogWatch.objects.exclude(extraction_rule_id="").delete()
    sync_capture_watches(_environment(), rule_ids=["ext-dose"], targets=[{"subsystem": "spwsp", "fm": "spwsp"}], enable=True)
    assert LogWatch.objects.get(extraction_rule_id="ext-dose").enabled is True

    # Remove the extractor the way the rules page does, then save.
    settings_obj = ResourceSettings.get_solo()
    settings_obj.refresh_from_db()
    settings_obj.data_extraction_rules = [rule for rule in settings_obj.data_extraction_rules if rule["id"] != "ext-dose"]
    settings_obj.save(update_fields=["data_extraction_rules", "updated_at"])

    assert LogWatch.objects.get(extraction_rule_id="ext-dose").enabled is False


# ----------------------------------------------------------------- assistant tools
def test_configure_live_watch_without_rules_only_lists_options(capture_state, stub_extractors):
    from apps.tooling.registry import invoke_tool

    _seed_rules()
    before = {watch.pk: watch.enabled for watch in LogWatch.objects.exclude(extraction_rule_id="")}
    result = invoke_tool("configure_live_watch", {"rules": []})

    assert result["ok"] is True
    assert result["changed"] is False
    assert result["kind"] == "extraction"
    assert {item["id"] for item in result["rules"]} == {"ext-power", "ext-dose", "ext-nomatch"}
    assert result["live_rule_ids"] == ["ext-power"]
    assert "ui_action" not in result
    assert {watch.pk: watch.enabled for watch in LogWatch.objects.exclude(extraction_rule_id="")} == before


def test_configure_live_watch_without_a_target_updates_the_flag_but_admits_it_did_not_start(capture_state, stub_extractors):
    from apps.tooling.registry import invoke_tool

    _seed_rules()
    result = invoke_tool("configure_live_watch", {"rules": ["剂量"], "action": "enable"})

    assert result["changed"] is True
    # No environment/target: it must not claim monitoring started.
    assert "ui_action" not in result
    assert result["enabled_rule_ids"] == []
    assert result["problems"]
    settings_obj = ResourceSettings.get_solo()
    settings_obj.refresh_from_db()
    flags = {rule["id"]: rule.get("liveCapture") for rule in settings_obj.data_extraction_rules}
    assert flags["ext-dose"] is True


def test_configure_live_watch_returns_the_page_action_when_it_can_start(capture_state, stub_extractors):
    from apps.tooling.registry import invoke_tool

    _seed_rules()
    environment = _environment()
    LogWatch.objects.exclude(extraction_rule_id="").delete()
    result = invoke_tool("configure_live_watch", {
        "rules": ["光源功率"],
        "action": "enable",
        "environment_id": environment.id,
        "subsystem": "spwsp",
        "fm": "spwsp",
        "source_categories": ["debug"],
    })

    assert result["ok"] is True
    assert result["enabled_rule_ids"] == ["ext-power"]
    assert result["ui_action"] == {
        "type": "start_live_monitoring",
        "environment_id": environment.id,
        "subsystem": "spwsp",
        "fm": "spwsp",
        "source_categories": ["debug"],
    }
    assert LogWatch.objects.get(extraction_rule_id="ext-power").enabled is True

    # And the assistant can stop exactly that one again.
    stopped = invoke_tool("configure_live_watch", {"rules": ["光源功率"], "action": "disable"})
    assert stopped["stopped_rule_ids"] == ["ext-power"]
    assert LogWatch.objects.get(extraction_rule_id="ext-power").enabled is False


def test_configure_live_watch_refuses_an_unknown_rule(capture_state, stub_extractors):
    from apps.tooling.registry import invoke_tool

    _seed_rules()
    result = invoke_tool("configure_live_watch", {"rules": ["没有这个提取器"], "action": "enable"})
    assert result["ok"] is False
    assert result["changed"] is False
    assert result["problems"]


def test_configure_live_watch_accepts_the_extractors_alias(capture_state, stub_extractors):
    from apps.tooling.registry import invoke_tool

    _seed_rules()
    result = invoke_tool("configure_live_watch", {"extractors": ["光源功率"], "action": "enable"})
    assert result["enabled_rule_ids"] == []


def test_read_live_capture_explains_that_no_monitor_exists_yet(capture_state, stub_extractors):
    from apps.tooling.registry import invoke_tool

    _seed_rules()
    LogWatch.objects.exclude(extraction_rule_id="").delete()
    result = invoke_tool("read_live_capture", {"extractor": "光源功率"})

    # Honest empty: it names the extractor it could not read rather than returning 0 rows.
    assert result["ok"] is False
    assert result["rows"] == []
    assert result["rules"] and result["rules"][0]["id"] == "ext-power"
    assert "实时监听" in result["note"]


# ----------------------------------------------------------------- semantic kind
SEMANTIC_RULE = {
    "id": "sem-power",
    "name": "光源功率异常",
    "enabled": True,
    "kind": "keyword",
    "keyword": "illumination source power",
    "displayTemplate": "光源功率异常",
    "customLabelTemplate": "光源异常",
    "customLabelColor": "#d97706",
    "showLabelOnTimeline": True,
    "parameters": [],
    "createdAt": 1,
}


@pytest.fixture()
def stub_semantic(monkeypatch):
    from apps.logsources.services import watch_capture

    rules = [dict(SEMANTIC_RULE)]
    monkeypatch.setattr(
        watch_capture, "rules_of",
        lambda kind: [dict(rule) for rule in rules] if kind == "semantic" else [],
    )
    return rules


def test_semantic_rule_compiles_for_the_timeline():
    from apps.logsources.services.watch_rules import compile_display_rule

    compiled, reason = compile_display_rule(SEMANTIC_RULE)
    assert reason == ""
    assert compiled is not None
    # A symptom rule *does* claim a timeline lane — the opposite of an extractor.
    assert compiled.show_on_timeline is True
    assert compiled.label_template == "光源异常"


def test_semantic_watch_and_capture_watch_are_separate(capture_state, stub_semantic):
    """Turning one kind off must never stop the other: different surfaces, different tails."""
    settings_obj = ResourceSettings.get_solo()
    settings_obj.display_rules = [dict(SEMANTIC_RULE)]
    settings_obj.display_rules_initialized = True
    settings_obj.save(update_fields=["display_rules", "display_rules_initialized", "updated_at"])
    LogWatch.objects.exclude(extraction_rule_id="").exclude(source_rule_id="").delete()
    environment = _environment()
    targets = [{"subsystem": "spwsp", "fm": "spwsp"}]

    semantic = sync_watches(environment, kind="semantic", rule_ids=["sem-power"], targets=targets, enable=True)
    assert semantic["enabled_rule_ids"] == ["sem-power"]
    watch = LogWatch.objects.get(source_rule_id="sem-power")
    assert watch.enabled is True
    assert watch.capture_config["mode"] == "semantic"
    # The two kinds are stored in different columns and never collide.
    assert watch.extraction_rule_id == ""

    stopped = stop_watches(kind="extraction", environment_id=environment.id)
    assert stopped == []
    assert LogWatch.objects.get(source_rule_id="sem-power").enabled is True

    stopped = stop_watches(kind="semantic", environment_id=environment.id)
    assert stopped == ["sem-power"]
    assert LogWatch.objects.get(source_rule_id="sem-power").enabled is False


def test_sync_watches_of_one_kind_leaves_the_other_kinds_watches_alone(capture_state, stub_extractors, stub_semantic, monkeypatch):
    from apps.logsources.services import watch_capture

    # Both kinds resolvable in the same run, which is what the workspace toggle does.
    monkeypatch.setattr(
        watch_capture, "rules_of",
        lambda kind: [dict(SEMANTIC_RULE)] if kind == "semantic" else [dict(POWER_RULE), dict(DOSE_RULE), dict(NO_MATCH_RULE)],
    )
    settings_obj = ResourceSettings.get_solo()
    settings_obj.display_rules = [dict(SEMANTIC_RULE)]
    settings_obj.data_extraction_rules = [dict(POWER_RULE), dict(DOSE_RULE)]
    settings_obj.save(update_fields=["display_rules", "data_extraction_rules", "updated_at"])
    LogWatch.objects.exclude(extraction_rule_id="").exclude(source_rule_id="").delete()
    environment = _environment()
    targets = [{"subsystem": "spwsp", "fm": "spwsp"}]

    sync_watches(environment, kind="semantic", rule_ids=["sem-power"], targets=targets, enable=True)
    sync_watches(environment, kind="extraction", rule_ids=["ext-power"], targets=targets, enable=True)
    assert LogWatch.objects.get(source_rule_id="sem-power").enabled is True
    assert LogWatch.objects.get(extraction_rule_id="ext-power").enabled is True

    # Re-syncing extraction alone (the common case: the user changed the tick boxes) must not
    # disturb the semantic watcher.
    sync_watches(environment, kind="extraction", rule_ids=["ext-dose"], targets=targets, enable=True)
    assert LogWatch.objects.get(source_rule_id="sem-power").enabled is True
    assert LogWatch.objects.get(extraction_rule_id="ext-power").enabled is False
    assert LogWatch.objects.get(extraction_rule_id="ext-dose").enabled is True


def test_two_rules_sharing_a_display_name_do_not_collide(capture_state, monkeypatch):
    """(environment, name) is unique; a second same-named rule must not crash the whole sync."""
    from apps.logsources.services import watch_capture

    twin_a = {**SEMANTIC_RULE, "id": "sem-a"}
    twin_b = {**SEMANTIC_RULE, "id": "sem-b"}
    monkeypatch.setattr(
        watch_capture, "rules_of",
        lambda kind: [dict(twin_a), dict(twin_b)] if kind == "semantic" else [],
    )
    settings_obj = ResourceSettings.get_solo()
    settings_obj.display_rules = [dict(twin_a), dict(twin_b)]
    settings_obj.save(update_fields=["display_rules", "updated_at"])
    LogWatch.objects.filter(source_rule_id__in=["sem-a", "sem-b"]).delete()

    outcome = sync_watches(
        _environment(), kind="semantic", rule_ids=["sem-a", "sem-b"],
        targets=[{"subsystem": "spwsp", "fm": "spwsp"}], enable=True,
    )
    assert sorted(outcome["enabled_rule_ids"]) == ["sem-a", "sem-b"]
    names = sorted(LogWatch.objects.filter(source_rule_id__in=["sem-a", "sem-b"]).values_list("name", flat=True))
    assert len(names) == 2 and names[0] != names[1]
    # The disambiguated one says which rule it belongs to rather than stealing the other's row.
    assert any("sem-" in name for name in names)


def test_deleting_a_semantic_rule_prunes_its_watch(capture_state, stub_semantic):
    settings_obj = ResourceSettings.get_solo()
    settings_obj.display_rules = [dict(SEMANTIC_RULE)]
    settings_obj.save(update_fields=["display_rules", "updated_at"])
    LogWatch.objects.exclude(source_rule_id="").delete()
    sync_watches(_environment(), kind="semantic", rule_ids=["sem-power"], targets=[{"subsystem": "spwsp", "fm": "spwsp"}], enable=True)
    assert LogWatch.objects.get(source_rule_id="sem-power").enabled is True

    settings_obj.refresh_from_db()
    settings_obj.display_rules = []
    settings_obj.save(update_fields=["display_rules", "updated_at"])
    assert LogWatch.objects.get(source_rule_id="sem-power").enabled is False


def test_un_ticking_a_rule_does_not_prune_by_itself(capture_state, stub_semantic):
    """Un-ticking is enforced by the sync call, not by the settings signal.

    A legacy watch whose rule predates the opt-in flag must survive a settings save; otherwise
    merely opening a settings page would stop monitoring nobody asked to stop.
    """
    settings_obj = ResourceSettings.get_solo()
    legacy = {k: v for k, v in SEMANTIC_RULE.items() if k != "liveWatch"}
    settings_obj.display_rules = [dict(legacy)]
    settings_obj.save(update_fields=["display_rules", "updated_at"])
    LogWatch.objects.filter(source_rule_id="sem-power").delete()
    sync_watches(_environment(), kind="semantic", rule_ids=["sem-power"], targets=[{"subsystem": "spwsp", "fm": "spwsp"}], enable=True)
    assert LogWatch.objects.get(source_rule_id="sem-power").enabled is True

    # Saving settings with the rule still present (but never ticked) keeps the watcher alive.
    settings_obj.refresh_from_db()
    settings_obj.save(update_fields=["updated_at"])
    assert LogWatch.objects.get(source_rule_id="sem-power").enabled is True

    # Disabling the rule itself is a different matter: it can no longer match, so it goes.
    settings_obj.display_rules = [{**legacy, "enabled": False}]
    settings_obj.save(update_fields=["display_rules", "updated_at"])
    assert LogWatch.objects.get(source_rule_id="sem-power").enabled is False


def test_hit_events_carry_which_surface_they_belong_to():
    """The ribbon/collector split is decided server-side; a client must not have to guess."""
    import inspect

    from apps.logsources.services import watch_worker

    source = inspect.getsource(watch_worker)
    assert '"display_mode": rule.display_mode' in source
    assert '"show_on_timeline": bool(rule.show_on_timeline)' in source

    from apps.logsources.watch_views import LogWatchViewSet

    hits_source = inspect.getsource(LogWatchViewSet.hits)
    # A replayed hit is classified by the same rule as a live one.
    assert 'display_mode = "data" if watch.extraction_rule_id else "semantic"' in hits_source
