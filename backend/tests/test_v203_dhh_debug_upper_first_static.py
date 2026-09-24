from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REMOTE = (ROOT / "apps/logsources/services/remote_logs.py").read_text(encoding="utf-8")
PAGE = (ROOT.parent / "frontend/src/components/PlatformSettingsPage.tsx").read_text(encoding="utf-8")


def test_dhh_debug_uses_strict_category_level_upper_priority():
    assert "if category == LogPathCategory.DEBUG:" in REMOTE
    assert "if selected_by_category.get(category):" in REMOTE
    assert "log.plan.dhh.debug_fallback.skip" in REMOTE
    assert "fallback_categories.add(category)" in REMOTE
    debug_branch = REMOTE.index("if category == LogPathCategory.DEBUG:")
    run_branch = REMOTE.index('if "run_flat" in rules:', debug_branch)
    assert debug_branch < run_branch


def test_settings_explains_debug_upper_hit_skips_dhh():
    assert "调试日志只要上位机在当前时间窗找到候选日志，就不再查询 DHH" in PAGE
    assert "只有上位机完全未找到时才回退 DHH" in PAGE
