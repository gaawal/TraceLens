from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_frontend_retries_cached_missing_event_log():
    source = (ROOT / 'frontend/src/components/AtLogAnalysisPage.tsx').read_text(encoding='utf-8')
    assert "cachedEventQuery?.found === false" in source


def test_event_selection_does_not_inherit_debug_target_filter():
    source = (ROOT / 'backend/apps/atlog/services.py').read_text(encoding='utf-8')
    assert 'event_components.update(module for _, module in selected_targets' not in source
    assert 'event_components = set(selected_components)' in source


def test_event_size_probe_falls_back_to_range():
    source = (ROOT / 'backend/apps/atlog/services.py').read_text(encoding='utf-8')
    assert 'Range": "bytes=0-0"' in source
