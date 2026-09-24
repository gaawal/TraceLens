from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_audit_table_uses_single_line_compact_fields():
    page = (ROOT / "frontend/src/components/LogAuditPage.tsx").read_text(encoding="utf-8")
    css = (ROOT / "frontend/src/styles.css").read_text(encoding="utf-8")

    assert '<th>日志时间范围</th><th>关键字</th><th>状态</th>' in page
    assert '<th>耗时</th><th>操作</th>' in page
    assert 'item.operation_id.slice' not in page
    assert "item.operator_username || item.client_ip || '—'" in page
    assert "item.target_username ? `(${item.target_username})` : ''" in page
    assert '至 {formatMoment(item.end_time)}' in page
    assert "`${item.matched_files.length} 个文件`" in page
    assert '<small>{item.result_count ?? 0} 条日志</small>' not in page
    assert '.audit-range-cell, .audit-single-line' in css


def test_audit_detail_only_lists_hit_files_in_one_row_each():
    page = (ROOT / "frontend/src/components/LogAuditPage.tsx").read_text(encoding="utf-8")
    css = (ROOT / "frontend/src/styles.css").read_text(encoding="utf-8")

    assert 'function AuditMatchedFilesPanel' in page
    assert 'audit-detail-file-row' in page
    assert 'audit-detail-file-path' in page
    assert "title=\"点击复制完整路径\"" in page
    assert '<span>文件时间：{auditFileTimeLabel(file)}</span>' in page
    assert 'grid-template-columns: minmax(110px, 180px) minmax(260px, 1fr) max-content;' in css
    assert 'grid-template-columns: 1fr;' not in css[css.index('/* v227 audit compact table'):css.index('/* ===== v0.92-v206')]


def test_audit_action_buttons_are_side_by_side():
    css = (ROOT / "frontend/src/styles.css").read_text(encoding="utf-8")
    block = css[css.index('/* v227 audit compact table'):css.index('/* ===== v0.92-v206')]
    assert '.audit-row-actions' in block
    assert 'display: flex;' in block
    assert 'white-space: nowrap;' in block
