"""字段定义里「单位转换」勾选框的位置契约（静态检查）。

背景：以前每个数值字段都**独占一行**放「单位转换（可选）」勾选框 + 两行说明，没勾选的字段也照样
占着那块高度，字段一多整页都是"单位转换"，非常占地方。现在勾选框缩成"单位转换"四个字放在**字段行
行尾**，勾上才在下面展开换算配置。这条契约守住这个形态，防止又退回"每个字段一大块"。
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_unit_toggle_sits_at_the_end_of_the_field_row():
    panel = (ROOT / "frontend/src/components/DataExtractionRulesPanel.tsx").read_text(encoding="utf-8")
    # 勾选框紧跟在「样例 …」之后、同一个 .data-field-main-row 里（不是独立的一行）。
    assert (
        "<small>样例 {field.sampleValue}</small>"
        "{(field.valueType === 'number' || field.valueType === 'integer') && "
        '<label className="data-field-unit-toggle"'
    ) in panel
    # 旧的独立标题行「单位转换（可选）」必须已经删掉。
    assert "单位转换（可选）" not in panel
    # 旧的"始终渲染、靠 expanded/collapsed 切样式"的写法也不该回来。
    assert "${isDataUnitConversionEnabled(field) ? 'expanded' : 'collapsed'}" not in panel


def test_unit_details_render_only_after_checking():
    panel = (ROOT / "frontend/src/components/DataExtractionRulesPanel.tsx").read_text(encoding="utf-8")
    assert (
        "isDataUnitConversionEnabled(field) && "
        '<div className="data-field-unit-config expanded data-field-unit-panel">'
    ) in panel
    # 展开后的配置块就是原来的 details，不再自带标题行。
    assert panel.count("data-field-unit-details") >= 1


def test_field_row_is_a_single_line_with_trailing_toggle():
    css = (ROOT / "frontend/src/styles.css").read_text(encoding="utf-8")
    assert (
        ".data-field-main-row {\n"
        "  display: flex; flex-wrap: nowrap; align-items: center; gap: 8px; min-height: 34px;\n"
        "}"
    ) in css
    # 勾选框被推到行尾。
    assert ".data-field-unit-toggle {\n  margin-left: auto;" in css
    # 面板内不再重复 details 的内边距，避免展开后又多出一块空白。
    assert ".data-field-unit-panel .data-field-unit-details { padding: 0; }" in css
