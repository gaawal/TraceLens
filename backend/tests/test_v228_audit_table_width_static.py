from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_audit_leading_columns_are_compact_and_actions_keep_room():
    css = (ROOT / "frontend/src/styles.css").read_text(encoding="utf-8")
    assert ".audit-table { min-width: 1810px; }" in css
    assert ".audit-table th:nth-child(1) { width: 135px; }" in css
    assert ".audit-table th:nth-child(2) { width: 95px; }" in css
    assert ".audit-table th:nth-child(3) { width: 135px; }" in css
    assert ".audit-table th:nth-child(13) { width: 150px; }" in css
