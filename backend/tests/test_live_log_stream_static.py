from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REMOTE = (ROOT / "apps/logsources/services/remote_logs.py").read_text(encoding="utf-8")
VIEWS = (ROOT / "apps/logsources/views.py").read_text(encoding="utf-8")
SERIALIZERS = (ROOT / "apps/logsources/serializers.py").read_text(encoding="utf-8")


def test_live_log_plan_targets_current_files_only():
    assert "def build_live_log_plan(" in REMOTE
    assert '"current"' in REMOTE
    assert 'current_by_fold' in REMOTE
    assert 'f"{fm}.log".casefold()' in REMOTE
    assert 'physical_pair(requested_subsystem: str, requested_fm: str)' in REMOTE
    assert 'f"{target.root.rstrip(\'/\')}/event.log"' in REMOTE
    assert "timestamped executor .log is a rotated archive" in REMOTE


def test_live_log_stream_starts_at_eof_and_reads_only_appends():
    assert "def stream_live_log_events(" in REMOTE
    assert '"offset": max(0, int(attr.st_size or 0))' in REMOTE
    assert "handle.seek(offset)" in REMOTE
    assert "size < int(state[\"offset\"])" in REMOTE
    assert "time.sleep(interval)" in REMOTE
    assert "_LIVE_LOG_INTERVAL_SECONDS = 5" in REMOTE
    assert "_LIVE_LOG_MAX_READ_BYTES" in REMOTE


def test_live_stream_keeps_existing_parser_markers():
    for marker in (
        "__TRACELENS_LOG_CATEGORY__=",
        "__TRACELENS_LOG_SUBSYSTEM__=",
        "__TRACELENS_LOG_MODULE__=",
        "__TRACELENS_LOG_SOURCE_PATH__=",
    ):
        assert marker in REMOTE


def test_live_endpoint_is_single_streaming_connection():
    assert '@action(detail=True, methods=["post"], url_path="live")' in VIEWS
    assert 'content_type="text/event-stream; charset=utf-8"' in VIEWS
    assert 'response["X-Accel-Buffering"] = "no"' in VIEWS
    assert "stream_live_log_events(environment, plan, interval_seconds=5)" in VIEWS


def test_live_request_has_no_time_window_contract():
    assert "class LiveLogRequestSerializer" in SERIALIZERS
    block = SERIALIZERS.split("class LiveLogRequestSerializer", 1)[1].split("class SemanticSourceRequestSerializer", 1)[0]
    assert "start_time" not in block
    assert "end_time" not in block
    assert "LiveLogRequestSerializer(data=request.data)" in VIEWS
