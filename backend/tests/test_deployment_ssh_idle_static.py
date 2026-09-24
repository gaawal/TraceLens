from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SSH = (ROOT / "apps/common/services/ssh.py").read_text(encoding="utf-8")
DEPLOY = (ROOT / "apps/environments/services/deployment.py").read_text(encoding="utf-8")
EVENTS = (ROOT / "apps/environments/services/deployment_events.py").read_text(encoding="utf-8")
WORKER = (ROOT / "apps/environments/management/commands/deployment_worker.py").read_text(encoding="utf-8")
SETTINGS = (ROOT / "config/settings.py").read_text(encoding="utf-8")


def test_idle_ssh_sessions_are_reaped_without_future_requests():
    assert "ssh-session-idle-reaper" in SSH
    assert "def _reaper_loop" in SSH
    assert "TRACELENS_SSH_SESSION_REAP_INTERVAL" in SETTINGS
    assert 'entry.close("idle_or_inactive")' in SSH


def test_deployment_disconnects_no_output_ssh_session():
    assert "TRACELENS_DEPLOYMENT_SSH_IDLE_OUTPUT_TIMEOUT" in SETTINGS
    assert "SSH 连续" in DEPLOY
    assert 'SSH_SESSION_POOL.invalidate(machine, "deployment_stream_timeout")' in DEPLOY


def test_worker_heartbeat_explains_pending_deployment():
    assert "WORKER_HEARTBEAT_KEY" in EVENTS
    assert "def refresh_worker_heartbeat" in EVENTS
    assert "def worker_alive" in EVENTS
    assert "DeploymentEventBus.refresh_worker_heartbeat(worker_id)" in WORKER
    assert "未检测到 deployment_worker 心跳" in DEPLOY
