import { registerPageContextReader } from '../assistant/contextRegistry';
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock3,
  Database,
  FileSearch,
  Folder,
  FolderOpen,
  FolderPlus,
  Gauge,
  Laptop,
  LoaderCircle,
  Network,
  Pencil,
  Play,
  Plus,
  PowerOff,
  RefreshCw,
  Search,
  Server,
  Settings2,
  Star,
  Terminal,
  Trash2,
  X,
} from 'lucide-react';
import {
  bulkEnvironmentAction,
  createEnvironmentFolder,
  createMachine,
  deleteEnvironment,
  deleteEnvironmentFolder,
  discoverEnvironment,
  getEnvironmentRuntimeStatus,
  getLatestEnvironmentDeployment,
  getLogTree,
  listActiveEnvironmentDeployments,
  getResourceSettings,
  listEnvironmentFolders,
  listEnvironments,
  queryVersion,
  runEnvironmentProcessControl,
  syncLowerMachineTime,
  testMachineDraft,
  updateEnvironment,
  updateEnvironmentFolder,
  updateMachine,
  disconnectMachineSession,
  type EnvironmentFolder,
  type EnvironmentLogTree,
  type EnvironmentRuntimeStatus,
  type EnvironmentDeployment,
  type EnvironmentDeploymentSummary,
  type EnvironmentSummary,
  type MachineDraftPayload,
} from '../api/resourceApi';
import { EnvironmentDeploymentDialog } from './EnvironmentDeploymentDialog';
import { subscribeDeploymentRealtime } from '../services/deploymentRealtime';
import {
  loadFavoriteEnvironmentIds,
  pruneFavoriteEnvironments,
  subscribeFavoriteEnvironments,
  toggleFavoriteEnvironment,
  watchFavoriteEnvironmentsStorage,
} from '../services/favoriteEnvironments';

interface Props {
  initialEnvironmentId?: number;
  onOpenLogLocator: (environment: EnvironmentSummary) => void;
  onOpenCpdReports: (environment: EnvironmentSummary) => void;
  onOpenCatalogSettings: () => void;
}

type EditDraft = {
  name: string;
  description: string;
  folder?: number | null;
  host: string;
  username: string;
  ssh_port: number;
  password: string;
};

type DhhEditDraft = {
  machineId: number;
  host: string;
  username: string;
  ssh_port: number;
  password: string;
  hasCredential: boolean;
};

const emptyMachineDraft: MachineDraftPayload = {
  host: '', ssh_port: 22, username: '', auth_type: 'password', password: '', role: 'upper',
};

const RESOURCE_TABS_STORAGE_KEY = 'tracelens-resource-tabs-v1';
const ACTIVE_RESOURCE_STORAGE_KEY = 'tracelens-active-resource-v1';
const RESOURCE_MESSAGES_STORAGE_KEY = 'tracelens-resource-messages-v1';
const RESOURCE_ERRORS_STORAGE_KEY = 'tracelens-resource-errors-v1';
const RESOURCE_FOLDER_COLLAPSE_STORAGE_KEY = 'tracelens-resource-folder-collapse-v1';

function loadStoredResourceTabs(): number[] {
  if (typeof window === 'undefined') return [];
  try {
    const value = JSON.parse(window.sessionStorage.getItem(RESOURCE_TABS_STORAGE_KEY) || '[]');
    return Array.isArray(value) ? value.filter((item): item is number => Number.isInteger(item)) : [];
  } catch { return []; }
}

function loadStoredActiveResource(): number | undefined {
  if (typeof window === 'undefined') return undefined;
  const value = Number(window.sessionStorage.getItem(ACTIVE_RESOURCE_STORAGE_KEY));
  return Number.isInteger(value) && value > 0 ? value : undefined;
}


function loadStoredCollapsedFolders(): Set<string> {
  if (typeof window === 'undefined') return new Set();
  try {
    const value = JSON.parse(window.localStorage.getItem(RESOURCE_FOLDER_COLLAPSE_STORAGE_KEY) || '[]');
    return new Set(Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : []);
  } catch { return new Set(); }
}

function loadStoredResourceNotices(key: string): Record<number, string> {
  if (typeof window === 'undefined') return {};
  try {
    const value = JSON.parse(window.sessionStorage.getItem(key) || '{}');
    if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
    return Object.fromEntries(
      Object.entries(value)
        .filter(([id, text]) => Number.isInteger(Number(id)) && Number(id) > 0 && typeof text === 'string')
        .map(([id, text]) => [Number(id), text as string]),
    );
  } catch { return {}; }
}

function statusDot(active: boolean | undefined, title: string) {
  const tone = active === undefined ? 'unknown' : active ? 'online' : 'offline';
  return <span className={`resource-runtime-dot ${tone}`} title={title} />;
}

function isSmallNetworkLower(host: string | undefined): boolean {
  return String(host || '').trim().startsWith('192.');
}

function isAtLogDualIpLower(machine: { host: string; station?: Record<string, unknown> }): boolean {
  return Boolean(String(machine.station?.topology_ip || '').trim()) && String(machine.station?.source || '').trim().toLowerCase() === 'atlog';
}

function formatTimeDelta(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '偏差未知';
  const absolute = Math.abs(Math.round(seconds));
  if (absolute < 60) return `偏差 ${absolute} 秒`;
  if (absolute < 3600) return `偏差 ${Math.round(absolute / 60)} 分钟`;
  if (absolute < 86400) return `偏差 ${(absolute / 3600).toFixed(1)} 小时`;
  return `偏差 ${(absolute / 86400).toFixed(1)} 天`;
}

interface EnvironmentMachineGridProps {
  environment: EnvironmentSummary;
  runtime?: EnvironmentRuntimeStatus;
  busy?: string;
  onSyncLowerTime?: (machineId: number, host: string) => void;
  onEditDhh?: (machine: NonNullable<EnvironmentSummary['dhh_machine']>) => void;
}

export function EnvironmentMachineGrid({ environment, runtime, busy = '', onSyncLowerTime, onEditDhh }: EnvironmentMachineGridProps) {
  return <div className="resource-machine-grid-v150">
    <article>
      <div>{statusDot(runtime?.upper.online, runtime ? 'SSH 状态' : '状态尚未检查')}<Laptop size={18} /><strong>上位机</strong></div>
      <h3>{environment.upper_machine.host}</h3>
      <p>{environment.upper_machine.username}</p>
      <p className="resource-machine-version">版本：{environment.software_version || '未查询'}</p>
    </article>
    {environment.dhh_machine && (() => {
      const dhh = runtime?.dhh;
      const logIp = String((environment.dhh_machine as any).station?.topology_ip || '').trim();
      const smallNetwork = !logIp && (Boolean(dhh?.skipped) || isSmallNetworkLower(environment.dhh_machine!.host));
      const running = smallNetwork ? undefined : dhh ? dhh.service_running : undefined;
      const statusText = smallNetwork ? '小网环境 · 跳过服务状态检测' : !dhh ? '状态未检查' : !dhh.online ? `SSH 不可连接${dhh.message ? ` · ${dhh.message}` : ''}` : dhh.service_running ? '运行中' : '未运行';
      const statusTitle = smallNetwork ? 'DHH 为 192.* 小网，执行器日志和服务状态检测均跳过' : !dhh ? 'DHH 服务状态尚未检查' : !dhh.online ? 'DHH SSH 不可连接' : dhh.service_running ? 'DHH 服务运行中' : 'DHH 服务未运行';
      return <article className="resource-dhh-machine">
        <div className="resource-dhh-card-head">
          <span className="resource-dhh-card-title">{statusDot(running, statusTitle)}<Server size={18} /><strong>DHH</strong></span>
          {onEditDhh && <button type="button" className="resource-dhh-edit-button" title="编辑 DHH 登录信息" onClick={() => onEditDhh(environment.dhh_machine!)}><Pencil size={13} /> 编辑</button>}
        </div>
        <h3>{environment.dhh_machine!.host}{smallNetwork ? '（小网）' : ''}</h3>
        <p>{statusText}</p>
        {logIp && logIp !== environment.dhh_machine!.host && <p>大网IP：{logIp}</p>}
      </article>;
    })()}
    {environment.lower_machines.map((machine) => {
      const lower = runtime?.lowers.find((item) => item.machine_id === machine.id);
      const logIp = String(machine.station?.topology_ip || '').trim();
      const smallNetwork = !logIp && (Boolean(lower?.skipped) || isSmallNetworkLower(machine.host));
      const simulatorText = smallNetwork ? '小网环境 · 跳过状态检测' : !lower ? '状态未检查' : !lower.online ? `SSH 不可连接${lower.message ? ` · ${lower.message}` : ''}` : lower.tb_simulator_running ? 'tb_simulator 运行中' : 'tb_simulator 未运行';
      const showTimeWarning = Boolean(!smallNetwork && lower?.online && lower.time_sync_required);
      const timeText = showTimeWarning ? `时间未同步 · ${formatTimeDelta(lower?.time_delta_seconds)}` : '';
      const timeTitle = showTimeWarning && lower?.remote_date ? `下位机：${lower.remote_date}${runtime?.upper.remote_date ? `；上位机：${runtime.upper.remote_date}` : ''}` : timeText;
      const versionMismatch = Boolean(environment.software_version && machine.software_version && machine.software_version !== environment.software_version);
      const warningClasses = [showTimeWarning ? 'resource-lower-time-warning' : '', versionMismatch ? 'resource-lower-version-warning' : ''].filter(Boolean).join(' ');
      return <article key={machine.id} className={warningClasses || undefined}>
        <div className="resource-lower-card-head">
          <span className="resource-lower-card-title">{statusDot(smallNetwork ? undefined : lower ? lower.tb_simulator_running : undefined, smallNetwork ? '小网环境（192.*），跳过 SSH 状态检测' : lower ? 'tb_simulator 状态' : '状态尚未检查')}<Server size={18} /><strong>{machine.station_name || '下位机'}</strong></span>
          {showTimeWarning && onSyncLowerTime && <button type="button" className="resource-time-sync-button" title="以上位机当前时间同步下位机" disabled={busy === `sync-time-${machine.id}`} onClick={() => onSyncLowerTime(machine.id, machine.host)}>{busy === `sync-time-${machine.id}` ? <LoaderCircle className="spin" size={12} /> : <Clock3 size={12} />} 同步时间</button>}
        </div>
        <h3>业务IP：{machine.host}</h3>
        <p>登录：{machine.username || 'root'}</p>
        {logIp && logIp !== machine.host && <p>大网IP：{logIp}</p>}
        <p>{simulatorText}</p>
        <p className="resource-machine-version">版本：{machine.software_version || '未查询'}</p>
        {versionMismatch && <p className="resource-machine-version-check warning">版本不一致</p>}
        {showTimeWarning && <p className="resource-lower-time-state warning" title={timeTitle}>{timeText}</p>}
      </article>;
    })}
    {!environment.lower_machines.length && <div className="resource-machine-card-empty">未发现下位机</div>}
  </div>;
}

export interface EnvironmentResourcePreviewProps {
  environment: EnvironmentSummary;
  runtime?: EnvironmentRuntimeStatus;
  onOpenLogLocator?: () => void;
  onOpenCpdReports?: () => void;
  showActions?: boolean;
}

export function EnvironmentResourcePreview({ environment, runtime, onOpenLogLocator, onOpenCpdReports, showActions = true }: EnvironmentResourcePreviewProps) {
  const statusItems = [
    ...(environment.version_mismatch ? [{ text: '版本不一致', tone: 'partial' }] : []),
    ...(environment.lower_machines.length === 0 ? [{ text: '未部署环境', tone: 'offline' }] : []),
  ];
  if (!statusItems.length) statusItems.push({ text: '正常', tone: 'healthy' });
  return <div className="atlog-environment-preview">
    <section className="resource-detail-summary atlog-environment-preview-summary">
      <div className="resource-detail-title"><div className="resource-card-icon large"><Network size={22} /></div><div><h2>{environment.name}{environment.is_dhh_environment ? <em className="resource-dhh-label">DHH环境</em> : null}</h2><p>{environment.upper_machine.host} · {environment.lower_machines.length} 台下位机{environment.dhh_machine ? ' · 1 台 DHH' : ''}</p></div></div>
      <div className="resource-card-status-list">{statusItems.map((item) => <strong className={`tone-${item.tone}`} key={item.text}>{item.text}</strong>)}</div>
    </section>
    {showActions && <section className="resource-feature-section resource-function-card-section atlog-environment-preview-features">
      <div className="resource-section-heading"><div><h2>资源功能</h2></div></div>
      <div className="resource-feature-card-grid" aria-label={`${environment.name} 功能入口`}>
        <button type="button" className="resource-feature-card tone-log" onClick={onOpenLogLocator} disabled={!onOpenLogLocator}>
          <span className="resource-feature-card-icon"><FileSearch size={22} /></span>
          <span className="resource-feature-card-copy"><strong>日志定位</strong><small>进入日志搜索</small></span>
          <span className="resource-feature-card-enter">进入</span>
        </button>
        <button type="button" className="resource-feature-card tone-cpd" onClick={onOpenCpdReports} disabled={!onOpenCpdReports}>
          <span className="resource-feature-card-icon"><Gauge size={22} /></span>
          <span className="resource-feature-card-copy"><strong>CPD 测校报告</strong><small>查看测校报告</small></span>
          <span className="resource-feature-card-enter">进入</span>
        </button>
      </div>
    </section>}
    <section className="resource-machine-inventory atlog-environment-preview-machines">
      <div className="resource-section-heading"><div><h2>机器清单</h2><span>{environment.lower_machines.length + 1 + (environment.dhh_machine ? 1 : 0)} 台</span></div></div>
      <EnvironmentMachineGrid environment={environment} runtime={runtime} />
    </section>
  </div>;
}

export function EnvironmentResourcePage({ initialEnvironmentId, onOpenLogLocator, onOpenCpdReports, onOpenCatalogSettings }: Props) {
  const [environments, setEnvironments] = useState<EnvironmentSummary[]>([]);
  const [folders, setFolders] = useState<EnvironmentFolder[]>([]);
  const [runtime, setRuntime] = useState<Record<number, EnvironmentRuntimeStatus>>({});
  const [folderFilter, setFolderFilter] = useState<number | 'unfiled' | undefined>();
  const [openTabs, setOpenTabs] = useState<number[]>(loadStoredResourceTabs);
  const [activeResourceId, setActiveResourceId] = useState<number | undefined>(loadStoredActiveResource);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  /** 最近一次「停/启进程」的命令与输出，贴在详情里，比一句「已完成」可信。 */
  const [processResult, setProcessResult] = useState<{ environmentId: number; action: 'stop' | 'start'; ok: boolean; label: string; command: string; output: string }>();
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [resourceMessages, setResourceMessages] = useState<Record<number, string>>(() => loadStoredResourceNotices(RESOURCE_MESSAGES_STORAGE_KEY));
  const [resourceErrors, setResourceErrors] = useState<Record<number, string>>(() => loadStoredResourceNotices(RESOURCE_ERRORS_STORAGE_KEY));
  const [addOpen, setAddOpen] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const [folderOpen, setFolderOpen] = useState(false);
  const [folderEdit, setFolderEdit] = useState<EnvironmentFolder>();
  const [folderDraft, setFolderDraft] = useState({ name: '', parent: null as number | null });
  const [machineDraft, setMachineDraft] = useState<MachineDraftPayload>({ ...emptyMachineDraft });
  const [connectionToken, setConnectionToken] = useState('');
  const [connectionTestDialog, setConnectionTestDialog] = useState<{ success: boolean; title: string; message: string }>();
  const [dragOverFolderId, setDragOverFolderId] = useState<number | 'unfiled'>();
  const [editConnectionToken, setEditConnectionToken] = useState('');
  const [editDraft, setEditDraft] = useState<EditDraft>();
  const [dhhEditOpen, setDhhEditOpen] = useState(false);
  const [dhhEditDraft, setDhhEditDraft] = useState<DhhEditDraft>();
  const [dhhConnectionToken, setDhhConnectionToken] = useState('');
  const [catalog, setCatalog] = useState<EnvironmentLogTree>();
  const [selectedResources, setSelectedResources] = useState<Set<number>>(new Set());
  /** 收藏是**本浏览器**的（localStorage），不是环境上的字段：每个人可以有自己的一份。 */
  const [favoriteIds, setFavoriteIds] = useState<Set<number>>(() => loadFavoriteEnvironmentIds());
  const [collapsedFolders, setCollapsedFolders] = useState<Set<string>>(loadStoredCollapsedFolders);
  const [treeSearchQuery, setTreeSearchQuery] = useState('');
  const [deploymentOpen, setDeploymentOpen] = useState(false);
  const [deploymentByEnvironment, setDeploymentByEnvironment] = useState<Record<number, EnvironmentDeploymentSummary>>({});
  const deploymentStateRef = useRef<Record<number, EnvironmentDeploymentSummary>>({});
  const completedDeploymentIds = useRef<Set<number>>(new Set());

  const activeResource = useMemo(
    () => environments.find((item) => item.id === activeResourceId),
    [environments, activeResourceId],
  );
  const activeDeployment = activeResourceId ? deploymentByEnvironment[activeResourceId] || null : null;

  useEffect(() => {
    if (!initialEnvironmentId || !environments.some((item) => item.id === initialEnvironmentId)) return;
    setActiveResourceId(initialEnvironmentId);
    setOpenTabs((current) => current.includes(initialEnvironmentId) ? current : [...current, initialEnvironmentId]);
  }, [initialEnvironmentId, environments]);

  // 收藏存在本浏览器：订阅同页面变化 + 多标签页 storage 事件；环境删掉后顺手清掉收藏。
  useEffect(() => {
    const unsubscribe = subscribeFavoriteEnvironments((ids) => setFavoriteIds(new Set(ids)));
    const stopWatching = watchFavoriteEnvironmentsStorage();
    return () => { unsubscribe(); stopWatching(); };
  }, []);

  useEffect(() => {
    if (!environments.length) return;
    setFavoriteIds(pruneFavoriteEnvironments(environments.map((item) => item.id)));
  }, [environments]);


  useEffect(() => {
    const handler = (event: Event) => {
      const custom = event as CustomEvent<{ context?: Record<string, unknown> }>;
      const context = custom.detail?.context;
      if (!context) return;
      context.page = 'resources';
      context.page_label = '环境资源';
      if (!activeResource) {
        context.environment_page = { selected: false, environment_count: environments.length };
        return;
      }
      const state = runtime[activeResource.id];
      context.environment_id = activeResource.id;
      context.environment_name = activeResource.name;
      context.environment_page = {
        selected: true,
        environment_id: activeResource.id,
        environment_name: activeResource.name,
        status: activeResource.status || '',
        station_user_id: activeResource.station_user_id || '',
        software_version: activeResource.software_version || '',
        version_mismatch: Boolean(activeResource.version_mismatch),
        upper: { host: activeResource.upper_machine.host, online: state?.upper.online ?? null },
        dhh: activeResource.dhh_machine ? {
          host: activeResource.dhh_machine.host,
          online: state?.dhh?.online ?? null,
          service_running: state?.dhh?.service_running ?? null,
        } : null,
        lowers: activeResource.lower_machines.slice(0, 12).map((machine) => {
          const lower = state?.lowers.find((item) => item.machine_id === machine.id);
          return `${machine.station_name || '下位机'}=${machine.host} | tb=${lower?.tb_simulator_running ?? '-'} | version=${machine.software_version || '-'}`;
        }),
        deployment_dialog_open: deploymentOpen,
        deployment: activeDeployment ? {
          id: activeDeployment.id,
          status: activeDeployment.status,
          target_version: activeDeployment.target_version,
          message: activeDeployment.message || '',
        } : null,
      };
    };
    return registerPageContextReader(handler, 10);
  }, [activeResource, environments.length, runtime, deploymentOpen, activeDeployment]);
  const environmentIdsKey = useMemo(() => environments.map((item) => item.id).sort((a, b) => a - b).join(','), [environments]);
  const visibleEnvironments = useMemo(() => environments.filter((item) => {
    if (folderFilter === undefined) return true;
    if (folderFilter === 'unfiled') return !item.folder;
    return item.folder === folderFilter;
  }), [environments, folderFilter]);
  const normalizedTreeSearchQuery = treeSearchQuery.trim().toLowerCase();
  const treeEnvironments = useMemo(() => {
    if (!normalizedTreeSearchQuery) return environments;
    return environments.filter((environment) => {
      const machines = [environment.upper_machine, ...environment.lower_machines, ...(environment.dhh_machine ? [environment.dhh_machine] : [])];
      const searchable = [
        environment.name,
        environment.station_user_id,
        environment.description,
        environment.folder_name,
        ...machines.flatMap((machine) => [
          machine.host,
          machine.username,
          machine.name,
          machine.station_name,
          machine.station_id,
          machine.station_type,
        ]),
      ];
      return searchable.some((value) => String(value || '').toLowerCase().includes(normalizedTreeSearchQuery));
    });
  }, [environments, normalizedTreeSearchQuery]);
  const treeEnvironmentIds = useMemo(() => new Set(treeEnvironments.map((item) => item.id)), [treeEnvironments]);
  const rootFolders = useMemo(() => folders.filter((folder) => !folder.parent), [folders]);
  const overviewGroups = useMemo(() => {
    const byParent = new Map<number | null, EnvironmentFolder[]>();
    folders.forEach((folder) => {
      const key = folder.parent ?? null;
      byParent.set(key, [...(byParent.get(key) ?? []), folder]);
    });
    const groups: Array<{ key: string; title: string; depth: number; environments: EnvironmentSummary[] }> = [];
    const walk = (parent: number | null, depth: number, path: string[]) => {
      (byParent.get(parent) ?? []).forEach((folder) => {
        const currentPath = [...path, folder.name];
        const items = environments.filter((item) => item.folder === folder.id);
        groups.push({ key: `folder-${folder.id}`, title: currentPath.join(' / '), depth, environments: items });
        walk(folder.id, depth + 1, currentPath);
      });
    };
    walk(null, 0, []);
    groups.push({ key: 'unfiled', title: '未分组', depth: 0, environments: environments.filter((item) => !item.folder) });
    return groups.filter((group) => group.environments.length > 0);
  }, [environments, folders]);

  // 总览统计：部署中取部署状态；收藏取**本浏览器的**收藏（服务端不再存，见 favoriteEnvironments）。
  const overviewStats = useMemo(() => ({
    deploying: environments.filter((item) => {
      const status = deploymentByEnvironment[item.id]?.status;
      return Boolean(status && ['pending', 'running', 'stopping'].includes(status));
    }).length,
    favorite: environments.filter((item) => favoriteIds.has(item.id)).length,
  }), [environments, deploymentByEnvironment, favoriteIds]);

  async function refresh(preferredId?: number) {
    setLoading(true);
    setError('');
    try {
      const [environmentList, folderList] = await Promise.all([
        listEnvironments(), listEnvironmentFolders(),
      ]);
      setEnvironments(environmentList);
      setFolders(folderList);
      if (preferredId && environmentList.some((item) => item.id === preferredId)) openResource(preferredId);
      if (activeResourceId && !environmentList.some((item) => item.id === activeResourceId)) setActiveResourceId(undefined);
      setOpenTabs((current) => current.filter((id) => environmentList.some((item) => item.id === id)));
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setLoading(false);
    }
  }

  async function loadRuntimeStatus(environmentId: number, force = false, notify = false) {
    try {
      const value = await getEnvironmentRuntimeStatus(environmentId, force);
      setRuntime((current) => ({ ...current, [environmentId]: value }));
      if (notify) {
        const mode = value.cache_status === 'hit' ? '已使用 1 分钟内缓存状态。' : '资源状态已刷新。';
        setResourceMessage(environmentId, mode);
      }
    } catch (exc) {
      if (notify) setResourceErrors((current) => ({ ...current, [environmentId]: exc instanceof Error ? exc.message : String(exc) }));
      if (force) throw exc;
      console.warn('runtime status failed', exc);
    }
  }

  useEffect(() => { void refresh(); }, []);

  useEffect(() => {
    if (!activeResourceId || !environments.some((item) => item.id === activeResourceId)) return;
    void loadRuntimeStatus(activeResourceId, false, false);
  }, [activeResourceId, environments]);

  function updateEnvironmentDeployment(environmentId: number, deployment: EnvironmentDeploymentSummary | null | undefined) {
    setDeploymentByEnvironment((current) => {
      const next = { ...current };
      if (deployment?.status === 'scheduled') return current;
      if (deployment && ['pending', 'running', 'stopping'].includes(deployment.status)) next[environmentId] = deployment;
      else delete next[environmentId];
      deploymentStateRef.current = next;
      return next;
    });
  }

  useEffect(() => {
    let cancelled = false;
    async function loadActiveDeploymentsOnce() {
      try {
        const records = await listActiveEnvironmentDeployments();
        if (cancelled) return;
        const next = Object.fromEntries(records.map((item) => [item.environment, item])) as Record<number, EnvironmentDeploymentSummary>;
        deploymentStateRef.current = next;
        setDeploymentByEnvironment(next);
      } catch (exc) {
        console.warn('active deployment snapshot failed', exc);
      }
    }
    if (!environmentIdsKey) {
      deploymentStateRef.current = {};
      setDeploymentByEnvironment({});
      return () => { cancelled = true; };
    }
    void loadActiveDeploymentsOnce();
    return () => { cancelled = true; };
  }, [environmentIdsKey]);

  useEffect(() => {
    if (!environmentIdsKey) return;
    return subscribeDeploymentRealtime((event) => {
      if (event.type !== 'deployment.state') return;
      const latest = event.deployment;
      if (!environments.some((item) => item.id === latest.environment)) return;
      const previous = deploymentStateRef.current[latest.environment];
      updateEnvironmentDeployment(latest.environment, latest);
      if (latest.status === 'success' && previous?.status !== 'success' && !completedDeploymentIds.current.has(latest.id)) {
        const environment = environments.find((item) => item.id === latest.environment);
        if (environment) void handleDeploymentCompleted(environment, latest);
      }
    });
  }, [environmentIdsKey, environments]);

  useEffect(() => {
    if (!activeResourceId || !environments.some((item) => item.id === activeResourceId)) return;
    let cancelled = false;
    const loadDeployment = async () => {
      try {
        const latest = await getLatestEnvironmentDeployment(activeResourceId);
        if (cancelled) return;
        updateEnvironmentDeployment(activeResourceId, latest);
      } catch (exc) {
        console.warn('deployment status failed', exc);
      }
    };
    void loadDeployment();
    return () => { cancelled = true; };
  }, [activeResourceId, environmentIdsKey]);

  useEffect(() => {
    window.sessionStorage.setItem(RESOURCE_TABS_STORAGE_KEY, JSON.stringify(openTabs));
  }, [openTabs]);

  useEffect(() => {
    if (activeResourceId) window.sessionStorage.setItem(ACTIVE_RESOURCE_STORAGE_KEY, String(activeResourceId));
    else window.sessionStorage.removeItem(ACTIVE_RESOURCE_STORAGE_KEY);
  }, [activeResourceId]);

  useEffect(() => {
    const pendingId = Number(window.sessionStorage.getItem('tracelens-ai-open-deployment-v1') || '');
    if (pendingId > 0 && environments.some((item) => item.id === pendingId)) {
      openResource(pendingId);
      setDeploymentOpen(true);
      window.sessionStorage.removeItem('tracelens-ai-open-deployment-v1');
    }
  }, [environments]);

  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent<Record<string, unknown>>).detail || {};
      const environmentId = Number(detail.environment_id || 0);
      if (!environmentId || !environments.some((item) => item.id === environmentId)) return;
      openResource(environmentId);
      if (detail.open_deployment) setDeploymentOpen(true);
    };
    window.addEventListener('tracelens:assistant-resource', handler as EventListener);
    return () => window.removeEventListener('tracelens:assistant-resource', handler as EventListener);
  }, [environments]);

  useEffect(() => {
    window.sessionStorage.setItem(RESOURCE_MESSAGES_STORAGE_KEY, JSON.stringify(resourceMessages));
  }, [resourceMessages]);

  useEffect(() => {
    window.sessionStorage.setItem(RESOURCE_ERRORS_STORAGE_KEY, JSON.stringify(resourceErrors));
  }, [resourceErrors]);

  useEffect(() => {
    window.localStorage.setItem(RESOURCE_FOLDER_COLLAPSE_STORAGE_KEY, JSON.stringify(Array.from(collapsedFolders)));
  }, [collapsedFolders]);

  useEffect(() => {
    if (!activeResource) { setEditDraft(undefined); setCatalog(undefined); return; }
    setEditDraft({
      name: activeResource.name,
      description: activeResource.description,
      folder: activeResource.folder ?? null,
      host: activeResource.upper_machine.host,
      username: activeResource.upper_machine.username,
      ssh_port: activeResource.upper_machine.ssh_port,
      password: '',
    });
    setCatalog(undefined);
    setEditConnectionToken('');
  }, [activeResource?.id]);

  function openResource(id: number) {
    setOpenTabs((current) => current.includes(id) ? current : [...current, id]);
    setActiveResourceId(id);
  }

  function closeResourceTab(id: number) {
    setOpenTabs((current) => current.filter((item) => item !== id));
    if (activeResourceId === id) {
      const remaining = openTabs.filter((item) => item !== id);
      setActiveResourceId(remaining.at(-1));
    }
  }

  async function run(label: string, fn: () => Promise<void>, resourceId?: number) {
    setBusy(label);
    if (resourceId) {
      setResourceErrors((current) => ({ ...current, [resourceId]: '' }));
      setResourceMessages((current) => ({ ...current, [resourceId]: '' }));
    } else {
      setError(''); setMessage('');
    }
    try { await fn(); } catch (exc) {
      const text = exc instanceof Error ? exc.message : String(exc);
      if (resourceId) setResourceErrors((current) => ({ ...current, [resourceId]: text }));
      else setError(text);
    } finally { setBusy(''); }
  }

  /**
   * 收藏：**只写本浏览器**（localStorage），不发请求。
   *
   * 收藏是"我的工作台"而不是环境的属性 —— 存在服务端会造成全团队共用一份收藏。
   */
  function toggleFavorite(environment: EnvironmentSummary) {
    const nowFavorite = toggleFavoriteEnvironment(environment.id);
    setFavoriteIds(loadFavoriteEnvironmentIds());
    setResourceMessage(environment.id, nowFavorite ? `已收藏 ${environment.name}` : `已取消收藏 ${environment.name}`);
  }

  /**
   * 一键停/启进程：跑的是部署流程里同一份 stop.sh / start.sh。
   * 会真的停掉现场进程，所以点之前必须确认，结果（命令 + 输出）原样贴出来。
   */
  async function runProcessControl(environment: EnvironmentSummary, action: 'stop' | 'start') {
    const label = action === 'stop' ? '停止进程' : '启动进程';
    if (!window.confirm(`确认对 ${environment.name} 执行「${label}」？\n将在上位机 ${environment.upper_machine.host} 上执行与部署相同的脚本。`)) return;
    await run(`process-${action}`, async () => {
      const result = await runEnvironmentProcessControl(environment.id, action);
      const output = [result.stdout, result.stderr].filter(Boolean).join('\n').trim();
      setProcessResult({ environmentId: environment.id, action, ok: result.ok, label: result.label, command: result.command, output });
      if (result.ok) setResourceMessage(environment.id, `${result.label}完成：${result.command}`);
      else setResourceErrors((current) => ({ ...current, [environment.id]: `${result.label}失败（exit ${result.exit_status}）：${output || '无输出'}` }));
    }, environment.id);
  }

  function setResourceMessage(resourceId: number, text: string) {
    setResourceMessages((current) => ({ ...current, [resourceId]: text }));
    setResourceErrors((current) => ({ ...current, [resourceId]: '' }));
  }

  function patchMachineDraft(patch: Partial<MachineDraftPayload>) {
    setMachineDraft((current) => ({ ...current, ...patch }));
    setConnectionToken('');
  }

  async function probeNewMachine() {
    setBusy('probe-new');
    setError('');
    try {
      const result = await testMachineDraft(machineDraft);
      if (!result.success || !result.connection_test_token) throw new Error(result.message || 'SSH 连接测试失败');
      setConnectionToken(result.connection_test_token);
      const text = `${result.hostname || machineDraft.host} · 连接正常`;
      setConnectionTestDialog({ success: true, title: '连接测试成功', message: text });
    } catch (exc) {
      const text = exc instanceof Error ? exc.message : String(exc);
      setConnectionToken('');
      setConnectionTestDialog({ success: false, title: '连接测试失败', message: text });
    } finally {
      setBusy('');
    }
  }

  async function addEnvironment() {
    await run('add-environment', async () => {
      if (!connectionToken) throw new Error('保存前必须测试 SSH 连接。');
      const machine = await createMachine({ ...machineDraft, role: 'upper', connection_test_token: connectionToken });
      if (!machine.environment_id) throw new Error('上位机已保存，但没有生成环境。');
      const environmentId = machine.environment_id;
      await updateEnvironment(environmentId, {
        name: `${machineDraft.host}(${machineDraft.username})`,
        folder: folderDraft.parent,
      });
      setMachineDraft({ ...emptyMachineDraft });
      setConnectionToken('');
      setAddOpen(false);

      const initializationErrors: string[] = [];
      try {
        const discovery = await discoverEnvironment(environmentId);
      } catch (exc) {
        initializationErrors.push(`上下位机解析失败：${exc instanceof Error ? exc.message : String(exc)}`);
      }
      try {
        const versionResult = await queryVersion(environmentId);
        const lowerFailures = versionResult.lower_versions.filter((item) => !item.skipped && item.message);
        if (lowerFailures.length) {
          initializationErrors.push(`下位机版本查询失败：${lowerFailures.map((item) => item.host).join('、')}`);
        }
      } catch (exc) {
        initializationErrors.push(`版本查询失败：${exc instanceof Error ? exc.message : String(exc)}`);
      }
      try {
        const value = await loadEnvironmentLogCatalog(environmentId);
        const failedSources = value.sources.filter((item) => item.scan_status === 'error' || item.status === 'error');
        if (failedSources.length) {
          initializationErrors.push(`日志目录扫描失败：${failedSources.slice(0, 3).map((item) => item.source_name).join('、')}`);
        }
      } catch (exc) {
        initializationErrors.push(`日志目录扫描失败：${exc instanceof Error ? exc.message : String(exc)}`);
      }

      await refresh(environmentId);
      if (initializationErrors.length) {
        setResourceErrors((current) => ({ ...current, [environmentId]: `环境已添加，自动初始化存在异常：${initializationErrors.join('；')}` }));
        setResourceMessages((current) => ({ ...current, [environmentId]: '' }));
      } else {
        setResourceMessage(environmentId, '环境已添加，并已自动完成上下位机解析、版本查询和日志目录扫描。');
      }
    });
  }

  function patchEdit(patch: Partial<EditDraft>) {
    setEditDraft((current) => current ? ({ ...current, ...patch }) : current);
    if (patch.host !== undefined || patch.username !== undefined || patch.ssh_port !== undefined || patch.password !== undefined) {
      setEditConnectionToken('');
    }
  }

  function editNeedsConnectionTest(): boolean {
    if (!activeResource || !editDraft) return false;
    return editDraft.host !== activeResource.upper_machine.host
      || editDraft.username !== activeResource.upper_machine.username
      || editDraft.ssh_port !== activeResource.upper_machine.ssh_port
      || Boolean(editDraft.password);
  }

  async function probeEditMachine() {
    if (!editDraft) return;
    setBusy('probe-edit');
    setError('');
    try {
      if (!editDraft.password && editNeedsConnectionTest()) throw new Error('修改连接参数时请输入当前或新密码，再进行连接测试。');
      const result = await testMachineDraft({
        host: editDraft.host, ssh_port: editDraft.ssh_port, username: editDraft.username,
        auth_type: 'password', password: editDraft.password, role: 'upper',
      });
      if (!result.success || !result.connection_test_token) throw new Error(result.message || 'SSH 连接测试失败');
      setEditConnectionToken(result.connection_test_token);
      const text = `${result.hostname || editDraft.host} · 连接正常`;
      setConnectionTestDialog({ success: true, title: '连接测试成功', message: text });
    } catch (exc) {
      const text = exc instanceof Error ? exc.message : String(exc);
      setEditConnectionToken('');
      setConnectionTestDialog({ success: false, title: '连接测试失败', message: text });
    } finally {
      setBusy('');
    }
  }

  async function saveEdit() {
    if (!activeResource || !editDraft) return;
    await run('save-edit', async () => {
      if (editNeedsConnectionTest() && !editConnectionToken) throw new Error('连接参数已变化，保存前必须重新测试 SSH。');
      await updateEnvironment(activeResource.id, { name: editDraft.name, description: editDraft.description, folder: editDraft.folder ?? null });
      if (editNeedsConnectionTest()) {
        const patch: Partial<MachineDraftPayload> = {
          host: editDraft.host, username: editDraft.username, ssh_port: editDraft.ssh_port,
          auth_type: 'password', password: editDraft.password, connection_test_token: editConnectionToken,
        };
        await updateMachine(activeResource.upper_machine.id, patch);
      }
      setEditOpen(false); setMessage('环境配置已更新。');
      await refresh(activeResource.id);
    });
  }

  function openDhhEdit(machine: NonNullable<EnvironmentSummary['dhh_machine']>) {
    setDhhEditDraft({
      machineId: machine.id,
      host: machine.host,
      username: machine.username || 'root',
      ssh_port: machine.ssh_port || 22,
      password: '',
      hasCredential: Boolean(machine.has_credential),
    });
    setDhhConnectionToken('');
    setDhhEditOpen(true);
  }

  function patchDhhEdit(patch: Partial<DhhEditDraft>) {
    setDhhEditDraft((current) => current ? ({ ...current, ...patch }) : current);
    if (patch.username !== undefined || patch.ssh_port !== undefined || patch.password !== undefined) {
      setDhhConnectionToken('');
    }
  }

  async function probeDhhMachine() {
    if (!dhhEditDraft) return;
    setBusy('probe-dhh');
    try {
      if (!dhhEditDraft.password) throw new Error('请输入 DHH 密码后再测试连接。');
      const result = await testMachineDraft({
        host: dhhEditDraft.host, ssh_port: dhhEditDraft.ssh_port, username: dhhEditDraft.username || 'root',
        auth_type: 'password', password: dhhEditDraft.password, role: 'lower',
      });
      if (!result.success || !result.connection_test_token) throw new Error(result.message || 'DHH SSH 连接测试失败');
      setDhhConnectionToken(result.connection_test_token);
      setConnectionTestDialog({ success: true, title: 'DHH 连接测试成功', message: `${result.hostname || dhhEditDraft.host} · 连接正常` });
    } catch (exc) {
      setDhhConnectionToken('');
      setConnectionTestDialog({ success: false, title: 'DHH 连接测试失败', message: exc instanceof Error ? exc.message : String(exc) });
    } finally {
      setBusy('');
    }
  }

  async function saveDhhEdit() {
    if (!dhhEditDraft || !activeResource) return;
    await run('save-dhh', async () => {
      const machine = activeResource.dhh_machine;
      if (!machine || machine.id !== dhhEditDraft.machineId) throw new Error('DHH 资源已变化，请刷新后重试。');
      const connectionChanged = dhhEditDraft.username !== (machine.username || 'root')
        || dhhEditDraft.ssh_port !== machine.ssh_port
        || Boolean(dhhEditDraft.password);
      if (connectionChanged && !dhhConnectionToken) throw new Error('DHH 连接参数发生变化，保存前请先测试连接。');
      if (!connectionChanged) {
        setDhhEditOpen(false);
        return;
      }
      await updateMachine(dhhEditDraft.machineId, {
        username: dhhEditDraft.username || 'root',
        ssh_port: dhhEditDraft.ssh_port,
        auth_type: 'password',
        password: dhhEditDraft.password,
        role: 'lower',
        connection_test_token: dhhConnectionToken,
      });
      // DHH 密码变化后主动清理旧 SSH 会话，避免继续复用旧密码连接。
      await disconnectMachineSession(dhhEditDraft.machineId).catch(() => undefined);
      setDhhEditOpen(false);
      setDhhConnectionToken('');
      setResourceMessage(activeResource.id, `DHH ${dhhEditDraft.host} 连接配置已保存。`);
      await refresh(activeResource.id);
    }, activeResource.id);
  }

  async function removeEnvironment(environment: EnvironmentSummary) {
    if (!window.confirm(`确定删除环境“${environment.name}”吗？`)) return;
    await run('delete-environment', async () => {
      await deleteEnvironment(environment.id);
      setEditOpen(false);
      setEditDraft(undefined);
      closeResourceTab(environment.id);
      setMessage('环境已删除。');
      await refresh();
    });
  }

  async function createOrUpdateFolder() {
    await run('save-folder', async () => {
      if (!folderDraft.name.trim()) throw new Error('文件夹名称不能为空。');
      if (folderEdit) await updateEnvironmentFolder(folderEdit.id, { name: folderDraft.name.trim(), parent: folderDraft.parent });
      else await createEnvironmentFolder({ name: folderDraft.name.trim(), parent: folderDraft.parent });
      setFolderOpen(false); setFolderEdit(undefined); setFolderDraft({ name: '', parent: null });
      await refresh();
    });
  }

  async function removeFolder(folder: EnvironmentFolder) {
    if (!window.confirm(`删除文件夹“${folder.name}”？文件夹中的环境不会被删除。`)) return;
    await run('delete-folder', async () => {
      await deleteEnvironmentFolder(folder.id);
      if (folderFilter === folder.id) setFolderFilter(undefined);
      await refresh();
    });
  }

  async function loadEnvironmentLogCatalog(environmentId: number): Promise<EnvironmentLogTree> {
    const settings = await getResourceSettings();
    const discoveryCategories = settings.log_paths
      .filter((profile) => profile.enabled && profile.path_template.trim() && profile.match_rules.some((rule) =>
        rule === 'fm' || rule === 'fm_timestamp' || rule === 'archive' || rule === 'executor_tree'))
      .map((profile) => profile.category);
    if (discoveryCategories.length === 0) {
      throw new Error('设置中没有启用可发现子系统/模块的日志类型。');
    }
    return getLogTree(environmentId, discoveryCategories, true);
  }

  async function refreshOneRuntime(environment: EnvironmentSummary) {
    await run('runtime', async () => {
      const failures: string[] = [];
      setCatalog(undefined);

      try {
        const discovery = await discoverEnvironment(environment.id);
      } catch (exc) {
        failures.push(`上下位机解析失败：${exc instanceof Error ? exc.message : String(exc)}`);
      }

      try {
        const versionResult = await queryVersion(environment.id);
        const lowerFailures = versionResult.lower_versions.filter((item) => !item.skipped && item.message);
        if (lowerFailures.length) {
          failures.push(`下位机版本查询失败：${lowerFailures.map((item) => item.host).join('、')}`);
        }
      } catch (exc) {
        failures.push(`版本查询失败：${exc instanceof Error ? exc.message : String(exc)}`);
      }

      try {
        const value = await loadEnvironmentLogCatalog(environment.id);
        const failedSources = value.sources.filter((item) => item.scan_status === 'error' || item.status === 'error');
        if (failedSources.length) {
          const details = failedSources.slice(0, 3).map((item) => item.source_name).join('、');
          const more = failedSources.length > 3 ? `等 ${failedSources.length} 个日志源` : '';
          failures.push(`日志目录扫描失败：${details}${more}`);
        } else {
          setCatalog(value);
        }
      } catch (exc) {
        failures.push(`日志目录扫描失败：${exc instanceof Error ? exc.message : String(exc)}`);
      }

      try {
        await loadRuntimeStatus(environment.id, true, false);
      } catch (exc) {
        failures.push(`运行状态刷新失败：${exc instanceof Error ? exc.message : String(exc)}`);
      }

      await refresh(environment.id);
      if (failures.length) {
        setResourceMessages((current) => ({ ...current, [environment.id]: '' }));
        setResourceErrors((current) => ({
          ...current,
          [environment.id]: `刷新状态部分失败：${failures.join('；')}`,
        }));
      } else {
        setResourceMessage(environment.id, '刷新状态完成。');
      }
    }, environment.id);
  }

  async function handleDeploymentCompleted(environment: EnvironmentSummary, deployment: EnvironmentDeployment | EnvironmentDeploymentSummary) {
    if (deployment.status === 'success') {
      if (completedDeploymentIds.current.has(deployment.id)) return;
      completedDeploymentIds.current.add(deployment.id);
    }
    updateEnvironmentDeployment(environment.id, deployment);
    await Promise.allSettled([
      loadRuntimeStatus(environment.id, true, false),
      queryVersion(environment.id),
    ]);
    await refresh(environment.id);
    setResourceMessage(environment.id, `部署完成：${deployment.target_version}，环境状态与版本已刷新。`);
  }


  async function syncLowerTime(environment: EnvironmentSummary, machineId: number, host: string) {
    await run(`sync-time-${machineId}`, async () => {
      const result = await syncLowerMachineTime(environment.id, [machineId]);
      setRuntime((current) => ({ ...current, [environment.id]: result.runtime_status }));
      const item = result.results.find((entry) => entry.machine_id === machineId);
      if (!item?.success) throw new Error(item?.message || `下位机 ${host} 时间同步失败。`);
      setResourceMessage(environment.id, `下位机 ${host} 时间已与上位机同步。`);
    }, environment.id);
  }

  function openEnvironmentEditor(environment: EnvironmentSummary) {
    openResource(environment.id);
    setEditDraft({
      name: environment.name,
      description: environment.description,
      folder: environment.folder ?? null,
      host: environment.upper_machine.host,
      username: environment.upper_machine.username,
      ssh_port: environment.upper_machine.ssh_port,
      password: '',
    });
    setEditConnectionToken('');
    setEditOpen(true);
  }

  async function moveResourceToFolder(environmentId: number, folder: number | null) {
    setBusy(`move-resource-${environmentId}`);
    try {
      await updateEnvironment(environmentId, { folder });
      setMessage(folder ? '资源已移动到文件夹。' : '资源已移动到未分组。');
      await refresh();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setDragOverFolderId(undefined);
      setBusy('');
    }
  }

  function beginResourceDrag(event: React.DragEvent<HTMLElement>, environmentId: number) {
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('application/x-tracelens-resource-id', String(environmentId));
    event.dataTransfer.setData('text/plain', String(environmentId));
  }

  function droppedResourceId(event: React.DragEvent<HTMLElement>): number | undefined {
    const value = Number(event.dataTransfer.getData('application/x-tracelens-resource-id') || event.dataTransfer.getData('text/plain'));
    return Number.isInteger(value) && value > 0 ? value : undefined;
  }

  function renderResourceTreeItem(environment: EnvironmentSummary, depth = 0) {
    return (
      <div
        className="resource-tree-environment-row"
        style={{ paddingLeft: 20 + depth * 14 }}
        key={environment.id}
        draggable
        onDragStart={(event) => beginResourceDrag(event, environment.id)}
      >
        <button type="button" className="resource-tree-environment" onClick={() => openResource(environment.id)}>
          <Network size={14} /><span>{environment.name}</span>
        </button>
        <button type="button" className="resource-tree-resource-edit" title="编辑资源" onClick={(event) => { event.stopPropagation(); openEnvironmentEditor(environment); }}><Pencil size={12} /></button>
      </div>
    );
  }

  function folderCollapseKey(folderId: number | 'unfiled'): string {
    return folderId === 'unfiled' ? 'unfiled' : `folder:${folderId}`;
  }

  function toggleFolderCollapsed(folderId: number | 'unfiled') {
    const key = folderCollapseKey(folderId);
    setCollapsedFolders((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
  }

  function folderContainsTreeMatch(folderId: number): boolean {
    if (!normalizedTreeSearchQuery) return true;
    if (treeEnvironments.some((item) => item.folder === folderId)) return true;
    return folders.some((item) => item.parent === folderId && folderContainsTreeMatch(item.id));
  }

  function renderFolder(folder: EnvironmentFolder, depth = 0): JSX.Element {
    const children = folders.filter((item) => item.parent === folder.id && folderContainsTreeMatch(item.id));
    const items = treeEnvironments.filter((item) => item.folder === folder.id);
    const collapsed = !normalizedTreeSearchQuery && collapsedFolders.has(folderCollapseKey(folder.id));
    const hasChildren = children.length > 0 || items.length > 0;
    return (
      <div className={`resource-tree-branch ${collapsed ? 'collapsed' : ''}`} key={folder.id}>
        <div
          className={`resource-tree-folder ${folderFilter === folder.id ? 'active' : ''} ${dragOverFolderId === folder.id ? 'drag-over' : ''}`}
          style={{ paddingLeft: 10 + depth * 14 }}
          onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = 'move'; setDragOverFolderId(folder.id); }}
          onDragLeave={() => setDragOverFolderId((current) => current === folder.id ? undefined : current)}
          onDrop={(event) => { event.preventDefault(); event.stopPropagation(); const id = droppedResourceId(event); if (id) void moveResourceToFolder(id, folder.id); }}
        >
          <button type="button" className="resource-tree-folder-toggle" disabled={!hasChildren} onClick={(event) => { event.stopPropagation(); if (hasChildren) toggleFolderCollapsed(folder.id); }} title={hasChildren ? (collapsed ? '展开文件夹' : '折叠文件夹') : '空文件夹'} aria-label={collapsed ? `展开 ${folder.name}` : `折叠 ${folder.name}`}>{hasChildren ? (collapsed ? <ChevronRight size={13}/> : <ChevronDown size={13}/>) : <span className="resource-tree-toggle-placeholder"/>}</button>
          <button type="button" className="resource-tree-folder-main" onClick={() => { setFolderFilter(folder.id); setActiveResourceId(undefined); }}>
            {collapsed ? <Folder size={15}/> : <FolderOpen size={15}/>}<span>{folder.name}</span><small>{items.length}</small>
          </button>
          <button type="button" onClick={() => { setFolderEdit(folder); setFolderDraft({ name: folder.name, parent: folder.parent ?? null }); setFolderOpen(true); }} title="编辑文件夹"><Pencil size={12} /></button>
          <button type="button" onClick={() => void removeFolder(folder)} title="删除文件夹"><Trash2 size={12} /></button>
        </div>
        {!collapsed && <div className="resource-tree-branch-children">
          {children.map((child) => renderFolder(child, depth + 1))}
          {items.map((environment) => renderResourceTreeItem(environment, depth))}
        </div>}
      </div>
    );
  }



  function toggleResourceSelection(id: number) { setSelectedResources((current) => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next; }); }
  async function refreshAllResourceOverview() {
    if (!environments.length || busy) return;
    setBusy('overview-refresh');
    setError('');
    setMessage('正在刷新全部环境的上下位机状态与版本号...');
    try {
      const result = await bulkEnvironmentAction(environments.map((item) => item.id), 'overview_refresh');
      const values = result.overview_results ?? [];
      setRuntime((current) => ({ ...current, ...Object.fromEntries(values.map((item) => [item.environment_id, item.runtime_status])) }));
      const versionFailures = values.filter((item) => item.version_error).length;
      await refresh();
      setMessage(`已刷新 ${result.affected}/${result.requested} 套资源的上下位机状态与版本${versionFailures ? `，${versionFailures} 套版本查询失败` : ''}。`);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
      setMessage('');
    } finally { setBusy(''); }
  }
  async function batchRefreshRuntime() {
    if (!selectedResources.size) return;
    setBusy('batch-runtime');
    try {
      const result = await bulkEnvironmentAction([...selectedResources], 'runtime_status');
      const values = result.results ?? [];
      setRuntime((current) => ({ ...current, ...Object.fromEntries(values.map((item) => [item.environment_id, item])) }));
      setMessage(`已刷新 ${result.affected}/${result.requested} 套资源状态。`);
    } catch (exc) { setError(exc instanceof Error ? exc.message : String(exc)); } finally { setBusy(''); }
  }
  async function batchDeleteResources() {
    if (!selectedResources.size || !window.confirm(`删除选中的 ${selectedResources.size} 套环境资源？`)) return;
    setBusy('batch-delete');
    try {
      const result = await bulkEnvironmentAction([...selectedResources], 'delete');
      setSelectedResources(new Set());
      setMessage(`已删除 ${result.affected}/${result.requested} 套资源${result.skipped_ids?.length ? `，跳过 ${result.skipped_ids.length} 个失效记录` : ''}。`);
      await refresh();
    } catch (exc) { setError(exc instanceof Error ? exc.message : String(exc)); } finally { setBusy(''); }
  }
  function renderResourceCard(environment: EnvironmentSummary) {
    const state = runtime[environment.id];
    const deploymentState = deploymentByEnvironment[environment.id];
    const isDeploying = Boolean(deploymentState && ['pending', 'running', 'stopping'].includes(deploymentState.status));
    const hasUnsyncedLower = Boolean(state?.lowers.some((item) => !item.skipped && item.online && item.time_sync_required));
    const hasVersionMismatch = Boolean(environment.version_mismatch);
    const isUndeployed = environment.lower_machines.length === 0;
    const hasEnvironmentWarning = hasUnsyncedLower || hasVersionMismatch || isUndeployed;
    // 卡片颜色只表达环境级结论；单机在线状态由 IP 前的状态点表达。
    // 因此没有环境告警时，无论是否刚刷新过运行状态，都应显示绿色正常卡片。
    const statusTone = hasEnvironmentWarning ? 'partial' : 'healthy';
    // 机器区本身可滚动，必须渲染全部下位机，不能只截取前 4 台。
    const previewLowers = environment.lower_machines;
    const statusItems = [
      ...(hasUnsyncedLower ? [{ text: '时间未同步', tone: 'partial' }] : []),
      ...(hasVersionMismatch ? [{ text: '版本不一致', tone: 'partial' }] : []),
      ...(isUndeployed ? [{ text: '未部署环境', tone: 'offline' }] : []),
    ];
    if (!statusItems.length) statusItems.push({ text: '正常', tone: 'healthy' });
    return (
      <article
        className={`resource-overview-card status-${statusTone}`}
        key={environment.id}
        onClick={(event) => {
          // 卡片里已经有勾选框和收藏星标，将来还会有别的控件。
          // 只在「点到的不是控件」时才进入详情 —— 星标自己 stopPropagation 只是第一道防线，
          // 真正的保证是这里：点任何控件都不会顺带把整张卡片当成一次点击。
          if ((event.target as HTMLElement).closest('button, input, a, select, label, [role="checkbox"]')) return;
          openResource(environment.id);
        }}
      >
        <header>
          <input className="row-check resource-card-check" type="checkbox" checked={selectedResources.has(environment.id)} onClick={(event) => event.stopPropagation()} onChange={() => toggleResourceSelection(environment.id)} aria-label={`选择资源 ${environment.name}`} />
          <div className="resource-card-title-block">
            <span className="resource-card-icon"><Network size={18} /></span>
            <div><strong>{environment.name}{environment.is_dhh_environment ? <em className="resource-dhh-label">DHH环境</em> : null}</strong><small>{environment.folder_name || '未分组'}</small></div>
          </div>
          {isDeploying && <span className="resource-card-deploying" title={deploymentState?.message || '部署中'}><LoaderCircle className="spin" size={14} />部署中</span>}
          <button
            type="button"
            className={`resource-card-favorite ${favoriteIds.has(environment.id) ? 'active' : ''}`}
            title={favoriteIds.has(environment.id) ? `取消收藏 ${environment.name}` : `收藏 ${environment.name}`}
            aria-label={favoriteIds.has(environment.id) ? `取消收藏 ${environment.name}` : `收藏 ${environment.name}`}
            aria-pressed={favoriteIds.has(environment.id)}
            // 按住就拦住：有些容器用 pointerdown 触发导航，光拦截 click 来不及。
            onPointerDown={(event) => event.stopPropagation()}
            onClick={(event) => { event.stopPropagation(); void toggleFavorite(environment); }}
          >
            <Star size={15} fill={favoriteIds.has(environment.id) ? 'currentColor' : 'none'} />
          </button>
        </header>

        <div className="resource-card-machine-section">
          <div className="resource-card-machine-row" title={`上位机 · ${environment.upper_machine.host}`}>
            {statusDot(state?.upper.online, !state ? '状态尚未检查' : state.upper.online ? '上位机可连接' : '上位机未连接')}
            <span>上位机</span><strong>{environment.upper_machine.host}</strong>
          </div>
          <div className="resource-card-lower-rows">
            {previewLowers.map((machine) => {
              const lower = state?.lowers.find((item) => item.machine_id === machine.id);
              const logIp = String(machine.station?.topology_ip || '').trim();
      const smallNetwork = !logIp && (Boolean(lower?.skipped) || isSmallNetworkLower(machine.host));
              const lowerTitle = smallNetwork ? '小网环境（192.*），跳过 SSH 状态检测' : !lower ? '状态尚未检查' : lower.time_sync_required ? `时间未同步：${formatTimeDelta(lower.time_delta_seconds)}` : lower.tb_simulator_running ? 'tb_simulator 运行中' : 'tb_simulator 未运行';
              return <div className="resource-card-machine-row lower" key={machine.id} title={`${machine.station_name || '下位机'} · ${machine.host}`}>{statusDot(smallNetwork ? undefined : lower ? lower.tb_simulator_running : undefined, lowerTitle)}<span>下位机</span><strong>{machine.host}</strong></div>;
            })}
            {!previewLowers.length && <div className="resource-card-machine-empty">未发现下位机</div>}
          </div>
        </div>

        <div className="resource-card-version-row">
          <span>版本</span>
          <strong title={environment.software_version || '版本未查询'}>{environment.software_version || '未查询'}</strong>
        </div>

        <div className="resource-card-environment-status">
          <span>状态</span>
          <div className="resource-card-status-list">
            {statusItems.map((item) => <strong className={`tone-${item.tone}`} key={item.text}>{item.text}</strong>)}
          </div>
        </div>
      </article>
    );
  }

  return (
    <div className="resource-page resource-page-v150">
      <aside className="resource-tree-sidebar">
        <div className="resource-tree-title"><div><strong>环境资源</strong><small>{environments.length} 套资源</small></div><div><button type="button" onClick={() => { setFolderEdit(undefined); setFolderDraft({ name: '', parent: null }); setFolderOpen(true); }} title="新建文件夹"><FolderPlus size={16} /></button><button type="button" onClick={() => { setFolderDraft({ name: '', parent: null }); setAddOpen(true); }} title="添加环境"><Plus size={17} /></button></div></div>
        <div className="resource-tree-search">
          <Search size={14} aria-hidden="true" />
          <input
            value={treeSearchQuery}
            onChange={(event) => setTreeSearchQuery(event.target.value)}
            placeholder="搜索 IP / 用户 / 别名"
            aria-label="搜索环境资源"
          />
          {treeSearchQuery && <button type="button" onClick={() => setTreeSearchQuery('')} title="清空搜索" aria-label="清空搜索"><X size={13} /></button>}
        </div>
        <button className={`resource-tree-all ${folderFilter === undefined && !activeResourceId ? 'active' : ''}`} type="button" onClick={() => { setFolderFilter(undefined); setActiveResourceId(undefined); }}><Database size={15} /><span>{normalizedTreeSearchQuery ? '搜索结果' : '全部资源'}</span><small>{treeEnvironments.length}</small></button>
        <div className="resource-tree-scroll">
          {rootFolders.filter((folder) => folderContainsTreeMatch(folder.id)).map((folder) => renderFolder(folder))}
          {(() => {
            const unfiled = treeEnvironments.filter((item) => !item.folder);
            if (normalizedTreeSearchQuery && !unfiled.length) return null;
            const collapsed = !normalizedTreeSearchQuery && collapsedFolders.has(folderCollapseKey('unfiled'));
            return <>
              <div
                className={`resource-tree-folder ${folderFilter === 'unfiled' ? 'active' : ''} ${dragOverFolderId === 'unfiled' ? 'drag-over' : ''}`}
                onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = 'move'; setDragOverFolderId('unfiled'); }}
                onDragLeave={() => setDragOverFolderId((current) => current === 'unfiled' ? undefined : current)}
                onDrop={(event) => { event.preventDefault(); const id = droppedResourceId(event); if (id) void moveResourceToFolder(id, null); }}
              >
                <button type="button" className="resource-tree-folder-toggle" disabled={!unfiled.length} onClick={(event) => { event.stopPropagation(); if (unfiled.length) toggleFolderCollapsed('unfiled'); }} title={unfiled.length ? (collapsed ? '展开未分组' : '折叠未分组') : '无未分组资源'}>{unfiled.length ? (collapsed ? <ChevronRight size={13}/> : <ChevronDown size={13}/>) : <span className="resource-tree-toggle-placeholder"/>}</button>
                <button type="button" className="resource-tree-folder-main" onClick={() => { setFolderFilter('unfiled'); setActiveResourceId(undefined); }}><Folder size={15} /><span>未分组</span><small>{unfiled.length}</small></button>
              </div>
              {!collapsed && unfiled.map((environment) => renderResourceTreeItem(environment))}
            </>;
          })()}
          {normalizedTreeSearchQuery && treeEnvironmentIds.size === 0 && <div className="resource-tree-search-empty">没有匹配的环境资源</div>}
        </div>
        <div className="resource-tree-footer"><button type="button" disabled={busy === 'overview-refresh'} onClick={() => void refreshAllResourceOverview()}><RefreshCw size={14} className={busy === 'overview-refresh' ? 'spin' : undefined} /> {busy === 'overview-refresh' ? '刷新中' : '刷新资源'}</button><button type="button" onClick={onOpenCatalogSettings}><Settings2 size={14} /> 设置</button></div>
      </aside>

      <main className="resource-workspace-v150">
        <header className="resource-workspace-header">
          <div><span className="eyebrow">ENVIRONMENT WORKSPACE</span><h1>{activeResource ? <>{activeResource.name}{activeResource.is_dhh_environment ? <em className="resource-dhh-label">DHH环境</em> : null}</> : '环境资源总览'}</h1>{activeResource && <p>{activeResource.upper_machine.host} · {activeResource.lower_machines.length} 台下位机{activeResource.dhh_machine ? ' · 1 台 DHH' : ''}</p>}</div>
          <div><button className="button primary" type="button" onClick={() => { setFolderDraft({ name: '', parent: typeof folderFilter === 'number' ? folderFilter : null }); setAddOpen(true); }}><Plus size={16} /> 添加环境</button></div>
        </header>

        {openTabs.length > 0 && <nav className="resource-inline-tabs"><button type="button" className={!activeResourceId ? 'active' : ''} onClick={() => setActiveResourceId(undefined)}>资源总览</button>{openTabs.map((id) => { const environment = environments.find((item) => item.id === id); if (!environment) return null; return <span className={activeResourceId === id ? 'active' : ''} key={id}><button type="button" onClick={() => setActiveResourceId(id)}>{environment.name}</button><button type="button" className="close" onClick={() => closeResourceTab(id)}><X size={12} /></button></span>; })}</nav>}

        {(activeResourceId ? resourceErrors[activeResourceId] : error) && <div className="resource-alert error"><AlertTriangle size={16} />{activeResourceId ? resourceErrors[activeResourceId] : error}</div>}
        {(activeResourceId ? resourceMessages[activeResourceId] : message) && <div className="resource-alert success"><CheckCircle2 size={16} />{activeResourceId ? resourceMessages[activeResourceId] : message}</div>}

        {loading ? <div className="resource-loading"><LoaderCircle className="spin" /> 正在读取环境资源...</div> : !activeResource ? (
          <section className="resource-overview-section">
            <div className="resource-section-heading"><div><h2>{folderFilter === undefined ? '全部环境' : folderFilter === 'unfiled' ? '未分组' : folders.find((item) => item.id === folderFilter)?.name || '环境资源'}</h2><span>{visibleEnvironments.length} 套</span><span className="resource-overview-stats" title="按全部环境资源统计"><em>统计</em>部署中 <strong className={overviewStats.deploying ? 'tone-run' : undefined}>{overviewStats.deploying}</strong> 台<em className="divider">·</em><Star size={12} fill={overviewStats.favorite ? 'currentColor' : 'none'} />收藏 <strong className={overviewStats.favorite ? 'tone-favorite' : undefined}>{overviewStats.favorite}</strong> 台</span></div><div className="resource-batch-actions">{selectedResources.size > 0 && <div className="batch-toolbar"><strong>已选 {selectedResources.size}</strong><button className="button ghost compact" type="button" onClick={() => void batchRefreshRuntime()}>刷新所选状态</button><button className="button danger-outline compact" type="button" onClick={() => void batchDeleteResources()}>删除</button><button className="button ghost compact" type="button" onClick={() => setSelectedResources(new Set())}>清空</button></div>}</div></div>
            {folderFilter === undefined ? <div className="resource-overview-groups">
              {overviewGroups.map((group) => <section className="resource-overview-group" key={group.key}>
                <header><div><FolderOpen size={15} /><strong>{group.title}</strong></div><span>{group.environments.length} 套</span></header>
                <div className="resource-overview-grid">{group.environments.map(renderResourceCard)}</div>
              </section>)}
              {overviewGroups.length === 0 && <div className="resource-empty large"><Server size={34} /><strong>这里还没有环境资源</strong><span>可以新建文件夹或添加环境。</span></div>}
            </div> : <div className="resource-overview-grid">{visibleEnvironments.map(renderResourceCard)}{visibleEnvironments.length === 0 && <div className="resource-empty large"><Server size={34} /><strong>这里还没有环境资源</strong><span>可以新建文件夹或添加环境。</span></div>}</div>}
          </section>
        ) : (
          <div className="resource-tab-content">
            <section className={`resource-detail-summary ${activeResource.version_mismatch ? 'version-warning' : ''}`}>
              <div className="resource-detail-title"><div className="resource-card-icon large"><Network size={22} /></div><div><h2>{activeResource.name}{activeResource.version_mismatch ? <em className="resource-version-warning-label">版本不一致</em> : null}</h2><p>{activeResource.description || '暂无描述'}</p></div></div>
              <div className="resource-detail-actions"><button type="button" className="button secondary" disabled={busy === 'runtime'} onClick={() => void refreshOneRuntime(activeResource)}>{busy === 'runtime' ? <LoaderCircle className="spin" size={15} /> : <Activity size={15} />} {busy === 'runtime' ? '刷新中' : '刷新状态'}</button><button type="button" className="button secondary resource-process-stop" disabled={Boolean(busy)} onClick={() => void runProcessControl(activeResource, 'stop')}>{busy === 'process-stop' ? <LoaderCircle className="spin" size={15} /> : <PowerOff size={15} />} {busy === 'process-stop' ? '停止中' : '停进程'}</button><button type="button" className="button secondary resource-process-start" disabled={Boolean(busy)} onClick={() => void runProcessControl(activeResource, 'start')}>{busy === 'process-start' ? <LoaderCircle className="spin" size={15} /> : <Play size={15} />} {busy === 'process-start' ? '启动中' : '启动进程'}</button><button type="button" className={`button secondary resource-deploy-button ${activeDeployment && ['pending', 'running', 'stopping'].includes(activeDeployment.status) ? 'running' : ''}`} onClick={() => setDeploymentOpen(true)}>{activeDeployment && ['pending', 'running', 'stopping'].includes(activeDeployment.status) ? <LoaderCircle className="spin" size={15} /> : <Terminal size={15} />} {activeDeployment && ['pending', 'running', 'stopping'].includes(activeDeployment.status) ? '部署中' : '部署环境'}</button><button type="button" className="button secondary" onClick={() => setEditOpen(true)}><Pencil size={15} /> 编辑</button></div>
            </section>

            {processResult && processResult.environmentId === activeResource.id && (
              <section className={`resource-process-result ${processResult.ok ? 'ok' : 'error'}`}>
                <header>
                  <div>
                    <strong>{processResult.label}{processResult.ok ? '完成' : '失败'}</strong>
                    <code>{processResult.command}</code>
                  </div>
                  <button type="button" className="button ghost compact" onClick={() => setProcessResult(undefined)}><X size={13} /> 关闭</button>
                </header>
                {processResult.output
                  ? <pre>{processResult.output}</pre>
                  : <p>脚本无输出，按退出码判定为{processResult.ok ? '成功' : '失败'}。</p>}
              </section>
            )}

            <section className="resource-feature-section resource-function-card-section">
              <div className="resource-section-heading"><div><h2>资源功能</h2><span>按功能卡片进入当前资源能力，后续功能会继续在同一行扩展。</span></div></div>
              <div className="resource-feature-card-grid" aria-label={`${activeResource.name} 功能入口`}>
                <button type="button" className="resource-feature-card tone-log" onClick={() => onOpenLogLocator(activeResource)}>
                  <span className="resource-feature-card-icon"><FileSearch size={22} /></span>
                  <span className="resource-feature-card-copy"><strong>日志定位</strong><small>按时间、子系统和模块还原日志现场</small></span>
                  <span className="resource-feature-card-enter">进入</span>
                </button>
                <button type="button" className="resource-feature-card tone-cpd" onClick={() => onOpenCpdReports(activeResource)}>
                  <span className="resource-feature-card-icon"><Gauge size={22} /></span>
                  <span className="resource-feature-card-copy"><strong>CPD 测校报告</strong><small>查看历史测校结果并快速场景还原</small></span>
                  <span className="resource-feature-card-enter">进入</span>
                </button>
              </div>
            </section>

            <section className="resource-machine-inventory">
              <div className="resource-section-heading"><div><h2>机器清单</h2><span>{activeResource.lower_machines.length + 1 + (activeResource.dhh_machine ? 1 : 0)} 台</span></div></div>
              <EnvironmentMachineGrid environment={activeResource} runtime={runtime[activeResource.id]} busy={busy} onSyncLowerTime={(machineId, host) => void syncLowerTime(activeResource, machineId, host)} onEditDhh={openDhhEdit} />
              {catalog && <div className="resource-catalog-result"><CheckCircle2 size={15} /> 已识别 {catalog.global_catalog.length} 个子系统、{catalog.global_catalog.reduce((sum, item) => sum + item.fms.length, 0)} 个模块；列表已写入全局配置数据库。</div>}
            </section>
          </div>
        )}
      </main>

      {deploymentOpen && activeResource && <EnvironmentDeploymentDialog
        environment={activeResource}
        initialTask={activeDeployment}
        onClose={() => setDeploymentOpen(false)}
        onTaskChange={(deployment) => updateEnvironmentDeployment(activeResource.id, deployment)}
        onDeploymentCompleted={(deployment) => handleDeploymentCompleted(activeResource, deployment)}
      />}

      {addOpen && <div className="resource-modal-backdrop" onMouseDown={() => setAddOpen(false)}><form className="resource-modal machine-modal" onSubmit={(event) => { event.preventDefault(); void addEnvironment(); }} onMouseDown={(event) => event.stopPropagation()}><header><div><span className="eyebrow">NEW ENVIRONMENT</span><h2>添加环境资源</h2><p>机器显示名称自动生成：IP（登录用户）。保存前必须测试连接。</p></div><button type="button" className="icon-button" onClick={() => setAddOpen(false)}><X size={18} /></button></header><label><span>IP / 主机名</span><input required value={machineDraft.host} onChange={(event) => patchMachineDraft({ host: event.target.value })} /></label><div className="resource-field-row"><label><span>登录用户</span><input required value={machineDraft.username} onChange={(event) => patchMachineDraft({ username: event.target.value })} /></label><label><span>SSH 端口</span><input type="number" value={machineDraft.ssh_port} onChange={(event) => patchMachineDraft({ ssh_port: Number(event.target.value) })} /></label></div><label><span>密码</span><input required type="password" value={machineDraft.password || ''} onChange={(event) => patchMachineDraft({ password: event.target.value, auth_type: 'password' })} /></label><label><span>所属文件夹</span><select value={folderDraft.parent ?? ''} onChange={(event) => setFolderDraft((current) => ({ ...current, parent: event.target.value ? Number(event.target.value) : null }))}><option value="">未分组</option>{folders.map((folder) => <option value={folder.id} key={folder.id}>{folder.name}</option>)}</select></label><footer><button className="button secondary" type="button" onClick={() => void probeNewMachine()}>{busy === 'probe-new' ? <LoaderCircle className="spin" size={15} /> : <Terminal size={15} />} 测试连接</button><button className="button primary" type="submit" disabled={!connectionToken || busy === 'add-environment'}>{busy === 'add-environment' ? <><LoaderCircle className="spin" size={15} /> 自动初始化中</> : '保存环境'}</button></footer></form></div>}

      {editOpen && activeResource && editDraft && <div className="resource-modal-backdrop" onMouseDown={() => setEditOpen(false)}><form className="resource-modal" onSubmit={(event) => { event.preventDefault(); void saveEdit(); }} onMouseDown={(event) => event.stopPropagation()}><header><div><span className="eyebrow">EDIT ENVIRONMENT</span><h2>编辑环境</h2></div><button type="button" className="icon-button" onClick={() => setEditOpen(false)}><X size={18} /></button></header><label><span>环境名称</span><input value={editDraft.name} onChange={(event) => patchEdit({ name: event.target.value })} /></label><label><span>所属文件夹</span><select value={editDraft.folder ?? ''} onChange={(event) => patchEdit({ folder: event.target.value ? Number(event.target.value) : null })}><option value="">未分组</option>{folders.map((folder) => <option value={folder.id} key={folder.id}>{folder.name}</option>)}</select></label><label><span>描述</span><textarea value={editDraft.description} onChange={(event) => patchEdit({ description: event.target.value })} /></label><label><span>上位机地址</span><input value={editDraft.host} onChange={(event) => patchEdit({ host: event.target.value })} /></label><div className="resource-field-row"><label><span>登录用户</span><input value={editDraft.username} onChange={(event) => patchEdit({ username: event.target.value })} /></label><label><span>SSH 端口</span><input type="number" value={editDraft.ssh_port} onChange={(event) => patchEdit({ ssh_port: Number(event.target.value) })} /></label></div><label><span>密码</span><input type="password" value={editDraft.password} placeholder="连接参数不变时可留空" onChange={(event) => patchEdit({ password: event.target.value })} /></label><footer><button className="button danger-outline resource-delete-in-editor" type="button" onClick={() => void removeEnvironment(activeResource)}><Trash2 size={15} /> 删除环境</button>{editNeedsConnectionTest() && <button className="button secondary" type="button" onClick={() => void probeEditMachine()}><Terminal size={15} /> 测试连接</button>}<button className="button primary" type="submit" disabled={editNeedsConnectionTest() && !editConnectionToken}>保存修改</button></footer></form></div>}

      {dhhEditOpen && dhhEditDraft && <div className="resource-modal-backdrop" onMouseDown={() => setDhhEditOpen(false)}><form className="resource-modal dhh-resource-modal" onSubmit={(event) => { event.preventDefault(); void saveDhhEdit(); }} onMouseDown={(event) => event.stopPropagation()}><header><div><span className="eyebrow">DHH RESOURCE</span><h2>编辑 DHH 资源</h2><p>DHH IP 由 stations 自动识别；登录配置和密码独立保存。</p></div><button type="button" className="icon-button" onClick={() => setDhhEditOpen(false)}><X size={18} /></button></header><label><span>DHH IP</span><input value={dhhEditDraft.host} disabled /></label><div className="resource-field-row"><label><span>登录用户</span><input value={dhhEditDraft.username} placeholder="root" onChange={(event) => patchDhhEdit({ username: event.target.value })} /></label><label><span>SSH 端口</span><input type="number" value={dhhEditDraft.ssh_port} onChange={(event) => patchDhhEdit({ ssh_port: Number(event.target.value) })} /></label></div><label><span>密码</span><input type="password" value={dhhEditDraft.password} placeholder={dhhEditDraft.hasCredential ? '已保存密码；修改请输入新密码' : '请输入 DHH 密码（默认配置可在此设置）'} onChange={(event) => patchDhhEdit({ password: event.target.value })} /></label><div className="dhh-credential-hint">默认用户名为 root。已经保存过的 DHH IP 再次扫描时会自动复用当前登录配置，不会被通用下位机账号覆盖。</div><footer><button className="button secondary" type="button" onClick={() => void probeDhhMachine()} disabled={!dhhEditDraft.password}>{busy === 'probe-dhh' ? <LoaderCircle className="spin" size={15} /> : <Terminal size={15} />} 测试连接</button><button className="button primary" type="submit">保存 DHH 配置</button></footer></form></div>}

      {connectionTestDialog && <div className="resource-modal-backdrop connection-feedback-backdrop" onMouseDown={() => setConnectionTestDialog(undefined)}><section className={`resource-modal connection-feedback-dialog ${connectionTestDialog.success ? 'success' : 'error'}`} role="dialog" aria-modal="true" onMouseDown={(event) => event.stopPropagation()}><header><div><span className="eyebrow">CONNECTION TEST</span><h2>{connectionTestDialog.title}</h2></div><button type="button" className="icon-button" onClick={() => setConnectionTestDialog(undefined)}><X size={18} /></button></header><div className="connection-feedback-body">{connectionTestDialog.success ? <CheckCircle2 size={28} /> : <AlertTriangle size={28} />}<p>{connectionTestDialog.message}</p></div><footer><button type="button" className="button primary" onClick={() => setConnectionTestDialog(undefined)}>确定</button></footer></section></div>}

      {folderOpen && <div className="resource-modal-backdrop" onMouseDown={() => setFolderOpen(false)}><form className="resource-modal folder-modal" onSubmit={(event) => { event.preventDefault(); void createOrUpdateFolder(); }} onMouseDown={(event) => event.stopPropagation()}><header><div><span className="eyebrow">RESOURCE FOLDER</span><h2>{folderEdit ? '编辑文件夹' : '新建文件夹'}</h2></div><button type="button" className="icon-button" onClick={() => setFolderOpen(false)}><X size={18} /></button></header><label><span>名称</span><input autoFocus value={folderDraft.name} onChange={(event) => setFolderDraft((current) => ({ ...current, name: event.target.value }))} /></label><label><span>父文件夹</span><select value={folderDraft.parent ?? ''} onChange={(event) => setFolderDraft((current) => ({ ...current, parent: event.target.value ? Number(event.target.value) : null }))}><option value="">根目录</option>{folders.filter((item) => item.id !== folderEdit?.id).map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label><footer><button className="button primary" type="submit">保存</button></footer></form></div>}

    </div>
  );
}
