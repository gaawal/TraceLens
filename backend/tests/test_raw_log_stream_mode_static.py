from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERIALIZERS = (ROOT / "apps" / "logsources" / "serializers.py").read_text(encoding="utf-8")
VIEWS = (ROOT / "apps" / "logsources" / "views.py").read_text(encoding="utf-8")
REMOTE = (ROOT / "apps" / "logsources" / "services" / "remote_logs.py").read_text(encoding="utf-8")
FRONTEND_ROOT = ROOT.parent / "frontend"
APP = (FRONTEND_ROOT / "src" / "App.tsx").read_text(encoding="utf-8")
API = (FRONTEND_ROOT / "src" / "api" / "resourceApi.ts").read_text(encoding="utf-8")
CSS = (FRONTEND_ROOT / "src" / "styles.css").read_text(encoding="utf-8")


def test_raw_mode_is_a_real_server_side_plain_text_stream():
    assert "raw_text = serializers.BooleanField(required=False, default=False)" in SERIALIZERS
    assert "include_internal_markers=not raw_text" in VIEWS
    assert "include_internal_markers: bool = True" in REMOTE
    assert "if include_internal_markers:" in REMOTE
    assert "raw_text?: boolean" in API
    assert "{ ...task.remoteRequest, raw_text: true }" in APP


def test_raw_mode_url_and_browser_preference_are_persistent():
    assert "tracelens.raw-log-mode.v1" in APP
    assert "params.set('raw', scene.view.rawLogMode ? '1' : '0')" in APP
    assert "params.has('raw') ? params.get('raw') === '1' : loadRawLogModePreference()" in APP
    assert 'label="原始日志"' in APP
    # 实时任务时 RawLogView 直接渲染流进来的原文；因此原始日志与实时监听可以同时开着，
    # 不再是「开一个就停另一个」。
    assert '<RawLogView task={activeTask} live={liveListening} />' in APP
    assert 'streamedText' in APP
    assert 'if (live) return undefined;' in APP


def test_log_toolbar_action_typography_matches_switches():
    assert ".metric-strip-actions .metric-action-button" in CSS
    assert ".error-navigation .error-nav-button" in CSS
    assert "font-size: 11px;" in CSS


def test_log_workspace_keeps_function_folding_and_separates_query_tools_from_bottom_enhancements():
    """折叠仍然可用；开关类控件在上方操作工具栏，底部只留统计与翻页。

    这条测试原本断言三个开关作为 props 挂在底部 LogPaginationBar 上。那套接线后来变成了
    「props 还在传、渲染被删掉」——按钮消失而代码看不出来。现在开关都在
    `.metric-strip-actions`，所以这里改为断言「开关存在且不再以 props 形式穿过底部栏」。
    """
    assert 'label="函数折叠"' in APP
    assert ') : foldingEnabled ? (' in APP
    assert '<MergedFmTimelineView' in APP
    assert '<ThreadGroupedFlatView' in APP
    # 显示开关在上方工具栏
    assert 'label="源码语义"' in APP
    assert 'label="原始日志"' in APP
    assert '调用导航' in APP
    # 底部增强区只剩统计，且不再有「传了却不渲染」的开关 props
    assert 'className="log-enhancement-tools"' in APP
    assert 'onToggleTimeline' not in APP
    assert 'onToggleCallFlow' not in APP
    assert 'onToggleSemantic' not in APP
    assert '.log-pagination-right' in CSS
    assert 'width: calc(100vw - 16px);' in CSS
