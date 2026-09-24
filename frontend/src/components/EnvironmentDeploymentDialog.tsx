import { registerPageContextReader } from '../assistant/contextRegistry';
import { DeploymentLogView } from './DeploymentLogView';
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Circle,
  Clock3,
  History,
  LoaderCircle,
  PackageCheck,
  Play,
  Rocket,
  Square,
  RotateCcw,
  Terminal,
  WandSparkles,
  X,
  XCircle,
} from 'lucide-react';
import {
  checkEnvironmentDeploymentSshTrust,
  syncEnvironmentDeploymentTime,
  getEnvironmentDeployment,
  getEnvironmentDeploymentDefaults,
  getEnvironmentRuntimeStatus,
  listEnvironmentDeployments,
  parseEnvironmentDeploymentCommands,
  previewEnvironmentDeployment,
  retryEnvironmentDeploymentStep,
  startEnvironmentDeployment,
  stopEnvironmentDeployment,
  type EnvironmentDeployment,
  type EnvironmentDeploymentStep,
  type EnvironmentDeploymentDraft,
  type DeploymentTimeSyncCheck,
  type EnvironmentDeploymentSummary,
  type DeploymentSshTrustCheck,
  type EnvironmentSummary,
} from '../api/resourceApi';
import { appendDeploymentLog, subscribeDeploymentRealtime, type DeploymentStepStateEvent } from '../services/deploymentRealtime';

type ViewMode = 'config' | 'progress' | 'history';
type LegacyDeploymentDraft = EnvironmentDeploymentDraft & {
  tb_mode?: string;
  install_mode?: string;
};


interface Props {
  environment: EnvironmentSummary;
  initialTask?: EnvironmentDeploymentSummary | null;
  onClose: () => void;
  onTaskChange?: (task: EnvironmentDeploymentSummary | null) => void;
  onDeploymentCompleted?: (task: EnvironmentDeployment) => void | Promise<void>;
}

const STATUS_TEXT: Record<string, string> = {
  scheduled: '待执行',
  pending: '等待中',
  running: '执行中',
  stopping: '停止中',
  stopped: '已停止',
  success: '成功',
  failed: '失败',
  skipped: '已跳过',
};

function statusIcon(status: string) {
  if (status === 'scheduled') return <Clock3 size={19} />;
  if (status === 'running' || status === 'stopping') return <LoaderCircle className="spin" size={19} />;
  if (status === 'success') return <CheckCircle2 size={19} />;
  if (status === 'stopped') return <Square size={17} />;
  if (status === 'failed') return <XCircle size={19} />;
  if (status === 'skipped') return <Circle size={19} />;
  return <Circle size={19} />;
}

function formatDateTime(value?: string | null) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function durationText(task: EnvironmentDeployment | EnvironmentDeploymentSummary) {
  const start = task.started_at ? new Date(task.started_at).getTime() : 0;
  const end = task.finished_at ? new Date(task.finished_at).getTime() : Date.now();
  if (!start || Number.isNaN(start) || Number.isNaN(end)) return '—';
  const seconds = Math.max(0, Math.round((end - start) / 1000));
  const minutes = Math.floor(seconds / 60);
  const remain = seconds % 60;
  return minutes > 0 ? `${minutes} 分 ${remain} 秒` : `${remain} 秒`;
}

function deploymentSourceLabel(value?: string) {
  if (value === 'assistant') return 'AI 助手';
  if (value === 'manual') return '页面操作';
  return value || '—';
}

function displayDeploymentParameter(value: unknown) {
  if (Array.isArray(value)) return value.length ? value.join(', ') : '—';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (value == null || value === '') return '—';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function modeGpbCount(mode?: string) {
  const matched = String(mode || '').match(/^(\d+)GPB(?:_|$)/i);
  return matched ? Math.max(0, Number(matched[1])) : 0;
}

function parseGpbIpText(value: string) {
  return value
    .split(/[,，\n]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

const DEFAULT_SINGLE_GPB_TB_MODE = '10GPB_SPLIT_1-3';
const TB_TOTAL_GPB_OPTIONS = Array.from({ length: 10 }, (_, index) => index + 1);
const TB_FRAME_OPTIONS = Array.from({ length: 10 }, (_, index) => index + 1);
const TB_SLOT_OPTIONS = Array.from({ length: 10 }, (_, index) => index + 1);

type TbCustomModeParts = { totalGpb: number; frame: number; slot: number };

function parseTbCustomMode(value?: string): TbCustomModeParts {
  const matched = String(value || '').trim().match(/^(\d+)GPB_SPLIT_(\d+)-(\d+)$/i);
  if (!matched) return { totalGpb: 10, frame: 1, slot: 3 };
  return {
    totalGpb: Math.max(1, Number(matched[1])),
    frame: Math.max(1, Number(matched[2])),
    slot: Math.max(1, Number(matched[3])),
  };
}

function formatTbCustomMode(parts: TbCustomModeParts) {
  return `${parts.totalGpb}GPB_SPLIT_${parts.frame}-${parts.slot}`;
}

function isTbCustomMode(value?: string) {
  return /^(\d+)GPB_SPLIT_(\d+)-(\d+)$/i.test(String(value || '').trim());
}

function realInstallSlotIps(draft?: EnvironmentDeploymentDraft) {
  if (!draft) return Array.from({ length: 10 }, () => 'none');
  const selected = new Set(draft.gpb_ips || []);
  const values = Array.from({ length: 10 }, () => 'none');
  for (const item of draft.available_gpb_slots || []) {
    if (selected.has(item.ip) && item.slot >= 1 && item.slot <= 10) values[item.slot - 1] = item.ip;
  }
  const mapped = new Set(values.filter((item) => item !== 'none'));
  if (mapped.size === selected.size) return values;
  if (draft.real_slot_ips?.length === 10) {
    const previewIps = new Set(draft.real_slot_ips.filter((item) => item && item !== 'none'));
    if (previewIps.size === selected.size && [...selected].every((item) => previewIps.has(item))) return draft.real_slot_ips;
  }
  return values;
}

function currentRealRequiredCount(draft?: EnvironmentDeploymentDraft) {
  return Math.max(1, draft?.available_gpb_ips?.length || draft?.gpb_ips.length || 0);
}

function installModeForGpbMode(mode?: string) {
  const value = String(mode || '').trim();
  return value.toUpperCase() === '4GPB_TB_UP' ? '4GPB_TB' : value;
}

function normalizeDraft(raw: LegacyDeploymentDraft): EnvironmentDeploymentDraft {
  const gpbMode = raw.gpb_mode || raw.install_mode || `${Math.max(1, raw.gpb_ips?.length || 0)}GPB_TB`;
  const required = raw.required_gpb_count || modeGpbCount(gpbMode);
  const requestedTbMode = String(raw.tb_mode || '').trim();
  const tbMode = required === 1
    ? (isTbCustomMode(requestedTbMode) ? requestedTbMode : DEFAULT_SINGLE_GPB_TB_MODE)
    : gpbMode;
  return {
    ...raw,
    gpb_mode: gpbMode,
    tb_mode: tbMode,
    install_mode: installModeForGpbMode(gpbMode),
    required_gpb_count: required,
    include_dhh: Boolean(raw.include_dhh),
    dhh_ip: raw.dhh_ip || '',
    dhh_user: raw.dhh_user || 'root',
    dhh_machine_id: raw.dhh_machine_id || 'TESTSPM',
    display_env: raw.display_env || '',
    post_start_script: raw.post_start_script || '',
    saved_post_start_scripts: raw.saved_post_start_scripts || [],
    save_post_start_script: Boolean(raw.save_post_start_script),
    precheck_stop_lower: Boolean(raw.precheck_stop_lower),
  };
}

function deploymentTargetSignature(draft?: EnvironmentDeploymentDraft) {
  if (!draft) return '';
  const targets = (draft.gpb_ips || []).map((host) => `gpb:root@${host}`);
  if (draft.include_dhh && draft.dhh_ip?.trim()) {
    targets.push(`dhh:${(draft.dhh_user || 'root').trim()}@${draft.dhh_ip.trim()}`);
  }
  return targets.join('|');
}

function initialStopCommand(draft?: Pick<EnvironmentDeploymentDraft, 'precheck_stop_lower'>) {
  return draft?.precheck_stop_lower ? 'cd ~/SW && stop.sh -les' : 'cd ~/SW && stop.sh -ls';
}

function startCommandForMode(mode?: EnvironmentDeploymentDraft['simulation_mode']) {
  if (mode === 'sim2') return 'cd ~/SW && start.sh -f';
  if (mode === 'sim0_real') return 'cd ~/SW && start.sh -ef';
  return 'cd ~/SW && start.sh -eif';
}

const DEPLOYMENT_STEP_KEYS = ['stop', 'deploy', 'tb_deploy', 'install', 'start', 'post_start_script'] as const;

function isModeSkippedStep(key: string, draft?: EnvironmentDeploymentDraft) {
  if (key === 'tb_deploy') return draft?.simulation_mode !== 'sim0_sil';
  if (key === 'post_start_script') return !draft?.post_start_script?.trim();
  return false;
}

function copiedRetryDefaultSteps(task: EnvironmentDeployment) {
  const selectable = new Set<string>(DEPLOYMENT_STEP_KEYS);
  const ordered = [...task.steps]
    .filter((step) => selectable.has(step.key))
    .sort((left, right) => left.sort_order - right.sort_order);
  const firstProblemIndex = ordered.findIndex((step) => step.status === 'failed' || step.status === 'stopped');
  if (firstProblemIndex < 0) {
    return ordered.filter((step) => step.status !== 'skipped').map((step) => step.key);
  }
  return ordered
    .filter((step, index) => {
      if (step.status === 'skipped' || step.status === 'success') return false;
      return index >= firstProblemIndex;
    })
    .map((step) => step.key);
}

function localCommandPreview(draft?: EnvironmentDeploymentDraft) {
  if (!draft) return [] as Array<{ key: string; name: string; command: string; skipped?: boolean }>;
  const gpb = (draft.gpb_ips || []).join(',');
  const mode = draft.gpb_mode || '<GPB模式>';
  const tbMode = modeGpbCount(mode) === 1 ? (draft.tb_mode || '<TB定制模式>') : mode;
  const installMode = installModeForGpbMode(mode);
  const deploy = draft.simulation_mode === 'sim0_real'
    ? `cd ~ && sh deploy.sh "${draft.target_version}" sim0_real`
    : `cd ~ && sh ./deploy.sh "${draft.task_name}" ${draft.simulation_mode} "${draft.target_version}"${draft.include_sdk ? ' SDK' : ''}`;
  const tb = draft.simulation_mode === 'sim0_sil'
    ? `cd ~ && sh ~/SW2/lch/bin/testbench/tb_deploy.sh ${tbMode} ${draft.upper_ip} ${gpb || '<GPB IP>'} InnerSILPkg`
    : `${draft.simulation_mode}：跳过安装仿真`;
  const dhhArgs = draft.include_dhh
    ? ` --dhhIp=${draft.dhh_ip || '<DHH IP>'} --dhhUser=${draft.dhh_user || 'root'} --machineId=${draft.dhh_machine_id || 'TESTSPM'}`
    : '';
  const install = draft.simulation_mode === 'sim0_real'
    ? `cd ~ && echo 'y' | ./SW2/script/install.sh ${draft.upper_ip} ${realInstallSlotIps(draft).join(',')} ${draft.install_port} --lchUser=root machine`
    : `cd ~ && echo 'y' | sh ~/SW2/script/install.sh ${draft.upper_ip} ${gpb || '<GPB IP>'} ${draft.install_port} --lchUser=root ${installMode}${dhhArgs}`;
  const start = startCommandForMode(draft.simulation_mode);
  return [
    { key: 'stop', name: '停止上位机进程', command: initialStopCommand(draft) },
    { key: 'deploy', name: '拉取软件包', command: deploy },
    { key: 'tb_deploy', name: '安装仿真', command: tb, skipped: draft.simulation_mode !== 'sim0_sil' },
    { key: 'install', name: '安装软件包', command: install },
    { key: 'start', name: '启动环境', command: start },
    { key: 'post_start_script', name: '启动后脚本', command: draft.post_start_script || '', skipped: !draft.post_start_script?.trim() },
  ];
}

function mergeDeploymentSnapshot(current: EnvironmentDeployment | null, incoming: EnvironmentDeployment): EnvironmentDeployment {
  if (!current || current.id !== incoming.id) return incoming;
  return { ...current, ...incoming };
}

function mergeDeploymentState(
  current: EnvironmentDeployment,
  summary: EnvironmentDeploymentSummary,
  stepStates: DeploymentStepStateEvent[],
): EnvironmentDeployment {
  const previousByKey = new Map(current.steps.map((step) => [step.key, step]));
  const steps: EnvironmentDeploymentStep[] = stepStates.map((state) => ({
    ...(previousByKey.get(state.key) || { stdout: '', stderr: '', logs_included: false }),
    ...state,
    stdout: previousByKey.get(state.key)?.stdout || '',
    stderr: previousByKey.get(state.key)?.stderr || '',
    process_log: state.process_log ?? previousByKey.get(state.key)?.process_log ?? '',
    logs_included: false,
    created_at: state.created_at || previousByKey.get(state.key)?.created_at || '',
    updated_at: state.updated_at || previousByKey.get(state.key)?.updated_at || '',
  } as EnvironmentDeploymentStep));
  return { ...current, ...summary, steps };
}

export function EnvironmentDeploymentDialog({ environment, initialTask, onClose, onTaskChange, onDeploymentCompleted }: Props) {
  const [view, setView] = useState<ViewMode>('config');
  const [draft, setDraft] = useState<EnvironmentDeploymentDraft>();
  const [gpbIpText, setGpbIpText] = useState('');
  const [gpbModeMenuOpen, setGpbModeMenuOpen] = useState(false);
  const [postScriptMenuOpen, setPostScriptMenuOpen] = useState(false);
  const [task, setTask] = useState<EnvironmentDeployment | null>(null);
  const [history, setHistory] = useState<EnvironmentDeploymentSummary[]>([]);
  const [scheduledAtLocal, setScheduledAtLocal] = useState('');
  const [commandText, setCommandText] = useState('');
  const [sshTrust, setSshTrust] = useState<DeploymentSshTrustCheck | null>(null);
  const [timeSync, setTimeSync] = useState<DeploymentTimeSyncCheck | null>(null);
  const [timeSyncWarning, setTimeSyncWarning] = useState('');
  const [preflightBusy, setPreflightBusy] = useState('');
  const [preflightError, setPreflightError] = useState('');
  const [selectedStepKey, setSelectedStepKey] = useState('stop');
  const [parallelStageOpen, setParallelStageOpen] = useState(false);
  const [retrySelectionMode, setRetrySelectionMode] = useState(false);
  const [selectedExecutionSteps, setSelectedExecutionSteps] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const completedTaskIds = useRef<Set<number>>(new Set());
  const taskRef = useRef<EnvironmentDeployment | null>(null);
  // While live deployment events are arriving we normally follow the running step.
  // Once the operator explicitly clicks a stage, keep that selection pinned so a
  // subsequent state event cannot immediately steal focus back to the running step.
  const manualStepSelectionRef = useRef(false);
  const gpbModeComboboxRef = useRef<HTMLDivElement | null>(null);
  const postScriptComboboxRef = useRef<HTMLDivElement | null>(null);

  const commands = useMemo(() => draft?.commands?.length ? draft.commands : localCommandPreview(draft), [draft]);
  const selectedStep = useMemo(() => task?.steps.find((step) => step.key === selectedStepKey) || task?.steps[0], [task, selectedStepKey]);
  useEffect(() => {
    const handler = (event: Event) => {
      const custom = event as CustomEvent<{ context?: Record<string, unknown> }>;
      const context = custom.detail?.context;
      if (!context) return;
      context.page = 'environment-deployment';
      context.page_label = '环境部署';
      context.environment_id = environment.id;
      context.environment_name = environment.name;
      const selected = selectedStep;
      context.deployment = {
        view,
        environment_id: environment.id,
        environment_name: environment.name,
        deployment_id: task?.id || null,
        task_name: task?.task_name || draft?.task_name || '',
        status: task?.status || (view === 'config' ? 'configuring' : 'none'),
        current_step: task?.current_step || selected?.key || '',
        selected_step: selected ? {
          key: selected.key,
          name: selected.name,
          status: selected.status,
          message: selected.message || '',
          exit_status: selected.exit_status ?? null,
          retry_count: selected.retry_count,
        } : null,
        visible_log: (selected?.process_log || selected?.stdout || '').slice(-16000),
        steps: (task?.steps || []).slice(0, 12).map((step) => ({
          key: step.key,
          name: step.name,
          status: step.status,
          message: step.message || '',
          exit_status: step.exit_status ?? null,
          retry_count: step.retry_count,
        })),
        tool_hint: task ? {
          tool: 'get_deployment_detail',
          environment_id: environment.id,
          deployment_id: task.id,
          step_key: selected?.key || task.current_step || '',
        } : null,
      };
    };
    return registerPageContextReader(handler, 30);
  }, [environment.id, environment.name, view, task, draft?.task_name, selectedStep]);
  const parallelInitialSteps = useMemo(() => task?.steps.filter((step) => step.key === 'stop' || step.key === 'deploy') || [], [task]);
  const serialDeploymentSteps = useMemo(() => task?.steps.filter((step) => step.key !== 'stop' && step.key !== 'deploy') || [], [task]);
  const parallelStageStatus = useMemo(() => {
    const statuses = parallelInitialSteps.map((step) => step.status);
    if (statuses.includes('running')) return 'running';
    if (statuses.includes('failed')) return 'failed';
    if (statuses.includes('stopped')) return 'stopped';
    if (statuses.length > 0 && statuses.every((status) => status === 'skipped')) return 'skipped';
    if (statuses.length > 0 && statuses.every((status) => status === 'success' || status === 'skipped')) return 'success';
    return 'pending';
  }, [parallelInitialSteps]);
  const deploymentRunning = task?.status === 'pending' || task?.status === 'running' || task?.status === 'stopping';
  const taskControllable = Boolean(task && ['scheduled', 'pending', 'running', 'stopping'].includes(task.status));
  const realMode = draft?.simulation_mode === 'sim0_real';
  const requiredGpbCount = draft ? (realMode ? currentRealRequiredCount(draft) : (draft.required_gpb_count || modeGpbCount(draft.gpb_mode))) : 0;
  const currentGpbCount = draft?.gpb_ips.length || 0;
  const gpbCountValid = realMode ? currentGpbCount > 0 : requiredGpbCount > 0 && currentGpbCount === requiredGpbCount;
  const singleGpbMode = requiredGpbCount === 1;
  const tbCustomParts = parseTbCustomMode(draft?.tb_mode);
  const tbCustomModeValid = draft?.simulation_mode !== 'sim0_sil' || !singleGpbMode || isTbCustomMode(draft?.tb_mode);
  const preflightSignature = deploymentTargetSignature(draft);
  const sshTrustValid = Boolean(sshTrust?.all_trusted && sshTrust.target_signature === preflightSignature);
  const timeSyncValid = Boolean(timeSync?.all_synced && timeSync.target_signature === preflightSignature);
  const timeSyncWarningActive = Boolean(sshTrustValid && preflightBusy !== 'time' && !timeSyncValid);
  const dhhConfigValid = !draft?.include_dhh || Boolean(draft.dhh_ip?.trim() && draft.dhh_user?.trim() && draft.dhh_machine_id?.trim());
  const scheduledTimeValue = scheduledAtLocal ? new Date(scheduledAtLocal).getTime() : 0;
  const futureScheduled = Boolean(scheduledAtLocal && !Number.isNaN(scheduledTimeValue) && scheduledTimeValue > Date.now());
  const readinessItems = [
    { label: '目标版本号', ok: Boolean(draft?.target_version.trim()), warning: false },
    { label: '任务名', ok: Boolean(draft?.task_name.trim()), warning: false },
    { label: 'DISPLAY 环境变量（可选）', ok: true, warning: false },
    { label: realMode ? 'GPB IP' : 'GPB 模式 / IP 数量', ok: gpbCountValid, warning: false },
    { label: 'TB 定制模式', ok: tbCustomModeValid, warning: false },
    { label: 'DHH 参数', ok: dhhConfigValid, warning: false },
    { label: preflightBusy === 'ssh' ? 'SSH 互信（自动检测/建立中）' : 'SSH 互信', ok: sshTrustValid, warning: false },
    {
      label: preflightBusy === 'time' ? '时间同步（自动检测中）' : '时间同步（建议 ±1 秒）',
      ok: true,
      warning: timeSyncWarningActive,
    },
  ];
  const deploymentReady = readinessItems.every((item) => item.ok);

  useEffect(() => {
    function closeGpbModeMenu(event: MouseEvent) {
      if (!gpbModeComboboxRef.current?.contains(event.target as Node)) setGpbModeMenuOpen(false);
      if (!postScriptComboboxRef.current?.contains(event.target as Node)) setPostScriptMenuOpen(false);
    }
    document.addEventListener('mousedown', closeGpbModeMenu);
    return () => document.removeEventListener('mousedown', closeGpbModeMenu);
  }, []);

  useEffect(() => {
    if (view !== 'config' || !draft || !gpbCountValid || !dhhConfigValid || !preflightSignature) {
      setSshTrust(null);
      setTimeSync(null);
      setTimeSyncWarning('');
      setPreflightBusy('');
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      void (async () => {
        setPreflightBusy('ssh');
        setPreflightError('');
        setTimeSyncWarning('');
        try {
          const trust = await checkEnvironmentDeploymentSshTrust(environment.id, draft.gpb_ips, {
            include_dhh: draft.include_dhh,
            dhh_ip: draft.dhh_ip,
            dhh_user: draft.dhh_user,
          });
          if (cancelled) return;
          setSshTrust(trust);
          if (!trust.all_trusted) {
            setTimeSync(null);
            setPreflightBusy('');
            return;
          }
          setPreflightBusy('time');
          try {
            const synced = await syncEnvironmentDeploymentTime(environment.id, draft.gpb_ips, {
              include_dhh: draft.include_dhh,
              dhh_ip: draft.dhh_ip,
              dhh_user: draft.dhh_user,
            });
            if (cancelled) return;
            setTimeSync(synced);
            if (!synced.all_synced) {
              setTimeSyncWarning('上下位机时间未完全同步，不影响部署执行。');
            }
          } catch (exc) {
            if (cancelled) return;
            setTimeSync(null);
            setTimeSyncWarning(`时间同步检测/同步失败，不影响部署：${exc instanceof Error ? exc.message : String(exc)}`);
          }
        } catch (exc) {
          if (cancelled) return;
          setPreflightError(exc instanceof Error ? exc.message : String(exc));
        } finally {
          if (!cancelled) setPreflightBusy('');
        }
      })();
    }, 250);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [environment.id, view, preflightSignature, gpbCountValid, dhhConfigValid]);

  useEffect(() => {
    taskRef.current = task;
  }, [task]);

  useEffect(() => {
    let cancelled = false;

    async function loadDefaults() {
      const defaultsRaw = await getEnvironmentDeploymentDefaults(environment.id);
      if (cancelled) return;
      const defaults = normalizeDraft(defaultsRaw);
      setDraft(defaults);
      setGpbIpText(defaults.gpb_ips.join(','));
      setSshTrust(null);
      setTimeSync(null);
      setTimeSyncWarning('');
    }

    async function loadHistory() {
      const records = await listEnvironmentDeployments(environment.id);
      if (!cancelled) setHistory(records);
      return records;
    }

    async function openActive(summary: EnvironmentDeploymentSummary) {
      const stepKey = summary.current_step || 'stop';
      setView('progress');
      manualStepSelectionRef.current = false;
      setSelectedStepKey(stepKey);
      onTaskChange?.(summary);
      const latest = await getEnvironmentDeployment(environment.id, summary.id, stepKey);
      if (cancelled) return;
      taskRef.current = latest;
      setTask(latest);
      manualStepSelectionRef.current = false;
      setSelectedStepKey(latest.current_step || latest.steps.find((item) => item.status === 'running')?.key || stepKey);
    }

    async function load() {
      setLoading(true);
      setError('');
      try {
        const initialActive = initialTask && ['pending', 'running', 'stopping'].includes(initialTask.status) ? initialTask : null;
        if (initialActive) {
          // Reopening an active deployment must not wait for remote DISPLAY/stations
          // defaults. Fetch the authoritative task/log snapshot first so progress is
          // immediately visible even while remote machines are busy.
          await openActive(initialActive);
          if (!cancelled) setLoading(false);
          void loadHistory().catch((exc) => console.warn('deployment history load failed', exc));
          return;
        }

        // History/detail are DB-only requests and take priority over the remote-backed
        // defaults endpoint. This prevents a slow defaults SSH read from hiding an
        // already-running task when the dialog is reopened.
        const records = await loadHistory();
        if (cancelled) return;
        const latestSummary = records[0] || null;
        onTaskChange?.(latestSummary);
        if (latestSummary && ['pending', 'running', 'stopping'].includes(latestSummary.status)) {
          await openActive(latestSummary);
          if (!cancelled) setLoading(false);
          return;
        }

        await loadDefaults();
        if (cancelled) return;
        taskRef.current = null;
        setTask(null);
      } catch (exc) {
        if (!cancelled) setError(exc instanceof Error ? exc.message : String(exc));
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    void load();
    return () => { cancelled = true; };
  }, [environment.id]);

  useEffect(() => {
    if (!task || view !== 'progress') return;
    const deploymentId = task.id;
    return subscribeDeploymentRealtime((event) => {
      const current = taskRef.current;
      if (!current || current.id !== deploymentId) return;


      if (event.type === 'deployment.log' && event.deployment_id === deploymentId) {
        let gap = false;
        const next = {...current, steps: current.steps.map(step => {
          if (step.key !== event.step_key) return step;
          const result = appendDeploymentLog(step.process_log || '', event);
          gap = result.gap;
          return {...step, process_log: result.text};
        })};
        if (!gap) {taskRef.current = next; setTask(next);}
        return;
      }
      if (event.type !== 'deployment.state' || event.deployment.id !== deploymentId) return;
      const next = mergeDeploymentState(current, event.deployment, event.steps);
      taskRef.current = next;
      setTask(next);
      onTaskChange?.(event.deployment);
      const runningStep = next.steps.find((step) => step.status === 'running');
      const selected = next.steps.find((step) => step.key === selectedStepKey);
      if (!manualStepSelectionRef.current && runningStep && (!selectedStepKey || selected?.status === 'pending')) {
        setSelectedStepKey(runningStep.key);
      }

      if ((next.status === 'success' || next.status === 'failed' || next.status === 'stopped') && !completedTaskIds.current.has(next.id)) {
        completedTaskIds.current.add(next.id);
        void listEnvironmentDeployments(environment.id).then(setHistory).catch((exc) => console.warn('deployment history refresh failed', exc));
        if (next.status === 'success') void onDeploymentCompleted?.(next);
      }
    });
  }, [environment.id, task?.id, view, selectedStepKey]);

  useEffect(() => {
    if (!task?.id || view !== 'progress') return;
    let stale = false;
    let pending = false;
    const id = task.id;
    const reconcile = async () => {
      if (pending) return;
      pending = true;
      try {
        const value = await getEnvironmentDeployment(environment.id, id, selectedStepKey);
        if (!stale && taskRef.current?.id === id) {
          const current = taskRef.current;
          // A snapshot may race newer SSE chunks. Keep whichever contains more output.
          value.steps = value.steps.map(step => {
            const old = current.steps.find(s => s.key === step.key);
            return old && (old.process_log || '').length > (step.process_log || '').length ? {...step, process_log:old.process_log} : step;
          });
          taskRef.current = value; setTask(value);
        }
      } catch (e) { console.warn('deployment log reconciliation failed', e); }
      finally {pending = false;}
    };
    void reconcile();
    const timer = window.setInterval(() => void reconcile(), 2000);
    return () => {stale = true; window.clearInterval(timer);};
  }, [environment.id, task?.id, selectedStepKey, view]);

  function selectDeploymentStep(stepKey: string) {
    manualStepSelectionRef.current = true;
    setSelectedStepKey(stepKey);
  }

  function patchDraft(patch: Partial<EnvironmentDeploymentDraft>) {
    setDraft((current) => current ? ({ ...current, ...patch, selected_steps: undefined, commands: undefined }) : current);
  }

  function patchDhhDraft(patch: Partial<EnvironmentDeploymentDraft>) {
    patchDraft(patch);
    setSshTrust(null);
    setTimeSync(null);
    setTimeSyncWarning('');
  }

  function handleSimulationModeChange(mode: EnvironmentDeploymentDraft['simulation_mode']) {
    if (!draft) return;
    const patch: Partial<EnvironmentDeploymentDraft> = { simulation_mode: mode };
    if (mode === 'sim0_real') {
      patch.include_sdk = false;
      patch.include_dhh = false;
      patch.gpb_ips = draft.available_gpb_ips?.length ? [...draft.available_gpb_ips] : draft.gpb_ips;
      patch.required_gpb_count = patch.gpb_ips.length;
    }
    patchDraft(patch);
    if (patch.gpb_ips) setGpbIpText(patch.gpb_ips.join(','));
    setSshTrust(null);
    setTimeSync(null);
    setTimeSyncWarning('');
  }

  function handleGpbModeChange(mode: string) {
    if (!draft) return;
    const required = modeGpbCount(mode);
    if (required <= 0) {
      patchDraft({
        gpb_mode: mode,
        install_mode: installModeForGpbMode(mode),
        required_gpb_count: 0,
      });
      setSshTrust(null);
      setTimeSync(null);
      setTimeSyncWarning('');
      return;
    }

    const autoDetected = draft.available_gpb_ips || [];
    const currentIps = draft.gpb_ips || [];
    const mergedIps = [...currentIps];
    for (const host of autoDetected) {
      if (mergedIps.length >= required) break;
      if (!mergedIps.includes(host)) mergedIps.push(host);
    }
    const nextIps = mergedIps.slice(0, required);
    patchDraft({
      gpb_mode: mode,
      install_mode: installModeForGpbMode(mode),
      tb_mode: required === 1 ? DEFAULT_SINGLE_GPB_TB_MODE : mode,
      required_gpb_count: required,
      gpb_ips: nextIps,
    });
    setGpbIpText(nextIps.join(','));
    setSshTrust(null);
    setTimeSync(null);
    setTimeSyncWarning('');
  }

  function handleTbCustomModeChange(part: keyof TbCustomModeParts, value: number) {
    if (!draft) return;
    const current = parseTbCustomMode(draft.tb_mode);
    patchDraft({ tb_mode: formatTbCustomMode({ ...current, [part]: value }) });
  }

  function handleGpbIpTextChange(value: string) {
    setGpbIpText(value);
    patchDraft({ gpb_ips: parseGpbIpText(value) });
    setSshTrust(null);
    setTimeSync(null);
    setTimeSyncWarning('');
  }

  async function parseCommands() {
    setBusy('parse');
    setError('');
    try {
      const parsed = normalizeDraft(await parseEnvironmentDeploymentCommands(environment.id, commandText));
      setDraft(parsed);
      setGpbIpText(parsed.gpb_ips.join(','));
      setSshTrust(null);
      setTimeSync(null);
      setTimeSyncWarning('');
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy('');
    }
  }

  function updatePreviewCommand(key: string, command: string) {
    setDraft((current) => {
      if (!current) return current;
      const nextBase = key === 'post_start_script' ? { ...current, post_start_script: command } : current;
      const source = nextBase.commands?.length ? nextBase.commands : localCommandPreview(nextBase);
      return {
        ...nextBase,
        commands: source.map((item) => item.key === key ? { ...item, command, skipped: key === 'post_start_script' ? !command.trim() : item.skipped } : { ...item }),
      };
    });
  }

  function toggleExecutionStep(key: string, checked: boolean) {
    setSelectedExecutionSteps((current) => {
      if (checked) return current.includes(key) ? current : [...current, key];
      return current.filter((item) => item !== key);
    });
  }

  function selectAllExecutionSteps() {
    if (!draft) return;
    setSelectedExecutionSteps(DEPLOYMENT_STEP_KEYS.filter((key) => !isModeSkippedStep(key, draft)));
  }

  function clearExecutionSteps() {
    setSelectedExecutionSteps([]);
  }

  async function beginDeployment() {
    if (!draft) return;
    if (!gpbCountValid) {
      setError(realMode ? 'sim0_real 至少需要一个 GPB IP。' : `${draft.gpb_mode} 需要 ${requiredGpbCount} 个 GPB IP，当前填写 ${currentGpbCount} 个。`);
      return;
    }
    if (!tbCustomModeValid) {
      setError('单 GPB 的 TB 定制模式参数无效，请重新选择 GPB 数、机框和槽位。');
      return;
    }
    if (!dhhConfigValid) {
      setError('部署 DHH 时请完整填写 DHH IP、DHH User 和 Machine ID。');
      return;
    }
    if (!sshTrustValid) {
      setError(preflightError || '部署前 SSH 互信尚未通过，请先完成互信。');
      return;
    }
    const scheduledAtIso: string | null = futureScheduled ? new Date(scheduledTimeValue).toISOString() : null;
    const effectiveSelectedSteps = retrySelectionMode
      ? selectedExecutionSteps.filter((key) => !isModeSkippedStep(key, draft))
      : undefined;
    if (retrySelectionMode && !effectiveSelectedSteps?.length) {
      setError('请至少勾选一个本次需要执行的部署步骤。');
      return;
    }
    setBusy('deploy');
    setError('');
    try {
      const runtimeSensitive = !retrySelectionMode || Boolean(effectiveSelectedSteps?.some((key) => ['stop', 'install', 'start'].includes(key)));
      if (!futureScheduled && runtimeSensitive) {
        // Runtime warning is meaningful for immediate deployment. A scheduled task may
        // execute much later, so its preflight is intentionally rechecked by the backend.
        const runtimeStatus = await getEnvironmentRuntimeStatus(environment.id, false);
        const runningLowers = runtimeStatus.lowers.filter((item) => !item.skipped && item.tb_simulator_running);
        if (runningLowers.length > 0) {
          const machineByHost = new Map(environment.lower_machines.map((item) => [item.host, item]));
          const runningText = runningLowers
            .map((item) => {
              const machine = machineByHost.get(item.host);
              const label = machine?.station_name || machine?.name || '下位机';
              return `${label}（${item.host}）`;
            })
            .join('、');
          const confirmed = window.confirm(`检测到下位机仍在运行：${runningText}。\n\n本次所选步骤可能涉及停止或重启环境，是否继续？`);
          if (!confirmed) return;
        }
      }
      const requestDraft: EnvironmentDeploymentDraft = {
        ...draft,
        selected_steps: effectiveSelectedSteps,
      };
      const preview = normalizeDraft(await previewEnvironmentDeployment(environment.id, requestDraft));
      setDraft(preview);
      setGpbIpText(preview.gpb_ips.join(','));
      const created = await startEnvironmentDeployment(environment.id, preview, scheduledAtIso);
      setTask(created);
      onTaskChange?.(created);
      manualStepSelectionRef.current = false;
      setSelectedStepKey(created.steps.find((item) => item.status === 'running')?.key
        || created.steps.find((item) => item.status !== 'skipped')?.key
        || created.steps[0]?.key
        || 'stop');
      setView('progress');
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy('');
    }
  }

  async function retryFailedStep(stepKey: string) {
    if (!task) return;
    setBusy(`retry-${stepKey}`);
    setError('');
    try {
      completedTaskIds.current.delete(task.id);
      const retried = await retryEnvironmentDeploymentStep(environment.id, task.id, stepKey);
      setTask(retried);
      onTaskChange?.(retried);
      manualStepSelectionRef.current = false;
      setSelectedStepKey(stepKey);
      setView('progress');
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy('');
    }
  }

  async function stopCurrentDeployment() {
    if (!task || !['scheduled', 'pending', 'running', 'stopping'].includes(task.status)) return;
    const confirmed = window.confirm(task.status === 'scheduled' ? '取消这个定时部署任务？' : '停止部署任务？\n\n将终止当前任务正在执行的远端部署脚本，并停止后续阶段。只会终止当前部署任务记录的脚本进程。');
    if (!confirmed) return;
    setBusy('stop-deployment');
    setError('');
    try {
      const stopping = await stopEnvironmentDeployment(environment.id, task.id);
      taskRef.current = stopping;
      setTask(stopping);
      onTaskChange?.(stopping);
      setView('progress');
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy('');
    }
  }

  async function openHistory() {
    setBusy('history');
    setError('');
    try {
      setHistory(await listEnvironmentDeployments(environment.id));
      setView('history');
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy('');
    }
  }

  async function openHistoryTask(item: EnvironmentDeploymentSummary) {
    setBusy(`task-${item.id}`);
    try {
      const detail = await getEnvironmentDeployment(environment.id, item.id);
      setTask(detail);
      manualStepSelectionRef.current = false;
      setSelectedStepKey(detail.current_step || detail.steps[0]?.key || 'stop');
      setView('progress');
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy('');
    }
  }

  function resetConfigFromTask() {
    if (task?.configuration) {
      const restoredMode = task.configuration.simulation_mode;
      const restored = normalizeDraft({
        ...task.configuration,
        selected_steps: undefined,
        recent_versions: draft?.recent_versions,
        saved_post_start_scripts: draft?.saved_post_start_scripts,
        available_gpb_ips: draft?.available_gpb_ips,
        available_gpb_modes: draft?.available_gpb_modes,
        commands: task.command_snapshot.map((item) => {
          if (item.key === 'stop') return { ...item, skipped: false, command: initialStopCommand(task.configuration) };
          if (item.key === 'start') return { ...item, skipped: false, command: startCommandForMode(restoredMode) };
          if (item.key === 'post_start_script') return { ...item, skipped: !task.configuration.post_start_script?.trim(), command: task.configuration.post_start_script || item.command };
          if (item.key === 'tb_deploy') return { ...item, skipped: restoredMode !== 'sim0_sil' };
          return { ...item, skipped: false };
        }),
      } as LegacyDeploymentDraft);
      setDraft(restored);
      setGpbIpText(restored.gpb_ips.join(','));
      setRetrySelectionMode(true);
      setSelectedExecutionSteps(copiedRetryDefaultSteps(task));
      setSshTrust(null);
      setTimeSync(null);
      setTimeSyncWarning('');
    }
    setView('config');
  }

  return <div className="resource-modal-backdrop deployment-backdrop" onMouseDown={onClose}>
    <section className="resource-modal deployment-modal" role="dialog" aria-modal="true" onMouseDown={(event) => event.stopPropagation()}>
      <header className="deployment-modal-header">
        <div>
          <span className="eyebrow">AUTOMATED DEPLOYMENT</span>
          <h2>部署环境 · {environment.name}</h2>
          <p>{environment.upper_machine.host} · {environment.lower_machines.length} 台下位机</p>
        </div>
        <div className="deployment-header-actions">
          <button type="button" className="button secondary" onClick={() => void openHistory()} disabled={busy === 'history'}><History size={16} /> 部署任务</button>
          <button type="button" className="icon-button" onClick={onClose}><X size={20} /></button>
        </div>
      </header>

      {error && <div className="resource-alert error deployment-alert"><XCircle size={16} />{error}</div>}
      {loading ? <div className="deployment-loading"><LoaderCircle className="spin" /> 正在读取部署配置...</div> : null}

      {!loading && view === 'config' && draft && <div className="deployment-config-layout">
        <div className="deployment-config-scroll">
          <section className="deployment-form-section">
            <div className="deployment-section-title"><Rocket size={18} /><div><h3>部署参数</h3></div></div>
            <div className="deployment-field-grid two">
              <label><span>目标版本号 *</span><input list={`deployment-versions-${environment.id}`} value={draft.target_version} placeholder="选择或输入目标版本" onChange={(event) => patchDraft({ target_version: event.target.value })} /><datalist id={`deployment-versions-${environment.id}`}>{(draft.recent_versions || []).map((item) => <option value={item} key={item} />)}</datalist></label>
              <label><span>任务名</span><input value={draft.task_name} onChange={(event) => patchDraft({ task_name: event.target.value })} /></label>
              <label><span>仿真模式</span><select value={draft.simulation_mode} onChange={(event) => handleSimulationModeChange(event.target.value as EnvironmentDeploymentDraft['simulation_mode'])}><option value="sim0_sil">sim0_sil</option><option value="sim2">sim2</option><option value="sim0_real">sim0_real</option></select></label>
              <label><span>安装端口</span><select value={draft.install_port} onChange={(event) => patchDraft({ install_port: Number(event.target.value) })}>{Array.from({ length: 12 }, (_, index) => <option value={index} key={index}>{index}</option>)}</select></label>
              <label><span>DISPLAY 环境变量</span><input value={draft.display_env || ''} placeholder="留空则不设置" onChange={(event) => patchDraft({ display_env: event.target.value })} /></label>
            </div>
            <div className="deployment-check-row"><label><input type="checkbox" checked={Boolean(draft.precheck_stop_lower)} onChange={(event) => patchDraft({ precheck_stop_lower: event.target.checked })} /> 预检查停止下位机进程</label></div>
            {!realMode && <div className="deployment-check-row"><label><input type="checkbox" checked={draft.include_sdk} onChange={(event) => patchDraft({ include_sdk: event.target.checked })} /> 拉取 SDK 包</label><span>deploy.sh 最后追加 SDK 参数</span></div>}
            {!realMode && <div className="deployment-check-row"><label><input type="checkbox" checked={Boolean(draft.include_dhh)} onChange={(event) => patchDhhDraft({ include_dhh: event.target.checked })} /> 部署 DHH</label></div>}
            {!realMode && draft.include_dhh && <div className="deployment-dhh-grid">
              <label><span>DHH IP *</span><input value={draft.dhh_ip || ''} placeholder="请输入 DHH IP" onChange={(event) => patchDhhDraft({ dhh_ip: event.target.value })} /></label>
              <label><span>DHH User *</span><input value={draft.dhh_user ?? ''} onChange={(event) => patchDhhDraft({ dhh_user: event.target.value })} /></label>
              <label><span>Machine ID *</span><input value={draft.dhh_machine_id ?? ''} onChange={(event) => patchDhhDraft({ dhh_machine_id: event.target.value })} /></label>
            </div>}
          </section>

          <section className="deployment-form-section">
            <div className="deployment-section-title"><Terminal size={18} /><div><h3>部署拓扑</h3></div></div>
            <div className="deployment-readonly-line"><span>上位机</span><strong>{draft.upper_ip}</strong></div>
            <div className="deployment-topology-stack">
              {!realMode ? <div className={`deployment-mode-row ${singleGpbMode ? 'single-gpb' : ''}`}>
                <div className="deployment-gpb-mode-field">
                  <span>GPB 部署模式 *</span>
                  <div className={`deployment-gpb-mode-combobox ${gpbModeMenuOpen ? 'open' : ''}`} ref={gpbModeComboboxRef}>
                    <input
                      className="deployment-gpb-mode-input"
                      value={draft.gpb_mode}
                      placeholder="选择或输入 GPB 部署模式"
                      role="combobox"
                      aria-expanded={gpbModeMenuOpen}
                      aria-controls={`deployment-gpb-modes-${environment.id}`}
                      aria-autocomplete="list"
                      onFocus={() => setGpbModeMenuOpen(true)}
                      onClick={() => setGpbModeMenuOpen(true)}
                      onKeyDown={(event) => {
                        if (event.key === 'Escape') setGpbModeMenuOpen(false);
                        if (event.key === 'ArrowDown') setGpbModeMenuOpen(true);
                      }}
                      onChange={(event) => {
                        handleGpbModeChange(event.target.value);
                        setGpbModeMenuOpen(true);
                      }}
                    />
                    {gpbModeMenuOpen && <div className="deployment-gpb-mode-menu" id={`deployment-gpb-modes-${environment.id}`} role="listbox">
                      {(draft.available_gpb_modes || []).map((mode) => <button
                        type="button"
                        role="option"
                        aria-selected={mode === draft.gpb_mode}
                        className={mode === draft.gpb_mode ? 'selected' : ''}
                        key={mode}
                        onMouseDown={(event) => event.preventDefault()}
                        onClick={() => { handleGpbModeChange(mode); setGpbModeMenuOpen(false); }}
                      >{mode}</button>)}
                    </div>}
                  </div>
                </div>
                {singleGpbMode && <div className="deployment-tb-custom-field">
                  <span>TB 定制模式 *</span>
                  <div className="deployment-tb-custom-selects">
                    <label><span>GPB 数</span><select value={tbCustomParts.totalGpb} onChange={(event) => handleTbCustomModeChange('totalGpb', Number(event.target.value))}>{TB_TOTAL_GPB_OPTIONS.map((value) => <option value={value} key={value}>{value}</option>)}</select></label>
                    <label><span>机框</span><select value={tbCustomParts.frame} onChange={(event) => handleTbCustomModeChange('frame', Number(event.target.value))}>{TB_FRAME_OPTIONS.map((value) => <option value={value} key={value}>{value}</option>)}</select></label>
                    <label><span>槽位</span><select value={tbCustomParts.slot} onChange={(event) => handleTbCustomModeChange('slot', Number(event.target.value))}>{TB_SLOT_OPTIONS.map((value) => <option value={value} key={value}>{value}</option>)}</select></label>
                  </div>
                </div>}
              </div> : null}
              <label><span>GPB IP *</span><textarea className="deployment-gpb-ip-input" rows={3} value={gpbIpText} placeholder="使用逗号分隔多个 IP" onChange={(event) => handleGpbIpTextChange(event.target.value)} /></label>
            </div>
            <div className={`deployment-gpb-count-status ${gpbCountValid ? 'valid' : 'invalid'}`}>
              {realMode ? <strong>{currentGpbCount} GPB</strong> : <strong>{currentGpbCount} / {requiredGpbCount}</strong>}
            </div>
            <div className="deployment-trust-panel">
              <div className="deployment-trust-head">
                <div><strong>SSH 互信</strong><span className={`deployment-auto-check ${sshTrustValid ? 'ready' : preflightBusy === 'ssh' ? 'checking' : 'waiting'}`}>{preflightBusy === 'ssh' ? <LoaderCircle className="spin" size={13} /> : sshTrustValid ? <CheckCircle2 size={13} /> : <Circle size={13} />}{preflightBusy === 'ssh' ? '自动检测/建立中' : sshTrustValid ? '已互信' : '自动检测/建立'}</span></div>
              </div>
              {sshTrust ? <div className="deployment-trust-list">{sshTrust.results.map((item) => <div className={`deployment-trust-item ${item.trusted ? 'trusted' : 'missing'}`} key={`${item.role}-${item.host}`}>
                <div><span>{item.trusted ? <CheckCircle2 size={15} /> : <XCircle size={15} />}</span><strong>{item.host}{item.role === 'dhh' ? ' · DHH' : ''}</strong><em>{item.trusted ? '已互信' : '互信失败'}</em></div>
              </div>)}</div> : null}
            </div>
            <div className={`deployment-time-sync-panel ${timeSyncWarningActive ? 'warning' : ''}`}>
              <div className="deployment-time-sync-head">
                <div><strong>时间同步</strong><span className={`deployment-auto-check ${timeSyncValid ? 'ready' : preflightBusy === 'time' ? 'checking' : timeSyncWarningActive ? 'warning' : 'waiting'}`}>{preflightBusy === 'time' ? <LoaderCircle className="spin" size={13} /> : timeSyncValid ? <CheckCircle2 size={13} /> : timeSyncWarningActive ? <AlertTriangle size={13} /> : <Circle size={13} />}{preflightBusy === 'time' ? '自动检测/同步中' : timeSyncValid ? '已同步（±1 秒）' : timeSyncWarningActive ? '未同步 · 不影响部署' : '等待互信后自动检测'}</span></div>
              </div>
              {timeSyncWarningActive && <div className="deployment-time-sync-warning"><AlertTriangle size={14} /><span>{timeSyncWarning || '上下位机时间未同步，部署仍可继续。'}</span></div>}
              {timeSync ? <div className="deployment-time-sync-list">{timeSync.results.map((item) => <div className={`deployment-time-sync-item ${item.success ? 'synced' : 'warning'}`} key={`${item.role}-${item.host}`}>
                <div><span>{item.success ? <CheckCircle2 size={15} /> : <AlertTriangle size={15} />}</span><strong>{item.host}{item.role === 'dhh' ? ' · DHH' : ''}</strong><em>{item.success ? '已同步' : '未同步'}</em></div>
                <span>{item.after_delta_seconds == null ? item.message : `${item.after_delta_seconds > 0 ? '+' : ''}${item.after_delta_seconds}s`}</span>
              </div>)}</div> : null}
            </div>
          </section>

          <section className="deployment-form-section">
            <div className="deployment-section-title"><WandSparkles size={18} /><div><h3>从现有命令解析</h3><p>粘贴 deploy / tb_deploy / install 命令后自动回填部署参数，粘贴内容不会直接执行。</p></div></div>
            <textarea className="deployment-command-parser" rows={7} value={commandText} placeholder="粘贴部署命令" onChange={(event) => setCommandText(event.target.value)} />
            <div className="deployment-inline-actions"><button type="button" className="button secondary" onClick={() => void parseCommands()} disabled={!commandText.trim() || busy === 'parse'}>{busy === 'parse' ? <LoaderCircle className="spin" size={15} /> : <WandSparkles size={15} />} 解析命令并回填</button></div>
          </section>
        </div>

        <aside className="deployment-preview-panel">
          <div className="deployment-section-title"><PackageCheck size={18} /><div><h3>执行预览</h3><p>{retrySelectionMode ? '复制重试部署：勾选本次需要执行的步骤，未勾选步骤将直接跳过。' : '可直接修改，部署时按最终确认的命令执行。'}</p></div></div>
          {retrySelectionMode && <div className="deployment-retry-selection-toolbar">
            <span>本次执行 {selectedExecutionSteps.filter((key) => !isModeSkippedStep(key, draft)).length} 项</span>
            <div><button type="button" onClick={selectAllExecutionSteps}>全选</button><button type="button" onClick={clearExecutionSteps}>清空</button></div>
          </div>}
          <div className="deployment-command-list">{commands.map((item, index) => {
            const stageLabel = item.key === 'stop' ? '1.1' : item.key === 'deploy' ? '1.2' : String(Math.max(2, index));
            const parallel = item.key === 'stop' || item.key === 'deploy';
            const modeSkipped = isModeSkippedStep(item.key, draft);
            const selectedForRetry = !retrySelectionMode || selectedExecutionSteps.includes(item.key);
            const skippedThisRun = modeSkipped || !selectedForRetry;
            return <article className={`${skippedThisRun ? 'skipped' : ''} ${parallel ? 'parallel' : ''}`} key={item.key}>
              {retrySelectionMode && <input className="deployment-step-select" type="checkbox" aria-label={`执行 ${item.name}`} checked={selectedForRetry && !modeSkipped} disabled={modeSkipped} onChange={(event) => toggleExecutionStep(item.key, event.target.checked)} />}
              <div>
                <span>{stageLabel}</span><strong>{item.key === 'post_start_script' ? '启动后脚本（可选）' : item.name}</strong>{parallel && <em className="parallel-badge">并行</em>}{modeSkipped && <em>当前模式跳过</em>}{retrySelectionMode && !modeSkipped && !selectedForRetry && <em>本次跳过</em>}
              </div>
              {item.key === 'post_start_script' ? <section className="deployment-post-script-step-editor">
                <div className="deployment-post-script-head"><span>可选 · 启动成功后执行</span><label><input type="checkbox" checked={Boolean(draft.save_post_start_script)} disabled={!draft.post_start_script?.trim()} onChange={(event) => patchDraft({ save_post_start_script: event.target.checked })} /> 保存到当前环境</label></div>
                <div className="deployment-post-script-combobox" ref={postScriptComboboxRef}>
                  <textarea
                    rows={3}
                    value={item.command}
                    placeholder="启动成功后执行，例如：cd ~/SW && ./post_deploy.sh"
                    onFocus={() => setPostScriptMenuOpen(true)}
                    onClick={() => setPostScriptMenuOpen(true)}
                    onChange={(event) => { updatePreviewCommand(item.key, event.target.value); setPostScriptMenuOpen(true); }}
                  />
                  {postScriptMenuOpen && Boolean(draft.saved_post_start_scripts?.length) && <div className="deployment-post-script-menu">
                    {(draft.saved_post_start_scripts || []).map((script) => <button type="button" key={script} onMouseDown={(event) => event.preventDefault()} onClick={() => { updatePreviewCommand(item.key, script); setPostScriptMenuOpen(false); }}><code>{script}</code></button>)}
                  </div>}
                </div>
                <small>此步骤位于“启动环境”之后；点击输入框可选择该环境已保存的历史脚本。</small>
              </section> : <textarea rows={3} value={item.command} disabled={skippedThisRun} onChange={(event) => updatePreviewCommand(item.key, event.target.value)} />}
            </article>;
          })}</div>
        </aside>
      </div>}

      {!loading && view === 'progress' && task && <div className="deployment-progress-layout">
        <aside className="deployment-timeline-panel">
          <div className="deployment-task-summary">
            <div className={`deployment-task-state ${task.status}`}>{task.status === 'scheduled' ? <Clock3 size={20} /> : task.status === 'running' || task.status === 'pending' || task.status === 'stopping' ? <LoaderCircle className="spin" size={20} /> : task.status === 'success' ? <CheckCircle2 size={20} /> : task.status === 'stopped' ? <Square size={18} /> : <XCircle size={20} />}<strong>{task.status_label}</strong></div>
            <h3>{task.target_version}</h3>
            <p>{task.task_name}</p>
            {task.message && <div className={`deployment-task-message ${task.status}`}>{task.message}</div>}
            <dl>{task.scheduled_at && <div><dt>计划</dt><dd>{formatDateTime(task.scheduled_at)}</dd></div>}<div><dt>触发者</dt><dd>{task.trigger_operator || '—'}</dd></div><div><dt>来源 IP</dt><dd>{task.trigger_client_ip || '—'}</dd></div><div><dt>入口</dt><dd>{deploymentSourceLabel(task.trigger_source)}</dd></div><div><dt>开始</dt><dd>{formatDateTime(task.started_at)}</dd></div><div><dt>耗时</dt><dd>{durationText(task)}</dd></div></dl>
          </div>
          <div className="deployment-step-timeline">
            <details className={`deployment-parallel-stage ${parallelStageStatus}`} open={parallelStageOpen} onToggle={(event) => setParallelStageOpen(event.currentTarget.open)}>
              <summary>
                <span className={`deployment-parallel-stage-icon ${parallelStageStatus}`}>{statusIcon(parallelStageStatus)}</span>
                <span className="deployment-parallel-stage-copy"><strong>环境准备</strong><small>停止上位机进程与拉取软件包并行执行</small></span>
                <em>并行</em>
              </summary>
              <div className="deployment-parallel-branches">
                {parallelInitialSteps.map((step) => <div className={`deployment-parallel-branch ${step.status}`} key={step.key}>
                  <button type="button" className={`deployment-parallel-branch-button ${selectedStep?.key === step.key ? 'active' : ''}`} onClick={() => selectDeploymentStep(step.key)}>
                    <span className="deployment-parallel-branch-index">{step.key === 'stop' ? '1.1' : '1.2'}</span>
                    <span className="deployment-parallel-branch-icon">{statusIcon(step.status)}</span>
                    <span><strong>{step.name}</strong><small>{STATUS_TEXT[step.status] || step.status}{step.started_at ? ` · ${formatDateTime(step.started_at)}` : ''}</small></span>
                  </button>
                  {step.status === 'failed' && task.status === 'failed' && <button type="button" className="deployment-step-retry deployment-parallel-retry" disabled={busy === `retry-${step.key}`} onClick={() => void retryFailedStep(step.key)}>
                    {busy === `retry-${step.key}` ? <LoaderCircle className="spin" size={14} /> : <RotateCcw size={14} />} 重试
                  </button>}
                </div>)}
              </div>
            </details>
            <div className="deployment-serial-stage-list">
              {serialDeploymentSteps.map((step) => <div className={`deployment-step-node-row ${step.status}`} key={step.key}>
                <button type="button" className={`deployment-step-node ${step.status} ${selectedStep?.key === step.key ? 'active' : ''}`} onClick={() => selectDeploymentStep(step.key)}>
                  <span className="deployment-step-icon">{statusIcon(step.status)}</span>
                  <span className="deployment-step-copy"><strong>{step.name}</strong><small>{STATUS_TEXT[step.status] || step.status}{step.started_at ? ` · ${formatDateTime(step.started_at)}` : ''}{step.retry_count > 0 ? ` · 重试 ${step.retry_count}` : ''}</small></span>
                </button>
                {step.status === 'failed' && task.status === 'failed' && <button type="button" className="deployment-step-retry" disabled={busy === `retry-${step.key}`} onClick={() => void retryFailedStep(step.key)}>
                  {busy === `retry-${step.key}` ? <LoaderCircle className="spin" size={14} /> : <RotateCcw size={14} />} 重试
                </button>}
              </div>)}
            </div>
          </div>
          {!taskControllable && <button type="button" className="button secondary deployment-reuse-button" onClick={resetConfigFromTask}>复制重试部署</button>}
        </aside>

        <section className="deployment-log-panel deployment-execution-detail-panel">
          {selectedStep ? <>
            <header><div><span className={`deployment-log-step-status ${selectedStep.status}`}>{statusIcon(selectedStep.status)} {STATUS_TEXT[selectedStep.status] || selectedStep.status}</span><h3>{selectedStep.name}</h3><p>{selectedStep.message || '等待步骤执行。'}</p></div><div className="deployment-log-metadata"><span>当前阶段：{selectedStep.name}</span><span>退出码：{selectedStep.exit_status ?? '—'}</span></div></header>
            <div className="deployment-trigger-strip"><span>触发者 <strong>{task.trigger_operator || '—'}</strong></span><span>来源 IP <strong>{task.trigger_client_ip || '—'}</strong></span><span>入口 <strong>{deploymentSourceLabel(task.trigger_source)}</strong></span></div>
            <div className="deployment-step-parameter-panel">
              <h4>本步骤参数</h4>
              {Object.entries(selectedStep.parameters || {}).length ? <dl>{Object.entries(selectedStep.parameters || {}).map(([key, value]) => <div key={key}><dt>{key}</dt><dd title={displayDeploymentParameter(value)}>{displayDeploymentParameter(value)}</dd></div>)}</dl> : <div className="deployment-parameter-empty">当前步骤没有额外参数。</div>}
            </div>
            <div className="deployment-log-command"><span>执行命令</span><code>{selectedStep.command || '当前模式跳过此步骤'}</code></div>
            <DeploymentLogView key={`${task.id}:${selectedStep.key}`} text={selectedStep.process_log || `${selectedStep.stdout || ''}${selectedStep.stderr || ''}`} taskId={task.id} stepKey={selectedStep.key} />
          </> : <div className="deployment-log-empty">暂无部署步骤。</div>}
        </section>
      </div>}

      {!loading && view === 'history' && <div className="deployment-history-view">
        <div className="deployment-history-title"><div><h3>部署任务</h3><p>查看待执行、执行中及已完成的部署任务。</p></div><button type="button" className="button secondary" onClick={() => setView(task && ['scheduled', 'running', 'pending', 'stopping'].includes(task.status) ? 'progress' : 'config')}>返回</button></div>
        <div className="deployment-history-list">{history.length ? history.map((item) => <button type="button" className="deployment-history-row" key={item.id} onClick={() => void openHistoryTask(item)}>
          <span className={`deployment-history-status ${item.status}`}>{item.status === 'scheduled' ? <Clock3 size={17} /> : item.status === 'running' || item.status === 'pending' || item.status === 'stopping' ? <LoaderCircle className="spin" size={17} /> : item.status === 'success' ? <CheckCircle2 size={17} /> : item.status === 'stopped' ? <Square size={15} /> : <XCircle size={17} />}{item.status_label}</span>
          <strong>{item.target_version}</strong><span>{item.task_name}</span><span>{item.simulation_mode}</span><span>{item.gpb_ips.length} GPB</span><span>{item.scheduled_at ? `计划 ${formatDateTime(item.scheduled_at)}` : formatDateTime(item.created_at)}</span><span>{durationText(item)}</span>
        </button>) : <div className="deployment-history-empty"><History size={30} /><p>当前环境还没有部署任务。</p></div>}</div>
      </div>}

      <footer className="deployment-modal-footer">
        <div className="deployment-footer-message">{task && view === 'progress' ? task.message : '部署过程较长，关闭窗口不会中断已开始的部署任务。'}</div>
        <div>{view === 'config' && <div className="deployment-submit-row"><div className={`deployment-schedule-control ${futureScheduled ? 'scheduled' : ''}`} title="可选：选择未来时间后将创建预约部署任务"><label htmlFor="deployment-scheduled-at">预约时间</label><div className="deployment-schedule-input"><Clock3 size={15} /><input id="deployment-scheduled-at" type="datetime-local" value={scheduledAtLocal} onChange={(event) => setScheduledAtLocal(event.target.value)} /></div></div><div className="deployment-submit-control"><button type="button" className="button primary deployment-start-button" onClick={() => void beginDeployment()} disabled={!draft || !deploymentReady || busy === 'deploy'}>{busy === 'deploy' ? <LoaderCircle className="spin" size={17} /> : futureScheduled ? <Clock3 size={17} /> : <Play size={17} />} {futureScheduled ? '预约部署' : '立即部署'}</button><div className="deployment-readiness-tooltip"><strong>部署检查</strong>{readinessItems.map((item) => <span className={item.warning ? 'warning' : item.ok ? 'ok' : 'missing'} key={item.label}>{item.warning ? '⚠' : item.ok ? '✓' : '○'} {item.label}</span>)}{preflightError && <em>{preflightError}</em>}</div></div></div>}{view === 'progress' && taskControllable && <div className="deployment-running-actions"><span className={`deployment-running-pill ${task?.status === 'scheduled' ? 'scheduled' : ''}`}>{task?.status === 'scheduled' ? <Clock3 size={16} /> : <LoaderCircle className="spin" size={16} />} {task?.status === 'scheduled' ? `待执行 · ${formatDateTime(task.scheduled_at)}` : task?.status === 'stopping' ? '正在停止当前脚本' : '部署中'}</span>{task && ['scheduled', 'pending', 'running', 'stopping'].includes(task.status) && <button type="button" className="button danger-outline deployment-stop-button" onClick={() => void stopCurrentDeployment()} disabled={busy === 'stop-deployment'}>{busy === 'stop-deployment' ? <LoaderCircle className="spin" size={15} /> : <Square size={14} />} {task.status === 'scheduled' ? '取消任务' : '停止任务'}</button>}</div>}</div>
      </footer>
    </section>
  </div>;
}
