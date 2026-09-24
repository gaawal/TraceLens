from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE = (ROOT / "apps/environments/services/deployment.py").read_text(encoding="utf-8")
VIEWS = (ROOT / "apps/environments/views.py").read_text(encoding="utf-8")
EVENTS = (ROOT / "apps/environments/services/deployment_events.py").read_text(encoding="utf-8")
SERIALIZERS = (ROOT / "apps/environments/serializers.py").read_text(encoding="utf-8")
MODELS = (ROOT / "apps/environments/models.py").read_text(encoding="utf-8")
WORKER = (ROOT / "apps/environments/management/commands/deployment_worker.py").read_text(encoding="utf-8")
COMMON_APPS = (ROOT / "apps/common/apps.py").read_text(encoding="utf-8")
SQLITE = (ROOT / "apps/common/sqlite_pragmas.py").read_text(encoding="utf-8")
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
MANAGE = (ROOT / "manage.py").read_text(encoding="utf-8")
FRONTEND = (ROOT.parent / "frontend/src/components/EnvironmentDeploymentDialog.tsx").read_text(encoding="utf-8")
API = (ROOT.parent / "frontend/src/api/resourceApi.ts").read_text(encoding="utf-8")


def test_core_deployment_commands_and_modes():
    assert 'precheck_stop_lower = bool(payload.get("precheck_stop_lower", False))' in SERVICE
    assert 'initial_stop_command = "cd ~/SW && stop.sh -les" if precheck_stop_lower else "cd ~/SW && stop.sh -ls"' in SERVICE
    assert '"precheck_stop_lower": False' in SERVICE
    assert 'precheck_stop_lower: Boolean(raw.precheck_stop_lower)' in FRONTEND
    assert '预检查停止下位机进程' in FRONTEND
    assert 'initialStopCommand(draft)' in FRONTEND
    assert 'if simulation_mode == "sim2":' in SERVICE
    assert 'start_command = "cd ~/SW && start.sh -f"' in SERVICE
    assert 'elif simulation_mode == "sim0_real":' in SERVICE
    assert 'start_command = "cd ~/SW && start.sh -ef"' in SERVICE
    assert 'start_command = "cd ~/SW && start.sh -eif"' in SERVICE
    assert 'modes.insert(insert_at, "4GPB_TB_UP")' in SERVICE
    assert 'def _install_mode_for_gpb_mode(mode: str)' in SERVICE
    assert 'if value.upper() == "4GPB_TB_UP":' in SERVICE
    assert 'return "4GPB_TB"' in SERVICE
    assert 'normalized["install_mode"] = _install_mode_for_gpb_mode(normalized["gpb_mode"])' in SERVICE
    assert 'required_count = _mode_required_gpb_count(normalized["gpb_mode"])' in SERVICE
    assert 'tb_deploy.sh' in SERVICE and 'InnerSILPkg' in SERVICE
    assert '--lchUser=root' in SERVICE


def test_parallel_environment_prepare_and_visible_prestart_stop():
    assert 'ThreadPoolExecutor' in SERVICE
    assert '_run_initial_steps_in_parallel(' in SERVICE
    assert 'runnable = [command for command in commands if not command.skipped]' in SERVICE
    assert 'accepted = {DeploymentStepStatus.SUCCESS, DeploymentStepStatus.SKIPPED}' in SERVICE
    assert 'STEP_PRESTART_STOP = "prestart_stop"' in SERVICE
    assert 'STEP_STOP: "停止上位机进程"' in SERVICE
    assert 'STEP_PRESTART_STOP: "停止下位机进程"' in SERVICE
    assert 'def _run_prestart_stop_step(' in SERVICE
    assert "LOWER_RUNTIME_PROBE_COMMAND = \"ps -ef | grep '[t]b_simulator' || true\"" in SERVICE
    assert 'running_hosts = _fresh_running_lower_hosts(deployment.environment)' in SERVICE
    assert 'if not running_hosts:' in SERVICE
    assert 'cd ~/SW && stop.sh -es' in SERVICE
    assert '下位机进程未运行，无需停止，跳过此步骤。' in SERVICE
    assert 'if command.key == STEP_PRESTART_STOP:' in SERVICE
    assert 'HIDDEN_PRESTART_STOP_KEY' not in SERVICE


def test_copy_retry_can_select_exact_user_steps_without_auto_guard():
    assert 'DEPLOYMENT_STEP_KEYS = (STEP_STOP, STEP_DEPLOY, STEP_TB, STEP_INSTALL, STEP_START, STEP_POST_START)' in SERVICE
    assert 'normalized["selected_steps"] = selected_steps' in SERVICE
    assert 'if not selected_steps:' in SERVICE
    assert 'raise ValueError("请至少选择一个需要执行的部署步骤。")' in SERVICE
    assert 'selected_steps = payload.get("selected_steps")' in SERVICE
    assert 'STEP_START not in selected if item.auto else item.key not in selected' in SERVICE
    assert '"本次复制重试未选择执行此步骤。"' in SERVICE
    assert 'runnable_keys = [command.key for command in commands if not command.skipped]' in SERVICE
    assert 'final_deployment.current_step = runnable_keys[-1] if runnable_keys else ""' in SERVICE


def test_web_request_does_not_execute_deployment_or_remote_preflight_inline():
    start_body = SERVICE[SERVICE.index('def start_deployment('):]
    assert 'threading.Thread(target=_run_deployment' not in start_body
    assert 'Remote SSH trust/time preflight is deliberately executed by deployment_worker' in start_body
    validate = SERVICE[SERVICE.index('def validate_payload('):SERVICE.index('def command_preview(')]
    assert 'defaults = _validation_defaults(environment)' in validate
    assert '_read_deployment_display' not in validate
    assert '_resolve_install_port' not in validate
    assert '等待部署执行器接管。' in SERVICE


def test_dedicated_deployment_worker_and_web_scaling_are_configured():
    assert 'class Command(BaseCommand)' in WORKER
    assert 'TRACELENS_DEPLOYMENT_WORKER_CONCURRENCY' in WORKER
    assert 'activate_due_scheduled_deployments' in WORKER
    assert 'EnvironmentDeployment.objects.filter(status=DeploymentStatus.PENDING)' in WORKER
    assert 'DeploymentEventBus.try_claim_runner' in WORKER
    assert 'deployment-worker:' in COMPOSE
    assert 'command: python manage.py deployment_worker' in COMPOSE
    assert 'TRACELENS_WEB_WORKERS:-4' in DOCKERFILE
    assert 'subprocess.Popen' in MANAGE and '"deployment_worker"' in MANAGE


def test_sqlite_wal_keeps_web_reads_available_during_worker_writes():
    assert 'sqlite_pragmas' in COMMON_APPS
    assert 'PRAGMA journal_mode=WAL' in SQLITE
    assert 'PRAGMA busy_timeout=30000' in SQLITE
    assert 'PRAGMA synchronous=NORMAL' in SQLITE


def test_process_log_is_durable_and_reopen_safe():
    assert 'process_log = models.TextField' in MODELS
    assert 'step.process_log = (step.process_log or "") + "".join' in SERVICE
    assert '"process_log"' in SERIALIZERS
    assert 'selectedStep.process_log' in FRONTEND
    assert 'Reopening an active deployment must not wait for remote DISPLAY/stations' in FRONTEND
    assert 'History/detail are DB-only requests and take priority' in FRONTEND


def test_display_is_optional_and_only_exported_when_present():
    assert 'normalized["display_env"] = str(normalized.get("display_env") or "").strip()' in SERVICE
    assert 'if normalized["display_env"] and' in SERVICE
    assert 'if display_env:' in SERVICE
    assert 'export DISPLAY=' in SERVICE
    assert 'bash -ic' in SERVICE and '__TRACELENS_DISPLAY__=' in SERVICE


def test_ssh_trust_is_required_while_time_sync_is_best_effort_and_bidirectional_for_gpb():
    assert 'def ensure_gpb_ssh_trust(' in SERVICE
    assert 'auto_trust' in SERVICE
    assert 'forward_trusted' in SERVICE
    assert 'reverse_trusted' in SERVICE
    assert 'authorized_keys' in SERVICE
    assert 'ssh-keygen' in SERVICE
    assert 'BatchMode=yes' in SERVICE
    assert 'NumberOfPasswordPrompts=0' in SERVICE
    assert 'def _lower_to_upper_ssh_command(' in SERVICE
    assert 'date --set=\\"$upper_time\\"' in SERVICE
    assert '下位机反向 SSH 获取上位机时间失败' in SERVICE
    assert 'DEPLOYMENT_TIME_SYNC_TOLERANCE_SECONDS = 1' in SERVICE
    assert 'Time drift never blocks deployment' in SERVICE
    assert 'continuing deployment' in SERVICE
    assert '上下位机时间未同步，按告警继续部署。' in SERVICE
    assert '部署目标时间未同步' not in SERVICE


def test_schedule_stop_retry_and_runner_controls_remain_available():
    assert 'DeploymentStatus.SCHEDULED' in SERVICE
    assert '_parse_scheduled_at' in SERVICE
    assert 'def request_stop_deployment(' in SERVICE
    assert '_deployment_pid_file(' in SERVICE
    assert 'kill -TERM' in SERVICE or 'TERM' in SERVICE
    assert 'def retry_deployment_step(' in SERVICE
    assert 'DeploymentEventBus' in SERVICE
    assert 'def try_claim_runner(' in EVENTS


def test_realtime_log_stream_preserves_order_without_stderr_tail():
    assert 'chunks=list(ordered_buffer)' in SERVICE
    assert '"chunks": chunks' in EVENTS or 'chunks' in EVENTS
    assert 'logs_included' in SERIALIZERS
    assert "event.chunks?.length" in FRONTEND
    assert "fallback.push({ stream: 'stderr'" not in FRONTEND
    assert 'deployment-log-secondary-output' not in FRONTEND


def test_frontend_aggregate_trust_and_schedule_layout_are_current():
    assert '已互信' in FRONTEND
    assert '互信失败' in FRONTEND
    assert '上→下' not in FRONTEND
    assert '下→上' not in FRONTEND
    assert '预约时间' in FRONTEND
    assert "futureScheduled ? '预约部署' : '立即部署'" in FRONTEND
    assert 'DISPLAY 环境变量（可选）' in FRONTEND


def test_api_supports_selective_retry_payload():
    assert 'selected_steps?: string[]' in API
    assert 'startEnvironmentDeployment' in API
    assert 'deployment-preview' in API
    assert 'deployment-retry' in VIEWS or 'deployments/(?P<deployment_id>' in VIEWS


def test_post_start_script_history_and_current_version_defaults():
    assert 'STEP_POST_START = "post_start_script"' in SERVICE
    assert 'STEP_POST_START: "启动后脚本"' in SERVICE
    assert 'post_start_script = str(payload.get("post_start_script") or "").strip()' in SERVICE
    assert '_saved_post_start_scripts(environment)' in SERVICE
    assert '_remember_post_start_script' in SERVICE
    assert 'deployment_scripts = models.JSONField' in MODELS
    assert '"target_version": str(environment.software_version or "").strip()' in SERVICE
    assert '启动后脚本（可选）' in FRONTEND
    assert '保存到当前环境' in FRONTEND
    assert 'saved_post_start_scripts?: string[]' in API
    assert 'deployment-post-script-editor' not in FRONTEND
    assert FRONTEND.count('deployment-post-script-step-editor') == 1
    assert "{ key: 'start', name: '启动环境'" in FRONTEND
    assert "{ key: 'post_start_script', name: '启动后脚本'" in FRONTEND
    assert FRONTEND.index("{ key: 'start', name: '启动环境'") < FRONTEND.index("{ key: 'post_start_script', name: '启动后脚本'")
