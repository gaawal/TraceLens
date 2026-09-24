from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_deployment_outputs_are_forwarded_to_terminal_process_logger():
    source = (ROOT / "apps/environments/services/deployment.py").read_text(encoding="utf-8")
    assert 'logging.getLogger("tracelens.process.deployment")' in source
    assert '"[DEPLOY][%s] task=%s step=%s(%s) | %s"' in source
    assert '_log_process_chunks(step.deployment_id, step.key, ordered_chunks)' in source
    assert '"[DEPLOY] task=%s step.start' in source
    assert '"[DEPLOY] task=%s success"' in source


def test_worker_claims_are_visible_in_terminal():
    source = (ROOT / "apps/environments/management/commands/deployment_worker.py").read_text(encoding="utf-8")
    assert 'logging.getLogger("tracelens.process.deployment")' in source
    assert '"[DEPLOY-WORKER] claim task=%s' in source


def test_live_monitor_logs_remote_component_target_not_source_watch():
    service = (ROOT / "apps/logsources/services/remote_logs.py").read_text(encoding="utf-8")
    view = (ROOT / "apps/logsources/views.py").read_text(encoding="utf-8")
    assert 'logging.getLogger("tracelens.process.live")' in service
    assert '"[LIVE] ready environment=%s machine=%s subsystem=%s module=%s path=%s offset=%s"' in service
    assert '"[LIVE] append environment=%s files=%s lines=%s bytes=%s modules=%s"' in service
    assert '"[LIVE] subscribe environment=%s target=%s files=%s"' in view
    assert '"[LIVE] disconnect environment=%s target=%s reason=client_closed"' in view


def test_ai_langgraph_nodes_are_visible_in_terminal():
    source = (ROOT / "apps/atlog/ai_agent.py").read_text(encoding="utf-8")
    assert 'logging.getLogger("tracelens.process.ai")' in source
    assert '"[AI] node.finish name=%s label=%s duration_ms=%s detail=%s"' in source
    assert '"[AI] diagnosis.start model=%s' in source
    assert '"[AI] diagnosis.finish case=%s' in source
