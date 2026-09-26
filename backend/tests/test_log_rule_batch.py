"""批量生成日志语义/标签规则：分组、同类特征签名、候选配置、去重。

这些都是**确定性**的部分（不调用模型）：模型只负责把每组写成中文，写不出来也有兜底。
真实链路（工具 → 前端动作 → 保存 → 渲染）用浏览器脚本 `/tmp/tl-verify/bulk-rules.cjs` 验证。
"""

from __future__ import annotations

import pytest

from apps.tooling import log_rule_batch as batch
from apps.tooling.services import bulk_generate_log_rules
from apps.tooling.services import ToolInputError

LINE_HOME_IN = "[2026-09-26 11:17:53.828] [INFO] [WSP] [23070] [30070] [wsp] [normal] [wsp:Stage_WSP_HOME:324] Stage_WSP_HOME() >() enter stage homing stage start step=1/6 wafer=W01 lot=LOT-1"
LINE_HOME_OUT = "[2026-09-26 11:18:02.100] [INFO] [WSP] [23070] [30070] [wsp] [normal] [wsp:Stage_WSP_HOME:324] Stage_WSP_HOME() <() leave stage homing stage end step=6/6 status=ok"
LINE_MOVE_A = "[2026-09-26 11:18:03.500] [INFO] [WSP] [23070] [30070] [wsp] [normal] [wsp:MoveAbsolute:154] MoveAbsolute() >() enter stage absolute move start dof=6 point=home_01"
LINE_MOVE_B = "[2026-09-26 11:18:04.900] [INFO] [WSP] [23070] [30070] [wsp] [normal] [wsp:MoveAbsolute:154] MoveAbsolute() >() enter stage absolute move start dof=6 point=load_02"


def test_parse_sample_extracts_function_and_body():
    sample = batch.parse_sample(LINE_HOME_IN)
    assert sample is not None
    assert sample.function_name == "Stage_WSP_HOME()"
    # 正文要去掉函数名与 >() 边界符，只留信息部分（和界面展示同一条规则）。
    assert sample.message == "enter stage homing stage start step=1/6 wafer=W01 lot=LOT-1"
    assert "2026-09-26" in sample.line


def test_parse_sample_accepts_structured_and_rejects_junk():
    structured = batch.parse_sample({"message": "enter stage absolute move start dof=6 point=home_01", "function_name": "MoveAbsolute()", "count": 3})
    assert structured is not None and structured.function_name == "MoveAbsolute()" and structured.count == 3
    assert batch.parse_sample("") is None
    assert batch.parse_sample("   ") is None
    assert batch.parse_sample(12345) is None


def test_message_signature_masks_variables():
    # k=v 的值一律抹成 *（同类特征的关键就在这儿：只有变量不同也要归成一组）
    signature = batch.message_signature("enter stage homing stage start step=1/6 wafer=W01 dur=12.5ms code=0xAB12CD")
    assert signature == "enter stage homing stage start step=* wafer=* dur=* code=*"
    # 没有 k=v 的裸数字/十六进制同样要抹掉，否则序号一变就被拆散。
    assert batch.message_signature("retry 3 of 5") == "retry <n> of <n>"
    assert batch.message_signature("handle 0xAB12CD done") == "handle <hex> done"


def test_group_by_function_and_similar():
    samples = [batch.parse_sample(line) for line in (LINE_HOME_IN, LINE_HOME_OUT, LINE_MOVE_A, LINE_MOVE_B)]
    samples = [item for item in samples if item]

    by_function = batch.group_samples(samples, group_by="function")
    assert [group.label for group in by_function] == ["MoveAbsolute()", "Stage_WSP_HOME()"] or sorted(group.label for group in by_function) == ["MoveAbsolute()", "Stage_WSP_HOME()"]
    counts = {group.label: group.count for group in by_function}
    assert counts == {"Stage_WSP_HOME()": 2, "MoveAbsolute()": 2}, "出现次数不能算错"

    by_similar = batch.group_samples(samples, group_by="similar")
    # 两条 MoveAbsolute 只有 point 不同 → 必须归成一组；进出场特征不同 → 分属两组。
    labels = [group.label for group in by_similar]
    assert "enter stage absolute move start dof=* point=*" in labels
    assert len(by_similar) == 3, labels


def test_candidates_use_keyword_for_functions_and_template_for_similar():
    samples = [batch.parse_sample(line) for line in (LINE_HOME_IN, LINE_MOVE_A, LINE_MOVE_B)]
    samples = [item for item in samples if item]

    function_rules = batch.build_candidates(batch.group_samples(samples, group_by="function"), mode="semantic")
    move = next(rule for rule in function_rules if rule["group"]["label"] == "MoveAbsolute()")
    assert move["kind"] == "keyword" and move["keyword"] == "MoveAbsolute()"
    assert move["scope"] == "function"
    assert move["display_mode"] == "semantic"
    assert move["custom_label_template"] == "", "只要语义时不该附带标签"
    assert move["display_template"], "兜底也要给出非空语义"

    similar_rules = batch.build_candidates(batch.group_samples(samples, group_by="similar"), mode="both")
    template = next(rule for rule in similar_rules if rule["group"]["label"].startswith("enter stage absolute move"))
    assert template["kind"] == "template"
    assert template["sample_message"].startswith("enter stage absolute move")
    assert {item["label"] for item in template["parameters"]} == {"dof", "point"}
    assert template["custom_label_template"], "both 模式下要给标签文本"
    assert template["custom_label_template"] in template["display_template"] or len(template["custom_label_template"]) <= 10


def test_label_mode_only_produces_labels():
    samples = [batch.parse_sample(LINE_HOME_IN)]
    rules = batch.build_candidates(batch.group_samples([item for item in samples if item], group_by="function"), mode="label")
    assert rules and all(rule["custom_label_template"] for rule in rules)
    assert all(rule["display_mode"] != "semantic" for rule in rules)


def test_candidates_without_parameters_fall_back_to_keyword():
    """模板规则必须有参数样例（前端校验会拦），没有 k=v 就退回关键字规则。"""
    line = "[2026-09-26 11:00:00.000] [INFO] [WSP] [1] [2] [wsp] [normal] [wsp:SomeStage:9] SomeStage() >() enter stage without variables here"
    samples = [batch.parse_sample(line)]
    rules = batch.build_candidates(batch.group_samples([item for item in samples if item], group_by="similar"), mode="semantic")
    assert rules[0]["kind"] == "keyword"
    assert rules[0]["keyword"]


def test_dedupe_skips_existing_and_duplicated_candidates():
    samples = [batch.parse_sample(LINE_MOVE_A), batch.parse_sample(LINE_MOVE_B)]
    rules = batch.build_candidates(batch.group_samples([item for item in samples if item], group_by="function"), mode="semantic")
    kept, dropped = batch.dedupe_candidates(rules * 2, [])
    assert len(kept) == 1 and dropped == 1, "同一批里的重复候选只能留一条"
    kept, dropped = batch.dedupe_candidates(rules, ["MoveAbsolute()"])
    assert kept == [] and dropped == 1, "与已有规则同关键字要跳过"


def test_bulk_tool_requires_mode_and_samples():
    with pytest.raises(ToolInputError) as missing_mode:
        bulk_generate_log_rules({"samples": [LINE_HOME_IN]})
    assert "语义" in str(missing_mode.value) and "标签" in str(missing_mode.value)

    with pytest.raises(ToolInputError):
        bulk_generate_log_rules({"mode": "semantic"})
    with pytest.raises(ToolInputError):
        bulk_generate_log_rules({"mode": "semantic", "samples": []})
    with pytest.raises(ToolInputError):
        bulk_generate_log_rules({"mode": "semantic", "samples": ["这不是日志", "也不是"]})
