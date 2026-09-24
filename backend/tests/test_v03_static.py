from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_v03_source_contracts():
    ssh = (ROOT / "apps/common/services/ssh.py").read_text(encoding="utf-8")
    settings = (ROOT / "config/settings.py").read_text(encoding="utf-8")
    models = (ROOT / "apps/environments/models.py").read_text(encoding="utf-8")
    remote = (ROOT / "apps/logsources/services/remote_logs.py").read_text(encoding="utf-8")
    assert "class SshSessionPool" in ssh
    assert "ssh.session.borrow" in ssh
    assert "ssh.session.promoted" in ssh
    assert "entry.borrowed == 0" in ssh
    assert "TRACELENS_SSH_SESSION_IDLE_TIMEOUT" in settings
    assert "CORS_ALLOW_ALL_ORIGINS" in settings
    assert "class LogPathProfile" in models
    assert 'DEBUG = "debug"' in models
    assert 'EXECUTOR = "executor"' in models
    assert "log.read.direct.progress" in remote
    assert "before_start_time" in remote


if __name__ == "__main__":
    test_v03_source_contracts()
    print("v0.3 static contracts passed")
