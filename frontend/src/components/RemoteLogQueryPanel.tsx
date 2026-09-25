import { registerPageContextReader, updateAssistantRuntimeContext } from '../assistant/contextRegistry';
import { type ReactNode, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  LoaderCircle,
  ListTree,
  Minus,
  Search,
  ServerCog,
  X,
} from 'lucide-react';
import {
  getEnvironmentRuntimeStatus,
  getGlobalLogCatalogTree,
  getLogTree,
  getResourceSettings,
  listEnvironmentFolders,
  listEnvironments,
  syncLowerMachineTime,
  type EnvironmentFolder,
  type EnvironmentRuntimeStatus,
  type EnvironmentSummary,
  type GlobalLogSubsystem,
  type LogPathProfile,
  type LogWindowRequest,
} from '../api/resourceApi';
import { SmartDateTimeInput } from './SmartDateTimeInput';

export interface RemoteLogPreset {
  token: string | number;
  environmentId: number;
  subsystem?: string;
  module?: string;
  /** 用户点名的组件名（AI 只会说人话里的名字），进页面后按目录解析成真实目标。 */
  componentName?: string;
  /** 助手下发的预设：即使当前已有日志任务，也要应用这次点名（用户本轮指令优先）。 */
  assistantDriven?: boolean;
  startTime?: string;
  endTime?: string;
  taskName?: string;
  request?: LogWindowRequest;
}

/**
 * 组件名 → 目录里的真实 (subsystem, fm)：AI 拿到的往往是「cpfr」这种人说的名字，
 * 大小写、显示名、子系统名都可能和目录里的键不一致，直接塞进 selectedTargets 会
 * 出现「选择框还是空的」。这里做一次宽松匹配：先按模块名，再按子系统名（整个子系统）。
 */
export function resolveComponentTargets(
  name: string,
  catalog: GlobalLogSubsystem[],
): string[] {
  const wanted = String(name || '').trim().toLowerCase();
  if (!wanted) return [];
  const names = (...values: Array<string | undefined>) => values.some((value) => String(value || '').trim().toLowerCase() === wanted);
  const moduleMatches: string[] = [];
  for (const subsystem of catalog) {
    for (const fm of subsystem.fms || []) {
      if (names(fm.name, fm.display_name, fm.effective_name)) {
        // 键必须和 HierarchyModuleSelect 用的完全一致（子系统用 name，模块用 name）。
        moduleMatches.push(targetKey(subsystem.name, fm.name, fm.kind === 'executor' ? 'executor' : 'normal'));
      }
    }
  }
  if (moduleMatches.length) return Array.from(new Set(moduleMatches));
  const subsystemTargets: string[] = [];
  for (const subsystem of catalog) {
    if (!names(subsystem.name, subsystem.display_name, subsystem.effective_name)) continue;
    for (const fm of subsystem.fms || []) {
      subsystemTargets.push(targetKey(subsystem.name, fm.name, fm.kind === 'executor' ? 'executor' : 'normal'));
    }
  }
  return Array.from(new Set(subsystemTargets));
}


export interface RemoteLogLocatorSnapshot {
  environment?: EnvironmentSummary;
  sourceCategories: string[];
  liveTargets: NonNullable<LogWindowRequest['fm_targets']>;
  startTime: string;
  endTime: string;
  keyword: string;
}

interface Props {
  initialEnvironmentId?: number;
  initialPreset?: RemoteLogPreset;
  activeTaskEnvironmentId?: number;
  activeTaskRequest?: LogWindowRequest;
  activeTaskLiveTargets?: LogWindowRequest['fm_targets'];
  activeTaskViewRange?: { startTime: string; endTime: string };
  activeTaskLocal?: boolean;
  taskQuery: string;
  onTaskQueryChange: (value: string) => void;
  taskQueryDisabled?: boolean;
  taskFilterControl?: ReactNode;
  timeControlsDisabled?: boolean;
  onStartSearch: (environment: EnvironmentSummary, request: LogWindowRequest, meta?: { taskName?: string; force?: boolean; liveTargets?: LogWindowRequest['fm_targets'] }) => void;
  onLocatorStateChange?: (snapshot: RemoteLogLocatorSnapshot) => void;
}

const MAX_LOG_RANGE_MS = 3 * 24 * 60 * 60 * 1000;

const QUICK_RANGES = [
  { label: '3分钟', seconds: 3 * 60 },
  { label: '10分钟', seconds: 10 * 60 },
  { label: '1小时', seconds: 60 * 60 },
  { label: '3小时', seconds: 3 * 60 * 60 },
  { label: '12小时', seconds: 12 * 60 * 60 },
  { label: '1天', seconds: 24 * 60 * 60 },
  { label: '2天', seconds: 2 * 24 * 60 * 60 },
  { label: '3天', seconds: 3 * 24 * 60 * 60 },
] as const;

function pad(value: number, length = 2): string {
  return String(value).padStart(length, '0');
}

function formatLocalDate(date: Date, milliseconds = false): string {
  const base = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  return milliseconds ? `${base}.${pad(date.getMilliseconds(), 3)}` : base;
}

function localInputDate(offsetSeconds = 0): string {
  return formatLocalDate(new Date(Date.now() + offsetSeconds * 1000));
}

function parseInputDate(value: string): Date | undefined {
  const normalized = value.trim().replace('T', ' ');
  const match = normalized.match(/^(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})(?::(\d{2}))?(?:\.(\d{1,6}))?$/);
  if (!match) return undefined;
  const [, year, month, day, hour, minute, second = '0', fraction = ''] = match;
  const date = new Date(Number(year), Number(month) - 1, Number(day), Number(hour), Number(minute), Number(second), Number(fraction.padEnd(3, '0').slice(0, 3) || '0'));
  return Number.isNaN(date.getTime()) ? undefined : date;
}

function canonicalQueryTime(value: string, parsed: Date): string {
  const normalized = value.trim().replace('T', ' ');
  const match = normalized.match(/^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})(?::(\d{2}))?(?:\.(\d{1,6}))?$/);
  if (!match) return formatLocalDate(parsed);
  const [, minutePart, seconds = '00', fraction = ''] = match;
  return `${minutePart}:${seconds}${fraction ? `.${fraction}` : ''}`;
}


function toggleSet(current: Set<string>, value: string): Set<string> {
  const next = new Set(current);
  if (next.has(value)) next.delete(value);
  else next.add(value);
  return next;
}

function catalogDisplayLabel(name: string, displayName?: string): string {
  const alias = (displayName || '').trim();
  return alias && alias !== name ? `${name}（${alias}）` : name;
}

function useDismissablePopover(open: boolean, onClose: () => void) {
  const ref = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    if (!open) return undefined;
    const onPointerDown = (event: PointerEvent) => {
      if (!ref.current?.contains(event.target as Node)) closeRef.current();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeRef.current();
    };
    document.addEventListener('pointerdown', onPointerDown, true);
    document.addEventListener('keydown', onKeyDown, true);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown, true);
      document.removeEventListener('keydown', onKeyDown, true);
    };
  }, [open]);
  return ref;
}

export type ModuleKind = 'normal' | 'executor';

export function targetKey(subsystem: string, module: string, kind: ModuleKind = 'normal'): string {
  return `${subsystem}\u0000${module}\u0000${kind}`;
}

export function splitTargetKey(value: string): { subsystem: string; fm: string; kind: ModuleKind } {
  const [subsystem = '', fm = '', rawKind = 'normal'] = value.split('\u0000');
  return { subsystem, fm, kind: rawKind === 'executor' ? 'executor' : 'normal' };
}

function moduleTone(value: string): number {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) hash = ((hash << 5) - hash + value.charCodeAt(index)) | 0;
  return Math.abs(hash) % 6;
}

export function EnvironmentSelect({
  environments,
  folders,
  value,
  onChange,
}: {
  environments: EnvironmentSummary[];
  folders: EnvironmentFolder[];
  value?: number;
  onChange: (value?: number) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useDismissablePopover(open, () => setOpen(false));
  const current = environments.find((item) => item.id === value);
  const groups = useMemo(() => {
    const byParent = new Map<number | null, EnvironmentFolder[]>();
    folders.forEach((folder) => {
      const parent = folder.parent ?? null;
      byParent.set(parent, [...(byParent.get(parent) ?? []), folder]);
    });
    byParent.forEach((items, parent) => byParent.set(parent, items.slice().sort((left, right) => left.sort_order - right.sort_order || left.name.localeCompare(right.name))));

    const result: Array<{ key: string; label: string; depth: number; items: EnvironmentSummary[] }> = [];
    const walk = (parent: number | null, depth: number, path: string[]) => {
      (byParent.get(parent) ?? []).forEach((folder) => {
        const folderPath = [...path, folder.name];
        const items = environments.filter((environment) => environment.folder === folder.id);
        if (items.length) result.push({ key: `folder-${folder.id}`, label: folderPath.join(' / '), depth, items });
        walk(folder.id, depth + 1, folderPath);
      });
    };
    walk(null, 0, []);
    const ungrouped = environments.filter((environment) => !environment.folder);
    if (ungrouped.length) result.push({ key: 'ungrouped', label: '未分组', depth: 0, items: ungrouped });
    return result;
  }, [environments, folders]);

  return (
    <div className="remote-resource-select" ref={ref}>
      <button type="button" className={`remote-resource-trigger ${open ? 'open' : ''}`} onClick={() => setOpen((item) => !item)}>
        <ServerCog size={15} />
        <span className="remote-control-label">环境</span><strong title={current?.name}>{current ? current.name : '选择环境'}</strong>
        <ChevronDown size={13} />
      </button>
      {open && (
        <div className="remote-resource-menu grouped">
          {groups.map((group) => (
            <section className="remote-resource-group" key={group.key}>
              <div className="remote-resource-group-title" style={{ '--resource-folder-depth': group.depth } as React.CSSProperties}>
                <span>{group.label}</span><small>{group.items.length}</small>
              </div>
              {group.items.map((item) => {
                const checked = item.id === value;
                return (
                  <button
                    type="button"
                    className={checked ? 'selected' : ''}
                    key={item.id}
                    onClick={() => { onChange(item.id); setOpen(false); }}
                  >
                    <span className="remote-option-check">{checked && <Check size={12} />}</span>
                    <span><strong>{item.name}</strong><small>{item.upper_machine.host} · {item.upper_machine.username}</small></span>
                  </button>
                );
              })}
            </section>
          ))}
          {!environments.length && <div className="remote-compact-empty">暂无环境资源</div>}
        </div>
      )}
    </div>
  );
}

export function LogTypeSelect({
  values,
  selected,
  onChange,
}: {
  values: Array<{ value: string; label: string; hint?: string }>;
  selected: Set<string>;
  onChange: (next: Set<string>) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useDismissablePopover(open, () => setOpen(false));
  const selectedLabels = values.filter((item) => selected.has(item.value)).map((item) => item.label);
  const summary = selectedLabels.length === 0 ? '未选择' : selectedLabels.length <= 2 ? selectedLabels.join('、') : `${selectedLabels.length} 类日志`;
  return (
    <div className="remote-logtype-select" ref={ref}>
      <button type="button" className={`remote-logtype-trigger ${open ? 'open' : ''}`} onClick={() => setOpen((item) => !item)}>
        <span className="remote-control-label">类型</span><strong title={summary}>{summary}</strong><ChevronDown size={13} />
      </button>
      {open && (
        <div className="remote-logtype-menu">
          {values.map((item, index) => {
            const checked = selected.has(item.value);
            return (
              <button type="button" key={item.value} className={checked ? 'selected' : ''} onClick={() => onChange(toggleSet(selected, item.value))}>
                <span className="remote-option-check">{checked && <Check size={12} />}</span>
                <span className={`logtype-tone tone-${index % 5}`} />
                <span><strong>{item.label}</strong>{item.hint && <small>{item.hint}</small>}</span>
              </button>
            );
          })}
          {!values.length && <div className="remote-compact-empty">资源设置中没有启用的日志类型</div>}
        </div>
      )}
    </div>
  );
}

export function HierarchyModuleSelect({
  catalog,
  selected,
  onChange,
  onCommit,
  deferCommit = false,
  disabled = false,
  allowedKinds = new Set<ModuleKind>(['normal', 'executor']),
  label = '子系统/模块',
  compact = false,
  moduleNameOnly = false,
}: {
  catalog: GlobalLogSubsystem[];
  selected: Set<string>;
  onChange: (next: Set<string>) => void;
  onCommit?: (next: Set<string>) => void;
  deferCommit?: boolean;
  disabled?: boolean;
  allowedKinds?: Set<ModuleKind>;
  label?: string;
  compact?: boolean;
  moduleNameOnly?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [draftSelected, setDraftSelected] = useState<Set<string>>(new Set(selected));
  const openSnapshotRef = useRef<Set<string>>(new Set(selected));

  function sameSelection(left: Set<string>, right: Set<string>): boolean {
    return left.size === right.size && Array.from(left).every((item) => right.has(item));
  }
  function openMenu() {
    const snapshot = new Set(selected);
    openSnapshotRef.current = snapshot;
    setDraftSelected(snapshot);
    setOpen(true);
  }
  function closeMenu() {
    if (!open) return;
    setOpen(false);
    if (deferCommit && !sameSelection(draftSelected, openSnapshotRef.current)) {
      (onCommit ?? onChange)(new Set(draftSelected));
    }
  }
  const ref = useDismissablePopover(open, closeMenu);
  const activeSelected = deferCommit && open ? draftSelected : selected;
  function applySelection(next: Set<string>) {
    if (deferCommit) setDraftSelected(next);
    else onChange(next);
  }
  const enabledCatalog = useMemo(() => catalog
    .filter((item) => item.enabled)
    .map((item) => ({ ...item, fms: item.fms.filter((fm) => fm.enabled && allowedKinds.has(fm.kind ?? 'normal')) }))
    .filter((item) => item.fms.length > 0), [catalog, allowedKinds]);
  const selectedPairs = Array.from(activeSelected).map(splitTargetKey);
  const selectedPair = selectedPairs.length === 1 ? selectedPairs[0] : undefined;
  const selectedSubsystem = selectedPair ? enabledCatalog.find((item) => item.name === selectedPair.subsystem) : undefined;
  const selectedFm = selectedPair ? selectedSubsystem?.fms.find((item) => item.name === selectedPair.fm && (item.kind ?? 'normal') === selectedPair.kind) : undefined;
  useEffect(() => {
    if (!open || selectedPairs.length === 0) return;
    setExpanded((current) => {
      const next = new Set(current);
      selectedPairs.forEach((item) => { if (item.subsystem) next.add(item.subsystem); });
      return next;
    });
  }, [open, activeSelected]);
  const summary = disabled
    ? '无需限定'
    : activeSelected.size === 0
      ? '未限定'
    : activeSelected.size === 1
      ? selectedPair
        ? `${moduleNameOnly ? selectedPair.fm : catalogDisplayLabel(selectedPair.fm, selectedFm?.display_name)}${selectedPair.kind === 'executor' ? '（执行器）' : ''}`
        : '1 个模块'
      : `${new Set(selectedPairs.map((item) => item.subsystem)).size} 子系统 · ${activeSelected.size} 模块`;

  function toggleSubsystem(subsystem: GlobalLogSubsystem) {
    const keys = subsystem.fms.filter((fm) => fm.enabled).map((fm) => targetKey(subsystem.name, fm.name, fm.kind ?? 'normal'));
    const allSelected = keys.length > 0 && keys.every((key) => activeSelected.has(key));
    const next = new Set(activeSelected);
    keys.forEach((key) => { if (allSelected) next.delete(key); else next.add(key); });
    applySelection(next);
  }

  return (
    <div className={`remote-hierarchy-select ${compact ? 'compact' : ''}`} ref={ref}>
      <button type="button" className={`remote-hierarchy-trigger ${open ? 'open' : ''}`} onClick={() => { if (disabled) return; if (open) closeMenu(); else openMenu(); }} aria-expanded={open} disabled={disabled}>
        <span className="remote-control-label">{label}</span><strong title={summary}>{summary}</strong><ChevronDown size={13} />
      </button>
      {open && !disabled && (
        <div className="remote-hierarchy-menu">
          <div className="remote-hierarchy-head"><strong>选择子系统 / 模块</strong></div>
          <div className="remote-hierarchy-tree">
            {enabledCatalog.map((subsystem) => {
              const keys = subsystem.fms.map((fm) => targetKey(subsystem.name, fm.name, fm.kind ?? 'normal'));
              const selectedCount = keys.filter((key) => activeSelected.has(key)).length;
              const allSelected = keys.length > 0 && selectedCount === keys.length;
              const partial = selectedCount > 0 && !allSelected;
              const isExpanded = expanded.has(subsystem.name);
              return (
                <div className="remote-hierarchy-group" key={subsystem.id}>
                  <div className="remote-hierarchy-subsystem-row">
                    <button
                      type="button"
                      className={`remote-subsystem-check ${allSelected ? 'selected' : ''} ${partial ? 'partial' : ''} ${!isExpanded ? 'disabled' : ''}`}
                      onClick={() => { if (isExpanded) toggleSubsystem(subsystem); }}
                      disabled={!isExpanded}
                      title={!isExpanded ? '请先展开该子系统，再选择全部模块' : allSelected ? '取消选择该子系统全部模块' : '选择该子系统全部模块'}
                    >
                      {allSelected ? <Check size={12} /> : partial ? <Minus size={12} /> : null}
                    </button>
                    <button type="button" className="remote-hierarchy-subsystem" onClick={() => setExpanded((current) => toggleSet(current, subsystem.name))}>
                      <ChevronRight className={isExpanded ? 'expanded' : ''} size={14} />
                      <strong title={catalogDisplayLabel(subsystem.name, subsystem.display_name)}>{catalogDisplayLabel(subsystem.name, subsystem.display_name)}</strong>
                      <small>{selectedCount ? `${selectedCount}/${keys.length}` : `${keys.length} 模块`}</small>
                    </button>
                  </div>
                  {isExpanded && (
                    <div className="remote-hierarchy-fms">
                      {subsystem.fms.map((fm) => {
                        const key = targetKey(subsystem.name, fm.name, fm.kind ?? 'normal');
                        const checked = activeSelected.has(key);
                        return (
                          <button type="button" key={fm.id} className={`module-option module-tone-${moduleTone(key)} ${checked ? 'selected' : ''}`} onClick={() => applySelection(toggleSet(activeSelected, key))}>
                            <span className="remote-option-check">{checked && <Check size={11} />}</span>
                            <span className="module-color-dot" />
                            <span><strong title={`${moduleNameOnly ? fm.name : catalogDisplayLabel(fm.name, fm.display_name)}${fm.kind === 'executor' ? '（执行器）' : ''}`}>{moduleNameOnly ? fm.name : catalogDisplayLabel(fm.name, fm.display_name)}{fm.kind === 'executor' ? '（执行器）' : ''}</strong></span>
                          </button>
                        );
                      })}
                    </div>
                  )}
                </div>
              );
            })}
            {!enabledCatalog.length && <div className="remote-compact-empty">全局数据库中暂无可用子系统/模块</div>}
          </div>
        </div>
      )}
    </div>
  );
}

function expandTargetsWithDependencies(selected: Set<string>, catalog: GlobalLogSubsystem[]): Set<string> {
  const next = new Set(selected);
  const byId = new Map<number, { subsystem: string; fm: GlobalLogSubsystem['fms'][number] }>();
  const byKey = new Map<string, GlobalLogSubsystem['fms'][number]>();
  catalog.forEach((subsystem) => subsystem.fms.forEach((fm) => {
    byId.set(fm.id, { subsystem: subsystem.name, fm });
    byKey.set(targetKey(subsystem.name, fm.name, fm.kind ?? 'normal'), fm);
  }));
  Array.from(selected).forEach((key) => {
    const fm = byKey.get(key);
    (fm?.target_module_ids ?? []).forEach((targetModuleId) => {
      const targetModule = byId.get(targetModuleId);
      if (targetModule?.fm.enabled) next.add(targetKey(targetModule.subsystem, targetModule.fm.name, targetModule.fm.kind ?? 'normal'));
    });
  });
  return next;
}

export function RemoteLogQueryPanel({
  initialEnvironmentId,
  initialPreset,
  activeTaskEnvironmentId,
  activeTaskRequest,
  activeTaskLiveTargets,
  activeTaskViewRange,
  activeTaskLocal,
  taskQuery,
  onTaskQueryChange,
  taskQueryDisabled,
  taskFilterControl,
  timeControlsDisabled = false,
  onStartSearch,
  onLocatorStateChange,
}: Props) {
  const [environments, setEnvironments] = useState<EnvironmentSummary[]>([]);
  const [environmentFolders, setEnvironmentFolders] = useState<EnvironmentFolder[]>([]);
  const [catalog, setCatalog] = useState<GlobalLogSubsystem[]>([]);
  const [logPaths, setLogPaths] = useState<LogPathProfile[]>([]);
  const [environmentId, setEnvironmentId] = useState<number>();
  const [selectedSources, setSelectedSources] = useState<Set<string>>(new Set(['debug']));
  const [selectedTargets, setSelectedTargets] = useState<Set<string>>(new Set());
  const [startTime, setStartTime] = useState(localInputDate(-3600));
  const [endTime, setEndTime] = useState(localInputDate());
  const [activeQuickRange, setActiveQuickRange] = useState('1小时');
  const [presetTaskName, setPresetTaskName] = useState<string>();
  const [busy, setBusy] = useState<'load' | 'time-check' | 'time-sync' | ''>('load');
  const [error, setError] = useState('');
  const [catalogSyncing, setCatalogSyncing] = useState(false);
  const [timeSyncPrompt, setTimeSyncPrompt] = useState<{
    environment: EnvironmentSummary;
    request: LogWindowRequest;
    upperDate?: string;
    lowers: EnvironmentRuntimeStatus['lowers'];
    liveTargets: NonNullable<LogWindowRequest['fm_targets']>;
  }>();
  const ensuredCatalogRef = useRef(new Set<string>());
  const catalogRequestIdRef = useRef(0);
  const appliedPresetRef = useRef<string | number>();

  const selectedEnvironment = useMemo(() => environments.find((item) => item.id === environmentId), [environmentId, environments]);
  const targetScopeRequired = useMemo(() => {
    if (!selectedSources.size) return true;
    const selectedProfiles = logPaths.filter((item) => selectedSources.has(item.category));
    return selectedProfiles.length !== selectedSources.size || selectedProfiles.some((item) => !item.match_rules.includes('run_flat'));
  }, [logPaths, selectedSources]);
  const allowedModuleKinds = useMemo(() => {
    const result = new Set<ModuleKind>();
    logPaths.filter((item) => selectedSources.has(item.category)).forEach((profile) => {
      if (profile.match_rules.includes('executor_tree')) result.add('executor');
      if (profile.match_rules.some((rule) => rule === 'fm' || rule === 'fm_timestamp' || rule === 'archive')) result.add('normal');
    });
    return result;
  }, [logPaths, selectedSources]);

  function applySourceSelection(next: Set<string>) {
    const nextKinds = new Set<ModuleKind>();
    logPaths.filter((item) => next.has(item.category)).forEach((profile) => {
      if (profile.match_rules.includes('executor_tree')) nextKinds.add('executor');
      if (profile.match_rules.some((rule) => rule === 'fm' || rule === 'fm_timestamp' || rule === 'archive')) nextKinds.add('normal');
    });
    // 切换日志类型时只清理已经不属于当前类型的模块，不自动补选同子系统的执行器模块。
    // 每个组件保持独立选择；只有点击子系统左侧全选按钮时才批量选择。
    const nextTargets = new Set(Array.from(selectedTargets).filter((key) => nextKinds.has(splitTargetKey(key).kind)));
    setSelectedSources(next);
    setSelectedTargets(nextTargets);
    setPresetTaskName(undefined);
    setError('');
  }

  function applyTargetSelection(next: Set<string>) {
    // 保存用户真实点击结果，不因为首次进入某个子系统而联选其它执行器组件。
    setSelectedTargets(new Set(next));
    setPresetTaskName(undefined);
    setError('');
  }
  const sourceOptions = useMemo(() => logPaths
    .filter((item) => item.enabled && item.path_template.trim())
    .map((item) => ({ value: item.category, label: item.display_name, hint: item.path_template })), [logPaths]);

  function applyQuickRange(seconds: number, label: string) {
    const end = new Date();
    setEndTime(formatLocalDate(end));
    setStartTime(formatLocalDate(new Date(end.getTime() - seconds * 1000)));
    setActiveQuickRange(label);
    setPresetTaskName(undefined);
    setError('');
  }

  async function loadOptions() {
    setBusy('load');
    setError('');
    try {
      const [environmentList, folderList, catalogTree, settings] = await Promise.all([listEnvironments(), listEnvironmentFolders(), getGlobalLogCatalogTree(false), getResourceSettings()]);
      setEnvironments(environmentList);
      setEnvironmentFolders(folderList);
      setCatalog(catalogTree);
      setLogPaths(settings.log_paths);
      setEnvironmentId((current) => {
        if (activeTaskLocal) return undefined;
        const preferred = activeTaskEnvironmentId ?? initialPreset?.environmentId ?? initialEnvironmentId;
        if (preferred && environmentList.some((item) => item.id === preferred)) return preferred;
        if (current && environmentList.some((item) => item.id === current)) return current;
        return undefined;
      });
      const enabled = settings.log_paths.filter((item) => item.enabled && item.path_template.trim());
      const names = new Set(enabled.map((item) => item.category));
      setSelectedSources((current) => {
        if (activeTaskLocal) return new Set();
        const valid = new Set(Array.from(current).filter((item) => names.has(item as LogPathProfile['category'])));
        return valid.size ? valid : names.has('debug') ? new Set(['debug']) : new Set();
      });
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy('');
    }
  }

  useEffect(() => { void loadOptions(); }, []);

  useEffect(() => {
    if (activeTaskLocal) return;
    const preferred = activeTaskEnvironmentId ?? initialPreset?.environmentId ?? initialEnvironmentId;
    if (preferred && environments.some((item) => item.id === preferred)) setEnvironmentId(preferred);
  }, [initialEnvironmentId, initialPreset?.environmentId, activeTaskEnvironmentId, activeTaskLocal, environments]);

  useEffect(() => {
    if (!activeTaskLocal) return;
    setEnvironmentId(undefined);
    setSelectedSources(new Set());
    setSelectedTargets(new Set());
    setStartTime('');
    setEndTime('');
    setActiveQuickRange('');
    setPresetTaskName(undefined);
    setError('');
  }, [activeTaskLocal]);

  useEffect(() => {
    if (!activeTaskRequest || !activeTaskEnvironmentId) return;
    setEnvironmentId(activeTaskEnvironmentId);
    setSelectedSources(new Set(activeTaskRequest.source_categories || []));
    const visibleTargets = activeTaskLiveTargets?.length ? activeTaskLiveTargets : (activeTaskRequest.fm_targets || []);
    setSelectedTargets(new Set(visibleTargets.map((item) => targetKey(item.subsystem, item.fm, item.kind ?? 'normal'))));
    setStartTime(activeTaskRequest.start_time.replace('T', ' '));
    setEndTime(activeTaskRequest.end_time.replace('T', ' '));
    setActiveQuickRange('');
    setPresetTaskName(undefined);
    setError('');
  }, [activeTaskEnvironmentId, activeTaskRequest, activeTaskLiveTargets]);

  useEffect(() => {
    if (!activeTaskRequest || !activeTaskEnvironmentId || !activeTaskViewRange) return;
    setStartTime(activeTaskViewRange.startTime.replace('T', ' '));
    setEndTime(activeTaskViewRange.endTime.replace('T', ' '));
    setActiveQuickRange('');
  }, [activeTaskEnvironmentId, activeTaskRequest, activeTaskViewRange?.endTime, activeTaskViewRange?.startTime]);

  useEffect(() => {
    if (activeTaskLocal) return;
    // 已有任务时默认不让旧预设覆盖任务状态；但助手这一轮明确点名了组件，属于新指令，必须应用。
    if (activeTaskRequest && !initialPreset?.assistantDriven) return;
    if (!initialPreset || appliedPresetRef.current === initialPreset.token) return;
    if (!environments.some((item) => item.id === initialPreset.environmentId)) return;
    setEnvironmentId(initialPreset.environmentId);

    const presetRequest = initialPreset.request;
    const presetStart = presetRequest?.start_time ?? initialPreset.startTime;
    const presetEnd = presetRequest?.end_time ?? initialPreset.endTime;
    if (presetStart && presetEnd) {
      setStartTime(String(presetStart).replace('T', ' '));
      setEndTime(String(presetEnd).replace('T', ' '));
      setActiveQuickRange('');
    } else {
      applyQuickRange(3600, '1小时');
    }

    if (presetRequest) {
      setSelectedSources(new Set(presetRequest.source_categories || []));
      onTaskQueryChange(presetRequest.keyword || '');
      let targets = (presetRequest.fm_targets || []).map((item) => targetKey(item.subsystem, item.fm, item.kind ?? 'normal'));
      // AI 只说了组件名（或者给的子系统/模块名和目录大小写不一致）时，按目录里的真实
      // 名字再解析一次 —— 否则选择框会是空的，用户会觉得 AI 根本没选中组件。
      if (initialPreset.componentName && catalog.length > 0) {
        const existing = new Set(targets.map((key) => key.toLowerCase()));
        const resolved = resolveComponentTargets(initialPreset.componentName, catalog)
          .filter((key) => !existing.has(key.toLowerCase()));
        if (resolved.length) targets = [...targets, ...resolved];
      }
      if (targets.length > 0 && catalog.length === 0) return;
      setSelectedTargets(new Set(targets));
      setPresetTaskName(initialPreset.taskName);
      appliedPresetRef.current = initialPreset.token;
      return;
    }

    if (initialPreset.componentName && catalog.length > 0) {
      const resolved = resolveComponentTargets(initialPreset.componentName, catalog);
      if (resolved.length) {
        setSelectedTargets(new Set(resolved));
        setPresetTaskName(initialPreset.taskName);
        appliedPresetRef.current = initialPreset.token;
        return;
      }
    }
    if (initialPreset.componentName && catalog.length === 0) return;

    if (initialPreset.subsystem && initialPreset.module && catalog.length > 0) {
      setSelectedTargets(new Set([targetKey(initialPreset.subsystem, initialPreset.module, 'normal')]));
      setPresetTaskName(initialPreset.taskName);
      appliedPresetRef.current = initialPreset.token;
    } else if (!initialPreset.subsystem || !initialPreset.module) {
      setSelectedTargets(new Set());
      setPresetTaskName(initialPreset.taskName);
      appliedPresetRef.current = initialPreset.token;
    }
  }, [initialPreset, activeTaskRequest, activeTaskLocal, environments, catalog, onTaskQueryChange]);

  useEffect(() => {
    if (!environmentId || !logPaths.length) return;
    const discoveryCategories = logPaths
      .filter((profile) => selectedSources.has(profile.category) && profile.match_rules.some((rule) => rule === 'fm' || rule === 'fm_timestamp' || rule === 'executor_tree'))
      .map((profile) => profile.category)
      .sort();
    if (!discoveryCategories.length) return;
    const cacheKey = `${environmentId}:${discoveryCategories.join(',')}`;
    if (ensuredCatalogRef.current.has(cacheKey)) return;
    ensuredCatalogRef.current.add(cacheKey);
    const requestId = ++catalogRequestIdRef.current;
    setCatalogSyncing(true);
    setError('');
    void getLogTree(environmentId, discoveryCategories, false)
      .then((value) => { if (requestId === catalogRequestIdRef.current) setCatalog(value.global_catalog); })
      .catch((exc) => {
        ensuredCatalogRef.current.delete(cacheKey);
        if (requestId === catalogRequestIdRef.current) setError(exc instanceof Error ? exc.message : String(exc));
      })
      .finally(() => { if (requestId === catalogRequestIdRef.current) setCatalogSyncing(false); });
  }, [environmentId, logPaths, selectedSources]);

  useEffect(() => {
    if (!catalog.length) return;
    const allowed = new Set<string>();
    catalog.filter((item) => item.enabled).forEach((subsystem) => subsystem.fms.filter((fm) => fm.enabled).forEach((fm) => allowed.add(targetKey(subsystem.name, fm.name, fm.kind ?? 'normal'))));
    setSelectedTargets((current) => new Set(Array.from(current).filter((item) => allowed.has(item))));
  }, [catalog]);

  function requestPayload(): LogWindowRequest {
    if (!selectedEnvironment) throw new Error('请选择环境资源。');
    if (!selectedSources.size) throw new Error('请至少选择一种日志类型。');
    if (targetScopeRequired && !selectedTargets.size) throw new Error('当前日志类型请至少勾选一个子系统/模块后再检索，禁止空选择全局扫描。');
    if (!startTime.trim() || !endTime.trim()) throw new Error('开始时间和结束时间为必填项。');
    const start = parseInputDate(startTime);
    const end = parseInputDate(endTime);
    if (!start || !end) throw new Error('时间格式应为 YYYY-MM-DD HH:mm:ss。');
    if (end.getTime() < start.getTime()) throw new Error('结束时间不能早于开始时间。');
    if (end.getTime() - start.getTime() > MAX_LOG_RANGE_MS) throw new Error('单次日志检索时间范围最多支持 3 天，请缩小开始/结束时间。');
    const effectiveTargetKeys = targetScopeRequired ? expandTargetsWithDependencies(selectedTargets, catalog) : new Set<string>();
    const targets = Array.from(effectiveTargetKeys).map(splitTargetKey).filter((item) => item.subsystem && item.fm);
    const effectiveSources = new Set(selectedSources);
    const selectedProfiles = logPaths.filter((profile) => effectiveSources.has(profile.category));
    if (targets.some((item) => item.kind === 'executor') && !selectedProfiles.some((profile) => profile.match_rules.includes('executor_tree'))) {
      const executorProfile = logPaths.find((profile) => profile.enabled && profile.match_rules.includes('executor_tree'));
      if (executorProfile) effectiveSources.add(executorProfile.category);
    }
    if (targets.some((item) => item.kind === 'normal') && !selectedProfiles.some((profile) => profile.match_rules.some((rule) => rule === 'fm' || rule === 'fm_timestamp' || rule === 'archive'))) {
      const normalProfile = logPaths.find((profile) => profile.enabled && profile.match_rules.some((rule) => rule === 'fm' || rule === 'fm_timestamp' || rule === 'archive'));
      if (normalProfile) effectiveSources.add(normalProfile.category);
    }
    return {
      start_time: canonicalQueryTime(startTime, start),
      end_time: canonicalQueryTime(endTime, end),
      source_categories: Array.from(effectiveSources),
      subsystems: Array.from(new Set(targets.map((item) => item.subsystem))),
      fms: Array.from(new Set(targets.map((item) => item.fm))),
      fm_targets: targets,
      keyword: taskQuery.trim(),
    };
  }

  function selectedLiveTargets(): NonNullable<LogWindowRequest['fm_targets']> {
    return Array.from(selectedTargets)
      .map(splitTargetKey)
      .filter((item) => item.subsystem && item.fm)
      .map((item) => ({ subsystem: item.subsystem, fm: item.fm, kind: item.kind }));
  }

  useEffect(() => {
    onLocatorStateChange?.({
      environment: selectedEnvironment,
      sourceCategories: Array.from(selectedSources),
      liveTargets: selectedLiveTargets(),
      startTime: startTime.trim(),
      endTime: endTime.trim(),
      keyword: taskQuery.trim(),
    });
  }, [onLocatorStateChange, selectedEnvironment, selectedSources, selectedTargets, startTime, endTime, taskQuery]);

  useEffect(() => {
    updateAssistantRuntimeContext({
      current_log_selection: {
        environment_id: selectedEnvironment?.id ?? null,
        source_categories: Array.from(selectedSources),
        targets: selectedLiveTargets(),
        time_range: { start: startTime.trim(), end: endTime.trim() },
        keyword: taskQuery.trim(),
      }
    });
  }, [selectedEnvironment, selectedSources, selectedTargets, startTime, endTime, taskQuery]);

  // Feed the exact live log-locator selection into TracePilot. The parent App
  // contributes task/result metadata, while this panel owns the current form state.
  // Keeping the structured fm_targets here prevents the AI from asking for a
  // component that is already selected on screen.
  useEffect(() => {
    const handler = (event: Event) => {
      if (activeTaskLocal) return;
      const custom = event as CustomEvent<{ context?: Record<string, unknown> }>;
      const context = custom.detail?.context;
      if (!context) return;
      const targets = selectedLiveTargets();
      const existing = context.log_locator && typeof context.log_locator === 'object'
        ? context.log_locator as Record<string, unknown>
        : {};
      context.log_locator = {
        ...existing,
        environment_id: selectedEnvironment?.id ?? existing.environment_id ?? null,
        query_time_range: startTime.trim() && endTime.trim()
          ? { start: startTime.trim(), end: endTime.trim() }
          : existing.query_time_range ?? null,
        source_categories: Array.from(selectedSources),
        targets: targets.slice(0, 12).map((item) => `${item.subsystem}/${item.fm}${item.kind === 'executor' ? ':executor' : ''}`),
        fm_targets: targets.slice(0, 12).map((item) => ({ subsystem: item.subsystem, fm: item.fm, kind: item.kind || 'normal' })),
        component_name: targets.length === 1 ? targets[0].fm : '',
        subsystem_name: targets.length === 1 ? targets[0].subsystem : '',
        keyword: taskQuery.trim(),
        selection_origin: 'log-locator-form',
      };
      if (selectedEnvironment) {
        context.environment_id = selectedEnvironment.id;
        context.environment_name = selectedEnvironment.name;
      }
    };
    return registerPageContextReader(handler, 10);
  }, [activeTaskLocal, selectedEnvironment, selectedSources, selectedTargets, startTime, endTime, taskQuery]);

  useEffect(() => {
    window.dispatchEvent(new CustomEvent('tracelens:assistant-context-changed', {
      detail: { source: 'log-locator-selection' },
    }));
  }, [environmentId, selectedSources, selectedTargets, startTime, endTime, taskQuery]);

  async function searchLogs() {
    if (!selectedEnvironment) { setError('请选择环境资源。'); return; }
    setError('');
    let request: LogWindowRequest;
    try {
      request = requestPayload();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
      return;
    }
    const liveTargets = selectedLiveTargets();
    setBusy('time-check');
    try {
      // 日志时间线依赖上下位机时钟。每次真正发起远程查询前强制刷新一次状态，
      // 避免下位机重启回到 1970 年后仍命中一分钟内的旧缓存。
      const runtime = await getEnvironmentRuntimeStatus(selectedEnvironment.id, true);
      const unsynced = runtime.lowers.filter((item) => !item.skipped && item.online && item.time_sync_required);
      if (unsynced.length > 0) {
        setTimeSyncPrompt({ environment: selectedEnvironment, request, upperDate: runtime.upper.remote_date, lowers: unsynced, liveTargets });
        return;
      }
      onStartSearch(selectedEnvironment, request, { taskName: presetTaskName, liveTargets });
    } catch (exc) {
      setError(`日志查询前检查下位机时间失败：${exc instanceof Error ? exc.message : String(exc)}`);
    } finally {
      setBusy('');
    }
  }

  function continueSearchWithoutTimeSync() {
    if (!timeSyncPrompt) return;
    const snapshot = timeSyncPrompt;
    setTimeSyncPrompt(undefined);
    onStartSearch(snapshot.environment, snapshot.request, { taskName: presetTaskName, liveTargets: snapshot.liveTargets });
  }

  async function syncTimeAndSearch() {
    if (!timeSyncPrompt) return;
    const snapshot = timeSyncPrompt;
    setBusy('time-sync');
    setError('');
    try {
      const result = await syncLowerMachineTime(snapshot.environment.id, snapshot.lowers.map((item) => item.machine_id));
      const remaining = result.runtime_status.lowers.filter((item) => !item.skipped && item.online && item.time_sync_required);
      if (!result.success || remaining.length > 0) {
        const failures = result.results.filter((item) => !item.success).map((item) => `${item.host || item.machine_id}：${item.message}`).join('；');
        setTimeSyncPrompt({ environment: snapshot.environment, request: snapshot.request, upperDate: result.runtime_status.upper.remote_date, lowers: remaining.length ? remaining : snapshot.lowers, liveTargets: snapshot.liveTargets });
        setError(failures || '仍有下位机时间未同步，请检查登录账号是否具有修改系统时间权限。');
        return;
      }
      setTimeSyncPrompt(undefined);
      onStartSearch(snapshot.environment, snapshot.request, { taskName: presetTaskName, liveTargets: snapshot.liveTargets });
    } catch (exc) {
      setError(`下位机时间同步失败：${exc instanceof Error ? exc.message : String(exc)}`);
    } finally {
      setBusy('');
    }
  }

  const disabled = busy !== '' || catalogSyncing;
  return (
    <div className="remote-query-composite">
      <div className="remote-unified-query-row remote-unified-query-row-v150" aria-label="日志定位与任务筛选">
        <EnvironmentSelect
          environments={environments}
          folders={environmentFolders}
          value={environmentId}
          onChange={(value) => {
            setEnvironmentId(value);
            setSelectedTargets(new Set());
            setPresetTaskName(undefined);
            setError('');
            applyQuickRange(3600, '1小时');
          }}
        />
        <div className={`remote-target-scope ${targetScopeRequired ? '' : 'optional'}`} title={targetScopeRequired ? '' : '当前选择的日志类型不区分子系统/模块'}><HierarchyModuleSelect catalog={catalog} selected={selectedTargets} disabled={!targetScopeRequired} allowedKinds={allowedModuleKinds} onChange={applyTargetSelection} />{!targetScopeRequired && <span className="remote-target-optional-badge">无需限定</span>}</div>
        <LogTypeSelect values={sourceOptions} selected={selectedSources} onChange={applySourceSelection} />

        <div className={timeControlsDisabled ? "remote-range-presets disabled" : "remote-range-presets"} aria-label="快捷时间范围" aria-disabled={timeControlsDisabled}>
          {QUICK_RANGES.map((item) => (
            <button type="button" className={activeQuickRange === item.label ? 'active' : ''} key={item.label} disabled={timeControlsDisabled} onClick={() => applyQuickRange(item.seconds, item.label)}>{item.label}</button>
          ))}
        </div>

        <SmartDateTimeInput value={startTime} label="开始时间" disabled={timeControlsDisabled} onChange={(value) => { setStartTime(value); setActiveQuickRange(''); setPresetTaskName(undefined); setError(''); }} />
        <span className="remote-time-separator">—</span>
        <SmartDateTimeInput value={endTime} label="结束时间" disabled={timeControlsDisabled} onChange={(value) => { setEndTime(value); setActiveQuickRange(''); setPresetTaskName(undefined); setError(''); }} />

        <div className="remote-unified-text-search">
          <Search size={15} />
          <input
            value={taskQuery}
            onChange={(event) => onTaskQueryChange(event.target.value)}
            placeholder="搜索正文 / 函数 / TraceID…（空格或逗号分隔多个关键字，命中任意一个）"
            title="支持多个关键字：用空格、逗号或顿号分隔，检索命中其中任意一个的日志行"
            disabled={taskQueryDisabled}
          />
          {taskQuery && <button type="button" onClick={() => onTaskQueryChange('')} aria-label="清空全文搜索"><X size={13} /></button>}
          {taskFilterControl}
        </div>

        <button className="button primary remote-search-apply" type="button" onClick={() => void searchLogs()} disabled={!selectedEnvironment || disabled}>
          {disabled ? <LoaderCircle className="spin" size={15} /> : <Search size={15} />} 搜索
        </button>
        {error && <span className="remote-inline-error" role="alert" title={error}><AlertTriangle size={13} /><span>{error}</span></span>}
      </div>

      {timeSyncPrompt && (
        <div className="remote-time-sync-backdrop" onMouseDown={() => busy !== 'time-sync' && setTimeSyncPrompt(undefined)}>
          <section className="remote-time-sync-dialog" role="dialog" aria-modal="true" aria-label="下位机时间未同步" onMouseDown={(event) => event.stopPropagation()}>
            <header><div><span className="remote-time-sync-alert"><AlertTriangle size={17} /></span><div><h3>下位机时间未同步</h3><p>日志时间线依赖上下位机时钟一致。检测到以下下位机时间与上位机偏差较大，是否先同步时间？</p></div></div><button type="button" onClick={() => setTimeSyncPrompt(undefined)} disabled={busy === 'time-sync'}><X size={16} /></button></header>
            <div className="remote-time-sync-list">
              {timeSyncPrompt.lowers.map((item) => <div key={item.machine_id}><ServerCog size={15} /><div><strong>{item.host}</strong><span>下位机：{item.remote_date || '时间未知'}</span>{timeSyncPrompt.upperDate && <span>上位机：{timeSyncPrompt.upperDate}</span>}</div><em>{item.time_delta_seconds === null || item.time_delta_seconds === undefined ? '偏差未知' : `相差 ${Math.abs(Math.round(item.time_delta_seconds)) >= 86400 ? `${(Math.abs(item.time_delta_seconds) / 86400).toFixed(1)} 天` : `${Math.abs(Math.round(item.time_delta_seconds))} 秒`}`}</em></div>)}
            </div>
            <div className="remote-time-sync-note">可以暂不同步继续查询，但跨上下位机日志的时间顺序可能失真。</div>
            <footer><button type="button" className="button secondary" onClick={continueSearchWithoutTimeSync} disabled={busy === 'time-sync'}>暂不同步，继续搜索</button><button type="button" className="button primary" onClick={() => void syncTimeAndSearch()} disabled={busy === 'time-sync'}>{busy === 'time-sync' ? <LoaderCircle className="spin" size={15} /> : null} 同步时间并搜索</button></footer>
          </section>
        </div>
      )}
    </div>
  );
}
