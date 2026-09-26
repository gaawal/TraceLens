"""「让用户选择」：结构化选项 + 常驻可用 + 不会卡住整轮。

背景：AI 以前只能把选项写进回复里让用户自己手打。现在 `ask_user_choice` 会把选项发成
`choices` 事件，前端渲染成固定的选择组件。这里守住三件事：
1. 选项校验（至少 2 个、去重、截断）；
2. 这个工具对任何领域都可用（否则模型只能在正文里列选项）；
3. choices 分支**不能**再发 ui_action —— 前端不会给不存在的页面动作回执，
   发了整轮就会一直等回执（实测表现为一直"运行中"、选项点不动）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.tooling.services import ToolInputError, ask_user_choice

ROOT = Path(__file__).resolve().parents[2]


def test_choice_payload_is_normalized():
    payload = ask_user_choice({
        "question": "这批日志要生成哪一种规则？",
        "options": [
            {"label": "语义说明", "detail": "展开后的一句话解释"},
            {"label": "标签", "detail": "列表右侧的小色块"},
            {"label": "语义说明", "detail": "重复的会被丢掉"},
            {"label": ""},
            {"label": "两者都要"},
        ],
    })
    assert payload["status"] == "waiting_for_choice"
    assert payload["question"].startswith("这批日志")
    assert [item["label"] for item in payload["options"]] == ["语义说明", "标签", "两者都要"]
    assert payload["options"][0]["id"] == "choice-1"
    assert payload["options"][0]["detail"] == "展开后的一句话解释"
    assert payload["multi"] is False and payload["allow_other"] is True


def test_choice_payload_accepts_plain_strings_and_multi():
    payload = ask_user_choice({"question": "选一个", "options": ["A", "B"], "multi": True, "allow_other": False})
    assert [item["label"] for item in payload["options"]] == ["A", "B"]
    assert payload["multi"] is True and payload["allow_other"] is False


def test_choice_requires_question_and_two_options():
    with pytest.raises(ToolInputError):
        ask_user_choice({"options": ["A", "B"]})
    with pytest.raises(ToolInputError):
        ask_user_choice({"question": "选一个", "options": ["只有一个"]})
    with pytest.raises(ToolInputError):
        ask_user_choice({"question": "选一个", "options": ["A", "A"]})


def test_choice_tool_is_registered_and_always_offered_to_the_agent():
    from apps.tooling.registry import TOOLS

    tool = next((item for item in TOOLS if item.id == "ask_user_choice"), None)
    assert tool is not None, "工具必须在注册表里"
    assert tool.handler is ask_user_choice

    graph = (ROOT / "backend/apps/tooling/ai_engine/graph.py").read_text(encoding="utf-8")
    assert 'selected_tool_ids.add("ask_user_choice")' in graph, "必须无条件投放给 Agent（不属于某个领域）"
    assert '"type": "choices", "choices": choice_payload' in graph
    # choices 分支之后的 ui_action 计算必须跳过，否则会一直等一个不存在的回执。
    assert "action = None if interactive_choice else" in graph


def test_router_tells_the_model_to_ask_before_bulk_generating():
    """用户没说要语义还是标签时，路由不要放行批量工具，逼出一次「让用户选择」。"""
    router = (ROOT / "backend/apps/tooling/ai_engine/router.py").read_text(encoding="utf-8")
    assert "本轮 tool_ids 只放 ask_user_choice，不要放 bulk_generate_log_rules" in router
    assistant = (ROOT / "backend/apps/tooling/assistant.py").read_text(encoding="utf-8")
    assert "必须调用 ask_user_choice" in assistant, "系统提示里要有「需要选择就用组件」的硬规则"


# --- 「回答上面的选择」标记：确定性认出"用户已经选过了" -------------------------

def test_parse_choice_answer_reads_the_frontend_marker():
    from apps.tooling.assistant import CHOICE_ANSWER_PREFIX, parse_choice_answer

    parsed = parse_choice_answer("回答上面的选择——「本轮要生成哪种规则？」：只要标签规则")
    assert parsed == {"question": "本轮要生成哪种规则？", "label": "只要标签规则"}
    # 半角冒号 / 多余空白也要认
    assert parse_choice_answer("回答上面的选择——「Q」:  两者都要 ")["label"] == "两者都要"
    # 普通消息不能被误判成"回答"
    assert parse_choice_answer("只要标签") is None
    assert parse_choice_answer("") is None
    assert parse_choice_answer("回答上面的选择——「Q」：") is None
    assert CHOICE_ANSWER_PREFIX == "回答上面的选择——"


def test_frontend_sends_the_marker_so_the_backend_can_stop_re_asking():
    """前端点选必须带上问题标记 —— 这是后端"不再重复问"的确定性依据。"""
    component = (ROOT / "frontend/src/components/AiAssistant.tsx").read_text(encoding="utf-8")
    assert "回答上面的选择——「${question}」：${label}" in component
    graph = (ROOT / "backend/apps/tooling/ai_engine/graph.py").read_text(encoding="utf-8")
    assert "choice_answer = a.parse_choice_answer(message)" in graph
    assert "if choice_answer is None and kernel.registry.get(\"ask_user_choice\") is not None:" in graph
    router = (ROOT / "backend/apps/tooling/ai_engine/router.py").read_text(encoding="utf-8")
    assert "必须**放行 bulk_generate_log_rules" in router
