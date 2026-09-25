"""AI 日志证据的「先量后压」与「组件自动选中」契约。

两件事都属于「AI 操作页面」这条链路的确定性部分，不需要模型参与：

1. 页面按用户配置的长度上限（默认 4 万字符）决定投喂原文还是压缩；
   没超上限的原文必须原样进提示词 —— 通用压缩器会把 >3000 字符的字符串切掉，
   那样「没超就直送」的决策等于没做。
2. 用户点名的组件名要能解析成页面可用的 (subsystem, fm) 目标；
   多候选时必须把候选交回模型，禁止擅自替用户选。
"""
from __future__ import annotations

import types

from pathlib import Path

from apps.tooling import assistant, services

ROOT = Path(__file__).resolve().parents[1]


def _fake_environment(environment_id: int, name: str = "SIM-EUV-01"):
    return types.SimpleNamespace(id=environment_id, name=name)


def test_open_log_locator_resolves_component_name_into_targets(monkeypatch):
    monkeypatch.setattr(services, "_environment", lambda value: _fake_environment(2))
    monkeypatch.setattr(
        services,
        "_component_target_rows",
        lambda name: {
            "component_name": name,
            "found": True,
            "exact_match": True,
            "ambiguous": False,
            "selection_required": False,
            "targets": [{"subsystem": "cpfr", "fm": "cpfr", "kind": "normal", "priority": 100}],
        },
    )
    result = services.open_log_locator({"environment_id": 2, "component_name": "cpfr"})
    action = result["ui_action"]
    assert action["type"] == "open_log_locator"
    assert action["component_name"] == "cpfr"
    # 页面只认 (subsystem, fm)，所以必须由这一层把组件名翻译过去，页面才能自动勾选。
    assert action["fm_targets"] == [{"subsystem": "cpfr", "fm": "cpfr", "kind": "normal"}]


def test_open_log_locator_returns_candidates_instead_of_guessing(monkeypatch):
    monkeypatch.setattr(services, "_environment", lambda value: _fake_environment(2))
    monkeypatch.setattr(
        services,
        "_component_target_rows",
        lambda name: {
            "component_name": name,
            "found": True,
            "exact_match": False,
            "ambiguous": True,
            "selection_required": True,
            "targets": [
                {"subsystem": "mecore", "fm": "mecore", "kind": "normal"},
                {"subsystem": "mecore", "fm": "cpcore", "kind": "normal"},
            ],
        },
    )
    result = services.open_log_locator({"environment_id": 2, "component_name": "热预算"})
    assert "ui_action" not in result
    assert result["selection_required"] is True
    assert len(result["candidates"]) == 2


def test_open_log_locator_reports_unknown_component(monkeypatch):
    monkeypatch.setattr(services, "_environment", lambda value: _fake_environment(2))
    monkeypatch.setattr(
        services,
        "_component_target_rows",
        lambda name: {"component_name": name, "found": False, "targets": []},
    )
    result = services.open_log_locator({"environment_id": 2, "component_name": "不存在"})
    assert "ui_action" not in result
    assert result["found"] is False
    assert "无法自动选择" in result["message"]


def test_runtime_context_passes_budgeted_raw_evidence_through():
    """没超上限的证据原文要完整进提示词，不能被 3000 字符的通用上限切掉。"""
    raw = "L1 2026-09-25 10:00:00 cpfr ERROR " + ("fringe contrast below limit " * 260)
    assert len(raw) > 3200
    context = {
        "page": "logs",
        "log_locator": {
            "task_id": "t1",
            "log_evidence": {
                "radius": 100,
                "mode": "raw",
                "max_chars": 40000,
                "text_chars": len(raw),
                "char_budget": 40512,
                "text": raw,
            },
        },
    }
    text = assistant._runtime_context_text(context)
    assert raw in text, "原文证据被通用字符串上限截断了"
    assert "…<truncated>" not in text
    # 元数据仍然留在 YAML 里，模型知道这段证据是怎么来的。
    assert "log_evidence" in text
    assert "raw" in text


def test_runtime_context_truncates_evidence_beyond_declared_budget():
    raw = "A" * 5000
    context = {
        "page": "logs",
        "log_locator": {
            "log_evidence": {"mode": "raw", "char_budget": 1000, "text": raw},
        },
    }
    text = assistant._runtime_context_text(context)
    assert "A" * 1000 in text
    assert "A" * 1001 not in text
    assert "已截断" in text


def test_log_skill_tells_the_model_to_select_the_named_component():
    """技能文档必须写明「查看某组件日志」要调 open_log_locator 并选中组件，
    否则模型又会只调用读取类工具，日志进了上下文但页面上没选中组件。"""
    doc = (ROOT / "apps/tooling/skills/logs/SKILL.md").read_text(encoding="utf-8")
    assert "open_log_locator" in doc
    assert "component_name" in doc
    assert "自动勾选" in doc or "自动选中" in doc


def test_atlog_page_evidence_is_sent_raw_when_under_budget():
    """用例分析（AtLog）用同一档长度上限：没超就直送页面原文行，超了才压缩。"""
    from apps.tooling import log_context

    rows = [
        {"time": f"2026-09-25 10:00:{index:02d}", "level": "ERROR" if index == 3 else "INFO",
         "component": "cpfr", "source_path": "cpfr.log", "line_number": 100 + index,
         "message": f"step {index} reached nominal state"}
        for index in range(12)
    ]
    raw = log_context.raw_log_rows_context(rows, max_chars=40000)
    assert raw is not None
    assert raw["mode"] == "raw"
    assert "step 3 reached nominal state" in raw["ai_context"]
    assert "L103" in raw["ai_context"], "要带原始行号才能回链"
    assert raw["source_row_count"] == 12
    assert "×" not in raw["ai_context"], "原文直送不该出现归并记号"

    # 上限调小到装不下时返回 None，调用方据此退回压缩路径。
    assert log_context.raw_log_rows_context(rows, max_chars=200) is None
    # 请求里带的预算优先于服务端默认值。
    assert log_context.evidence_max_chars({"log_evidence_max_chars": 12345}) == 12345
    assert log_context.evidence_max_chars({}) >= 4000
