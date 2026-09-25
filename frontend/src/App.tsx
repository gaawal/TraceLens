import { registerPageContextReader } from './assistant/contextRegistry';
import { componentHue, componentStyle } from './rendering/componentColor';
import { createAbnormalEvidence } from './rendering/abnormalKnowledge';
import { createContext, memo, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, useTransition, type CSSProperties, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  BookOpenCheck,
  Boxes,
  CalendarDays,
  ChevronDown,
  ChevronRight,
  CircleDot,
  Clock,
  ClipboardPaste,
  ClipboardList,
  Copy,
  Download,
  Database,
  FileCode2,
  FileSearch,
  FileUp,
  FlaskConical,
  GitBranch,
  ListTree,
  Layers,
  Wand2,
  BookOpen,
  Link2,
  LoaderCircle,
  Pin,
  PinOff,
  Plus,
  Radio,
  Search,
  Settings,
  ServerCog,
  Share2,
  Sparkles,
  TerminalSquare,
  UploadCloud,
  X,
  ZoomIn,
  ZoomOut,
} from 'lucide-react';
import { EnvironmentResourcePage, EnvironmentResourcePreview } from './components/EnvironmentResourcePage';
import { LogCatalogSettingsPage } from './components/LogCatalogSettingsPage';
import { CpdReportPage } from './components/CpdReportPage';
import { LiveRibbon } from './components/LiveRibbon';
import { RemoteLogQueryPanel, type RemoteLogLocatorSnapshot, type RemoteLogPreset } from './components/RemoteLogQueryPanel';
import { TaskDetailFilterPopover } from './components/TaskDetailFilterPopover';
import { GlobalApiActivity } from './components/GlobalApiActivity';
import { PlatformSettingsPage } from './components/PlatformSettingsPage';
import { LogRulesSettingsPage } from './components/LogRulesSettingsPage';
import { LogAuditPage } from './components/LogAuditPage';
import { ExtractedDataPage } from './components/ExtractedDataPage';
import { DataExtractionRunDialog, type DataExtractionCandidate, type DataExtractionResultView } from './components/DataExtractionRunDialog';
import { FlowMapView } from './components/FlowMapView';
import { TimeTreeView } from './components/TimeTreeView';
import { SmartAnalysisDialog } from './components/SmartAnalysisDialog';
import type { AbnormalCase, AbnormalCaseEvidence } from './api/resourceApi';
import { KnowledgeBasePage } from './components/KnowledgeBasePage';
import { ToolCenterPage } from './components/ToolCenterPage';
import { createTracePilotActionRegistry } from './assistant/actionRegistry';
import { afterPaint, type UiReceipt } from './assistant/workstation';
import { saveTracePilotAgentContext } from './assistant/agentContext';
import { setLiveMonitoring } from './services/liveMonitoring';
import { useLiveCaptureProgress } from './services/liveCaptureProgress';
import { EventRestoreDialog } from './components/EventRestoreDialog';
import { AtLogAnalysisPage } from './components/AtLogAnalysisPage';
import { APP_VERSION } from './appConfig';
import { cancelLogSearch, createDataExtractionRecord, syncCaptureWatches, fetchLogWindow, fetchUrlLogImportContent, getEnvironmentRuntimeStatus, getGlobalLogCatalogTree, getResourceSettings, inspectUrlLogImports, listEnvironments, listRuntimeLogFormatRules, recognizeSemanticSourcesBatch, streamLiveLogWindow, updateDataExtractionRecord, updateLogAuditClientResult, updateResourceSettings, type DataExtractionRecord, type EnvironmentRuntimeStatus, type EnvironmentSummary, type LiveLogStreamEvent, type LogAuditRecord, type LogSearchProgress, type LogWindowRequest, type SemanticSourceResult, type UrlLogImportItem } from './api/resourceApi';
import {
  DEFAULT_ERROR_RULES,
  createErrorMatchRule,
  detectSeverity,
  type ErrorMatchRule,
} from './parser/logParser';
import type { ImportProgress, ImportStrategy, LogStreamWorkerResponse, WorkerImportSource } from './workers/logStreamProtocol';
import { buildCrossComponentTraces, buildProcessTimelines, durationNs, formatDuration, mergeRepeatedFunctionGroups } from './parser/treeBuilder';
import {
  createCrossTraceScopeIndex,
  createProcessScopeIndex,
  scopeCrossTracesForWindow,
  scopeCrossTracesFromEntries,
  scopeProcessesForWindow,
  scopeProcessesFromEntries,
  sliceEntriesByRange,
} from './parser/timeIndex';
import type { TimeRangeFilter } from './parser/timeIndex';
import { createStoredZip } from './utils/simpleZip';
import {
  buildPatternTokens,
  createDisplayRuleId,
  loadSemanticLabelsEnabled,
  localDisplayRuleStore,
  functionNodeEntries,
  matchDisplayRuleToMessage,
  matchDisplayLabelRulesToEntry,
  matchDisplayRulesToEntry,
  matchDisplayRulesToFunction,
  saveSemanticLabelsEnabled,
  validateDisplayRule,
} from './rendering/displayRules';
import type { DisplayRule, DisplayRuleEditorRequest, DisplayRuleParameter, ParameterMark } from './rendering/displayRules';
import { isEntryMasked, loadMaskingRules, saveMaskingRules, type MaskingRule } from './rendering/maskingRules';
import { loadFoldingRules, saveFoldingRules, type FoldingRule } from './rendering/foldingRules';
import { createEmptyDataExtractionRule, detectStructuredDataCandidates, extractDataRow, inferDataSourceUnit, inferDataValueType, loadDataExtractionRules, saveDataExtractionRules, validateDataExtractionRule, type DataExtractionField, type DataExtractionRule, type DataValueType } from './rendering/dataExtractionRules';
import { extractFromLoadedEntries, type ExtractionProgress } from './rendering/dataExtractionRuntime';
import { downloadMergedTemporaryRuleData, downloadTemporaryRuleData, saveTemporaryExtractionSession, temporarySessionKey } from './rendering/extractedDataStore';
import type {
  ContentSeverity,
  FunctionNode,
  LogEntry,
  LogFormatParserRuleConfig,
  ParseIssue,
  ProcessTimeline,
  ThreadTimeline,
  TimelineItem,
  TraceTimeline,
} from './types';

type TimelineGroupingMode = 'process' | 'merged';
type LogSortOrder = 'asc' | 'desc';
type WorkspacePage = 'resources' | 'logs' | 'catalog' | 'reports' | 'audit' | 'data' | 'knowledge' | 'atlog' | 'tools' | 'platform-settings';
type ResourceWorkspaceFeature = 'resource' | 'logs' | 'reports';

const MAX_REMOTE_LOG_RANGE_NS = 3n * 24n * 60n * 60n * 1_000_000_000n;
const LOG_FOLDING_ENABLED_KEY = 'tracelens.log-folding.enabled.v1';

interface DurationRangeFilter {
  minMs?: string;
  maxMs?: string;
}

interface Filters {
  query: string;
  levels: Set<string>;
  components: Set<string>;
  modes: Set<string>;
  timeRange?: TimeRangeFilter;
  durationRange?: DurationRangeFilter;
  errorsOnly?: boolean;
}


interface SharedLogSceneFilters {
  query: string;
  levels: string[];
  components: string[];
  modes: string[];
  timeRange?: { startNs: string; endNs: string };
  durationRange?: DurationRangeFilter;
  errorsOnly: boolean;
}

interface SharedLogSceneErrorAnchor {
  index: number;
  timestampNs?: string;
  component?: string;
  sourceFile?: string;
  sourceLine?: number;
}

interface SharedLogScene {
  version: 1;
  environmentId: number;
  taskName?: string;
  request: LogWindowRequest;
  filters: SharedLogSceneFilters;
  view: {
    showTimeline: boolean;
    /** true = 时间线嵌在日志区上方（不悬浮）；false = 悬浮窗口。 */
    timelineDocked: boolean;
    timelineGroupingMode: TimelineGroupingMode;
    semanticLabelsEnabled: boolean;
    rawLogMode: boolean;
    processTimelineExpanded: boolean;
    hiddenTimelineComponents: string[];
    logPageSize: number;
    logSortOrder: LogSortOrder;
    error?: SharedLogSceneErrorAnchor;
  };
}

const URL_PAGE_PARAM = 'page';
const DEFAULT_LOG_PAGE_SIZE = 1000;
const RAW_LOG_MODE_STORAGE_KEY = 'tracelens.raw-log-mode.v1';
const WORKSPACE_PAGES = new Set<WorkspacePage>(['resources', 'logs', 'catalog', 'reports', 'audit', 'data', 'knowledge', 'atlog', 'tools', 'platform-settings']);

function loadRawLogModePreference(): boolean {
  if (typeof window === 'undefined') return false;
  try {
    const params = new URL(window.location.href).searchParams;
    if (params.get(URL_PAGE_PARAM) === 'logs' && params.has('raw')) return params.get('raw') === '1';
    return window.localStorage.getItem(RAW_LOG_MODE_STORAGE_KEY) === '1';
  } catch {
    return false;
  }
}

function saveRawLogModePreference(enabled: boolean): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(RAW_LOG_MODE_STORAGE_KEY, enabled ? '1' : '0');
  } catch {
    // Browser storage is only a preference cache; URL state remains authoritative when present.
  }
}

function workspacePageFromUrl(): WorkspacePage {
  if (typeof window === 'undefined') return 'resources';
  const page = new URL(window.location.href).searchParams.get(URL_PAGE_PARAM)?.trim() as WorkspacePage | undefined;
  return page && WORKSPACE_PAGES.has(page) ? page : 'resources';
}

function splitUrlList(value: string | null): string[] {
  return String(value || '').split(',').map((item) => item.trim()).filter(Boolean);
}

function compactDateTime(value: string): string {
  const text = String(value || '').trim();
  const match = text.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.(\d{1,9}))?$/);
  if (!match) return text;
  const fraction = match[7] ? `.${match[7]}` : '';
  return `${match[1]}${match[2]}${match[3]}T${match[4]}${match[5]}${match[6] || '00'}${fraction}`;
}

function expandDateTime(value: string | null): string | undefined {
  const text = String(value || '').trim();
  const match = text.match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(?:\.(\d{1,9}))?$/);
  if (match) {
    const fraction = match[7] ? `.${match[7]}` : '';
    return `${match[1]}-${match[2]}-${match[3]}T${match[4]}:${match[5]}:${match[6]}${fraction}`;
  }
  return text || undefined;
}

function parseFmTargets(value: string | null, kind: 'normal' | 'executor'): Array<{ subsystem: string; fm: string; kind: 'normal' | 'executor' }> {
  return splitUrlList(value).flatMap((item) => {
    const index = item.indexOf(':');
    if (index <= 0 || index >= item.length - 1) return [];
    const subsystem = item.slice(0, index).trim();
    const fm = item.slice(index + 1).trim();
    return subsystem && fm ? [{ subsystem, fm, kind }] : [];
  });
}

function encodeFmTargets(targets: Array<{ subsystem: string; fm: string; kind?: 'normal' | 'executor' }>, kind: 'normal' | 'executor'): string {
  return targets
    .filter((item) => (item.kind || 'normal') === kind && item.subsystem && item.fm)
    .map((item) => `${item.subsystem}:${item.fm}`)
    .join(',');
}

function parseViewRange(params: URLSearchParams): { startNs: string; endNs: string } | undefined {
  const startValue = params.get('vs');
  const endValue = params.get('ve');
  if (!startValue || !endValue) return undefined;

  const decodeRangeValue = (value: string): bigint | undefined => {
    try {
      // New URLs append `n` and store the exact nanosecond value. Plain digits
      // remain the legacy millisecond format so already shared URLs still work.
      if (/^\d+n$/.test(value)) return BigInt(value.slice(0, -1));
      if (/^\d+$/.test(value)) return BigInt(value) * 1_000_000n;
    } catch {
      return undefined;
    }
    return undefined;
  };

  const startNs = decodeRangeValue(startValue);
  const endNs = decodeRangeValue(endValue);
  if (startNs === undefined || endNs === undefined || endNs < startNs) return undefined;
  return { startNs: startNs.toString(), endNs: endNs.toString() };
}

function sharedLogSceneFromUrl(): SharedLogScene | undefined {
  if (typeof window === 'undefined') return undefined;
  const params = new URL(window.location.href).searchParams;
  if (params.get(URL_PAGE_PARAM) !== 'logs') return undefined;
  const environmentId = Number(params.get('env'));
  const startTime = expandDateTime(params.get('st'));
  const endTime = expandDateTime(params.get('et'));
  if (!Number.isInteger(environmentId) || environmentId <= 0 || !startTime || !endTime) return undefined;

  const normalTargets = parseFmTargets(params.get('fm'), 'normal');
  const executorTargets = parseFmTargets(params.get('efm'), 'executor');
  const fmTargets = [...normalTargets, ...executorTargets];
  const flatSubsystems = splitUrlList(params.get('ss'));
  const flatFms = splitUrlList(params.get('mods'));
  const sourceCategories = splitUrlList(params.get('type'));
  const request: LogWindowRequest = {
    start_time: startTime,
    end_time: endTime,
    source_categories: sourceCategories,
    subsystems: fmTargets.length ? [...new Set(fmTargets.map((item) => item.subsystem))] : flatSubsystems,
    fms: fmTargets.length ? [...new Set(fmTargets.map((item) => item.fm))] : flatFms,
    ...(fmTargets.length ? { fm_targets: fmTargets } : {}),
    ...(params.get('rk') ? { keyword: params.get('rk') || undefined } : {}),
  };

  const errorIndex = Number(params.get('ai'));
  const pageSize = Number(params.get('ps'));
  return {
    version: 1,
    environmentId,
    request,
    filters: {
      query: params.get('q') || '',
      levels: splitUrlList(params.get('level')),
      components: splitUrlList(params.get('comp')),
      modes: splitUrlList(params.get('mode')),
      timeRange: parseViewRange(params),
      durationRange: (() => {
        const minRaw = params.get('dmin')?.trim();
        const maxRaw = params.get('dmax')?.trim();
        const valid = (value?: string) => value !== undefined && value !== '' && Number.isFinite(Number(value)) && Number(value) >= 0;
        const minMs = valid(minRaw) ? minRaw : undefined;
        const maxMs = valid(maxRaw) ? maxRaw : undefined;
        return minMs !== undefined || maxMs !== undefined ? { minMs, maxMs } : undefined;
      })(),
      errorsOnly: params.get('err') === '1',
    },
    view: {
      showTimeline: params.get('tl') === '1',
      timelineDocked: params.get('tld') === '1',
      timelineGroupingMode: params.get('grp') === 'p' ? 'process' : 'merged',
      semanticLabelsEnabled: params.get('sem') !== '0',
      rawLogMode: params.has('raw') ? params.get('raw') === '1' : loadRawLogModePreference(),
      processTimelineExpanded: params.get('pex') !== '0',
      hiddenTimelineComponents: splitUrlList(params.get('hide')),
      logPageSize: Number.isFinite(pageSize) && pageSize >= 100 ? pageSize : DEFAULT_LOG_PAGE_SIZE,
      logSortOrder: params.get('order') === 'desc' ? 'desc' : 'asc',
      ...(Number.isInteger(errorIndex) && errorIndex > 0 ? { error: { index: errorIndex - 1 } } : {}),
    },
  };
}

function readableSearch(params: URLSearchParams): string {
  // Keep structural separators readable. Values that can contain arbitrary
  // user text (q/rk) remain safely percent-encoded by URLSearchParams.
  return params.toString().replace(/%2C/gi, ',').replace(/%3A/gi, ':');
}

function routeEnvironmentIdFromUrl(): number | undefined {
  if (typeof window === 'undefined') return undefined;
  const value = Number(new URL(window.location.href).searchParams.get('env'));
  return Number.isInteger(value) && value > 0 ? value : undefined;
}

function writeWorkspacePageUrl(page: WorkspacePage, environmentId?: number, rawLogMode = false): void {
  if (typeof window === 'undefined') return;
  const params = new URLSearchParams();
  params.set(URL_PAGE_PARAM, page);
  if (page === 'logs') params.set('raw', rawLogMode ? '1' : '0');
  if (page === 'reports' && environmentId) params.set('env', String(environmentId));
  const url = new URL(window.location.href);
  const search = readableSearch(params);
  window.history.replaceState(window.history.state, '', `${url.pathname}?${search}`);
}

function writeSharedLogSceneUrl(scene: SharedLogScene): void {
  if (typeof window === 'undefined') return;
  const params = new URLSearchParams();
  params.set(URL_PAGE_PARAM, 'logs');
  params.set('env', String(scene.environmentId));
  params.set('st', compactDateTime(scene.request.start_time));
  params.set('et', compactDateTime(scene.request.end_time));
  if (scene.request.source_categories.length) params.set('type', scene.request.source_categories.join(','));

  const targets = scene.request.fm_targets || [];
  if (targets.length) {
    const normal = encodeFmTargets(targets, 'normal');
    const executor = encodeFmTargets(targets, 'executor');
    if (normal) params.set('fm', normal);
    if (executor) params.set('efm', executor);
  } else {
    if (scene.request.subsystems.length) params.set('ss', scene.request.subsystems.join(','));
    if (scene.request.fms.length) params.set('mods', scene.request.fms.join(','));
  }
  if (scene.request.keyword) params.set('rk', scene.request.keyword);
  if (scene.filters.query) params.set('q', scene.filters.query);
  if (scene.filters.levels.length) params.set('level', scene.filters.levels.join(','));
  if (scene.filters.components.length) params.set('comp', scene.filters.components.join(','));
  if (scene.filters.modes.length) params.set('mode', scene.filters.modes.join(','));
  if (scene.filters.timeRange) {
    params.set('vs', `${scene.filters.timeRange.startNs}n`);
    params.set('ve', `${scene.filters.timeRange.endNs}n`);
  }
  if (scene.filters.durationRange?.minMs !== undefined) params.set('dmin', String(scene.filters.durationRange.minMs));
  if (scene.filters.durationRange?.maxMs !== undefined) params.set('dmax', String(scene.filters.durationRange.maxMs));
  // 这些视图开关始终显式写入 URL。这样复制/刷新日志定位链接时，
  // 即使当前值恰好等于默认值，也不会依赖另一个浏览器/后续版本的默认配置。
  params.set('err', scene.filters.errorsOnly ? '1' : '0');
  params.set('tl', scene.view.showTimeline ? '1' : '0');
  params.set('tld', scene.view.timelineDocked ? '1' : '0');
  params.set('grp', scene.view.timelineGroupingMode === 'merged' ? 'm' : 'p');
  params.set('sem', scene.view.semanticLabelsEnabled ? '1' : '0');
  params.set('raw', scene.view.rawLogMode ? '1' : '0');
  if (!scene.view.processTimelineExpanded) params.set('pex', '0');
  if (scene.view.hiddenTimelineComponents.length) params.set('hide', scene.view.hiddenTimelineComponents.join(','));
  params.set('ps', String(scene.view.logPageSize));
  params.set('order', scene.view.logSortOrder);
  if (scene.view.error && scene.view.error.index >= 0) params.set('ai', String(scene.view.error.index + 1));

  const url = new URL(window.location.href);
  const search = readableSearch(params);
  window.history.replaceState(window.history.state, '', `${url.pathname}?${search}`);
}

function sharedSceneFilters(scene: SharedLogScene): Filters {
  let timeRange: TimeRangeFilter | undefined;
  if (scene.filters.timeRange) {
    try {
      const startNs = BigInt(scene.filters.timeRange.startNs);
      const endNs = BigInt(scene.filters.timeRange.endNs);
      if (endNs >= startNs) timeRange = { startNs, endNs };
    } catch {
      timeRange = undefined;
    }
  }
  return {
    query: scene.filters.query || '',
    levels: new Set(scene.filters.levels || []),
    components: new Set(scene.filters.components || []),
    modes: new Set(scene.filters.modes || []),
    timeRange,
    durationRange: scene.filters.durationRange ? { ...scene.filters.durationRange } : undefined,
    errorsOnly: Boolean(scene.filters.errorsOnly),
  };
}

type TaskStatus = 'ready' | 'analyzing' | 'cancelled' | 'error';

interface LogTask {
  id: string;
  name: string;
  sourceNames: string[];
  sources: WorkerImportSource[];
  createdAt: number;
  status: TaskStatus;
  entries: LogEntry[];
  issues: ParseIssue[];
  progress?: ImportProgress;
  errorMessage?: string;
  importStrategy?: ImportStrategy;
  fullLoaded?: boolean;
  requestedStartNs?: bigint;
  latestAvailableNs?: bigint;
  earliestImportedNs?: bigint;
  latestImportedNs?: bigint;
  selectedBytes?: number;
  totalFileBytes?: number;
  skippedSourceCount?: number;
  remoteEnvironmentId?: number;
  remoteOperationId?: string;
  remoteRequest?: LogWindowRequest;
  /** 用户在查询面板中直接勾选的模块；不包含日志查询自动展开的依赖模块，仅用于实时监听。 */
  liveTargets?: NonNullable<LogWindowRequest['fm_targets']>;
  remoteProgress?: LogSearchProgress & { receivedBytes?: number };
  remoteTaskKey?: string;
  /** 远端任务已经确认检索过的连续时间覆盖范围；即使区间内没有日志也算已加载。 */
  loadedStartNs?: bigint;
  loadedEndNs?: bigint;
  /** 双端时间窗越界时的增量补检索状态。 */
  incrementalLoading?: boolean;
  incrementalMessage?: string;
  incrementalAddedCount?: number;
  viewFilters?: Filters;
  /** 自动源码语义只用于当前日志任务展示，不属于持久化规则。 */
  autoSemantics?: Record<string, SemanticSourceResult>;
  semanticAutoStatus?: 'loading' | 'ready' | 'error';
  semanticAutoMessage?: string;
}

interface StreamingImportOptions {
  strategy?: ImportStrategy;
  recentSeconds?: number;
  targetStartNs?: bigint;
  replaceTask?: boolean;
}

interface ImportBufferState {
  entryChunks: LogEntry[][];
  issueChunks: ParseIssue[][];
  remoteOperationId?: string;
  remoteKeyword?: string;
  /** 增量解析时 worker 使用独立 taskId，最终合并回这个现有日志任务。 */
  mergeIntoTaskId?: string;
  /** 实时监听增量：解析后按单条日志排队增量渲染。 */
  live?: boolean;
  /** 用于丢弃停止监听后仍在渲染队列中的旧批次。 */
  liveGeneration?: number;
}

const EMPTY_FILTERS: Filters = {
  query: '',
  levels: new Set(),
  components: new Set(),
  modes: new Set(),
  timeRange: undefined,
  durationRange: undefined,
  errorsOnly: false,
};

function cloneFilters(filters: Filters): Filters {
  return {
    ...filters,
    levels: new Set(filters.levels),
    components: new Set(filters.components),
    modes: new Set(filters.modes),
    timeRange: filters.timeRange ? { ...filters.timeRange } : undefined,
    durationRange: filters.durationRange ? { ...filters.durationRange } : undefined,
  };
}

function semanticSourceKey(sourceFile: string | undefined, functionName: string | undefined): string {
  const basename = String(sourceFile || '').replace(/\\/g, '/').split('/').pop()?.trim().toLowerCase() || '';
  const fn = String(functionName || '').trim().replace(/\(\)$/, '').split(/::|\./).pop()?.toLowerCase() || '';
  return `${basename}|${fn}`;
}

const DEFAULT_RANGE_SECONDS = 7 * 24 * 60 * 60;
const LOG_PAGE_SIZE_OPTIONS = [100, 250, 500, 1000, 2000, 3000, 5000] as const;
/** Upper bound for every page-size entry point (select, shared scene URL, saved view). */
const MAX_LOG_PAGE_SIZE = 5000;
const ERROR_RULE_STORAGE_KEY = 'tracelens.error-rules.v3';
const LEGACY_ERROR_KEYWORD_STORAGE_KEY = 'tracelens.error-keywords.v2';
const ErrorRuleContext = createContext<readonly ErrorMatchRule[]>(DEFAULT_ERROR_RULES);
const FoldingRuleContext = createContext<readonly FoldingRule[]>([]);
interface SemanticDisplayContextValue {
  enabled: boolean;
  rules: readonly DisplayRule[];
  autoSemantics?: Record<string, SemanticSourceResult>;
  onEditEntryRule?: (entry: LogEntry, ruleId?: string) => void;
  onEditFunctionRule?: (node: FunctionNode, ruleId?: string, autoDescription?: string) => void;
}

const SemanticDisplayContext = createContext<SemanticDisplayContextValue>({ enabled: false, rules: [] });

function loadErrorRules(): ErrorMatchRule[] {
  if (typeof window === 'undefined') return DEFAULT_ERROR_RULES.map((rule) => ({ ...rule }));
  try {
    const stored = window.localStorage.getItem(ERROR_RULE_STORAGE_KEY);
    if (stored) {
      const parsed = JSON.parse(stored);
      if (Array.isArray(parsed)) {
        const rules = parsed.flatMap((item, index) => {
          if (!item || typeof item !== 'object') return [];
          const keyword = String(item.keyword ?? '').trim();
          if (!keyword) return [];
          return [{
            id: String(item.id || `error-rule-loaded-${index}`),
            keyword,
            caseSensitive: Boolean(item.caseSensitive),
            wholeWord: Boolean(item.wholeWord),
            enabled: item.enabled !== false,
          } satisfies ErrorMatchRule];
        });
        return rules;
      }
    }

    // v2 仅保存字符串关键字；迁移时保留旧版“区分大小写 + 全词匹配”的行为。
    const legacy = window.localStorage.getItem(LEGACY_ERROR_KEYWORD_STORAGE_KEY);
    if (legacy) {
      const parsed = JSON.parse(legacy);
      if (Array.isArray(parsed)) {
        const seen = new Set<string>();
        const migrated = parsed.flatMap((value, index) => {
          const keyword = String(value).trim();
          if (!keyword || seen.has(keyword)) return [];
          seen.add(keyword);
          return [{ id: `legacy-error-rule-${index}`, keyword, caseSensitive: true, wholeWord: true, enabled: true } satisfies ErrorMatchRule];
        });
        return migrated;
      }
    }
    return DEFAULT_ERROR_RULES.map((rule) => ({ ...rule }));
  } catch {
    return DEFAULT_ERROR_RULES.map((rule) => ({ ...rule }));
  }
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

const SEVERITY_RANK: Record<ContentSeverity, number> = {
  normal: 0,
  warning: 1,
  error: 2,
};

function classNames(...values: Array<string | false | null | undefined>): string {
  return values.filter(Boolean).join(' ');
}

function formatBytes(value: number | undefined): string {
  if (!value || value <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let size = value;
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  return `${size >= 100 || unitIndex === 0 ? Math.round(size) : size.toFixed(1)} ${units[unitIndex]}`;
}

function hasTextSelection(): boolean {
  return Boolean(window.getSelection()?.toString().trim());
}

function maxSeverity(...values: ContentSeverity[]): ContentSeverity {
  return values.reduce<ContentSeverity>(
    (highest, current) => (SEVERITY_RANK[current] > SEVERITY_RANK[highest] ? current : highest),
    'normal',
  );
}

function itemSeverity(item: TimelineItem): ContentSeverity {
  if (item.kind === 'log') return item.entry.severity;
  return maxSeverity(...item.children.map(itemSeverity));
}

function traceSeverity(trace: TraceTimeline): ContentSeverity {
  return maxSeverity(...trace.entries.map((entry) => entry.severity));
}

function threadSeverity(thread: ThreadTimeline): ContentSeverity {
  return maxSeverity(...thread.traces.map(traceSeverity));
}

function processSeverity(process: ProcessTimeline): ContentSeverity {
  return maxSeverity(...process.threads.map(threadSeverity));
}

function processErrorCount(process: ProcessTimeline): number {
  return process.threads.reduce(
    (total, thread) => total + thread.traces.reduce(
      (traceTotal, trace) => traceTotal + trace.entries.filter((entry) => entry.severity === 'error').length,
      0,
    ),
    0,
  );
}

function traceHasError(trace: TraceTimeline): boolean {
  return trace.entries.some((entry) => entry.severity === 'error');
}

function functionCount(items: TimelineItem[]): number {
  return items.reduce((total, item) => {
    if (item.kind === 'log') return total;
    // repeated 只是展示层外壳，不额外增加一次“函数/子流程”统计；真实调用仍由其 children 计数。
    return total + (item.origin === 'repeated' ? 0 : 1) + functionCount(item.children);
  }, 0);
}

function collectFunctionIds(items: TimelineItem[], target = new Set<string>()): Set<string> {
  items.forEach((item) => {
    if (item.kind === 'function') {
      target.add(item.id);
      collectFunctionIds(item.children, target);
    }
  });
  return target;
}


function shortTime(timestamp: string): string {
  return timestamp.split(' ')[1] ?? timestamp;
}

function isZeroTrace(traceKey: string): boolean {
  return traceKey === 'local';
}

function compactTraceId(traceId: string): string {
  return `#${traceId.slice(-6)}`;
}

interface EntryTimeRange {
  start: LogEntry;
  end: LogEntry;
  startNs?: bigint;
  endNs?: bigint;
  duration?: bigint;
}

function processEntries(process: ProcessTimeline): LogEntry[] {
  return process.threads.flatMap((thread) => thread.traces.flatMap((trace) => trace.entries));
}

function entryTimeRange(entries: LogEntry[]): EntryTimeRange | undefined {
  if (entries.length === 0) return undefined;
  const ordered = entries.slice().sort(compareEntries);
  const start = ordered[0];
  const end = ordered[ordered.length - 1];
  const startNs = start.timestampNs;
  const endNs = end.timestampNs;
  return {
    start,
    end,
    startNs,
    endNs,
    duration: startNs !== undefined && endNs !== undefined && endNs >= startNs ? endNs - startNs : undefined,
  };
}

function compactTimestamp(timestamp: string, includeDate = false): string {
  const [date = '', time = timestamp] = timestamp.split(' ');
  const compactTime = time.replace(/(\.\d{3})\d+$/, '$1');
  return includeDate ? `${date} ${compactTime}` : compactTime;
}

function secondTimestamp(timestamp: string, includeDate = false): string {
  const [date = '', time = timestamp] = timestamp.split(' ');
  const secondTime = time.split('.')[0] ?? time;
  return includeDate ? `${date} ${secondTime}` : secondTime;
}

function compactEntryRangeSeconds(entries: LogEntry[], includeDate = false): string {
  const range = entryTimeRange(entries);
  if (!range) return '无时间信息';
  if (!includeDate) {
    return `${secondTimestamp(range.start.timestamp)}–${secondTimestamp(range.end.timestamp)}`;
  }
  const startDate = range.start.timestamp.slice(0, 10);
  const endDate = range.end.timestamp.slice(0, 10);
  if (startDate === endDate) {
    return `${startDate} ${secondTimestamp(range.start.timestamp)}–${secondTimestamp(range.end.timestamp)}`;
  }
  return `${secondTimestamp(range.start.timestamp, true)}–${secondTimestamp(range.end.timestamp, true)}`;
}

function compactRangeDuration(value?: bigint): string {
  if (value === undefined) return '—';
  if (value < 1_000_000n) return `${Number(value) / 1_000} μs`;
  if (value < 1_000_000_000n) return `${(Number(value) / 1_000_000).toFixed(value < 10_000_000n ? 2 : 1)} ms`;
  if (value < 60_000_000_000n) return `${(Number(value) / 1_000_000_000).toFixed(value < 10_000_000_000n ? 2 : 1)} s`;
  const totalSeconds = Number(value / 1_000_000_000n);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes < 60) return `${minutes}m${seconds.toString().padStart(2, '0')}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h${(minutes % 60).toString().padStart(2, '0')}m`;
}

function compactEntryRange(entries: LogEntry[], includeDuration = true): string {
  const range = entryTimeRange(entries);
  if (!range) return '无时间信息';
  const sameDate = range.start.timestamp.slice(0, 10) === range.end.timestamp.slice(0, 10);
  const start = compactTimestamp(range.start.timestamp, !sameDate);
  const end = compactTimestamp(range.end.timestamp, !sameDate);
  return includeDuration ? `${start}–${end} · ${compactRangeDuration(range.duration)}` : `${start}–${end}`;
}

function formatNsTick(value: bigint, includeDate: boolean, includeMilliseconds: boolean): string {
  const date = new Date(Number(value / 1_000_000n));
  const pad = (part: number, length = 2) => String(part).padStart(length, '0');
  const datePart = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  const timePart = `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  const milliseconds = includeMilliseconds ? `.${pad(date.getMilliseconds(), 3)}` : '';
  return includeDate ? `${datePart} ${timePart}${milliseconds}` : `${timePart}${milliseconds}`;
}

function timestampTextToNs(value: string): bigint | undefined {
  const normalized = value.trim().replace(/^\[/, '').replace(/]$/, '').replace('T', ' ');
  const match = normalized.match(/^(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?$/);
  if (!match) return undefined;

  const [, year, month, day, hour, minute, second, fraction = ''] = match;
  const date = new Date(
    Number(year),
    Number(month) - 1,
    Number(day),
    Number(hour),
    Number(minute),
    Number(second),
    0,
  );
  if (Number.isNaN(date.getTime())) return undefined;
  const epochSeconds = BigInt(Math.trunc(date.getTime() / 1000));
  const nanoseconds = BigInt(fraction.padEnd(9, '0').slice(0, 9) || '0');
  return epochSeconds * 1_000_000_000n + nanoseconds;
}

function nsToTimestampText(value: bigint): string {
  const date = new Date(Number(value / 1_000_000n));
  const pad = (part: number, length = 2) => String(part).padStart(length, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${pad(date.getMilliseconds(), 3)}`;
}

function nsToQueryEndTimestampText(value: bigint): string {
  const millisecond = 1_000_000n;
  const rounded = value % millisecond === 0n ? value : ((value / millisecond) + 1n) * millisecond;
  return nsToTimestampText(rounded);
}

function requestTimeRangeNs(request?: LogWindowRequest): TimeRangeFilter | undefined {
  if (!request) return undefined;
  const startNs = timestampTextToNs(request.start_time);
  const endNs = timestampTextToNs(request.end_time);
  if (startNs === undefined || endNs === undefined || endNs < startNs) return undefined;
  return { startNs, endNs };
}

function logEntryMergeKey(entry: LogEntry): string {
  return [
    entry.timestampNs?.toString() ?? entry.timestamp,
    entry.component,
    entry.processId,
    entry.threadId,
    entry.logCategory ?? '',
    entry.raw,
  ].join('\u0001');
}

function mergeLogEntries(existing: LogEntry[], incoming: LogEntry[]): LogEntry[] {
  if (incoming.length === 0) return existing;
  const merged = new Map<string, LogEntry>();
  existing.forEach((entry) => merged.set(logEntryMergeKey(entry), entry));
  incoming.forEach((entry) => {
    const key = logEntryMergeKey(entry);
    if (!merged.has(key)) merged.set(key, entry);
  });
  return Array.from(merged.values());
}

function mergeParseIssues(existing: ParseIssue[], incoming: ParseIssue[]): ParseIssue[] {
  if (incoming.length === 0) return existing;
  const merged = new Map<string, ParseIssue>();
  existing.forEach((issue) => merged.set(`${issue.sourceFile}\u0001${issue.raw}\u0001${issue.reason}`, issue));
  incoming.forEach((issue) => merged.set(`${issue.sourceFile}\u0001${issue.raw}\u0001${issue.reason}`, issue));
  return Array.from(merged.values());
}

function rangePercent(value: bigint, start: bigint, span: bigint): number {
  if (span <= 0n) return 0;
  return Number(((value - start) * 10_000n) / span) / 100;
}

const TIMELINE_MAX_CANVAS_WIDTH = 24_000_000;
const TIMELINE_MAX_ADAPTIVE_ZOOM = 240;
// 默认自动展开只占“真正可见的时间轨道”约 2/3，并把检索窗口居中。
// 左侧固定模块列不属于时间轨道，因此目标比例必须按可用轨道宽度动态折算，
// 不能直接拿整个滚动容器的 2/3，否则 Brush 会看起来越出当前屏幕。
const TIMELINE_AUTO_VISIBLE_TRACK_RATIO = 2 / 3;
const TIMELINE_MIN_AUTO_RANGE_SPAN_NS = 250_000_000n;

function timelineAutoTargetRatio(viewportWidth: number, labelWidth: number): number {
  const safeViewportWidth = Math.max(1, viewportWidth);
  const frozenColumnWidth = Math.min(safeViewportWidth * 0.55, Math.max(0, labelWidth) + 64);
  const visibleTrackWidth = Math.max(1, safeViewportWidth - frozenColumnWidth);
  const ratio = (visibleTrackWidth / safeViewportWidth) * TIMELINE_AUTO_VISIBLE_TRACK_RATIO;
  return Math.max(0.12, Math.min(TIMELINE_AUTO_VISIBLE_TRACK_RATIO, ratio));
}

function maxTimelineZoomForSpan(viewSpan: bigint, viewportWidth: number): number {
  const timelineBaseCanvasWidth = Math.max(1, viewportWidth);
  const maxZoomByCanvas = TIMELINE_MAX_CANVAS_WIDTH / timelineBaseCanvasWidth;
  const maxZoomByPrecision = viewSpan > BigInt(TIMELINE_MAX_CANVAS_WIDTH)
    ? maxZoomByCanvas
    : Number(viewSpan) / timelineBaseCanvasWidth;
  return Math.max(1, Math.floor(Math.min(maxZoomByCanvas, maxZoomByPrecision) * 10) / 10);
}

function adaptiveTimelineZoom(viewSpan: bigint, focusSpan: bigint, targetRatio: number, maxTimelineZoom: number): number {
  if (viewSpan <= 0n || focusSpan <= 0n) return 1;
  const rawZoom = (Number(viewSpan) / Number(focusSpan)) * targetRatio;
  const boundedZoom = Math.max(1, Math.min(TIMELINE_MAX_ADAPTIVE_ZOOM, maxTimelineZoom, rawZoom));
  return boundedZoom >= 100
    ? Math.round(boundedZoom)
    : Number(boundedZoom.toFixed(1));
}

type TimelineLabelHoverDetail = {
  text: string;
  color: string;
  ruleName: string;
  timestamp: string;
};

type TimelineAnomalyHoverDetail = {
  severity: 'error' | 'warning';
  level: string;
  timestamp: string;
  message: string;
  component: string;
  sourceFile: string;
  lineNumber: number;
};

function suppressTimelineNativeTitle(event: React.MouseEvent<HTMLElement>) {
  const timelineTarget = event.currentTarget.closest<HTMLElement>('.timeline-click-target');
  if (!timelineTarget || timelineTarget.dataset.suppressedTimelineTitle !== undefined) return;
  const title = timelineTarget.getAttribute('title');
  if (!title) return;
  timelineTarget.dataset.suppressedTimelineTitle = title;
  timelineTarget.removeAttribute('title');
}

function restoreTimelineNativeTitle(event: React.MouseEvent<HTMLElement>) {
  const timelineTarget = event.currentTarget.closest<HTMLElement>('.timeline-click-target');
  if (!timelineTarget) return;
  const title = timelineTarget.dataset.suppressedTimelineTitle;
  if (title === undefined) return;
  timelineTarget.setAttribute('title', title);
  delete timelineTarget.dataset.suppressedTimelineTitle;
}

function TimelineAnomalyMarkers({
  entries,
  startNs,
  endNs,
  onLabelHover,
  onLabelLeave,
  onAnomalyHover,
  onAnomalyLeave,
  onEntryClick,
}: {
  entries: LogEntry[];
  startNs: bigint;
  endNs: bigint;
  onLabelHover?: (event: React.MouseEvent<HTMLElement>, detail: TimelineLabelHoverDetail) => void;
  onLabelLeave?: () => void;
  onAnomalyHover?: (event: React.MouseEvent<HTMLElement>, detail: TimelineAnomalyHoverDetail) => void;
  onAnomalyLeave?: () => void;
  onEntryClick?: (event: React.MouseEvent<HTMLElement>, entry: LogEntry) => void;
}) {
  const semanticDisplay = useContext(SemanticDisplayContext);
  const span = endNs - startNs;
  if (span <= 0n) return null;
  return (
    <>
      {entries.flatMap((entry) => {
        if (entry.timestampNs === undefined || entry.timestampNs < startNs || entry.timestampNs > endNs) return [];
        const left = Math.max(0, Math.min(100, rangePercent(entry.timestampNs, startNs, span)));
        const markers: React.ReactNode[] = [];
        if (entry.severity === 'error' || entry.severity === 'warning') {
          const anomalyDetail: TimelineAnomalyHoverDetail = {
            severity: entry.severity,
            level: entry.level,
            timestamp: entry.timestamp,
            message: entry.message || entry.summary || entry.raw,
            component: entry.component,
            sourceFile: entry.sourceFile,
            lineNumber: entry.lineNumber,
          };
          markers.push(
            <i
              role="img"
              aria-label={`${entry.level} 日志 ${entry.timestamp}`}
              className={classNames('process-gantt-anomaly-marker', `severity-${entry.severity}`)}
              key={`timeline-anomaly-${entry.id}`}
              style={{ '--anomaly-left': `${left}%` } as React.CSSProperties}
              onMouseEnter={(event) => {
                suppressTimelineNativeTitle(event);
                onAnomalyHover?.(event, anomalyDetail);
              }}
              onMouseMove={(event) => {
                event.stopPropagation();
                onAnomalyHover?.(event, anomalyDetail);
              }}
              onMouseLeave={(event) => {
                restoreTimelineNativeTitle(event);
                onAnomalyLeave?.();
              }}
              onClick={(event) => onEntryClick?.(event, entry)}
            />,
          );
        }
        if (semanticDisplay.enabled) {
          matchDisplayLabelRulesToEntry(semanticDisplay.rules, entry)
            .filter((match) => match.showLabelOnTimeline !== false)
            .slice(0, 4)
            .forEach((match, index) => {
              const labelColor = match.customLabelColor || '#2563eb';
              const hoverDetail: TimelineLabelHoverDetail = {
                text: match.customLabelText || '',
                color: labelColor,
                ruleName: match.ruleName,
                timestamp: entry.timestamp,
              };
              markers.push(
                <span
                  role="img"
                  aria-label={`时间线标签 ${match.customLabelText}`}
                  className="process-gantt-custom-label-marker"
                  key={`timeline-label-${entry.id}-${match.ruleId}-${index}`}
                  style={{ '--label-left': `${left}%`, '--label-color': labelColor, '--label-offset': `${index * 4}px` } as React.CSSProperties}
                  onMouseEnter={(event) => {
                    suppressTimelineNativeTitle(event);
                    onLabelHover?.(event, hoverDetail);
                  }}
                  onMouseMove={(event) => {
                    event.stopPropagation();
                    onLabelHover?.(event, hoverDetail);
                  }}
                  onMouseLeave={(event) => {
                    restoreTimelineNativeTitle(event);
                    onLabelLeave?.();
                  }}
                  onClick={(event) => onEntryClick?.(event, entry)}
                />,
              );
            });
        }
        return markers;
      })}
    </>
  );
}

// One tone per module, deliberately spread across *hue families* rather than shades of
// blue→green. The previous palette was eight pastels whose closest pair differed by only
// 11.4 in RGB distance (#d1fae5 vs #dcfce7), so two different modules rendered as the same
// colour and the timeline read as if it had no colour coding at all.
// All fills stay light enough for the dark `text` tone used on top of them.
const TIMELINE_COMPONENT_PALETTE = [
  { fill: '#bfdbfe', border: '#93c5fd', text: '#1d4ed8' }, // 蓝
  { fill: '#ddd6fe', border: '#c4b5fd', text: '#5b21b6' }, // 紫
  { fill: '#fbcfe8', border: '#f9a8d4', text: '#9d174d' }, // 粉
  { fill: '#fde68a', border: '#fcd34d', text: '#92400e' }, // 琥珀
  { fill: '#fed7aa', border: '#fdba74', text: '#9a3412' }, // 橙
  { fill: '#99f6e4', border: '#5eead4', text: '#0f766e' }, // 青
  { fill: '#bbf7d0', border: '#86efac', text: '#166534' }, // 绿
  { fill: '#e2e8f0', border: '#cbd5e1', text: '#475569' }, // 灰
] as const;

function timelineComponentStyle(component: string): React.CSSProperties {
  const palette = TIMELINE_COMPONENT_PALETTE;
  const index = Math.abs(componentHue(component)) % palette.length;
  const tone = palette[index];
  return {
    '--timeline-fill': tone.fill,
    '--timeline-border': tone.border,
    '--timeline-text': tone.text,
  } as React.CSSProperties;
}

function ComponentBadge({ component, compact = false }: { component: string; compact?: boolean }) {
  return (
    <span
      className={classNames('component-badge', compact && 'compact')}
      style={componentStyle(component)}
      title={`模块：${component}`}
    >
      {component}
    </span>
  );
}

interface MethodTraceMember {
  thread: ThreadTimeline;
  trace: TraceTimeline;
}

interface MethodTraceGroup {
  id: string;
  name: string;
  members: MethodTraceMember[];
  threadCount: number;
  entryCount: number;
  errorCount: number;
  severity: ContentSeverity;
}

function buildMethodTraceGroups(process: ProcessTimeline): MethodTraceGroup[] {
  const grouped = new Map<string, MethodTraceMember[]>();

  process.threads.forEach((thread) => {
    thread.traces.forEach((trace) => {
      const name = trace.firstFunctionName || '无函数入口';
      const members = grouped.get(name) ?? [];
      members.push({ thread, trace });
      grouped.set(name, members);
    });
  });

  return Array.from(grouped.entries())
    .map(([name, members]) => ({
      id: `method-${process.processId}-${name}`,
      name,
      members: members.sort((left, right) => {
        const leftEntry = left.trace.entries[0];
        const rightEntry = right.trace.entries[0];
        return leftEntry && rightEntry ? compareEntries(leftEntry, rightEntry) : 0;
      }),
      threadCount: new Set(members.map((member) => member.thread.threadId)).size,
      entryCount: members.reduce((sum, member) => sum + member.trace.entries.length, 0),
      errorCount: members.reduce(
        (sum, member) => sum + member.trace.entries.filter((entry) => entry.severity === 'error').length,
        0,
      ),
      severity: maxSeverity(...members.map((member) => traceSeverity(member.trace))),
    }))
    .sort((left, right) => {
      const leftEntry = left.members[0]?.trace.entries[0];
      const rightEntry = right.members[0]?.trace.entries[0];
      return leftEntry && rightEntry ? compareEntries(leftEntry, rightEntry) : left.name.localeCompare(right.name);
    });
}

interface CrossMethodTraceGroup {
  id: string;
  name: string;
  traces: TraceTimeline[];
  traceCount: number;
  componentCount: number;
  entryCount: number;
  errorCount: number;
  severity: ContentSeverity;
}

function buildCrossMethodTraceGroups(traces: TraceTimeline[]): CrossMethodTraceGroup[] {
  const grouped = new Map<string, TraceTimeline[]>();

  traces.forEach((trace) => {
    const name = trace.firstFunctionName || '无函数入口';
    const members = grouped.get(name) ?? [];
    members.push(trace);
    grouped.set(name, members);
  });

  return Array.from(grouped.entries())
    .map(([name, members]) => {
      const sortedTraces = members.slice().sort((left, right) => {
        const leftEntry = left.entries[0];
        const rightEntry = right.entries[0];
        return leftEntry && rightEntry ? compareEntries(leftEntry, rightEntry) : 0;
      });
      return {
        id: `cross-method-${name}`,
        name,
        traces: sortedTraces,
        traceCount: sortedTraces.length,
        componentCount: new Set(sortedTraces.flatMap((trace) => trace.components)).size,
        entryCount: sortedTraces.reduce((sum, trace) => sum + trace.entries.length, 0),
        errorCount: sortedTraces.reduce(
          (sum, trace) => sum + trace.entries.filter((entry) => entry.severity === 'error').length,
          0,
        ),
        severity: maxSeverity(...sortedTraces.map(traceSeverity)),
      };
    })
    .sort((left, right) => {
      const leftEntry = left.traces[0]?.entries[0];
      const rightEntry = right.traces[0]?.entries[0];
      return leftEntry && rightEntry ? compareEntries(leftEntry, rightEntry) : left.name.localeCompare(right.name);
    });
}

function traceEntryLog(trace: TraceTimeline): LogEntry | undefined {
  const firstFunction = trace.items.find((item): item is FunctionNode => item.kind === 'function');
  return firstFunction?.startEntry ?? trace.entries[0];
}

function compareEntries(left: LogEntry, right: LogEntry): number {
  if (left.timestampNs !== undefined && right.timestampNs !== undefined) {
    if (left.timestampNs < right.timestampNs) return -1;
    if (left.timestampNs > right.timestampNs) return 1;
  }
  return left.lineNumber - right.lineNumber;
}

/**
 * Split a search box into keywords.
 *
 * Space, comma and the full-width comma all separate — typing `ERROR 超时，通信` in one box is
 * how people actually search. Multiple keywords are OR-ed (a line matching *any* of them is
 * kept), which is what makes one search return several different lines instead of one.
 */
function splitSearchKeywords(value: string): string[] {
  return String(value || '')
    .split(/[\s,，、]+/)
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean)
    .slice(0, 24);
}

function matchesEntry(entry: LogEntry, filters: Filters): boolean {
  if (filters.errorsOnly && entry.severity !== 'error') return false;
  if (filters.timeRange) {
    if (entry.timestampNs === undefined) return false;
    if (entry.timestampNs < filters.timeRange.startNs || entry.timestampNs > filters.timeRange.endNs) return false;
  }
  if (filters.levels.size > 0 && !filters.levels.has(entry.level)) return false;
  if (filters.components.size > 0 && !filters.components.has(entry.component)) return false;
  if (filters.modes.size > 0 && !filters.modes.has(entry.mode)) return false;

  const keywords = splitSearchKeywords(filters.query);
  if (!keywords.length) return true;
  const haystacks = [
    entry.raw,
    entry.message,
    entry.functionName,
    entry.component,
    entry.processId,
    entry.threadId,
    entry.source.raw,
    entry.sourceFile,
    entry.rpc.raw,
  ].filter(Boolean).map((value) => String(value).toLowerCase());
  return keywords.some((keyword) => haystacks.some((value) => value.includes(keyword)));
}

function normalizedDurationBounds(range?: DurationRangeFilter): { minNs?: bigint; maxNs?: bigint } | undefined {
  if (!range) return undefined;
  const parse = (value: string | undefined) => {
    if (value === undefined || !value.trim()) return undefined;
    const numeric = Number(value);
    return Number.isFinite(numeric) && numeric >= 0 ? numeric : undefined;
  };
  let minMs = parse(range.minMs);
  let maxMs = parse(range.maxMs);
  if (minMs === undefined && maxMs === undefined) return undefined;
  if (minMs !== undefined && maxMs !== undefined && minMs > maxMs) [minMs, maxMs] = [maxMs, minMs];
  const toNs = (value: number) => BigInt(Math.round(value * 1_000_000));
  return {
    ...(minMs !== undefined ? { minNs: toNs(minMs) } : {}),
    ...(maxMs !== undefined ? { maxNs: toNs(maxMs) } : {}),
  };
}

function functionMatchesDuration(node: FunctionNode, range?: DurationRangeFilter): boolean {
  const bounds = normalizedDurationBounds(range);
  if (!bounds) return true;
  const duration = durationNs(node);
  if (duration === undefined) return false;
  if (bounds.minNs !== undefined && duration < bounds.minNs) return false;
  if (bounds.maxNs !== undefined && duration > bounds.maxNs) return false;
  return true;
}

function itemMatchesBase(item: TimelineItem, filters: Filters): boolean {
  if (item.kind === 'log') return matchesEntry(item.entry, filters);
  if (matchesEntry(item.startEntry, filters)) return true;
  if (item.endEntry && matchesEntry(item.endEntry, filters)) return true;
  return item.children.some((child) => itemMatchesBase(child, filters));
}

function itemMatches(item: TimelineItem, filters: Filters): boolean {
  if (!normalizedDurationBounds(filters.durationRange)) return itemMatchesBase(item, filters);
  if (item.kind === 'log') return false;
  const baseFilters: Filters = { ...filters, durationRange: undefined };
  if (functionMatchesDuration(item, filters.durationRange) && itemMatchesBase(item, baseFilters)) return true;
  return item.children.some((child) => itemMatches(child, filters));
}

function filteredItems(items: TimelineItem[], filters: Filters): TimelineItem[] {
  const hasFilters =
    Boolean(filters.query.trim()) ||
    filters.levels.size > 0 ||
    filters.components.size > 0 ||
    filters.modes.size > 0 ||
    Boolean(filters.timeRange) ||
    Boolean(normalizedDurationBounds(filters.durationRange)) ||
    Boolean(filters.errorsOnly);

  if (!hasFilters) return items;
  return items.filter((item) => itemMatches(item, filters));
}

function collectDurationMatchedEntryIds(processes: ProcessTimeline[], range?: DurationRangeFilter): Set<string> | undefined {
  if (!normalizedDurationBounds(range)) return undefined;
  const ids = new Set<string>();
  const visit = (items: TimelineItem[]) => {
    items.forEach((item) => {
      if (item.kind !== 'function') return;
      if (functionMatchesDuration(item, range)) functionNodeEntries(item).forEach((entry) => ids.add(entry.id));
      visit(item.children);
    });
  };
  processes.forEach((process) => process.threads.forEach((thread) => thread.traces.forEach((trace) => visit(trace.items))));
  return ids;
}


interface TimelinePageEntryLocation {
  traceId: string;
  leaf: Extract<TimelineItem, { kind: 'log' }>;
  parentNodeId?: string;
}

interface TimelinePageNodeLocation {
  traceId: string;
  node: FunctionNode;
  parentNodeId?: string;
}

interface TimelinePageIndex {
  traces: Map<string, TraceTimeline>;
  entries: Map<string, TimelinePageEntryLocation>;
  nodes: Map<string, TimelinePageNodeLocation>;
}

/**
 * 为分页建立一次轻量定位索引。翻页时不再递归遍历整棵函数树，
 * 只根据当前页最多 1000 条日志反查必要的父函数路径。
 */
function createTimelinePageIndex(traces: TraceTimeline[]): TimelinePageIndex {
  const index: TimelinePageIndex = {
    traces: new Map(traces.map((trace) => [trace.id, trace])),
    entries: new Map(),
    nodes: new Map(),
  };

  const visit = (trace: TraceTimeline, items: TimelineItem[], parentNodeId?: string) => {
    items.forEach((item) => {
      if (item.kind === 'log') {
        index.entries.set(item.entry.id, { traceId: trace.id, leaf: item, parentNodeId });
        return;
      }
      index.nodes.set(item.id, { traceId: trace.id, node: item, parentNodeId });
      visit(trace, item.children, item.id);
    });
  };

  traces.forEach((trace) => visit(trace, trace.items));
  return index;
}

function finalizePageItems(items: TimelineItem[]): TimelineItem[] {
  return items.map((item) => {
    if (item.kind === 'log') return item;
    const children = finalizePageItems(item.children);
    if (item.origin !== 'consecutive') return { ...item, children };

    const visibleLogs = children.filter((child): child is Extract<TimelineItem, { kind: 'log' }> => child.kind === 'log');
    const firstEntry = visibleLogs[0]?.entry ?? item.startEntry;
    const lastEntry = visibleLogs[visibleLogs.length - 1]?.entry ?? firstEntry;
    return {
      ...item,
      component: firstEntry.component,
      processId: firstEntry.processId,
      threadId: firstEntry.threadId,
      rpc: firstEntry.rpc,
      source: firstEntry.source,
      startEntry: firstEntry,
      endEntry: lastEntry,
      children,
    };
  });
}

function buildPaginatedTraces(index: TimelinePageIndex, pageEntries: LogEntry[]): TraceTimeline[] {
  interface MutablePageTrace {
    trace: TraceTimeline;
    roots: TimelineItem[];
    nodeClones: Map<string, FunctionNode>;
    entries: LogEntry[];
    seenEntries: Set<string>;
  }

  const builders = new Map<string, MutablePageTrace>();

  pageEntries.forEach((entry) => {
    const location = index.entries.get(entry.id);
    if (!location) return;
    const trace = index.traces.get(location.traceId);
    if (!trace) return;

    let builder = builders.get(trace.id);
    if (!builder) {
      builder = { trace, roots: [], nodeClones: new Map(), entries: [], seenEntries: new Set() };
      builders.set(trace.id, builder);
    }
    if (builder.seenEntries.has(entry.id)) return;
    builder.seenEntries.add(entry.id);
    builder.entries.push(entry);

    const path: TimelinePageNodeLocation[] = [];
    let parentNodeId = location.parentNodeId;
    while (parentNodeId) {
      const nodeLocation = index.nodes.get(parentNodeId);
      if (!nodeLocation) break;
      path.unshift(nodeLocation);
      parentNodeId = nodeLocation.parentNodeId;
    }

    let container = builder.roots;
    path.forEach(({ node }) => {
      let clone = builder!.nodeClones.get(node.id);
      if (!clone) {
        clone = { ...node, children: [] };
        builder!.nodeClones.set(node.id, clone);
        container.push(clone);
      }
      container = clone.children;
    });
    container.push(location.leaf);
  });

  return Array.from(builders.values()).map((builder) => ({
    ...builder.trace,
    entries: builder.entries,
    items: finalizePageItems(builder.roots),
  }));
}

function HighlightedText({ text }: { text: string }) {
  const errorRules = useContext(ErrorRuleContext);
  const marks = useMemo(() => {
    type Mark = { start: number; end: number; tone: 'error' | 'warning' };
    const candidates: Mark[] = [];
    const pushMatches = (
      keyword: string,
      caseSensitive: boolean,
      wholeWord: boolean,
      tone: 'error' | 'warning',
    ) => {
      if (!keyword) return;
      const escaped = escapeRegExp(keyword);
      const body = wholeWord
        ? `(?<![\\p{L}\\p{N}_])${escaped}(?![\\p{L}\\p{N}_])`
        : escaped;
      const regex = new RegExp(body, `${caseSensitive ? '' : 'i'}gu`);
      for (const match of text.matchAll(regex)) {
        const index = match.index ?? -1;
        if (index >= 0 && match[0]) candidates.push({ start: index, end: index + match[0].length, tone });
      }
    };

    errorRules.filter((rule) => rule.enabled).forEach((rule) => pushMatches(rule.keyword, rule.caseSensitive, rule.wholeWord, 'error'));
    ['WARN', 'WARNING', 'WRAN'].forEach((keyword) => pushMatches(keyword, true, true, 'warning'));
    candidates.sort((left, right) => left.start - right.start || (right.end - right.start) - (left.end - left.start) || (left.tone === 'error' ? -1 : 1));

    const accepted: Mark[] = [];
    let cursor = -1;
    candidates.forEach((candidate) => {
      if (candidate.start < cursor) return;
      accepted.push(candidate);
      cursor = candidate.end;
    });
    return accepted;
  }, [errorRules, text]);

  if (!marks.length) return <>{text}</>;
  const parts: React.ReactNode[] = [];
  let cursor = 0;
  marks.forEach((mark, index) => {
    if (mark.start > cursor) parts.push(text.slice(cursor, mark.start));
    parts.push(<mark className={`keyword keyword-${mark.tone}`} key={`${mark.start}-${index}`}>{text.slice(mark.start, mark.end)}</mark>);
    cursor = mark.end;
  });
  if (cursor < text.length) parts.push(text.slice(cursor));
  return <>{parts}</>;
}

function normalizeDateTimePickerValue(value: string): string {
  if (!value) return '';
  const normalized = value.replace('T', ' ');
  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(normalized)) return `${normalized}:00.000`;
  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(normalized)) return `${normalized}.000`;
  return normalized;
}

function timestampToPickerValue(value: string): string {
  const match = value.trim().replace(/^\[|\]$/g, '').match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})(?::(\d{2}))?(?:\.(\d{1,3}))?/);
  if (!match) return '';
  const seconds = match[3] ?? '00';
  const milliseconds = (match[4] ?? '000').padEnd(3, '0').slice(0, 3);
  return `${match[1]}T${match[2]}:${seconds}.${milliseconds}`;
}

function DateTimeFilterInput({
  value,
  onChange,
  label,
}: {
  value: string;
  onChange: (value: string) => void;
  label: string;
}) {
  const pickerRef = useRef<HTMLInputElement>(null);

  function openPicker() {
    const picker = pickerRef.current;
    if (!picker) return;
    picker.value = timestampToPickerValue(value);
    if (typeof picker.showPicker === 'function') picker.showPicker();
    else picker.click();
  }

  return (
    <div className="date-time-filter-input">
      <input
        type="text"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        aria-label={label}
        placeholder={`粘贴${label}`}
        title="支持粘贴完整日志时间戳，也可以点击右侧日历选择"
      />
      <button type="button" className="date-picker-button" onClick={openPicker} aria-label={`选择${label}`} title={`选择${label}`}>
        <CalendarDays size={14} />
      </button>
      <input
        ref={pickerRef}
        className="date-picker-native"
        type="datetime-local"
        step="0.001"
        tabIndex={-1}
        aria-hidden="true"
        onChange={(event) => onChange(normalizeDateTimePickerValue(event.target.value))}
      />
    </div>
  );
}

function ToggleChip({
  active,
  children,
  onClick,
}: {
  active: boolean;
  children: React.ReactNode;
  onClick: () => void;
}) {
  return (
    <button type="button" className={classNames('filter-chip', active && 'active')} onClick={onClick}>
      {children}
    </button>
  );
}

function SwitchControl({
  checked,
  label,
  hint,
  tone = 'default',
  disabled = false,
  onChange,
}: {
  checked: boolean;
  label: string;
  hint: string;
  tone?: 'default' | 'danger';
  disabled?: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      className={classNames('switch-control', checked && 'checked', tone === 'danger' && 'danger')}
      disabled={disabled}
      title={hint}
      onClick={() => !disabled && onChange(!checked)}
    >
      <span className="switch-track"><span className="switch-thumb" /></span>
      <span className="switch-copy"><strong>{label}</strong><small>{hint}</small></span>
    </button>
  );
}

function RawLogView({ task, live = false }: { task: LogTask; live?: boolean }) {
  const [content, setContent] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [sourceIndex, setSourceIndex] = useState(0);
  const remoteRequestKey = JSON.stringify(task.remoteRequest ?? null);
  const localSource = task.sources[Math.min(sourceIndex, Math.max(0, task.sources.length - 1))];
  const preRef = useRef<HTMLPreElement | null>(null);
  /**
   * 实时监听中：直接展示已经流进来的原文行。
   *
   * RawLogView 原本只会取一次 raw 时间窗，所以「原始日志」和「实时监听」互斥（开一个就停另一个）。
   * 实时任务里每一行的 `entry.raw` 就是原文，边到边追加即可，不必再拉一次文件。
   */
  const streamedText = useMemo(
    () => task.entries.map((entry) => entry.raw || entry.message || '').filter(Boolean).join('\n'),
    [task.entries],
  );

  useEffect(() => {
    if (live) return undefined;   // 实时任务用流进来的原文，不再拉一次性时间窗
    let cancelled = false;
    const controller = new AbortController();
    setLoading(true);
    setError('');

    const load = async () => {
      try {
        let text = '';
        if (task.remoteEnvironmentId && task.remoteRequest) {
          const { blob } = await fetchLogWindow(task.remoteEnvironmentId, { ...task.remoteRequest, raw_text: true }, { signal: controller.signal });
          text = await blob.text();
        } else if (localSource?.kind === 'text') {
          text = localSource.text;
        } else if (localSource?.kind === 'file') {
          text = await localSource.file.text();
        }
        if (!cancelled) setContent(text);
      } catch (exc) {
        if (controller.signal.aborted || cancelled) return;
        if (!cancelled) setError(exc instanceof Error ? exc.message : String(exc));
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    void load();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [task.id, task.remoteEnvironmentId, remoteRequestKey, localSource?.id, live]);

  // 实时模式下新行到达就贴到底部（和日志列表的跟随行为一致）。
  useEffect(() => {
    if (!live) return;
    const node = preRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [live, streamedText]);

  useEffect(() => {
    if (sourceIndex >= task.sources.length) setSourceIndex(0);
  }, [sourceIndex, task.sources.length]);

  const sourceLabel = task.remoteEnvironmentId
    ? `${task.name} · 远程检索原文`
    : (localSource?.name || task.sourceNames[0] || task.name);

  return (
    <section className="raw-log-view" aria-label="原始日志查看">
      <header className="raw-log-view-header">
        <div className="raw-log-source-name"><FileCode2 size={15} /><strong>{sourceLabel}</strong></div>
        {!task.remoteEnvironmentId && task.sources.length > 1 && (
          <label className="raw-log-source-select">
            <span>文件</span>
            <select value={sourceIndex} onChange={(event) => setSourceIndex(Number(event.target.value))}>
              {task.sources.map((source, index) => <option key={source.id} value={index}>{source.name}</option>)}
            </select>
          </label>
        )}
        <span className="raw-log-mode-note">{live ? '纯文本 · 实时追加 · 不解析' : '纯文本 · 不解析 · 保留原始顺序'}</span>
      </header>
      {live ? (
        streamedText
          ? <pre className="raw-log-pre" tabIndex={0} ref={preRef}>{streamedText}</pre>
          : <div className="raw-log-loading"><LoaderCircle size={17} className="spin" />实时监听中，等待新增日志…</div>
      ) : loading ? (
        <div className="raw-log-loading"><LoaderCircle size={17} className="spin" />正在读取日志原文…</div>
      ) : error ? (
        <div className="raw-log-error"><AlertTriangle size={17} />{error}</div>
      ) : (
        <pre className="raw-log-pre" tabIndex={0}>{content}</pre>
      )}
    </section>
  );
}

function MetricItem({
  icon,
  label,
  value,
  tone = 'default',
}: {
  icon: React.ReactNode;
  label: string;
  value: string | number;
  tone?: 'default' | 'success' | 'warning' | 'danger';
}) {
  return (
    <div className={classNames('metric-item', `tone-${tone}`)}>
      <span className="metric-item-icon">{icon}</span>
      <span className="metric-item-label">{label}</span>
      <strong className="metric-item-value">{value}</strong>
    </div>
  );
}

const ProcessTimelineOverview = memo(function ProcessTimelineOverview({
  processes,
  selectedProcessId,
  selectedThreadId,
  selectedTraceId,
  selectedTimeRange,
  loadedTimeRange,
  timeSelectionDisabled,
  incrementalLoading,
  incrementalMessage,
  expanded,
  navigationPending,
  onToggle,
  onSelectProcess,
  onSelectTrace,
  onSelectTimeRange,
  onClearTimeRange,
  onNavigateTime,
  onNavigateComponentTime,
  onNavigateEntry,
  onTimeCursorInteractionChange,
  onHiddenComponentsChange,
  restoredHiddenComponents,
  filterScopeKey,
  headerActions,
  dragHandleProps,
}: {
  processes: ProcessTimeline[];
  selectedProcessId?: string;
  selectedThreadId?: string;
  selectedTraceId?: string;
  selectedTimeRange?: TimeRangeFilter;
  loadedTimeRange?: TimeRangeFilter;
  timeSelectionDisabled?: boolean;
  incrementalLoading?: boolean;
  incrementalMessage?: string;
  expanded: boolean;
  navigationPending: boolean;
  onToggle: () => void;
  onSelectProcess: (process: ProcessTimeline) => void;
  onSelectTrace: (thread: ThreadTimeline | undefined, trace: TraceTimeline) => void;
  onSelectTimeRange: (range: TimeRangeFilter) => void;
  onClearTimeRange: () => void;
  onNavigateTime: (timestampNs: bigint) => void;
  onNavigateComponentTime: (component: string, timestampNs: bigint) => void;
  onNavigateEntry: (entry: LogEntry) => void;
  onTimeCursorInteractionChange?: (active: boolean) => void;
  onHiddenComponentsChange?: (hiddenComponents: Set<string>) => void;
  restoredHiddenComponents?: Set<string>;
  filterScopeKey?: string;
  /** 渲染在标题行右侧的按钮（悬浮窗的「固定 / 关闭」）；仅悬浮窗传。 */
  headerActions?: ReactNode;
  /** 悬浮模式下把标题行当拖动手柄：把 pointer 事件透传给窗口拖拽逻辑。 */
  dragHandleProps?: React.HTMLAttributes<HTMLDivElement>;
}) {
  const [expandedComponents, setExpandedComponents] = useState<Set<string>>(new Set());
  const [hiddenTimelineComponents, setHiddenTimelineComponents] = useState<Set<string>>(new Set());
  const [expandedProcesses, setExpandedProcesses] = useState<Set<string>>(new Set());
  const [zoom, setZoom] = useState(1);
  // ATLog renders this timeline inside an expandable case card. Starting from a
  // fixed 940px canvas can participate in the card's min-content sizing before
  // ResizeObserver gets a chance to measure the real viewport, which lets a
  // zoomed canvas push the whole page wider.  Start ATLog timelines from 1px;
  // useLayoutEffect below measures the actual container before paint.
  const [timelineViewportWidth, setTimelineViewportWidth] = useState(() => filterScopeKey?.startsWith('atlog:') ? 1 : 940);
  const [viewportRange, setViewportRange] = useState<TimeRangeFilter>();
  const [labelWidth, setLabelWidth] = useState(320);
  const [brushDraft, setBrushDraft] = useState<{ start: number; end: number }>();
  const [timeCursorNs, setTimeCursorNs] = useState<bigint>();
  const [timelineHover, setTimelineHover] = useState<{ x: number; y: number; ns: bigint }>();
  const [timelineLabelHover, setTimelineLabelHover] = useState<(TimelineLabelHoverDetail & { x: number; y: number })>();
  const [timelineAnomalyHover, setTimelineAnomalyHover] = useState<(TimelineAnomalyHoverDetail & { x: number; y: number })>();
  const brushDraftRef = useRef<{ start: number; end: number }>();
  const brushDragRef = useRef<{
    pointerId: number;
    mode: 'new' | 'left' | 'right' | 'move';
    anchor: number;
    initialStart: number;
    initialEnd: number;
  }>();
  const brushFrameRef = useRef<number>();
  const timeCursorDragRef = useRef<{ pointerId: number }>();
  const timeCursorPendingRef = useRef<bigint>();
  const timeCursorTimerRef = useRef<number>();
  const timeCursorReleaseTimerRef = useRef<number>();
  const brushRef = useRef<HTMLDivElement>(null);
  const timelineScrollRef = useRef<HTMLDivElement>(null);
  const timelineShellRef = useRef<HTMLDivElement>(null);
  const labelUserResizedRef = useRef(false);
  const timeCursorElementRef = useRef<HTMLButtonElement>(null);
  const pendingZoomAnchorRef = useRef<{
    cursorScreenX?: number;
    fallbackContentRatio?: number;
    fallbackPointerX?: number;
  }>();
  const adaptiveZoomAppliedKeyRef = useRef<string>();
  const pendingAdaptiveFocusNsRef = useRef<bigint>();
  const labelResizeRef = useRef<{ pointerId: number; startX: number; startWidth: number }>();

  const processRows = useMemo(() => processes
    .flatMap((process) => {
      const entries = processEntries(process);
      const range = entryTimeRange(entries);
      if (!range || range.startNs === undefined || range.endNs === undefined) return [];
      const traceRows = process.threads
        .flatMap((thread) => thread.traces.flatMap((trace) => {
          const traceRange = entryTimeRange(trace.entries);
          if (!traceRange || traceRange.startNs === undefined || traceRange.endNs === undefined) return [];
          return [{ thread, trace, range: traceRange as EntryTimeRange & { startNs: bigint; endNs: bigint } }];
        }))
        .sort((left, right) => {
          const timeOrder = compareEntries(left.range.start, right.range.start);
          if (timeOrder !== 0) return timeOrder;
          return left.trace.firstFunctionName.localeCompare(right.trace.firstFunctionName);
        });
      return [{
        process,
        range: range as EntryTimeRange & { startNs: bigint; endNs: bigint },
        traceRows,
        entries,
        severity: processSeverity(process),
        errorCount: entries.filter((entry) => entry.severity === 'error').length,
      }];
    })
    .sort((left, right) => compareEntries(left.range.start, right.range.start)), [processes]);

  const componentGroups = useMemo(() => {
    const grouped = new Map<string, typeof processRows>();
    processRows.forEach((row) => {
      const rows = grouped.get(row.process.component) ?? [];
      rows.push(row);
      grouped.set(row.process.component, rows);
    });

    return Array.from(grouped.entries()).map(([component, rows]) => {
      const sortedRows = rows.slice().sort((left, right) => compareEntries(left.range.start, right.range.start));
      const startRow = sortedRows.reduce((earliest, row) => row.range.startNs < earliest.range.startNs ? row : earliest, sortedRows[0]);
      const endRow = sortedRows.reduce((latest, row) => row.range.endNs > latest.range.endNs ? row : latest, sortedRows[0]);
      return {
        component,
        processRows: sortedRows,
        range: {
          start: startRow.range.start,
          end: endRow.range.end,
          startNs: startRow.range.startNs,
          endNs: endRow.range.endNs,
          duration: endRow.range.endNs - startRow.range.startNs,
        },
        severity: maxSeverity(...sortedRows.map((row) => row.severity)),
        errorCount: sortedRows.reduce((sum, row) => sum + row.errorCount, 0),
        entryCount: sortedRows.reduce((sum, row) => sum + row.entries.length, 0),
        traceCount: sortedRows.reduce((sum, row) => sum + row.traceRows.length, 0),
      };
    }).sort((left, right) => compareEntries(left.range.start, right.range.start));
  }, [processRows]);

  function resolveTimelineBounds() {
    const globalStartRow = processRows.length > 0
      ? processRows.reduce((earliest, row) => row.range.startNs < earliest.range.startNs ? row : earliest, processRows[0])
      : undefined;
    const globalEndRow = processRows.length > 0
      ? processRows.reduce((latest, row) => row.range.endNs > latest.range.endNs ? row : latest, processRows[0])
      : undefined;
    const actualDataStart = globalStartRow?.range.startNs as bigint | undefined;
    const actualDataEnd = globalEndRow?.range.endNs as bigint | undefined;
    const loadedStart = loadedTimeRange?.startNs ?? actualDataStart;
    const loadedEnd = loadedTimeRange?.endNs ?? actualDataEnd;
    if (loadedStart === undefined || loadedEnd === undefined) return undefined;

    const loadedSpan = loadedEnd > loadedStart ? loadedEnd - loadedStart : 1n;
    const minExplorerPadding = 60n * 1_000_000_000n;
    const maxExplorerPadding = 10n * 60n * 1_000_000_000n;
    const desiredExplorerPadding = loadedSpan / 5n;
    const explorerPadding = loadedTimeRange
      ? desiredExplorerPadding < minExplorerPadding
        ? minExplorerPadding
        : desiredExplorerPadding > maxExplorerPadding
          ? maxExplorerPadding
          : desiredExplorerPadding
      : 0n;
    const explorerBaseStart = loadedStart - explorerPadding;
    const explorerBaseEnd = loadedEnd + explorerPadding;
    const explorerStart = selectedTimeRange && selectedTimeRange.startNs < explorerBaseStart ? selectedTimeRange.startNs : explorerBaseStart;
    const explorerEnd = selectedTimeRange && selectedTimeRange.endNs > explorerBaseEnd ? selectedTimeRange.endNs : explorerBaseEnd;
    const viewStart = viewportRange?.startNs ?? explorerStart;
    const viewEnd = viewportRange?.endNs ?? explorerEnd;
    const viewSpan = viewEnd > viewStart ? viewEnd - viewStart : 1n;

    return {
      globalStartRow,
      globalEndRow,
      actualDataStart,
      actualDataEnd,
      loadedStart,
      loadedEnd,
      loadedSpan,
      explorerPadding,
      explorerStart,
      explorerEnd,
      viewStart,
      viewEnd,
      viewSpan,
    };
  }

  useEffect(() => {
    if (!selectedProcessId) return;
    const owner = processRows.find((row) => row.process.id === selectedProcessId);
    if (!owner) return;
    setExpandedComponents((current) => new Set(current).add(owner.process.component));
  }, [processRows, selectedProcessId]);

  useEffect(() => {
    if (!selectedTraceId && !selectedThreadId) return;
    const owner = processRows.find((row) => row.traceRows.some(({ thread, trace }) => (
      (selectedTraceId && trace.id === selectedTraceId) ||
      (!selectedTraceId && selectedThreadId && thread.id === selectedThreadId)
    )));
    if (!owner) return;
    setExpandedComponents((current) => new Set(current).add(owner.process.component));
    setExpandedProcesses((current) => new Set(current).add(owner.process.id));
  }, [processRows, selectedThreadId, selectedTraceId]);

  useEffect(() => () => {
    if (brushFrameRef.current !== undefined) window.cancelAnimationFrame(brushFrameRef.current);
    if (timeCursorTimerRef.current !== undefined) window.clearTimeout(timeCursorTimerRef.current);
    if (timeCursorReleaseTimerRef.current !== undefined) window.clearTimeout(timeCursorReleaseTimerRef.current);
    onTimeCursorInteractionChange?.(false);
  }, [onTimeCursorInteractionChange]);

  useLayoutEffect(() => {
    if (!expanded) return;
    const scroll = timelineScrollRef.current;
    const shell = timelineShellRef.current;
    if (!scroll || !shell) return;
    const isAtLogTimeline = Boolean(filterScopeKey?.startsWith('atlog:'));
    let frame = 0;

    const updateViewportWidth = () => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => {
        // ATLog is nested inside an expandable case card. Measure the outer shell,
        // not the zoom canvas/scroller content, so the 100% view always follows the
        // actual case-detail width after expand, workbook-tab changes and resize.
        const shellWidth = shell.getBoundingClientRect().width;
        const nextWidth = Math.max(1, Math.floor(shellWidth || scroll.clientWidth));
        setTimelineViewportWidth((current) => current === nextWidth ? current : nextWidth);
        if (isAtLogTimeline && !labelUserResizedRef.current) {
          // Keep labels readable, but give the time track most of the case width.
          const responsiveLabelWidth = Math.max(190, Math.min(260, Math.round(nextWidth * 0.20)));
          setLabelWidth((current) => current === responsiveLabelWidth ? current : responsiveLabelWidth);
        }
      });
    };

    updateViewportWidth();
    const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(updateViewportWidth) : undefined;
    observer?.observe(shell);
    observer?.observe(scroll);
    window.addEventListener('resize', updateViewportWidth);
    return () => {
      window.cancelAnimationFrame(frame);
      observer?.disconnect();
      window.removeEventListener('resize', updateViewportWidth);
    };
  }, [expanded, filterScopeKey]);

  useEffect(() => {
    if (filterScopeKey?.startsWith('atlog:')) labelUserResizedRef.current = false;
    const restored = new Set(restoredHiddenComponents ?? []);
    setHiddenTimelineComponents(restored);
    onHiddenComponentsChange?.(restored);
    setViewportRange(undefined);
    setZoom(1);
    brushDragRef.current = undefined;
    brushDraftRef.current = undefined;
    timeCursorDragRef.current = undefined;
    timeCursorPendingRef.current = undefined;
    if (timeCursorTimerRef.current !== undefined) window.clearTimeout(timeCursorTimerRef.current);
    if (timeCursorReleaseTimerRef.current !== undefined) window.clearTimeout(timeCursorReleaseTimerRef.current);
    timeCursorTimerRef.current = undefined;
    timeCursorReleaseTimerRef.current = undefined;
    onTimeCursorInteractionChange?.(false);
    setBrushDraft(undefined);
    setTimeCursorNs(undefined);
  }, [filterScopeKey, onHiddenComponentsChange]);

  useEffect(() => {
    if (!restoredHiddenComponents) return;
    setHiddenTimelineComponents(new Set(restoredHiddenComponents));
  }, [restoredHiddenComponents]);

  useEffect(() => {
    // 远端日志任务的时间轴需要始终保留“已加载区”两侧的探索空间，
    // 不能再因为选中范围变化而把 viewport 自动缩到选区本身。
    if (loadedTimeRange) return;
    if (processRows.length === 0) {
      setViewportRange(undefined);
      return;
    }
    if (!selectedTimeRange) {
      setViewportRange(undefined);
      return;
    }
    const first = processRows.reduce((earliest, row) => row.range.startNs < earliest.range.startNs ? row : earliest, processRows[0]);
    const last = processRows.reduce((latest, row) => row.range.endNs > latest.range.endNs ? row : latest, processRows[0]);
    const startNs = selectedTimeRange.startNs > first.range.startNs ? selectedTimeRange.startNs : first.range.startNs;
    const endNs = selectedTimeRange.endNs < last.range.endNs ? selectedTimeRange.endNs : last.range.endNs;
    setViewportRange(endNs > startNs ? { startNs, endNs } : undefined);
    setZoom(1);
  }, [loadedTimeRange, processRows, selectedTimeRange?.endNs, selectedTimeRange?.startNs]);

  useEffect(() => {
    if (!selectedTimeRange || selectedTimeRange.endNs <= selectedTimeRange.startNs) {
      setTimeCursorNs(undefined);
      return;
    }
    setTimeCursorNs((current) => {
      if (current !== undefined && current >= selectedTimeRange.startNs && current <= selectedTimeRange.endNs) return current;
      return selectedTimeRange.startNs + (selectedTimeRange.endNs - selectedTimeRange.startNs) / 2n;
    });
  }, [selectedTimeRange?.endNs, selectedTimeRange?.startNs]);

  useEffect(() => {
    if (!expanded || !selectedTimeRange || selectedTimeRange.endNs <= selectedTimeRange.startNs || processRows.length === 0) return;

    // 本地日志会把 viewport 收敛到当前筛选范围；等该范围真正生效后再计算默认倍率，
    // 避免先按全量日志跨度放大、随后又切到局部 viewport 导致倍率过高。
    if (!loadedTimeRange) {
      const first = processRows.reduce((earliest, row) => row.range.startNs < earliest.range.startNs ? row : earliest, processRows[0]);
      const last = processRows.reduce((latest, row) => row.range.endNs > latest.range.endNs ? row : latest, processRows[0]);
      const expectedStart = selectedTimeRange.startNs > first.range.startNs ? selectedTimeRange.startNs : first.range.startNs;
      const expectedEnd = selectedTimeRange.endNs < last.range.endNs ? selectedTimeRange.endNs : last.range.endNs;
      if (expectedEnd > expectedStart && (
        !viewportRange || viewportRange.startNs !== expectedStart || viewportRange.endNs !== expectedEnd
      )) return;
    }

    const adaptiveKey = `${filterScopeKey ?? 'timeline'}:${selectedTimeRange.startNs.toString()}:${selectedTimeRange.endNs.toString()}`;
    if (adaptiveZoomAppliedKeyRef.current === adaptiveKey) return;

    const bounds = resolveTimelineBounds();
    if (!bounds) return;
    const selectionSpan = selectedTimeRange.endNs - selectedTimeRange.startNs;
    const maxTimelineZoom = maxTimelineZoomForSpan(bounds.viewSpan, timelineViewportWidth);
    const selectionFocusSpan = selectionSpan < TIMELINE_MIN_AUTO_RANGE_SPAN_NS
      ? TIMELINE_MIN_AUTO_RANGE_SPAN_NS
      : selectionSpan;
    const autoTargetRatio = timelineAutoTargetRatio(timelineViewportWidth, labelWidth);
    const targetZoom = adaptiveTimelineZoom(
      bounds.viewSpan,
      selectionFocusSpan,
      autoTargetRatio,
      maxTimelineZoom,
    );
    // 自动倍率始终以“本次检索范围”作为核心窗口。这样上方 Brush 与下方时间线
    // 使用完全相同的可视区，两个拖拽手柄默认都留在屏幕内；ERROR 再密集也不会
    // 为了追一个更窄异常簇而二次过度放大。用户之后仍可手动继续无限缩放。
    const focusNs = selectedTimeRange.startNs + selectionSpan / 2n;

    adaptiveZoomAppliedKeyRef.current = adaptiveKey;
    pendingAdaptiveFocusNsRef.current = focusNs;
    setZoom(targetZoom);
  }, [
    expanded,
    filterScopeKey,
    labelWidth,
    loadedTimeRange?.endNs,
    loadedTimeRange?.startNs,
    processRows,
    selectedTimeRange?.endNs,
    selectedTimeRange?.startNs,
    timelineViewportWidth,
    viewportRange?.endNs,
    viewportRange?.startNs,
  ]);

  useLayoutEffect(() => {
    const anchor = pendingZoomAnchorRef.current;
    if (!anchor) return;
    pendingZoomAnchorRef.current = undefined;
    const scroll = timelineScrollRef.current;
    if (!scroll) return;

    if (anchor.cursorScreenX !== undefined) {
      const cursor = timeCursorElementRef.current;
      if (!cursor) return;
      const cursorBounds = cursor.getBoundingClientRect();
      const nextCursorScreenX = cursorBounds.left + cursorBounds.width / 2;
      scroll.scrollLeft += nextCursorScreenX - anchor.cursorScreenX;
      return;
    }

    if (anchor.fallbackContentRatio !== undefined && anchor.fallbackPointerX !== undefined) {
      scroll.scrollLeft = Math.max(0, anchor.fallbackContentRatio * scroll.scrollWidth - anchor.fallbackPointerX);
    }
  }, [zoom, viewportRange?.endNs, viewportRange?.startNs]);

  useLayoutEffect(() => {
    const focusNs = pendingAdaptiveFocusNsRef.current;
    if (focusNs === undefined) return;
    const bounds = resolveTimelineBounds();
    const scroll = timelineScrollRef.current;
    const brush = brushRef.current;
    if (!bounds || !scroll || !brush || bounds.viewSpan <= 0n) return;

    pendingAdaptiveFocusNsRef.current = undefined;
    const clampedFocusNs = focusNs < bounds.viewStart
      ? bounds.viewStart
      : focusNs > bounds.viewEnd
        ? bounds.viewEnd
        : focusNs;
    const focusRatio = Math.max(0, Math.min(1, rangePercent(clampedFocusNs, bounds.viewStart, bounds.viewSpan) / 100));
    const brushBounds = brush.getBoundingClientRect();
    const scrollBounds = scroll.getBoundingClientRect();
    if (brushBounds.width <= 0 || scrollBounds.width <= 0) return;

    const focusScreenX = brushBounds.left + brushBounds.width * focusRatio;
    const frozenColumnWidth = Math.min(scrollBounds.width * 0.55, labelWidth + 64);
    const desiredScreenX = scrollBounds.left + frozenColumnWidth + (scrollBounds.width - frozenColumnWidth) / 2;
    scroll.scrollLeft = Math.max(0, scroll.scrollLeft + focusScreenX - desiredScreenX);
  }, [
    labelWidth,
    selectedTimeRange?.endNs,
    selectedTimeRange?.startNs,
    viewportRange?.endNs,
    viewportRange?.startNs,
    zoom,
  ]);

  function beginLabelResize(event: React.PointerEvent<HTMLDivElement>) {
    labelUserResizedRef.current = true;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    labelResizeRef.current = { pointerId: event.pointerId, startX: event.clientX, startWidth: labelWidth };
  }

  function moveLabelResize(event: React.PointerEvent<HTMLDivElement>) {
    const state = labelResizeRef.current;
    if (!state || state.pointerId !== event.pointerId) return;
    setLabelWidth(Math.max(240, Math.min(560, state.startWidth + event.clientX - state.startX)));
  }

  function endLabelResize(event: React.PointerEvent<HTMLDivElement>) {
    const state = labelResizeRef.current;
    if (!state || state.pointerId !== event.pointerId) return;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    labelResizeRef.current = undefined;
  }

  const timelineBounds = resolveTimelineBounds();
  if (!timelineBounds) return null;
  const {
    globalStartRow,
    globalEndRow,
    loadedStart,
    loadedEnd,
    viewStart,
    viewEnd,
    viewSpan,
  } = timelineBounds;
  const sameDate = formatNsTick(viewStart, true, false).slice(0, 10) === formatNsTick(viewEnd, true, false).slice(0, 10);
  // 时间轴不再使用固定 1000% 上限。理论上放大到 1px≈1ns 后就没有继续放大的意义；
  // 同时限制单个超宽 DOM 的安全宽度，避免浏览器达到布局尺寸极限后 scrollWidth 失真。
  const timelineBaseCanvasWidth = Math.max(1, timelineViewportWidth);
  const maxTimelineZoom = maxTimelineZoomForSpan(viewSpan, timelineViewportWidth);
  const timelineZoom = Math.min(zoom, maxTimelineZoom);
  const canvasWidth = Math.round(timelineBaseCanvasWidth * timelineZoom);
  const visibleTimelineSpanNs = BigInt(Math.max(1, Math.round(Number(viewSpan) / Math.max(1, timelineZoom))));
  const includeMilliseconds = visibleTimelineSpanNs < 60_000_000_000n;
  // 刻度密度跟随实际横向画布宽度。深度放大后不能仍固定只有 40 个刻度，
  // 否则同一秒内的异常虽然被拉开，时间标尺却没有足够的局部参考点。
  const tickCount = Math.min(600, Math.max(4, Math.round(canvasWidth / 220)));
  const ticks = Array.from({ length: tickCount + 1 }, (_, index) => (
    viewStart + (viewSpan * BigInt(index)) / BigInt(tickCount)
  ));

  const selectedStartPercent = selectedTimeRange
    ? Math.max(0, Math.min(100, rangePercent(selectedTimeRange.startNs, viewStart, viewSpan)))
    : 0;
  const selectedEndPercent = selectedTimeRange
    ? Math.max(0, Math.min(100, rangePercent(selectedTimeRange.endNs, viewStart, viewSpan)))
    : 0;
  const selectedWidthPercent = Math.max(0, selectedEndPercent - selectedStartPercent);
  const timeCursorPercent = timeCursorNs !== undefined
    ? Math.max(selectedStartPercent, Math.min(selectedEndPercent, rangePercent(timeCursorNs, viewStart, viewSpan)))
    : selectedStartPercent + selectedWidthPercent / 2;
  const draftStartPercent = brushDraft ? Math.min(brushDraft.start, brushDraft.end) : 0;
  const draftEndPercent = brushDraft ? Math.max(brushDraft.start, brushDraft.end) : 0;
  const loadedStartPercent = Math.max(0, Math.min(100, rangePercent(loadedStart, viewStart, viewSpan)));
  const loadedEndPercent = Math.max(0, Math.min(100, rangePercent(loadedEnd, viewStart, viewSpan)));
  const loadedWidthPercent = Math.max(0, loadedEndPercent - loadedStartPercent);
  const overlapsSelection = (startNs: bigint, endNs: bigint) => !selectedTimeRange || (
    endNs >= selectedTimeRange.startNs && startNs <= selectedTimeRange.endNs
  );
  const displayGroups = componentGroups.flatMap((group) => {
    const rows = group.processRows.flatMap((row) => {
      if (!overlapsSelection(row.range.startNs, row.range.endNs)) return [];
      return [{
        ...row,
        traceRows: row.traceRows.filter((traceRow) => overlapsSelection(traceRow.range.startNs, traceRow.range.endNs)),
      }];
    });
    if (rows.length === 0) return [];
    const startRow = rows.reduce((earliest, row) => row.range.startNs < earliest.range.startNs ? row : earliest, rows[0]);
    const endRow = rows.reduce((latest, row) => row.range.endNs > latest.range.endNs ? row : latest, rows[0]);
    return [{
      ...group,
      processRows: rows,
      range: {
        start: startRow.range.start,
        end: endRow.range.end,
        startNs: startRow.range.startNs,
        endNs: endRow.range.endNs,
        duration: endRow.range.endNs - startRow.range.startNs,
      },
      severity: maxSeverity(...rows.map((row) => row.severity)),
      errorCount: rows.reduce((sum, row) => sum + row.errorCount, 0),
      entryCount: rows.reduce((sum, row) => sum + row.entries.length, 0),
      traceCount: rows.reduce((sum, row) => sum + row.traceRows.length, 0),
    }];
  });

  function toggleTimelineComponent(component: string) {
    setHiddenTimelineComponents((current) => {
      const next = new Set(current);
      if (next.has(component)) next.delete(component);
      else next.add(component);
      onHiddenComponentsChange?.(next);
      return next;
    });
  }

  function showAllTimelineComponents() {
    const next = new Set<string>();
    setHiddenTimelineComponents(next);
    onHiddenComponentsChange?.(next);
  }

  function hideAllTimelineComponents() {
    const next = new Set(componentGroups.map((group) => group.component));
    setHiddenTimelineComponents(next);
    onHiddenComponentsChange?.(next);
  }

  function toggleComponent(component: string) {
    setExpandedComponents((current) => {
      const next = new Set(current);
      if (next.has(component)) next.delete(component);
      else next.add(component);
      return next;
    });
  }

  function toggleProcessRow(processId: string) {
    setExpandedProcesses((current) => {
      const next = new Set(current);
      if (next.has(processId)) next.delete(processId);
      else next.add(processId);
      return next;
    });
  }

  function pointerPercent(event: React.PointerEvent<HTMLElement>): number {
    const bounds = brushRef.current?.getBoundingClientRect();
    if (!bounds || bounds.width <= 0) return 0;
    return Math.max(0, Math.min(100, ((event.clientX - bounds.left) / bounds.width) * 100));
  }

  function percentToNs(percent: number): bigint {
    const scaled = BigInt(Math.round(Math.max(0, Math.min(100, percent)) * 10_000));
    return viewStart + (viewSpan * scaled) / 1_000_000n;
  }

  function pointerNsInElement(event: React.MouseEvent<HTMLElement>): bigint | undefined {
    const bounds = event.currentTarget.getBoundingClientRect();
    if (bounds.width <= 0) return undefined;
    const percent = Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width));
    const scaled = BigInt(Math.round(percent * 1_000_000));
    return viewStart + (viewSpan * scaled) / 1_000_000n;
  }

  function pointerNsInTrack(event: React.MouseEvent<HTMLElement>): bigint | undefined {
    const ns = pointerNsInElement(event);
    if (ns === undefined) return undefined;
    if (selectedTimeRange && (ns < selectedTimeRange.startNs || ns > selectedTimeRange.endNs)) return undefined;
    return ns;
  }

  function updateTimelineHover(event: React.MouseEvent<HTMLElement>) {
    if (timelineLabelHover || timelineAnomalyHover) return;
    const ns = pointerNsInElement(event);
    if (ns === undefined) {
      setTimelineHover(undefined);
      return;
    }
    setTimelineHover({ x: event.clientX + 12, y: event.clientY + 14, ns });
  }

  function updateTimelineLabelHover(event: React.MouseEvent<HTMLElement>, detail: TimelineLabelHoverDetail) {
    event.stopPropagation();
    const tooltipWidth = 280;
    const tooltipHeight = 82;
    const x = Math.max(8, Math.min(window.innerWidth - tooltipWidth - 8, event.clientX + 12));
    const preferredY = event.clientY + 14;
    const y = preferredY + tooltipHeight <= window.innerHeight - 8
      ? preferredY
      : Math.max(8, event.clientY - tooltipHeight - 12);
    setTimelineHover(undefined);
    setTimelineAnomalyHover(undefined);
    setTimelineLabelHover({ ...detail, x, y });
  }

  function clearTimelineLabelHover() {
    setTimelineLabelHover(undefined);
  }

  function updateTimelineAnomalyHover(event: React.MouseEvent<HTMLElement>, detail: TimelineAnomalyHoverDetail) {
    event.stopPropagation();
    const tooltipWidth = 420;
    const tooltipHeight = 132;
    const x = Math.max(8, Math.min(window.innerWidth - tooltipWidth - 8, event.clientX + 12));
    const preferredY = event.clientY + 14;
    const y = preferredY + tooltipHeight <= window.innerHeight - 8
      ? preferredY
      : Math.max(8, event.clientY - tooltipHeight - 12);
    setTimelineHover(undefined);
    setTimelineLabelHover(undefined);
    setTimelineAnomalyHover({ ...detail, x, y });
  }

  function clearTimelineAnomalyHover() {
    setTimelineAnomalyHover(undefined);
  }

  function navigateTimelineEntry(event: React.MouseEvent<HTMLElement>, entry: LogEntry) {
    event.preventDefault();
    event.stopPropagation();
    setTimelineHover(undefined);
    setTimelineLabelHover(undefined);
    setTimelineAnomalyHover(undefined);
    if (entry.timestampNs !== undefined) {
      setTimeCursorNs(clampTimeCursor(entry.timestampNs));
    }
    onNavigateEntry(entry);
  }

  function navigateComponentAtPointer(event: React.MouseEvent<HTMLElement>, component: string) {
    const ns = pointerNsInTrack(event);
    if (ns === undefined) return;
    event.preventDefault();
    event.stopPropagation();
    setTimeCursorNs(clampTimeCursor(ns));
    holdTimeCursorScrollLock();
    onNavigateComponentTime(component, ns);
    releaseTimeCursorScrollLock();
  }

  function captureTimelineZoomAnchor(fallbackClientX?: number) {
    const scroll = timelineScrollRef.current;
    if (!scroll) return;
    const bounds = scroll.getBoundingClientRect();
    const cursor = timeCursorElementRef.current;
    if (cursor) {
      const cursorBounds = cursor.getBoundingClientRect();
      const cursorScreenX = cursorBounds.left + cursorBounds.width / 2;
      // 当前时间标尺可见时，它就是唯一缩放锚点：缩放前后保持屏幕 X 坐标完全不变。
      if (cursorScreenX >= bounds.left && cursorScreenX <= bounds.right) {
        pendingZoomAnchorRef.current = { cursorScreenX };
        return;
      }
    }

    // 没有可见时间标尺时才退回鼠标位置/视口中心，保证无选区场景仍可自然缩放。
    const fallbackPointerX = fallbackClientX === undefined
      ? bounds.width / 2
      : Math.max(0, Math.min(bounds.width, fallbackClientX - bounds.left));
    const contentX = scroll.scrollLeft + fallbackPointerX;
    pendingZoomAnchorRef.current = {
      fallbackContentRatio: scroll.scrollWidth > 0 ? contentX / scroll.scrollWidth : 0,
      fallbackPointerX,
    };
  }

  function changeTimelineZoom(nextZoom: number, fallbackClientX?: number) {
    const boundedZoom = Math.max(1, Math.min(maxTimelineZoom, nextZoom));
    const normalizedZoom = boundedZoom >= 100
      ? Math.round(boundedZoom)
      : Number(boundedZoom.toFixed(1));
    if (Math.abs(normalizedZoom - timelineZoom) < 0.0001) return;
    captureTimelineZoomAnchor(fallbackClientX);
    setZoom(normalizedZoom);
  }

  function nextTimelineZoom(direction: 1 | -1): number {
    if (direction > 0) {
      if (timelineZoom >= maxTimelineZoom) return maxTimelineZoom;
      if (timelineZoom < 5) return Math.min(maxTimelineZoom, timelineZoom + 0.5);
      // 高倍率后按比例放大，避免从 1000% 继续放大时需要滚动数百次。
      return Math.min(maxTimelineZoom, timelineZoom * 1.25);
    }
    if (timelineZoom <= 1) return 1;
    if (timelineZoom <= 5) return Math.max(1, timelineZoom - 0.5);
    return Math.max(1, timelineZoom / 1.25);
  }

  function handleTimelineWheel(event: React.WheelEvent<HTMLDivElement>) {
    event.preventDefault();
    const direction: 1 | -1 = event.deltaY < 0 ? 1 : -1;
    changeTimelineZoom(nextTimelineZoom(direction), event.clientX);
  }

  function publishBrushDraft() {
    brushFrameRef.current = undefined;
    const current = brushDraftRef.current;
    if (current) setBrushDraft({ ...current });
  }

  function clampTimeCursor(ns: bigint): bigint {
    if (!selectedTimeRange) return ns;
    if (ns < selectedTimeRange.startNs) return selectedTimeRange.startNs;
    if (ns > selectedTimeRange.endNs) return selectedTimeRange.endNs;
    return ns;
  }

  function navigateTimeCursor(ns: bigint, immediate = false) {
    timeCursorPendingRef.current = ns;
    if (immediate) {
      if (timeCursorTimerRef.current !== undefined) window.clearTimeout(timeCursorTimerRef.current);
      timeCursorTimerRef.current = undefined;
      timeCursorPendingRef.current = undefined;
      onNavigateTime(ns);
      return;
    }
    if (timeCursorTimerRef.current !== undefined) return;
    timeCursorTimerRef.current = window.setTimeout(() => {
      timeCursorTimerRef.current = undefined;
      const pending = timeCursorPendingRef.current;
      timeCursorPendingRef.current = undefined;
      if (pending !== undefined) onNavigateTime(pending);
    }, 90);
  }

  function updateTimeCursor(event: React.PointerEvent<HTMLElement>, immediate = false) {
    if (!selectedTimeRange) return;
    const ns = clampTimeCursor(percentToNs(pointerPercent(event)));
    setTimeCursorNs(ns);
    navigateTimeCursor(ns, immediate);
  }

  function holdTimeCursorScrollLock() {
    if (timeCursorReleaseTimerRef.current !== undefined) {
      window.clearTimeout(timeCursorReleaseTimerRef.current);
      timeCursorReleaseTimerRef.current = undefined;
    }
    onTimeCursorInteractionChange?.(true);
  }

  function releaseTimeCursorScrollLock(delayMs = 220) {
    if (timeCursorReleaseTimerRef.current !== undefined) window.clearTimeout(timeCursorReleaseTimerRef.current);
    timeCursorReleaseTimerRef.current = window.setTimeout(() => {
      timeCursorReleaseTimerRef.current = undefined;
      onTimeCursorInteractionChange?.(false);
    }, delayMs);
  }

  function beginTimeCursor(event: React.PointerEvent<HTMLButtonElement>) {
    if (event.button !== 0 || !selectedTimeRange || incrementalLoading) return;
    holdTimeCursorScrollLock();
    event.preventDefault();
    event.stopPropagation();
    event.currentTarget.setPointerCapture(event.pointerId);
    timeCursorDragRef.current = { pointerId: event.pointerId };
    updateTimeCursor(event, true);
  }

  function moveTimeCursor(event: React.PointerEvent<HTMLButtonElement>) {
    if (timeCursorDragRef.current?.pointerId !== event.pointerId || !event.currentTarget.hasPointerCapture(event.pointerId)) return;
    event.preventDefault();
    event.stopPropagation();
    updateTimeCursor(event);
  }

  function endTimeCursor(event: React.PointerEvent<HTMLButtonElement>) {
    if (timeCursorDragRef.current?.pointerId !== event.pointerId) return;
    event.preventDefault();
    event.stopPropagation();
    updateTimeCursor(event, true);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    timeCursorDragRef.current = undefined;
    releaseTimeCursorScrollLock();
  }

  function beginBrush(event: React.PointerEvent<HTMLDivElement>) {
    if (event.button !== 0 || incrementalLoading || timeSelectionDisabled) return;
    const percent = pointerPercent(event);
    event.preventDefault();
    brushRef.current?.setPointerCapture(event.pointerId);
    brushDragRef.current = {
      pointerId: event.pointerId,
      mode: 'new',
      anchor: percent,
      initialStart: percent,
      initialEnd: percent,
    };
    const draft = { start: percent, end: percent };
    brushDraftRef.current = draft;
    setBrushDraft(draft);
  }

  function beginSelectionDrag(event: React.PointerEvent<HTMLElement>, mode: 'left' | 'right' | 'move') {
    if (event.button !== 0 || incrementalLoading || timeSelectionDisabled || !selectedTimeRange || selectedWidthPercent <= 0) return;
    event.preventDefault();
    event.stopPropagation();
    const anchor = pointerPercent(event);
    brushRef.current?.setPointerCapture(event.pointerId);
    brushDragRef.current = {
      pointerId: event.pointerId,
      mode,
      anchor,
      initialStart: selectedStartPercent,
      initialEnd: selectedEndPercent,
    };
    const draft = { start: selectedStartPercent, end: selectedEndPercent };
    brushDraftRef.current = draft;
    setBrushDraft(draft);
  }

  function moveBrush(event: React.PointerEvent<HTMLDivElement>) {
    const drag = brushDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId || !brushRef.current?.hasPointerCapture(event.pointerId)) return;
    const percent = pointerPercent(event);
    const minWidth = 0.35;
    let nextStart = drag.initialStart;
    let nextEnd = drag.initialEnd;
    if (drag.mode === 'new') {
      nextStart = Math.min(drag.anchor, percent);
      nextEnd = Math.max(drag.anchor, percent);
    } else if (drag.mode === 'left') {
      nextStart = Math.min(percent, drag.initialEnd - minWidth);
      nextEnd = drag.initialEnd;
    } else if (drag.mode === 'right') {
      nextStart = drag.initialStart;
      nextEnd = Math.max(percent, drag.initialStart + minWidth);
    } else {
      const width = drag.initialEnd - drag.initialStart;
      let shiftedStart = drag.initialStart + (percent - drag.anchor);
      shiftedStart = Math.max(0, Math.min(100 - width, shiftedStart));
      nextStart = shiftedStart;
      nextEnd = shiftedStart + width;
    }
    brushDraftRef.current = {
      start: Math.max(0, Math.min(100, nextStart)),
      end: Math.max(0, Math.min(100, nextEnd)),
    };
    if (brushFrameRef.current === undefined) {
      brushFrameRef.current = window.requestAnimationFrame(publishBrushDraft);
    }
  }

  function endBrush(event: React.PointerEvent<HTMLDivElement>) {
    const current = brushDraftRef.current;
    if (!current) return;
    if (brushRef.current?.hasPointerCapture(event.pointerId)) {
      brushRef.current.releasePointerCapture(event.pointerId);
    }
    if (brushFrameRef.current !== undefined) {
      window.cancelAnimationFrame(brushFrameRef.current);
      brushFrameRef.current = undefined;
    }
    const start = Math.min(current.start, current.end);
    const end = Math.max(current.start, current.end);
    brushDragRef.current = undefined;
    brushDraftRef.current = undefined;
    setBrushDraft(undefined);
    if (end - start < 0.35) return;
    onSelectTimeRange({ startNs: percentToNs(start), endNs: percentToNs(end) });
  }

  function selectionWindow() {
    if (!selectedTimeRange || selectedWidthPercent <= 0) return null;
    return (
      <span
        className="process-gantt-selection-window"
        style={{ '--selection-left': `${selectedStartPercent}%`, '--selection-width': `${selectedWidthPercent}%` } as React.CSSProperties}
      />
    );
  }

  function processBar(row: typeof processRows[number], index: number) {
    const startPercent = rangePercent(row.range.startNs, viewStart, viewSpan);
    const endPercent = rangePercent(row.range.endNs, viewStart, viewSpan);
    const widthPercent = Math.max(endPercent - startPercent, 0.45);
    return (
      <span
        className={classNames(
          'process-gantt-component-segment',
          selectedProcessId === row.process.id && 'active',
        )}
        key={row.process.id}
        style={{
          '--bar-left': `${startPercent}%`,
          '--bar-width': `${widthPercent}%`,
          '--segment-order': index,
          ...timelineComponentStyle(row.process.component),
        } as React.CSSProperties}
      >
        <TimelineAnomalyMarkers entries={row.entries} startNs={row.range.startNs} endNs={row.range.endNs} onLabelHover={updateTimelineLabelHover} onLabelLeave={clearTimelineLabelHover} onAnomalyHover={updateTimelineAnomalyHover} onAnomalyLeave={clearTimelineAnomalyHover} onEntryClick={navigateTimelineEntry} />
        <span className="sr-only">PID {row.process.processId}</span>
      </span>
    );
  }

  const visibleProcessCount = displayGroups.reduce((sum, group) => (
    hiddenTimelineComponents.has(group.component) ? sum : sum + group.processRows.length
  ), 0);
  const visibleTimelineComponentCount = componentGroups.filter((group) => !hiddenTimelineComponents.has(group.component)).length;
  const timelineRangeLabel = loadedTimeRange
    ? `已加载 ${formatNsTick(loadedStart, !sameDate, false)}–${formatNsTick(loadedEnd, !sameDate, false)}`
    : globalStartRow && globalEndRow
      ? `${compactTimestamp(globalStartRow.range.start.timestamp, !sameDate)}–${compactTimestamp(globalEndRow.range.end.timestamp, !sameDate)}`
      : '当前范围无日志';

  return (
    <section className={classNames('process-gantt', !expanded && 'collapsed', navigationPending && 'navigation-pending')}>
      {/* 标题行同时是悬浮窗的拖动手柄和「固定/关闭」的落点。
          之前在它上面还有一条独立的「时间线」标题栏，只为了放两个按钮和一句说明，
          既占高度又和这一行重复，已经去掉。 */}
      <div className="process-gantt-header" {...(dragHandleProps || {})}>
        <button type="button" className="process-gantt-header-toggle" onClick={onToggle} aria-expanded={expanded}>
          <span className="process-gantt-title"><Clock size={15} /><strong>模块 / 进程 / Trace 时间分布</strong><small>{visibleTimelineComponentCount}/{componentGroups.length} 个模块 · {visibleProcessCount} 个进程 · {timelineRangeLabel}</small></span>
          {expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
        </button>
        {headerActions}
      </div>
      {expanded && (
        <div className="process-gantt-content">
          <div className="process-gantt-tools">
            {incrementalMessage && <span className={classNames('process-gantt-incremental-status', incrementalLoading && 'loading')}>{incrementalLoading && <LoaderCircle className="spin" size={11} />}{incrementalMessage}</span>}
            {selectedTimeRange && (
              <span className="process-gantt-selected-range">
                {formatNsTick(selectedTimeRange.startNs, !sameDate, true)}–{formatNsTick(selectedTimeRange.endNs, !sameDate, true)}
                <button type="button" onClick={onClearTimeRange} aria-label="清除时间范围"><X size={12} /></button>
              </span>
            )}
            <div className="process-gantt-zoom" aria-label="时间轴缩放">
              <button type="button" disabled={timelineZoom <= 1} onClick={() => changeTimelineZoom(nextTimelineZoom(-1))} title="缩小时间轴（保持当前时间标尺位置不变）"><ZoomOut size={13} /></button>
              <span>{Math.round(timelineZoom * 100).toLocaleString()}%</span>
              <button type="button" disabled={timelineZoom >= maxTimelineZoom} onClick={() => changeTimelineZoom(nextTimelineZoom(1))} title="放大时间轴（保持当前时间标尺位置不变，可持续放大至时间精度/绘制极限）"><ZoomIn size={13} /></button>
              <button type="button" className="fit-button" disabled={timelineZoom === 1} onClick={() => changeTimelineZoom(1)}>适配</button>
              <button type="button" className="fit-button" disabled={!viewportRange} onClick={() => { captureTimelineZoomAnchor(); setViewportRange(undefined); setZoom(1); }} title="恢复查看任务完整时间轴，保留当前时间标尺位置与日志筛选">还原全局</button>
            </div>
          </div>

          <div className="process-gantt-scroll-shell" ref={timelineShellRef} style={{ '--gantt-label-width': `${labelWidth}px` } as React.CSSProperties}>
            <div className="process-gantt-scroll" ref={timelineScrollRef} onWheel={handleTimelineWheel}>
              <div
                className={classNames('process-gantt-canvas', loadedTimeRange && 'remote-time-explorer')}
                style={{
                  width: `${canvasWidth}px`,
                  minWidth: `${canvasWidth}px`,
                  '--gantt-label-width': `${labelWidth}px`,
                  '--loaded-left': `${loadedStartPercent}%`,
                  '--loaded-width': `${loadedWidthPercent}%`,
                } as React.CSSProperties}
              >
              <div className="process-gantt-axis" onMouseMove={updateTimelineHover} onMouseLeave={() => setTimelineHover(undefined)}>
                {ticks.map((tick, index) => (
                  <span key={index} style={{ left: `${(index / tickCount) * 100}%` }}>{formatNsTick(tick, !sameDate, includeMilliseconds)}</span>
                ))}
              </div>

              <div className="process-gantt-brush-row">
                <label className="process-gantt-check-cell process-gantt-check-header" title={visibleTimelineComponentCount === componentGroups.length ? '隐藏全部模块时间线' : '显示全部模块时间线'}>
                  <input
                    type="checkbox"
                    aria-label="显示全部模块时间线"
                    checked={componentGroups.length > 0 && visibleTimelineComponentCount === componentGroups.length}
                    onChange={() => visibleTimelineComponentCount === componentGroups.length ? hideAllTimelineComponents() : showAllTimelineComponents()}
                  />
                </label>
                <span className="process-gantt-brush-label">模块</span>
                <div
                  ref={brushRef}
                  className={classNames('process-gantt-brush', brushDraft && 'dragging', incrementalLoading && 'loading', timeSelectionDisabled && 'disabled')}
                  aria-busy={incrementalLoading || undefined}
                  aria-disabled={timeSelectionDisabled || undefined}
                  onMouseMove={updateTimelineHover}
                  onMouseLeave={() => setTimelineHover(undefined)}
                  onPointerDown={beginBrush}
                  onPointerMove={moveBrush}
                  onPointerUp={endBrush}
                  onPointerCancel={() => {
                    brushDragRef.current = undefined;
                    brushDraftRef.current = undefined;
                    setBrushDraft(undefined);
                  }}
                >
                  {loadedTimeRange && loadedStartPercent >= 7 && <span className="process-gantt-unloaded-label left">未加载</span>}
                  {loadedTimeRange && (100 - loadedEndPercent) >= 7 && <span className="process-gantt-unloaded-label right">未加载</span>}
                  {selectedTimeRange && selectedWidthPercent > 0 && (
                    <span
                      className="process-gantt-brush-selection active"
                      style={{ left: `${selectedStartPercent}%`, width: `${selectedWidthPercent}%` }}
                      onPointerDown={(event) => beginSelectionDrag(event, 'move')}
                      title="拖动中间滑动时间窗口"
                    >
                      <button type="button" className="process-gantt-brush-handle left" aria-label="调整开始时间" onPointerDown={(event) => beginSelectionDrag(event, 'left')} />
                      <button type="button" className="process-gantt-brush-handle right" aria-label="调整结束时间" onPointerDown={(event) => beginSelectionDrag(event, 'right')} />
                    </span>
                  )}
                  {selectedTimeRange && selectedWidthPercent > 0 && timeCursorNs !== undefined && !brushDraft && (
                    <button
                      ref={timeCursorElementRef}
                      type="button"
                      className="process-gantt-time-cursor"
                      style={{ left: `${timeCursorPercent}%` }}
                      aria-label={`时间标尺 ${formatNsTick(timeCursorNs, !sameDate, true)}`}
                      title={`时间标尺 ${formatNsTick(timeCursorNs, !sameDate, true)} · 左右拖动自动定位下方日志`}
                      onPointerDown={beginTimeCursor}
                      onPointerMove={moveTimeCursor}
                      onPointerUp={endTimeCursor}
                      onPointerCancel={(event) => {
                        if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
                        timeCursorDragRef.current = undefined;
                        releaseTimeCursorScrollLock(120);
                      }}
                    >
                      <span>{formatNsTick(timeCursorNs, !sameDate, true)}</span>
                    </button>
                  )}
                  {brushDraft && (
                    <span className="process-gantt-brush-selection draft" style={{ left: `${draftStartPercent}%`, width: `${Math.max(draftEndPercent - draftStartPercent, 0.2)}%` }} />
                  )}
                </div>
                <span className="process-gantt-brush-range">{selectedTimeRange ? compactRangeDuration(selectedTimeRange.endNs - selectedTimeRange.startNs) : '拖拽选择'}</span>
              </div>

              <div className="process-gantt-rows">
                {displayGroups.length === 0 && (
                  <div className="process-gantt-empty-filter">当前时间范围没有可展示的模块时间线。</div>
                )}
                {displayGroups.map((group) => {
                  const componentVisible = !hiddenTimelineComponents.has(group.component);
                  const componentExpanded = componentVisible && expandedComponents.has(group.component);
                  return (
                    <div className={classNames('process-gantt-component-group', componentExpanded && 'expanded', !componentVisible && 'timeline-hidden')} key={group.component}>
                      <div className={classNames('process-gantt-row component-row', `severity-${group.severity}`, !componentVisible && 'timeline-hidden')}>
                        <label className="process-gantt-check-cell process-gantt-component-check" title={`${hiddenTimelineComponents.has(group.component) ? '显示' : '隐藏'} ${group.component} 时间线`}>
                          <input
                            type="checkbox"
                            aria-label={`${hiddenTimelineComponents.has(group.component) ? '显示' : '隐藏'} ${group.component} 时间线`}
                            checked={!hiddenTimelineComponents.has(group.component)}
                            onChange={() => toggleTimelineComponent(group.component)}
                          />
                        </label>
                        <span className="process-gantt-label component-label">
                          <button type="button" className="process-gantt-expand" onClick={() => toggleComponent(group.component)} aria-label={componentExpanded ? '折叠组件进程' : '展开组件进程'} aria-expanded={componentExpanded}>
                            {componentExpanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                          </button>
                          <button type="button" className="process-gantt-label-button component-only" onClick={() => toggleComponent(group.component)}>
                            <ComponentBadge component={group.component} compact />
                          </button>
                        </span>
                        <div className="process-gantt-track component-track timeline-click-target" onMouseMove={updateTimelineHover} onMouseLeave={() => setTimelineHover(undefined)} onClick={(event) => navigateComponentAtPointer(event, group.component)}>
                          {componentVisible && selectionWindow()}
                          {componentVisible ? group.processRows.map(processBar) : <span className="process-gantt-hidden-track-note">已隐藏</span>}
                        </div>
                        <span className="process-gantt-range">{secondTimestamp(group.range.start.timestamp)}–{secondTimestamp(group.range.end.timestamp)}</span>
                      </div>

                      {componentVisible && componentExpanded && group.processRows.map((row) => {
                        const { process, range, traceRows, entries, severity, errorCount } = row;
                        const startPercent = rangePercent(range.startNs, viewStart, viewSpan);
                        const endPercent = rangePercent(range.endNs, viewStart, viewSpan);
                        const widthPercent = Math.max(endPercent - startPercent, 0.8);
                        const processExpanded = expandedProcesses.has(process.id);
                        return (
                          <div className={classNames('process-gantt-group process-level-group', processExpanded && 'expanded')} key={process.id}>
                            <div className={classNames('process-gantt-row process-row', `severity-${severity}`, selectedProcessId === process.id && 'active')}>
                              <span className="process-gantt-check-cell process-gantt-check-placeholder" aria-hidden="true" />
                              <span className="process-gantt-label process-label">
                                <button type="button" className="process-gantt-expand" onClick={() => toggleProcessRow(process.id)} aria-label={processExpanded ? '折叠 Trace 时间段' : '展开 Trace 时间段'} aria-expanded={processExpanded}>
                                  {processExpanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                                </button>
                                <button type="button" className="process-gantt-label-button process-label-copy" onClick={() => onSelectProcess(process)}>
                                  <strong title={`${range.start.timestamp}–${range.end.timestamp}`}>{compactEntryRangeSeconds(entries, true)}</strong>
                                </button>
                              </span>
                              <button type="button" className="process-gantt-track timeline-click-target" onMouseMove={updateTimelineHover} onMouseLeave={() => setTimelineHover(undefined)} onClick={(event) => navigateComponentAtPointer(event, process.component)}>
                                {selectionWindow()}
                                <span className="process-gantt-bar" style={{ '--bar-left': `${startPercent}%`, '--bar-width': `${widthPercent}%`, ...timelineComponentStyle(process.component) } as React.CSSProperties}>
                                  <TimelineAnomalyMarkers entries={entries} startNs={range.startNs} endNs={range.endNs} onLabelHover={updateTimelineLabelHover} onLabelLeave={clearTimelineLabelHover} onAnomalyHover={updateTimelineAnomalyHover} onAnomalyLeave={clearTimelineAnomalyHover} onEntryClick={navigateTimelineEntry} />
                                  <span>{compactRangeDuration(range.duration)}</span>
                                </span>
                              </button>
                              <span className="process-gantt-range">{secondTimestamp(range.start.timestamp)}–{secondTimestamp(range.end.timestamp)}</span>
                            </div>

                            {processExpanded && traceRows.map(({ thread, trace, range: traceRange }) => {
                              const traceStartPercent = rangePercent(traceRange.startNs, viewStart, viewSpan);
                              const traceEndPercent = rangePercent(traceRange.endNs, viewStart, viewSpan);
                              const traceWidthPercent = Math.max(traceEndPercent - traceStartPercent, 0.6);
                              const severity = traceSeverity(trace);
                              const errorCount = trace.entries.filter((entry) => entry.severity === 'error').length;
                              const traceLabel = isZeroTrace(trace.traceKey) ? 'LOCAL' : compactTraceId(trace.rpc.traceId);
                              const active = selectedTraceId === trace.id || (!selectedTraceId && selectedThreadId === thread.id);
                              return (
                                <div className={classNames('process-gantt-row trace-row', `severity-${severity}`, active && 'active')} key={`${thread.id}:${trace.id}`}>
                                  <span className="process-gantt-check-cell process-gantt-check-placeholder" aria-hidden="true" />
                                  <span className="process-gantt-label trace-label">
                                    <span className="trace-branch" aria-hidden="true" />
                                    <button type="button" className="process-gantt-label-button trace-label-copy" onClick={() => onSelectTrace(thread, trace)}>
                                      <span className="gantt-entry-time" title={traceEntryLog(trace)?.timestamp}>{traceEntryLog(trace)?.timestamp ?? traceRange.start.timestamp}</span>
                                      <strong title={trace.firstFunctionName}>{trace.firstFunctionName}</strong>
                                      <small>{traceLabel}</small>
                                    </button>
                                  </span>
                                  <button type="button" className="process-gantt-track trace-track timeline-click-target" onMouseMove={updateTimelineHover} onMouseLeave={() => setTimelineHover(undefined)} onClick={(event) => navigateComponentAtPointer(event, thread.component)}>
                                    {selectionWindow()}
                                    <span className="process-gantt-bar trace-bar" style={{ '--bar-left': `${traceStartPercent}%`, '--bar-width': `${traceWidthPercent}%`, ...timelineComponentStyle(thread.component) } as React.CSSProperties}>
                                      <TimelineAnomalyMarkers entries={trace.entries} startNs={traceRange.startNs} endNs={traceRange.endNs} onLabelHover={updateTimelineLabelHover} onLabelLeave={clearTimelineLabelHover} onAnomalyHover={updateTimelineAnomalyHover} onAnomalyLeave={clearTimelineAnomalyHover} onEntryClick={navigateTimelineEntry} />
                                      <span>{compactRangeDuration(traceRange.duration)}</span>
                                    </span>
                                  </button>
                                  <span className="process-gantt-range">{secondTimestamp(traceRange.start.timestamp)}–{secondTimestamp(traceRange.end.timestamp)}</span>
                                </div>
                              );
                            })}
                          </div>
                        );
                      })}
                    </div>
                  );
                })}
              </div>
            </div>
            </div>
            {typeof document !== 'undefined' && timelineHover && !timelineLabelHover && !timelineAnomalyHover && createPortal(
              <div
                className="process-gantt-time-hover"
                style={{ left: timelineHover.x, top: timelineHover.y }}
              >
                {formatNsTick(timelineHover.ns, true, true)}
              </div>,
              document.body,
            )}
            {typeof document !== 'undefined' && timelineLabelHover && createPortal(
              <div
                className="process-gantt-label-hover"
                style={{
                  left: timelineLabelHover.x,
                  top: timelineLabelHover.y,
                  '--custom-label-color': timelineLabelHover.color,
                } as React.CSSProperties}
              >
                <span className="log-custom-label process-gantt-label-hover-chip">{timelineLabelHover.text}</span>
                <span className="process-gantt-label-hover-time">{timelineLabelHover.timestamp}</span>
                <span className="process-gantt-label-hover-rule">{timelineLabelHover.ruleName}</span>
              </div>,
              document.body,
            )}
            {typeof document !== 'undefined' && timelineAnomalyHover && createPortal(
              <div
                className={classNames('process-gantt-anomaly-hover', `severity-${timelineAnomalyHover.severity}`)}
                style={{ left: timelineAnomalyHover.x, top: timelineAnomalyHover.y }}
              >
                <div className="process-gantt-anomaly-hover-head">
                  <strong>{timelineAnomalyHover.level}</strong>
                  <span>{timelineAnomalyHover.timestamp}</span>
                </div>
                <div className="process-gantt-anomaly-hover-message">{timelineAnomalyHover.message}</div>
                <small>{timelineAnomalyHover.component} · {timelineAnomalyHover.sourceFile}:{timelineAnomalyHover.lineNumber}</small>
              </div>,
              document.body,
            )}
            <div
              className="process-gantt-label-resizer"
              style={{ left: `${labelWidth + 48}px` }}
              onPointerDown={beginLabelResize}
              onPointerMove={moveLabelResize}
              onPointerUp={endLabelResize}
              onPointerCancel={endLabelResize}
              role="separator"
              aria-orientation="vertical"
              aria-label="调整甘特图左侧标签宽度"
              title="左右拖拽调整标签列宽度"
            />
          </div>
        </div>
      )}
    </section>
  );
});

function LogPaginationBar({
  page,
  pageSize,
  total,
  onPageChange,
  onPageSizeChange,
  statistics,
}: {
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (page: number) => void;
  onPageSizeChange: (pageSize: number) => void;
  statistics?: { source: number; modules: number; processes: number; threads: number; functions: number; errorTraces: number; errors: number };
}) {
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const safePage = Math.min(page, pageCount);
  const start = total === 0 ? 0 : (safePage - 1) * pageSize + 1;
  const end = total === 0 ? 0 : Math.min(total, safePage * pageSize);

  return (
    <div className="log-pagination-bar" aria-label="日志工具与分页">
      {/* 底部只留统计与翻页；「源码语义 / 调用导航」等显示开关都在上方操作工具栏，
          与同类的开关和窗口按钮放在一起。 */}
      <div className="log-enhancement-tools" aria-label="日志统计">
        {statistics && <>
          <MetricItem icon={<FileCode2 size={15} />} label="日志源" value={statistics.source} />
          <MetricItem icon={<Boxes size={15} />} label="模块" value={statistics.modules} />
          <MetricItem icon={<ServerCog size={15} />} label="进程" value={statistics.processes} />
          <MetricItem icon={<ListTree size={15} />} label="线程" value={statistics.threads} />
          <MetricItem icon={<GitBranch size={15} />} label="函数" value={statistics.functions} />
          <MetricItem icon={<AlertTriangle size={15} />} label="错误链路" value={statistics.errorTraces} tone={statistics.errorTraces > 0 ? 'danger' : 'default'} />
          <MetricItem icon={<AlertTriangle size={15} />} label="ERROR" value={statistics.errors} tone={statistics.errors > 0 ? 'danger' : 'default'} />
        </>}

      </div>
      <div className="log-pagination-right">
        <div className="log-pagination-summary">
          <strong>{start.toLocaleString()}–{end.toLocaleString()}</strong>
          <span>共 {total.toLocaleString()} 条日志</span>
        </div>
        <div className="log-pagination-controls">
          <label className="page-size-control">
            <span>每页</span>
            <select value={pageSize} onChange={(event) => onPageSizeChange(Number(event.target.value))}>
              {LOG_PAGE_SIZE_OPTIONS.map((size) => <option key={size} value={size}>{size} 条</option>)}
            </select>
          </label>
          <button type="button" className="button ghost compact-button" disabled={safePage <= 1} onClick={() => onPageChange(1)}>首页</button>
          <button type="button" className="button ghost compact-button" disabled={safePage <= 1} onClick={() => onPageChange(safePage - 1)}>上一页</button>
          <span className="page-position">第 <strong>{safePage}</strong> / {pageCount} 页</span>
          <button type="button" className="button ghost compact-button" disabled={safePage >= pageCount} onClick={() => onPageChange(safePage + 1)}>下一页</button>
          <button type="button" className="button ghost compact-button" disabled={safePage >= pageCount} onClick={() => onPageChange(pageCount)}>末页</button>
        </div>
      </div>
    </div>
  );
}


function sourceLocationText(entry: LogEntry): string {
  const file = entry.source.fileName?.trim() || entry.source.raw?.trim() || '';
  if (!file) return '';
  return entry.source.lineNumber ? `${file}:${entry.source.lineNumber}` : file;
}

function sourceLocationLabel(entry: LogEntry): string {
  const full = sourceLocationText(entry);
  if (!full) return '';
  const normalized = full.replace(/\\/g, '/');
  const parts = normalized.split('/');
  return parts.at(-1) || full;
}

function logCategoryMeta(category?: string): { label: string; className: string } | undefined {
  switch (category) {
    case 'debug': return { label: '调试日志', className: 'debug' };
    case 'executor': return { label: '执行器日志', className: 'executor' };
    case 'run': return { label: '运行日志', className: 'run' };
    case 'helf': return { label: 'HELF日志', className: 'helf' };
    case 'sil': return { label: 'SIL日志', className: 'sil' };
    // mixed/imported 只是任务或导入方式，不是这一条日志的真实类型，不展示类型标签。
    default: return undefined;
  }
}

function functionLogCategoryMetas(node: FunctionNode): Array<{ label: string; className: string }> {
  const seen = new Set<string>();
  const metas: Array<{ label: string; className: string }> = [];
  functionNodeEntries(node).forEach((entry) => {
    const meta = logCategoryMeta(entry.logCategory);
    if (!meta || seen.has(meta.className)) return;
    seen.add(meta.className);
    metas.push(meta);
  });
  return metas;
}

function LogRow({
  entry,
  selected,
  onSelect,
}: {
  entry: LogEntry;
  selected: boolean;
  onSelect: (entry: LogEntry) => void;
}) {
  const semanticDisplay = useContext(SemanticDisplayContext);
  const [sourceCopied, setSourceCopied] = useState(false);
  // 规则配置入口与“是否展示语义”解耦：即使关闭语义展示，也必须能识别并编辑已有规则。
  const configuredSemanticMatch = useMemo(
    () => matchDisplayRulesToEntry(semanticDisplay.rules, entry),
    [entry, semanticDisplay.rules],
  );
  const configuredLabelMatches = useMemo(
    () => matchDisplayLabelRulesToEntry(semanticDisplay.rules, entry),
    [entry, semanticDisplay.rules],
  );
  const configuredAnyRuleMatch = configuredSemanticMatch ?? configuredLabelMatches[0];
  const semanticMatch = semanticDisplay.enabled ? configuredSemanticMatch : undefined;
  const customLabelMatches = semanticDisplay.enabled ? configuredLabelMatches : [];
  const visibleCustomLabels = customLabelMatches.slice(0, 3);
  const hiddenCustomLabelCount = Math.max(0, customLabelMatches.length - visibleCustomLabels.length);
  // 日志行左侧展示语义，右侧展示自定义标签；源码自动语义仍只属于函数折叠标题。
  const categoryMeta = logCategoryMeta(entry.logCategory);
  const sourceText = sourceLocationText(entry);
  const sourceLabel = sourceLocationLabel(entry);

  function quoteLogToAssistant(event: React.MouseEvent<HTMLButtonElement>) {
    event.stopPropagation();
    window.dispatchEvent(new CustomEvent('tracelens:assistant-quote-log', {
      detail: {
        raw: entry.raw || entry.message,
        message: entry.message,
        timestamp: entry.timestamp,
        component: entry.component,
        level: entry.level,
        source: sourceLocationText(entry),
      },
    }));
  }

  async function copySourceLocation(event: React.MouseEvent<HTMLButtonElement>) {
    event.stopPropagation();
    if (!sourceText) return;
    try {
      await navigator.clipboard.writeText(sourceText);
      setSourceCopied(true);
      window.setTimeout(() => setSourceCopied(false), 1200);
    } catch {
      const textarea = document.createElement('textarea');
      textarea.value = sourceText;
      textarea.style.position = 'fixed';
      textarea.style.opacity = '0';
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand('copy');
      textarea.remove();
      setSourceCopied(true);
      window.setTimeout(() => setSourceCopied(false), 1200);
    }
  }

  return (
    <div
      id={`log-entry-${entry.id}`}
      role="button"
      tabIndex={0}
      className={classNames('log-row', `severity-${entry.severity}`, selected && 'selected')}
      style={componentStyle(entry.component)}
      onClick={() => { if (!hasTextSelection()) onSelect(entry); }}
      onKeyDown={(event) => {
        if ((event.key === 'Enter' || event.key === ' ') && !hasTextSelection()) {
          event.preventDefault();
          onSelect(entry);
        }
      }}
      title={entry.raw}
    >
      <span className="log-time" title={entry.timestamp}>{entry.timestamp}</span>
      <ComponentBadge component={entry.component} compact />
      <span className={classNames('level-badge', `level-${entry.level.toLowerCase()}`)}>{entry.level}</span>
      <span className="log-summary" title={semanticMatch ? `${entry.message}\n用户语义 · ${semanticMatch.ruleName}: ${semanticMatch.text}${semanticMatch.supplementalText ? `\n补充说明：${semanticMatch.supplementalText}` : ''}` : entry.message}>
        <HighlightedText text={entry.message} />
        {semanticMatch && <span className="log-inline-semantic" title={`用户语义 · ${semanticMatch.ruleName}\n${semanticMatch.text}${semanticMatch.supplementalText ? `\n补充说明：${semanticMatch.supplementalText}` : ''}`}>{semanticMatch.text}</span>}
      </span>
      <span className="log-row-meta">
        {sourceText ? <button
          type="button"
          className={classNames('source-location-button', sourceCopied && 'copied')}
          onClick={copySourceLocation}
          title={`${sourceText}\n点击复制代码文件和行号`}
          aria-label={`复制源码位置 ${sourceText}`}
        >
          <Copy size={12}/><span>{sourceCopied ? '已复制' : sourceLabel}</span>
        </button> : <span className="source-location-placeholder" />}
        {semanticMatch && <span className="semantic-origin-slot">
          <span className="semantic-origin-badge user" title={`用户语义 · ${semanticMatch.ruleName}\n${semanticMatch.text}${semanticMatch.supplementalText ? `\n补充说明：${semanticMatch.supplementalText}` : ''}`}>用户语义</span>
        </span>}
        {visibleCustomLabels.length > 0 && <span className="log-custom-labels" aria-label="自定义标签">
          {visibleCustomLabels.map((match) => <span
            key={`${match.ruleId}-${match.customLabelText}`}
            className="log-custom-label"
            style={{ '--custom-label-color': match.customLabelColor || '#2563eb' } as React.CSSProperties}
            title={`${match.ruleName}\n${match.customLabelText}`}
          >{match.customLabelText}</span>)}
          {hiddenCustomLabelCount > 0 && <span className="log-custom-label more" title={customLabelMatches.slice(3).map((match) => match.customLabelText).join('\n')}>+{hiddenCustomLabelCount}</span>}
        </span>}
        {categoryMeta && <span className="log-category-slot">
          <span className={`log-category-badge ${categoryMeta.className}`}>{categoryMeta.label}</span>
        </span>}
      </span>
      <span className="log-ai-tip-slot">
        {selected && <button
          type="button"
          className="log-ai-tip-button"
          onClick={quoteLogToAssistant}
          title="引用当前日志询问 TracePilot"
          aria-label="引用当前日志询问 TracePilot"
        ><Sparkles size={13}/><span>问 AI</span></button>}
      </span>
      <button
        type="button"
        className={classNames('inline-rule-button', 'log-rule-edit-button', configuredAnyRuleMatch && 'configured')}
        onClick={(event) => {
          event.stopPropagation();
          semanticDisplay.onEditEntryRule?.(entry, configuredAnyRuleMatch?.ruleId);
        }}
        title={configuredAnyRuleMatch ? `编辑当前日志规则：${configuredAnyRuleMatch.ruleName}` : '基于当前日志配置语义/异常/屏蔽规则'}
        aria-label="编辑当前日志规则"
      >
        <Plus size={14} />
      </button>
    </div>
  );
}

function FunctionItem({
  node,
  depth,
  expandedIds,
  filters,
  sortOrder = 'asc',
  selectedEntryId,
  onToggle,
  onSelectEntry,
}: {
  node: FunctionNode;
  depth: number;
  expandedIds: Set<string>;
  filters: Filters;
  sortOrder?: LogSortOrder;
  selectedEntryId?: string;
  onToggle: (id: string) => void;
  onSelectEntry: (entry: LogEntry) => void;
}) {
  const expanded = expandedIds.has(node.id);
  const childFilters = functionMatchesDuration(node, filters.durationRange)
    ? { ...filters, durationRange: undefined }
    : filters;
  const filteredChildren = filteredItems(node.children, childFilters);
  const children = sortOrder === 'desc' ? filteredChildren.slice().reverse() : filteredChildren;
  const nestedFunctionCount = functionCount(children);
  const severity = itemSeverity(node);
  const semanticDisplay = useContext(SemanticDisplayContext);
  // 函数规则配置入口不能依赖“语义展示”开关。规则始终匹配，展示层再决定是否显示语义。
  const configuredSemanticMatch = useMemo(
    () => matchDisplayRulesToFunction(semanticDisplay.rules, node),
    [node, semanticDisplay.rules],
  );
  const autoSemanticCandidate = semanticDisplay.autoSemantics?.[semanticSourceKey(node.source.fileName, node.name)];
  const semanticMatch = semanticDisplay.enabled ? configuredSemanticMatch : undefined;
  // 源码自动语义属于解析结果，不受用户自定义标签开关影响。仅控制自定义规则显示。
  const autoSemantic = autoSemanticCandidate;
  const semanticText = semanticMatch?.text ?? autoSemantic?.description;
  const semanticOrigin = semanticText
    ? semanticMatch
      ? { label: '用户语义', className: 'user', title: `用户编辑语义 · ${semanticMatch.ruleName}\n${semanticText}${semanticMatch.supplementalText ? `\n补充说明：${semanticMatch.supplementalText}` : ''}` }
      : { label: '源码语义', className: 'source', title: `源码语义${autoSemantic?.source_path ? ` · ${autoSemantic.source_path}` : ''}\n${semanticText}` }
    : undefined;
  const categoryMetas = useMemo(() => functionLogCategoryMetas(node), [node]);
  const nodeLogCount = useMemo(() => functionNodeEntries(node).length, [node]);

  return (
    <div
      id={`function-node-${node.id}`}
      className={classNames('function-wrap', `severity-${severity}`, (node.origin === 'consecutive' || node.origin === 'context') && 'consecutive-function-group', node.origin === 'repeated' && 'repeated-function-group')}
      style={{ '--depth': depth, '--component-hue': componentHue(node.component) } as React.CSSProperties}
    >
      <div className="timeline-node-dot" />
      <div className="function-card-shell">
        <button type="button" className="function-card" onClick={() => { if (!hasTextSelection()) onToggle(node.id); }}>
          <span className="function-chevron">{expanded ? <ChevronDown size={17} /> : <ChevronRight size={17} />}</span>
          <span className="function-main">
            <span className="function-title-row">
              <span className="function-entry-time" title={node.startEntry.timestamp}>{node.startEntry.timestamp}</span>
              <ComponentBadge component={node.component} compact />
              <span className="function-name-semantic">
                <span className="function-name" title={node.name}>{node.name}</span>
                {node.origin === 'repeated' && <span className="function-repeat-badge">×{node.repeatCount ?? children.length}</span>}
                {semanticText && <span className={classNames('semantic-function-description', semanticMatch ? 'rule' : 'auto')} title={semanticOrigin?.title}>{semanticText}</span>}
                {semanticOrigin && <span className={`semantic-origin-badge function-semantic-origin ${semanticOrigin.className}`} title={semanticOrigin.title}>{semanticOrigin.label}</span>}
              </span>
              {severity === 'error' && <span className="severity-badge error">ERROR 链路</span>}
              {severity === 'warning' && <span className="severity-badge warning">WARN</span>}
            </span>
          </span>
          <span className="function-stats">
            {(node.origin === 'boundary' || node.origin === 'repeated') && <span className="function-source" title={node.source.raw}>{node.source.raw}</span>}
            <span className="duration">{formatDuration(durationNs(node))}</span>
            {node.origin === 'repeated' ? (
              <>
                <span>{node.repeatCount ?? children.length} 次调用</span>
                <span>{nodeLogCount} 条日志</span>
              </>
            ) : (
              <>
                <span>{children.length} 条日志</span>
                {nestedFunctionCount > 0 && <span>{nestedFunctionCount} 个子流程</span>}
              </>
            )}
            {categoryMetas.map((meta) => <span key={meta.className} className={`log-category-badge function-log-category ${meta.className}`}>{meta.label}</span>)}
          </span>
        </button>
        {semanticDisplay.onEditFunctionRule && (
          <button
            type="button"
            className={classNames('inline-rule-button function-inline-rule-button', configuredSemanticMatch && 'configured')}
            onClick={() => semanticDisplay.onEditFunctionRule?.(
              node,
              configuredSemanticMatch?.ruleId,
              configuredSemanticMatch ? undefined : autoSemanticCandidate?.description,
            )}
            title={configuredSemanticMatch
              ? `编辑展示规则：${configuredSemanticMatch.ruleName}`
              : autoSemanticCandidate
                ? '基于当前源码语义打开规则配置并预览，确认后再保存'
                : '为当前函数流程配置语义/异常/屏蔽规则'}
            aria-label={configuredSemanticMatch ? '编辑当前函数规则' : '配置当前函数规则'}
          >
            <Plus size={15} />
          </button>
        )}
      </div>

      {expanded && (
        <div className="function-children">
          {children.map((child) =>
            child.kind === 'function' ? (
              <FunctionItem
                key={child.id}
                node={child}
                depth={depth + 1}
                expandedIds={expandedIds}
                filters={filters}
                sortOrder={sortOrder}
                selectedEntryId={selectedEntryId}
                onToggle={onToggle}
                onSelectEntry={onSelectEntry}
              />
            ) : (
              <LogRow
                key={child.id}
                entry={child.entry}
                selected={selectedEntryId === child.entry.id}
                onSelect={onSelectEntry}
              />
            ),
          )}
          {children.length === 0 && <div className="empty-inline">当前筛选条件下无日志</div>}
        </div>
      )}
    </div>
  );
}

function TracePanel({
  trace,
  filters,
  expandedIds,
  selectedEntryId,
  onToggle,
  onSelectEntry,
  stickyTop,
}: {
  trace: TraceTimeline;
  filters: Filters;
  expandedIds: Set<string>;
  selectedEntryId?: string;
  onToggle: (id: string) => void;
  onSelectEntry: (entry: LogEntry) => void;
  stickyTop: number;
}) {
  const items = filteredItems(trace.items, filters);
  const visibleCount = trace.entries.filter((entry) => matchesEntry(entry, filters)).length;
  const spanCount = new Set(trace.entries.map((entry) => entry.rpc.spanId).filter((id) => id !== '0')).size;
  const severity = traceSeverity(trace);
  const errorCount = trace.entries.filter((entry) => entry.severity === 'error').length;
  const warningCount = trace.entries.filter((entry) => entry.severity === 'warning').length;

  return (
    <section className={classNames('trace-panel', `severity-${severity}`)}>
      <header className="trace-header" style={{ top: stickyTop }}>
        <div className="trace-heading">
          <div>
            <h3>{isZeroTrace(trace.traceKey) ? '本地调用链' : `Trace ${trace.rpc.traceId}`}</h3>
          </div>
          <div className="trace-components">
            {trace.components.map((component) => <ComponentBadge key={component} component={component} compact />)}
          </div>
        </div>
        <div className="trace-meta">
          {errorCount > 0 && <span className="trace-severity error">{errorCount} 条 ERROR</span>}
          {warningCount > 0 && <span className="trace-severity warning">{warningCount} 条 WARN</span>}
          {trace.crossComponent && <span>{trace.participantCount} 条执行泳道</span>}
          {!isZeroTrace(trace.traceKey) && <span>{spanCount} 个 Span</span>}
          <span>{visibleCount}/{trace.entries.length} 条</span>
        </div>
      </header>
      <div className="timeline-list">
        {items.map((item) =>
          item.kind === 'function' ? (
            <FunctionItem
              key={item.id}
              node={item}
              depth={0}
              expandedIds={expandedIds}
              filters={filters}
              selectedEntryId={selectedEntryId}
              onToggle={onToggle}
              onSelectEntry={onSelectEntry}
            />
          ) : (
            <div className={classNames('root-log', `severity-${item.entry.severity}`)} key={item.id}>
              <div className="timeline-node-dot" />
              <LogRow
                entry={item.entry}
                selected={selectedEntryId === item.entry.id}
                onSelect={onSelectEntry}
              />
            </div>
          ),
        )}
        {items.length === 0 && (
          <div className="empty-state compact"><Search size={22} /><span>当前调用链没有匹配日志</span></div>
        )}
      </div>
    </section>
  );
}


function timelineItemStartNs(item: TimelineItem): bigint {
  return item.kind === 'function'
    ? item.startEntry.timestampNs ?? 0n
    : item.entry.timestampNs ?? 0n;
}

function FlatTimelineItems({
  traces,
  filters,
  sortOrder,
  expandedIds,
  selectedEntryId,
  onToggle,
  onSelectEntry,
}: {
  traces: TraceTimeline[];
  filters: Filters;
  sortOrder: LogSortOrder;
  expandedIds: Set<string>;
  selectedEntryId?: string;
  onToggle: (id: string) => void;
  onSelectEntry: (entry: LogEntry) => void;
}) {
  // 先按未过滤的真实时间顺序做“连续重复函数”二次收纳，再应用展示筛选。
  // 这样多个 Trace/Span 中连续出现、入口/出口完全一致的同一函数也能恢复 ×N，
  // 同时不会因为搜索条件隐藏了中间项而把原本不连续的调用误合并。
  const orderedItems = traces
    .flatMap((trace) => trace.items)
    .sort((left, right) => {
      const leftTime = timelineItemStartNs(left);
      const rightTime = timelineItemStartNs(right);
      return leftTime < rightTime ? -1 : leftTime > rightTime ? 1 : 0;
    });
  const foldingRules = useContext(FoldingRuleContext);
  const mergedItems = filteredItems(mergeRepeatedFunctionGroups(orderedItems, foldingRules), filters);
  const items = sortOrder === 'desc' ? mergedItems.slice().reverse() : mergedItems;

  return (
    <div className="timeline-list">
      {items.map((item) =>
        item.kind === 'function' ? (
          <FunctionItem
            key={item.id}
            node={item}
            depth={0}
            expandedIds={expandedIds}
            filters={filters}
            sortOrder={sortOrder}
            selectedEntryId={selectedEntryId}
            onToggle={onToggle}
            onSelectEntry={onSelectEntry}
          />
        ) : (
          <div className={classNames('root-log', `severity-${item.entry.severity}`)} key={item.id}>
            <div className="timeline-node-dot" />
            <LogRow
              entry={item.entry}
              selected={selectedEntryId === item.entry.id}
              onSelect={onSelectEntry}
            />
          </div>
        ),
      )}
      {items.length === 0 && (
        <div className="empty-state compact"><Search size={22} /><span>当前线程没有匹配日志</span></div>
      )}
    </div>
  );
}

function threadIdentity(trace: TraceTimeline): string {
  const first = trace.entries[0];
  if (!first) return `unknown:${trace.id}`;
  return `${first.component}:${first.processId}:${first.threadId}`;
}

function ThreadTraceFlatView({
  traces,
  filters,
  sortOrder,
  expandedIds,
  selectedEntryId,
  onToggle,
  onSelectEntry,
}: {
  traces: TraceTimeline[];
  filters: Filters;
  sortOrder: LogSortOrder;
  expandedIds: Set<string>;
  selectedEntryId?: string;
  onToggle: (id: string) => void;
  onSelectEntry: (entry: LogEntry) => void;
}) {
  const severity = maxSeverity(...traces.map(traceSeverity));

  return (
    <section className={classNames('thread-trace-flat-view', `severity-${severity}`)}>
      <FlatTimelineItems
        traces={traces}
        filters={filters}
        sortOrder={sortOrder}
        expandedIds={expandedIds}
        selectedEntryId={selectedEntryId}
        onToggle={onToggle}
        onSelectEntry={onSelectEntry}
      />
    </section>
  );
}

function ThreadGroupedFlatView({
  traces,
  filters,
  sortOrder,
  expandedIds,
  selectedEntryId,
  onToggle,
  onSelectEntry,
}: {
  traces: TraceTimeline[];
  filters: Filters;
  sortOrder: LogSortOrder;
  expandedIds: Set<string>;
  selectedEntryId?: string;
  onToggle: (id: string) => void;
  onSelectEntry: (entry: LogEntry) => void;
}) {
  const groups = Array.from(
    traces.reduce((map, trace) => {
      const key = threadIdentity(trace);
      const current = map.get(key);
      if (current) current.push(trace);
      else map.set(key, [trace]);
      return map;
    }, new Map<string, TraceTimeline[]>()),
  )
    .map(([key, groupTraces]) => {
      const orderedEntries = groupTraces.flatMap((trace) => trace.entries).sort(compareEntries);
      return {
        key,
        traces: groupTraces,
        firstEntry: (sortOrder === 'desc' ? orderedEntries[orderedEntries.length - 1] : orderedEntries[0])!,
      };
    })
    .sort((left, right) => sortOrder === 'desc' ? -compareEntries(left.firstEntry, right.firstEntry) : compareEntries(left.firstEntry, right.firstEntry));

  if (groups.length === 1) {
    return (
      <ThreadTraceFlatView
        traces={groups[0].traces}
        filters={filters}
        sortOrder={sortOrder}
        expandedIds={expandedIds}
        selectedEntryId={selectedEntryId}
        onToggle={onToggle}
        onSelectEntry={onSelectEntry}
      />
    );
  }

  return (
    <div className="thread-grouped-flat-view">
      {groups.map((group) => {
        const first = group.firstEntry;
        const severity = maxSeverity(...group.traces.map(traceSeverity));
        const entryCount = group.traces.reduce((sum, trace) => sum + trace.entries.length, 0);
        return (
          <section key={group.key} className={classNames('thread-flat-group', `severity-${severity}`)}>
            <div className="thread-flat-separator">
              <ComponentBadge component={first.component} compact />
              <strong>PID {first.processId}</strong>
              <span>线程 {first.threadId}</span>
              <small>{group.traces.length} 条调用链 · {entryCount} 条日志</small>
            </div>
            <FlatTimelineItems
              traces={group.traces}
              filters={filters}
              sortOrder={sortOrder}
              expandedIds={expandedIds}
              selectedEntryId={selectedEntryId}
              onToggle={onToggle}
              onSelectEntry={onSelectEntry}
            />
          </section>
        );
      })}
    </div>
  );
}

function MergedFmTimelineView({
  traces,
  filters,
  sortOrder,
  expandedIds,
  selectedEntryId,
  onToggle,
  onSelectEntry,
}: {
  traces: TraceTimeline[];
  filters: Filters;
  sortOrder: LogSortOrder;
  expandedIds: Set<string>;
  selectedEntryId?: string;
  onToggle: (id: string) => void;
  onSelectEntry: (entry: LogEntry) => void;
}) {
  return (
    <section className="merged-fm-timeline-view">
      <FlatTimelineItems
        traces={traces}
        filters={filters}
        sortOrder={sortOrder}
        expandedIds={expandedIds}
        selectedEntryId={selectedEntryId}
        onToggle={onToggle}
        onSelectEntry={onSelectEntry}
      />
    </section>
  );
}

function FlatLogView({
  entries: sourceEntries,
  filters,
  sortOrder,
  selectedEntryId,
  onSelectEntry,
  stickyTop,
}: {
  entries: LogEntry[];
  filters: Filters;
  sortOrder: LogSortOrder;
  selectedEntryId?: string;
  onSelectEntry: (entry: LogEntry) => void;
  stickyTop: number;
}) {
  const entries = sourceEntries
    .slice()
    .sort((left, right) => sortOrder === 'desc' ? -compareEntries(left, right) : compareEntries(left, right))
    .filter((entry) => matchesEntry(entry, filters));

  return (
    <section className="trace-panel flat-panel">
      <header className="trace-header" style={{ top: stickyTop }}>
        <div><h3>{sortOrder === 'asc' ? '按时间升序的日志' : '按时间降序的日志'}</h3></div>
        <div className="trace-meta"><span>{entries.length} 条</span></div>
      </header>
      <div className="flat-list">
        {entries.map((entry) => (
          <LogRow
            key={entry.id}
            entry={entry}
            selected={selectedEntryId === entry.id}
            onSelect={onSelectEntry}
          />
        ))}
        {entries.length === 0 && (
          <div className="empty-state compact"><Search size={22} /><span>当前条件下没有匹配日志</span></div>
        )}
      </div>
    </section>
  );
}

interface CallGraphState {
  title: string;
  subtitle: string;
  traces: TraceTimeline[];
}

interface PendingFunctionFocus {
  nodeId: string;
  traceId: string;
  expandedPath: string[];
}

function functionChildren(node: FunctionNode): FunctionNode[] {
  return node.children.filter((item): item is FunctionNode => item.kind === 'function');
}

function findFunctionPath(
  items: TimelineItem[],
  targetId: string,
  ancestors: string[] = [],
): string[] | undefined {
  for (const item of items) {
    if (item.kind !== 'function') continue;
    const path = [...ancestors, item.id];
    if (item.id === targetId) return path;
    const nested = findFunctionPath(item.children, targetId, path);
    if (nested) return nested;
  }
  return undefined;
}

function EntryInspector({ entry, onClose }: { entry: LogEntry; onClose: () => void }) {
  const semanticDisplay = useContext(SemanticDisplayContext);
  const semanticMatch = useMemo(() => {
    const logMatch = matchDisplayRulesToEntry(semanticDisplay.rules, entry);
    if (logMatch) return logMatch;
    // 函数标题语义也属于当前日志上下文：详情抽屉应继续提供同一条规则的补充说明。
    for (const rule of semanticDisplay.rules) {
      if (!rule.enabled || rule.scope === 'log') continue;
      if (rule.kind === 'keyword') {
        const keyword = rule.keyword?.trim();
        const functionName = entry.functionName ?? entry.boundaryFunctionName ?? '';
        if (keyword && functionName.includes(keyword)) {
          return {
            ruleId: rule.id,
            ruleName: rule.name,
            text: rule.displayTemplate,
            supplementalText: rule.supplementalDescription?.trim() || undefined,
            parameters: {},
            sourceMessage: functionName,
            sourceEntryId: entry.id,
          };
        }
      }
      const messageMatch = matchDisplayRuleToMessage(rule, entry.message);
      if (messageMatch) return { ...messageMatch, sourceEntryId: entry.id };
    }
    return undefined;
  }, [entry, semanticDisplay.rules]);
  const runEventTypeLabels: Record<string, string> = { SET: 'SET · 异常上报', EVT: 'EVT · 事件上报', DEA: 'DEA · 去激活', CLR: 'CLR · 异常清除' };
  const fields = entry.runEvent ? [
    ['时间戳', entry.timestamp],
    ['日志类别', '运行日志'],
    ['事件码所属组件', entry.component],
    ['进程 ID', entry.processId],
    ['源码位置', entry.source.raw],
    ['事件类别', entry.runEvent.eventCategory],
    ['事件级别', entry.runEvent.eventLevel],
    ['当前事件码', entry.runEvent.currentEventCode || '—'],
    ['链接到的事件码', entry.runEvent.linkedEventCodes.join(', ') || '—'],
    ['当前异常 ErrIId', entry.runEvent.currentErrIId || '—'],
    ['链接异常 ErrIId', entry.runEvent.linkedErrIIds.join(', ') || '—'],
    ['当前异常 DisplayCode', entry.runEvent.displayCode || '—'],
    ['链接异常 DisplayCode', entry.runEvent.linkedDisplayCodes.join(', ') || '—'],
    ['事件类型', runEventTypeLabels[entry.runEvent.eventType] ?? entry.runEvent.eventType],
    ['正文严重度', entry.severity === 'error' ? 'ERROR' : entry.severity === 'warning' ? 'WARN' : '正常事件'],
    ['日志文件', entry.sourceFile],
  ] : [
    ['时间戳', entry.timestamp],
    ['日志级别', entry.level],
    ['正文严重度', entry.severity === 'error' ? 'ERROR' : entry.severity === 'warning' ? 'WARN' : '正常'],
    ['日志文件', entry.sourceFile],
    ['模块', entry.component],
    ['进程 / 线程', `${entry.processId} / ${entry.threadId}`],
    ['源码位置', entry.source.raw],
    ['跟踪模式', entry.mode === '100' ? '100 · 内部日志' : entry.mode === '101' ? '101 · 外部日志' : entry.mode],
    ['RPC 调用链', entry.rpc.raw],
    ['Trace / Span / Father', `${entry.rpc.traceId} / ${entry.rpc.spanId} / ${entry.rpc.fatherSpanId}`],
    ['函数', entry.functionName ?? '—'],
    ['流程标记', entry.marker === 'start' ? '函数开始 >()' : entry.marker === 'end' ? '函数结束 <()' : '普通日志'],
  ];

  return (
    <aside className={classNames('inspector', `severity-${entry.severity}`)}>
      <header className="inspector-header">
        <div><div className="eyebrow">LOG INSPECTOR</div><h3>日志详情</h3></div>
        <div className="inspector-header-actions">
          <button
            type="button"
            className="log-ai-inspector-button"
            onClick={() => window.dispatchEvent(new CustomEvent('tracelens:assistant-quote-log', { detail: { raw: entry.raw || entry.message, message: entry.message, timestamp: entry.timestamp, component: entry.component, level: entry.level, source: sourceLocationText(entry) } }))}
            title="引用这条日志询问 TracePilot"
          ><Sparkles size={13}/> 问 AI</button>
          <button type="button" className="icon-button" onClick={onClose} aria-label="关闭详情"><X size={18} /></button>
        </div>
      </header>
      <div className="inspector-body">
        <div className="inspector-grid">
          {fields.map(([label, value]) => (
            <div className="inspector-field" key={label}><span>{label}</span><strong>{value}</strong></div>
          ))}
        </div>
        {semanticMatch?.supplementalText && <div className="semantic-supplement-panel">
          <div className="semantic-supplement-title"><Sparkles size={15} /> 语义补充说明</div>
          <div className="semantic-supplement-semantic"><strong>{semanticMatch.text}</strong><span>{semanticMatch.ruleName}</span></div>
          <p>{semanticMatch.supplementalText}</p>
        </div>}
        <div className="raw-block">
          <div className="raw-title"><TerminalSquare size={15} /> 完整日志文本</div>
          <pre><HighlightedText text={entry.raw} /></pre>
        </div>
      </div>
    </aside>
  );
}

function IssuePanel({ issues, onClose }: { issues: ParseIssue[]; onClose: () => void }) {
  return (
    <aside className="inspector issues-panel">
      <header className="inspector-header">
        <div><div className="eyebrow">PARSE REPORT</div><h3>未解析日志</h3></div>
        <button type="button" className="icon-button" onClick={onClose} aria-label="关闭解析报告"><X size={18} /></button>
      </header>
      <div className="inspector-body">
        {issues.length === 0 ? (
          <div className="empty-state compact"><Sparkles size={22} /><span>所有非空行均解析成功</span></div>
        ) : (
          issues.map((issue) => (
            <div className="issue-item" key={`${issue.lineNumber}-${issue.raw}`}>
              <div><AlertTriangle size={15} /> {issue.sourceFile} · 第 {issue.lineNumber} 行 · {issue.reason}</div>
              <pre>{issue.raw}</pre>
            </div>
          ))
        )}
      </div>
    </aside>
  );
}


function CurrentEnvironmentResourceView({
  environment,
  onOpenLogLocator,
  onOpenCpdReports,
}: {
  environment: EnvironmentSummary;
  onOpenLogLocator: () => void;
  onOpenCpdReports: () => void;
}) {
  const [runtime, setRuntime] = useState<EnvironmentRuntimeStatus>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const loadRuntime = useCallback(async (refresh = false) => {
    setLoading(true);
    setError('');
    try {
      setRuntime(await getEnvironmentRuntimeStatus(environment.id, refresh));
    } catch (runtimeError) {
      setError(runtimeError instanceof Error ? runtimeError.message : '资源状态读取失败');
    } finally {
      setLoading(false);
    }
  }, [environment.id]);

  useEffect(() => { void loadRuntime(false); }, [loadRuntime]);

  return (
    <main className="current-environment-resource-view">
      <div className="current-environment-resource-toolbar">
        <strong>{environment.upper_machine.host} · 本机资源</strong>
        <button type="button" className="button ghost compact-button" disabled={loading} onClick={() => void loadRuntime(true)}>
          {loading ? <LoaderCircle className="spin" size={14}/> : <ServerCog size={14}/>} 刷新状态
        </button>
      </div>
      {error && <div className="current-environment-resource-error">{error}</div>}
      <EnvironmentResourcePreview environment={environment} runtime={runtime} onOpenLogLocator={onOpenLogLocator} onOpenCpdReports={onOpenCpdReports} showActions={false}/>
    </main>
  );
}

export default function App() {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [workspacePage, setWorkspacePage] = useState<WorkspacePage>(() => workspacePageFromUrl());
  const [preferredRemoteEnvironmentId, setPreferredRemoteEnvironmentId] = useState<number>();
  const [preferredRemotePreset, setPreferredRemotePreset] = useState<RemoteLogPreset>();
  const [reportEnvironment, setReportEnvironment] = useState<EnvironmentSummary>();
  const initialRouteEnvironmentIdRef = useRef<number | undefined>(routeEnvironmentIdFromUrl());
  const [resourceWorkspaceEnvironment, setResourceWorkspaceEnvironment] = useState<EnvironmentSummary>();
  const [resourceWorkspaceFeature, setResourceWorkspaceFeature] = useState<'resource' | 'logs'>('logs');
  const [resourceWorkspaceTabs, setResourceWorkspaceTabs] = useState<EnvironmentSummary[]>([]);
  // 本地导入日志也占用一个与环境资源平级的一级工作区 Tab。
  // 关闭该 Tab 只隐藏工作区，不删除已经导入的任务。
  const [localImportWorkspaceOpen, setLocalImportWorkspaceOpen] = useState(false);
  const analysisStackRef = useRef<HTMLDivElement>(null);
  const logScrollRef = useRef<HTMLDivElement>(null);
  const streamWorkerRef = useRef<Worker>();
  const remoteTaskControllersRef = useRef(new Map<string, { controller: AbortController; environmentId: number; operationId: string; workerTaskId?: string }>());
  const importBuffersRef = useRef(new Map<string, ImportBufferState>());
  const importCompletionRef = useRef(new Map<string, { resolve: (count: number) => void; reject: (error: Error) => void }>());
  const [tasks, setTasks] = useState<LogTask[]>([]);
  const [activeTaskId, setActiveTaskId] = useState('');
  const taskSequenceRef = useRef(1);

  // 刷新恢复仅保存任务入口，不保存日志/分析内容，避免浏览器存储爆满。
  const CASE_URL_STATE_KEY = 'tracelens-case-url-state-v1';
  useEffect(() => {
    try {
      const compact = tasks.map((task) => ({
        id: task.id,
        name: task.name,
        createdAt: task.createdAt,
        remoteEnvironmentId: task.remoteEnvironmentId,
        urls: task.sources
          .map((source) => source.sourcePath)
          .filter((item): item is string => Boolean(item)),
      })).filter((item) => item.urls.length > 0);

      window.localStorage.setItem(CASE_URL_STATE_KEY, JSON.stringify({
        activeTaskId,
        tasks: compact,
      }));

      // 清理旧版本完整快照，避免历史版本残留继续占用空间。
      [
        'case_workspace_state',
        'tracelens-case-workspace-state',
        'tracelens-ai-context-snapshot',
        'tracelens-context-snapshot-v1',
      ].forEach((key) => window.localStorage.removeItem(key));
    } catch {
      // 存储空间不足时不影响当前日志分析。
    }
  }, [tasks, activeTaskId]);
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [selectedProcessId, setSelectedProcessId] = useState<string>();
  const [selectedThreadId, setSelectedThreadId] = useState<string>();
  const [selectedTraceId, setSelectedTraceId] = useState<string>();
  const [selectedCrossTraceId, setSelectedCrossTraceId] = useState<string>();
  const [selectedEntry, setSelectedEntry] = useState<LogEntry>();
  /** 异常导航只做定位/高亮，不触发日志详情抽屉。 */
  const [focusedEntryId, setFocusedEntryId] = useState<string>();
  const [showIssues, setShowIssues] = useState(false);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const [callFlowDialogOpen, setCallFlowDialogOpen] = useState(false);
  /**
   * 调用导航的两种看法：
   * - tree  ：原来的缩进树，适合逐层核对某一条调用；
   * - flowmap：横向流程地图，适合「一眼看完整流程、哪里不对」。
   * 默认给流程地图 —— 用户打开调用导航通常就是想快速定位异常在那一段。
   */
  const [callFlowView, setCallFlowView] = useState<'flowmap' | 'tree'>('flowmap');
  const [dragging, setDragging] = useState(false);
  const [showTimeline, setShowTimeline] = useState(false);
  // 悬浮 / 固定二选一。固定时时间线嵌回日志区上方（占位排版），不再盖在日志上。
  const [timelineDocked, setTimelineDocked] = useState(false);
  const [timelineWindowPosition, setTimelineWindowPosition] = useState(() => ({
    left: 8,
    top: 86,
  }));
  const timelineWindowDragRef = useRef<{ pointerId: number; startX: number; startY: number; left: number; top: number }>();
  const timelineWindowRef = useRef<HTMLElement | null>(null);
  /**
   * Natural height of the timeline content (chrome + lanes).
   *
   * The window used to be a fixed `min(620px, 62vh)`, so a single component left a large blank
   * area above nothing. Measuring lets the window hug its content and only reach for the
   * scrollbar once it hits the CSS max-height.
   */
  const [timelineContentHeight, setTimelineContentHeight] = useState(0);
  const [timelineGroupingMode, setTimelineGroupingMode] = useState<TimelineGroupingMode>('merged');
  const [logPage, setLogPage] = useState(1);
  const [logPageSize, setLogPageSize] = useState<number>(DEFAULT_LOG_PAGE_SIZE);
  const [logSortOrder, setLogSortOrder] = useState<LogSortOrder>('asc');
  const [liveListening, setLiveListening] = useState(false);
  // The collector panel renders beside <App/> (like AiAssistant), so the single
  // 实时监控 toggle is published as an event rather than a prop.
  const [liveConnectionStatus, setLiveConnectionStatus] = useState<'off' | 'connecting' | 'connected' | 'error'>('off');
  useEffect(() => {
    // Published as observable state, not an event: the ribbon/collector mount on demand and
    // would otherwise miss a notification that fired before they existed.
    setLiveMonitoring({ on: liveListening, environmentId: preferredRemoteEnvironmentId });
  }, [liveListening, preferredRemoteEnvironmentId]);

  const [liveAddedCount, setLiveAddedCount] = useState(0);
  const [liveMessage, setLiveMessage] = useState('');
  const [remoteLogLocator, setRemoteLogLocator] = useState<RemoteLogLocatorSnapshot>();
  // The UI-action registry closes over its own render; the ref keeps 开始采集 reading the
  // locator the user has selected right now instead of the one from when the effect ran.
  const remoteLogLocatorRef = useRef<RemoteLogLocatorSnapshot>();
  useEffect(() => { remoteLogLocatorRef.current = remoteLogLocator; }, [remoteLogLocator]);
  const liveBoundTaskIdRef = useRef<string>();
  const liveAbortRef = useRef<AbortController>();
  const liveSessionRef = useRef(0);
  const liveImportQueueRef = useRef<Promise<unknown>>(Promise.resolve());
  const liveRenderQueueRef = useRef<Promise<void>>(Promise.resolve());
  const liveRenderGenerationRef = useRef(0);
  const liveFollowTailRef = useRef(true);
  const livePendingFollowRef = useRef(false);
  const [pendingPageEntryId, setPendingPageEntryId] = useState<string>();
  const [pendingEntryFocusId, setPendingEntryFocusId] = useState<string>();
  const [pendingEntryFocusKind, setPendingEntryFocusKind] = useState<'navigation' | 'time-cursor'>('navigation');
  const [timeCursorScrollLocked, setTimeCursorScrollLocked] = useState(false);
  const [timelineHiddenComponents, setTimelineHiddenComponents] = useState<Set<string>>(new Set());
  const timelineCursorTargetRef = useRef<string>();
  const [traceHeaderTop, setTraceHeaderTop] = useState(160);
  const [expandedMethodGroups, setExpandedMethodGroups] = useState<Set<string>>(new Set());
  const [expandedCrossMethodGroups, setExpandedCrossMethodGroups] = useState<Set<string>>(new Set());
  const [expandedProcessIds, setExpandedProcessIds] = useState<Set<string>>(new Set());
  const [crossSectionExpanded, setCrossSectionExpanded] = useState(false);
  const [callGraphState, setCallGraphState] = useState<CallGraphState>();
  const [pendingFunctionFocus, setPendingFunctionFocus] = useState<PendingFunctionFocus>();
  const [pasteDialogOpen, setPasteDialogOpen] = useState(false);
  const [importDialogMode, setImportDialogMode] = useState<'choose' | 'paste' | 'url'>('choose');
  const [pastedLogText, setPastedLogText] = useState('');
  const [importUrlText, setImportUrlText] = useState('');
  const [urlImportItems, setUrlImportItems] = useState<UrlLogImportItem[]>([]);
  const [urlArchiveSelections, setUrlArchiveSelections] = useState<Record<string, string>>({});
  const [urlImportBusy, setUrlImportBusy] = useState(false);
  const [urlImportError, setUrlImportError] = useState('');
  const importUrlList = useMemo(() => Array.from(new Set(importUrlText.split(/\s+/).map((item) => item.trim()).filter(Boolean))), [importUrlText]);
  const urlImportReady = urlImportItems.length > 0
    && urlImportItems.every((item) => item.status === 'success' && (item.kind !== 'archive' || Boolean(urlArchiveSelections[item.url])));
  const [processTimelineExpanded, setProcessTimelineExpanded] = useState(true);
  const [initialSharedScene] = useState<SharedLogScene | undefined>(() => sharedLogSceneFromUrl());
  const [sharedSceneRestoreRevision, setSharedSceneRestoreRevision] = useState(0);
  const sharedSceneLaunchRef = useRef(false);
  const pendingSharedSceneRef = useRef<SharedLogScene>();
  const pendingSharedErrorRef = useRef<SharedLogSceneErrorAnchor>();
  const [optimisticTraceId, setOptimisticTraceId] = useState<string>();
  const [navigationPending, startNavigationTransition] = useTransition();
  const [customStartTime, setCustomStartTime] = useState('');
  const [customEndTime, setCustomEndTime] = useState('');
  const [errorRules, setErrorRules] = useState<ErrorMatchRule[]>(loadErrorRules);
  const [displayRules, setDisplayRules] = useState<DisplayRule[]>(() => localDisplayRuleStore.load());
  const displayRulesBackendReadyRef = useRef(false);
  const [semanticLabelsEnabled, setSemanticLabelsEnabled] = useState(loadSemanticLabelsEnabled);
  const [rawLogMode, setRawLogMode] = useState(loadRawLogModePreference);
  const [maskingRules, setMaskingRules] = useState<MaskingRule[]>(loadMaskingRules);
  const [foldingRules, setFoldingRules] = useState<FoldingRule[]>(loadFoldingRules);
  const [foldingEnabled, setFoldingEnabled] = useState(() => typeof window === 'undefined' ? true : window.localStorage.getItem(LOG_FOLDING_ENABLED_KEY) !== '0');
  const effectiveFoldingRules = useMemo(() => foldingEnabled ? foldingRules : foldingRules.map((rule) => ({ ...rule, enabled: false })), [foldingEnabled, foldingRules]);
  const [dataExtractionRules, setDataExtractionRules] = useState<DataExtractionRule[]>(loadDataExtractionRules);
  const dataExtractionRulesBackendReadyRef = useRef(false);
  const logFormatRulesRef = useRef<LogFormatParserRuleConfig[]>([]);
  const [dataSourceOperationFilter, setDataSourceOperationFilter] = useState('');
  const [dataExtractionDialog, setDataExtractionDialog] = useState<{
    open: boolean;
    phase: 'select' | 'running' | 'done' | 'error';
    candidates: DataExtractionCandidate[];
    selectedIds: Set<string>;
    progress?: ExtractionProgress;
    results?: DataExtractionResultView[];
    error?: string;
    recordId?: number;
    recordSaved?: boolean;
    /** 当前有没有可做批量提取的已解析日志（实时监听期间没有，但实时采集仍可用）。 */
    batchAvailable?: boolean;
  }>({ open: false, phase: 'select', candidates: [], selectedIds: new Set() });
  const dataExtractionAbortRef = useRef<AbortController>();
  const pendingAiExtractionRef = useRef<Record<string, unknown>>();
  const [eventRestoreOpen, setEventRestoreOpen] = useState(false);
  const [eventRestorePreset, setEventRestorePreset] = useState<{ environmentId: number; environmentName: string; startTime: string; endTime: string }>();
  // 「案例录入」和「相似案例匹配」是同一条工作流的进出两端，合成一个带标签页的窗口。
  const [smartAnalysisTab, setSmartAnalysisTab] = useState<'cases' | 'analysis' | undefined>(undefined);
  const abnormalCaseEditorOpen = smartAnalysisTab === 'cases';
  const abnormalCaseAnalysisOpen = smartAnalysisTab === 'analysis';
  const setAbnormalCaseEditorOpen = (next: boolean) => setSmartAnalysisTab(next ? 'cases' : undefined);
  const setAbnormalCaseAnalysisOpen = (next: boolean) => setSmartAnalysisTab(next ? 'analysis' : undefined);
  /** Case fields drafted by the assistant, awaiting the user's review before saving. */
  const [caseImportDraft, setCaseImportDraft] = useState<{ draft: Partial<AbnormalCase>; evidences: AbnormalCaseEvidence[] }>();
  const [ruleEditorRequest, setRuleEditorRequest] = useState<DisplayRuleEditorRequest>();
  const [inlineRuleSeed, setInlineRuleSeed] = useState<DisplayRuleEditorRequest>();
  const [preferredSettingsTab, setPreferredSettingsTab] = useState<'resource' | 'catalog' | 'rules'>('resource');
  const defaultRangeTaskRef = useRef<string>();

  useEffect(() => {
    let disposed = false;
    const reload = () => {
      void listRuntimeLogFormatRules().then((rules) => {
        if (!disposed) logFormatRulesRef.current = rules;
      }).catch(() => {
        // 后端尚未迁移/不可达时保留旧版内置解析器，不阻断日志定位。
      });
    };
    reload();
    const onChanged = () => reload();
    window.addEventListener('tracelens:log-format-rules-changed', onChanged);
    return () => { disposed = true; window.removeEventListener('tracelens:log-format-rules-changed', onChanged); };
  }, []);

  function enqueueLiveParsedEntries(
    taskId: string,
    entries: LogEntry[],
    issues: ParseIssue[],
    generation: number,
  ): Promise<void> {
    const renderDelayMs = entries.length > 300 ? 6 : entries.length > 120 ? 10 : 20;
    const run = liveRenderQueueRef.current.then(async () => {
      for (let index = 0; index < entries.length; index += 1) {
        if (generation !== liveRenderGenerationRef.current) return;
        if (index > 0) await new Promise<void>((resolve) => window.setTimeout(resolve, renderDelayMs));
        if (generation !== liveRenderGenerationRef.current) return;
        const entry = entries[index];
        livePendingFollowRef.current = true;
        setTasks((current) => current.map((task) => {
          if (task.id !== taskId) return task;
          const timestampNs = entry.timestampNs;
          return {
            ...task,
            entries: [...task.entries, entry],
            incrementalAddedCount: (task.incrementalAddedCount ?? 0) + 1,
            loadedEndNs: timestampNs !== undefined
              ? (task.loadedEndNs === undefined || timestampNs > task.loadedEndNs ? timestampNs : task.loadedEndNs)
              : task.loadedEndNs,
            latestImportedNs: timestampNs !== undefined
              ? (task.latestImportedNs === undefined || timestampNs > task.latestImportedNs ? timestampNs : task.latestImportedNs)
              : task.latestImportedNs,
            latestAvailableNs: timestampNs !== undefined
              ? (task.latestAvailableNs === undefined || timestampNs > task.latestAvailableNs ? timestampNs : task.latestAvailableNs)
              : task.latestAvailableNs,
            semanticAutoStatus: undefined,
            semanticAutoMessage: undefined,
          };
        }));
      }
      if (issues.length > 0 && generation === liveRenderGenerationRef.current) {
        setTasks((current) => current.map((task) => task.id === taskId ? {
          ...task,
          issues: [...task.issues, ...issues],
        } : task));
      }
    });
    liveRenderQueueRef.current = run.catch(() => undefined);
    return run;
  }

  useEffect(() => {
    const worker = new Worker(new URL('./workers/logStream.worker.ts', import.meta.url), { type: 'module' });
    streamWorkerRef.current = worker;

    worker.onmessage = (event: MessageEvent<LogStreamWorkerResponse>) => {
      const message = event.data;
      if (message.type === 'IMPORT_BATCH') {
        const buffer = importBuffersRef.current.get(message.taskId);
        if (!buffer) return;
        if (message.entries.length > 0) buffer.entryChunks.push(message.entries);
        if (message.issues.length > 0) buffer.issueChunks.push(message.issues);
        return;
      }

      if (message.type === 'IMPORT_PROGRESS') {
        const buffer = importBuffersRef.current.get(message.progress.taskId);
        if (buffer?.mergeIntoTaskId) return;
        setTasks((current) => current.map((task) => (
          task.id === message.progress.taskId
            ? { ...task, progress: message.progress }
            : task
        )));
        return;
      }

      if (message.type === 'IMPORT_COMPLETE') {
        const buffer = importBuffersRef.current.get(message.taskId);
        const entries = buffer?.entryChunks.flat() ?? [];
        const issues = buffer?.issueChunks.flat() ?? [];
        if (buffer?.remoteOperationId) {
          const query = (buffer.remoteKeyword || '').trim();
          const resultCount = query
            ? entries.filter((entry) => matchesEntry(entry, { ...cloneFilters(EMPTY_FILTERS), query })).length
            : entries.length;
          void updateLogAuditClientResult(buffer.remoteOperationId, resultCount).catch(() => undefined);
        }
        if (buffer?.mergeIntoTaskId) {
          const targetTaskId = buffer.mergeIntoTaskId;
          importBuffersRef.current.delete(message.taskId);
          const completion = importCompletionRef.current.get(message.taskId);
          importCompletionRef.current.delete(message.taskId);
          const latestIncomingNs = entries.reduce<bigint | undefined>((latest, entry) => {
            if (entry.timestampNs === undefined) return latest;
            return latest === undefined || entry.timestampNs > latest ? entry.timestampNs : latest;
          }, undefined);
          if (buffer.live) {
            const generation = buffer.liveGeneration ?? liveRenderGenerationRef.current;
            void enqueueLiveParsedEntries(targetTaskId, entries, issues, generation)
              .then(() => completion?.resolve(entries.length))
              .catch((error) => completion?.reject(error instanceof Error ? error : new Error(String(error))));
            return;
          }
          setTasks((current) => current.map((task) => (
            task.id === targetTaskId
              ? {
                  ...task,
                  entries: mergeLogEntries(task.entries, entries),
                  issues: mergeParseIssues(task.issues, issues),
                  incrementalAddedCount: (task.incrementalAddedCount ?? 0) + entries.length,
                  semanticAutoStatus: entries.length > 0 ? undefined : task.semanticAutoStatus,
                  semanticAutoMessage: entries.length > 0 ? undefined : task.semanticAutoMessage,
                }
              : task
          )));
          completion?.resolve(entries.length);
          return;
        }
        importBuffersRef.current.delete(message.taskId);
        setTasks((current) => current.map((task) => (
          task.id === message.taskId
            ? {
                ...task,
                status: 'ready',
                entries,
                issues,
                progress: task.progress ? { ...task.progress, phase: 'finalizing', percent: 100 } : undefined,
                importStrategy: message.strategy,
                fullLoaded: message.fullLoaded,
                requestedStartNs: message.requestedStartNs ? BigInt(message.requestedStartNs) : undefined,
                latestAvailableNs: message.latestTimestampNs ? BigInt(message.latestTimestampNs) : undefined,
                earliestImportedNs: message.earliestImportedNs ? BigInt(message.earliestImportedNs) : undefined,
                latestImportedNs: message.latestImportedNs ? BigInt(message.latestImportedNs) : undefined,
                selectedBytes: message.selectedBytes,
                totalFileBytes: message.totalFileBytes,
                skippedSourceCount: message.skippedSourceCount,
              }
            : task
        )));
        return;
      }

      if (message.type === 'IMPORT_ERROR') {
        const buffer = importBuffersRef.current.get(message.taskId);
        importBuffersRef.current.delete(message.taskId);
        if (buffer?.mergeIntoTaskId) {
          const completion = importCompletionRef.current.get(message.taskId);
          importCompletionRef.current.delete(message.taskId);
          const error = new Error(message.message);
          setTasks((current) => current.map((task) => task.id === buffer.mergeIntoTaskId ? {
            ...task,
            incrementalLoading: false,
            incrementalMessage: `增量日志解析失败：${message.message}`,
          } : task));
          completion?.reject(error);
          return;
        }
        setTasks((current) => current.map((task) => (
          task.id === message.taskId
            ? { ...task, status: 'error', errorMessage: message.message }
            : task
        )));
      }
    };

    return () => {
      worker.terminate();
      streamWorkerRef.current = undefined;
      importBuffersRef.current.clear();
      importCompletionRef.current.forEach(({ reject }) => reject(new Error('日志解析 Worker 已关闭')));
      importCompletionRef.current.clear();
    };
  }, []);

  useEffect(() => {
    window.localStorage.setItem(ERROR_RULE_STORAGE_KEY, JSON.stringify(errorRules));
  }, [errorRules]);

  useEffect(() => {
    let cancelled = false;
    // 必须在任何后台结果写回 localStorage 之前抓取旧浏览器数据。
    const localRules = localDisplayRuleStore.load();
    const backupRules = localDisplayRuleStore.loadBackup();
    const recoveryRules = localRules.length > 0 ? localRules : backupRules;
    void getResourceSettings()
      .then(async (settings) => {
        if (cancelled) return;
        const backendInitialized = settings.display_rules_initialized === true;
        const backendRules = Array.isArray(settings.display_rules) ? settings.display_rules as DisplayRule[] : [];

        if (backendInitialized) {
          // 已明确初始化后，数据库是唯一主数据源；即便规则为 [] 也代表用户明确清空。
          displayRulesBackendReadyRef.current = true;
          setDisplayRules(backendRules);
          localDisplayRuleStore.save(backendRules);
          return;
        }

        // 尚未初始化时优先恢复旧浏览器规则；主 key 被旧版本清空时再尝试非破坏性备份。
        if (recoveryRules.length > 0) {
          await updateResourceSettings({ display_rules: recoveryRules });
          if (cancelled) return;
          setDisplayRules(recoveryRules);
          localDisplayRuleStore.save(recoveryRules);
        }
        // 没有可恢复数据时保持“未初始化”，不要主动 PATCH []，让其它旧浏览器仍有迁移机会。
        displayRulesBackendReadyRef.current = true;
      })
      .catch(() => { displayRulesBackendReadyRef.current = true; });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    localDisplayRuleStore.save(displayRules);
    if (!displayRulesBackendReadyRef.current) return undefined;
    const timer = window.setTimeout(() => {
      void updateResourceSettings({ display_rules: displayRules }).catch(() => undefined);
    }, 250);
    return () => window.clearTimeout(timer);
  }, [displayRules]);

  useEffect(() => {
    saveMaskingRules(maskingRules);
  }, [maskingRules]);

  useEffect(() => {
    saveFoldingRules(foldingRules);
  }, [foldingRules]);

  useEffect(() => {
    window.localStorage.setItem(LOG_FOLDING_ENABLED_KEY, foldingEnabled ? '1' : '0');
  }, [foldingEnabled]);

  useEffect(() => {
    let cancelled = false;
    const localRules = loadDataExtractionRules();
    void getResourceSettings()
      .then(async (settings) => {
        if (cancelled) return;
        // null / 未返回表示“尚未完成旧浏览器规则迁移”；空数组 [] 则表示用户明确删除了全部共享提取器。
        const backendInitialized = Array.isArray(settings.data_extraction_rules);
        const backendRules = backendInitialized
          ? settings.data_extraction_rules as DataExtractionRule[]
          : [];
        if (backendInitialized) {
          dataExtractionRulesBackendReadyRef.current = true;
          setDataExtractionRules(backendRules);
          saveDataExtractionRules(backendRules);
          return;
        }
        // 升级兼容：只有数据库仍为 null 时，才允许把旧 localStorage 提取器迁移到共享数据库。
        // 这样某个用户把共享提取器删空后，其他用户的旧浏览器缓存不会把已删除规则“复活”。
        if (localRules.length > 0) {
          await updateResourceSettings({ data_extraction_rules: localRules });
          if (cancelled) return;
          setDataExtractionRules(localRules);
        }
        dataExtractionRulesBackendReadyRef.current = true;
      })
      .catch(() => {
        // 后台不可达/尚未迁移时继续使用本地缓存，不阻断日志分析。
        dataExtractionRulesBackendReadyRef.current = true;
      });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    // localStorage 仅作为离线/升级缓存；数据库中的 ResourceSettings 才是共享主数据源。
    saveDataExtractionRules(dataExtractionRules);
    if (!dataExtractionRulesBackendReadyRef.current) return undefined;
    const timer = window.setTimeout(() => {
      void updateResourceSettings({ data_extraction_rules: dataExtractionRules }).catch(() => undefined);
    }, 250);
    return () => window.clearTimeout(timer);
  }, [dataExtractionRules]);

  useEffect(() => {
    saveSemanticLabelsEnabled(semanticLabelsEnabled);
  }, [semanticLabelsEnabled]);

  useEffect(() => {
    saveRawLogModePreference(rawLogMode);
  }, [rawLogMode]);

  const ruleSourceContext = useCallback((entry: LogEntry) => {
    const task = tasks.find((item) => item.id === activeTaskId);
    if (!task?.remoteEnvironmentId || !task.remoteRequest) return {};
    let targets = [...(task.remoteRequest.fm_targets || [])].map((item) => ({ subsystem: item.subsystem, module: item.fm }));
    if (!targets.length && task.remoteRequest.subsystems.length === 1 && task.remoteRequest.fms.length === 1) {
      targets = [{ subsystem: task.remoteRequest.subsystems[0], module: task.remoteRequest.fms[0] }];
    }
    return {
      environmentId: task.remoteEnvironmentId,
      sourceFile: entry.source.fileName,
      sourceLine: entry.source.lineNumber,
      sourceTargets: targets,
    };
  }, [activeTaskId, tasks]);

  const openEntryRuleEditor = useCallback((entry: LogEntry, ruleId?: string) => {
    const suggestedKeyword = entry.functionName ?? entry.boundaryFunctionName ?? entry.message.slice(0, 80);
    setInlineRuleSeed({
      requestId: `entry-${entry.id}-${Date.now()}`,
      ruleId,
      source: 'log',
      functionName: entry.functionName ?? entry.boundaryFunctionName,
      sampleRaw: entry.raw,
      suggestedName: `${entry.functionName ?? entry.boundaryFunctionName ?? '日志片段'} 语义说明`,
      suggestedKeyword,
      suggestedScope: 'log',
      ...ruleSourceContext(entry),
    });
  }, [ruleSourceContext]);

  const openFunctionRuleEditor = useCallback((node: FunctionNode, ruleId?: string, autoDescription?: string) => {
    setInlineRuleSeed({
      requestId: `function-${node.id}-${Date.now()}`,
      ruleId,
      source: 'function',
      functionName: node.name,
      sampleRaw: node.startEntry.raw,
      suggestedName: `${node.name} 语义说明`,
      suggestedKeyword: node.name,
      suggestedScope: 'function',
      suggestedDisplayTemplate: autoDescription,
      openSemanticEditor: Boolean(autoDescription && !ruleId),
      ...ruleSourceContext(node.startEntry),
    });
  }, [ruleSourceContext]);

  const scopedTasks = useMemo(
    () => resourceWorkspaceEnvironment
      ? tasks.filter((task) => task.remoteEnvironmentId === resourceWorkspaceEnvironment.id)
      : tasks.filter((task) => !task.remoteEnvironmentId),
    [tasks, resourceWorkspaceEnvironment?.id],
  );
  const activeTask = useMemo(
    () => scopedTasks.find((task) => task.id === activeTaskId),
    [activeTaskId, scopedTasks],
  );
  /**
   * 实时采集 — keep the server's capture watches in step with the opted-in extractors.
   *
   * Runs for every state this depends on, including "monitoring stopped": the request then
   * carries `enable: false`, which stops the watchers instead of leaving an SSH tail open
   * for data nobody is watching.
   */
  const watchSyncSignature = useMemo(() => JSON.stringify({
    environmentId: preferredRemoteEnvironmentId ?? 0,
    taskId: activeTaskId,
    listening: liveListening,
    targets: (activeTask?.liveTargets || []).map((item) => `${item.subsystem}:${item.fm}`),
    categories: activeTask?.remoteRequest?.source_categories || [],
    // One sync per rule kind: 语义规则 feeds the timeline ribbon, 数据提取器 feeds the collector.
    // They never share a watch, so an empty kind must still be synced to stop its tail.
    semantic: displayRules.filter((rule) => rule.enabled && rule.liveWatch === true).map((rule) => rule.id),
    extraction: dataExtractionRules.filter((rule) => rule.enabled && rule.liveCapture === true).map((rule) => rule.id),
  }), [preferredRemoteEnvironmentId, activeTaskId, liveListening, activeTask?.liveTargets, activeTask?.remoteRequest, displayRules, dataExtractionRules]);
  useEffect(() => {
    const state = JSON.parse(watchSyncSignature) as {
      environmentId: number;
      listening: boolean;
      targets: string[];
      categories: string[];
      semantic: string[];
      extraction: string[];
    };
    if (!state.environmentId) return;
    const targets = state.targets.map((item) => {
      const [subsystem, fm] = item.split(':');
      return { subsystem, fm, kind: 'normal' as const };
    });
    for (const [kind, ruleIds] of [['semantic', state.semantic], ['extraction', state.extraction]] as const) {
      const enable = state.listening && targets.length > 0 && ruleIds.length > 0;
      void syncCaptureWatches({
        environment_id: state.environmentId,
        kind,
        rule_ids: enable ? [...ruleIds] : [],
        targets,
        source_categories: state.categories,
        enable,
      }).catch(() => undefined);
    }
  }, [watchSyncSignature]);
  const activeTaskLoadedRange = useMemo<TimeRangeFilter | undefined>(() => {
    if (!activeTask?.remoteRequest) return undefined;
    const requestRange = requestTimeRangeNs(activeTask.remoteRequest);
    const startNs = activeTask.loadedStartNs ?? requestRange?.startNs;
    const endNs = activeTask.loadedEndNs ?? requestRange?.endNs;
    return startNs !== undefined && endNs !== undefined && endNs >= startNs ? { startNs, endNs } : undefined;
  }, [activeTask?.loadedEndNs, activeTask?.loadedStartNs, activeTask?.remoteRequest]);
  // Real-time monitoring is deliberately ephemeral and must never survive a
  // browser refresh / Vite Fast Refresh.  Explicitly reset it on mount and abort
  // the streaming request before the page is discarded so the backend generator
  // can release its SSH sessions promptly.
  useEffect(() => {
    liveSessionRef.current += 1;
    liveAbortRef.current?.abort();
    liveAbortRef.current = undefined;
    setLiveListening(false);
    setLiveConnectionStatus('off');
    setLiveAddedCount(0);
    setLiveMessage('');
    liveBoundTaskIdRef.current = undefined;

    const stopLiveOnPageExit = () => {
      liveSessionRef.current += 1;
      liveAbortRef.current?.abort();
      liveAbortRef.current = undefined;
    };
    window.addEventListener('pagehide', stopLiveOnPageExit);
    window.addEventListener('beforeunload', stopLiveOnPageExit);
    return () => {
      stopLiveOnPageExit();
      window.removeEventListener('pagehide', stopLiveOnPageExit);
      window.removeEventListener('beforeunload', stopLiveOnPageExit);
    };
  }, []);

  // Real-time monitoring is deliberately ephemeral. Leaving log locator,
  // switching tasks, or leaving the log workspace always tears down the live
  // connection so a hidden page can never keep consuming SSH/network resources.
  useEffect(() => {
    // 只在离开日志工作区时停：原始日志模式现在能显示实时追加的原文，
    // 过去「开原始日志就停实时监听」把两个功能变成了互斥。
    if (workspacePage === 'logs') return;
    if (liveListening) setLiveListening(false);
  }, [workspacePage, liveListening]);

  useEffect(() => {
    if (!liveListening) return;
    const boundTaskId = liveBoundTaskIdRef.current;
    if (boundTaskId && activeTaskId && activeTaskId !== boundTaskId) setLiveListening(false);
  }, [activeTaskId, liveListening]);

  useEffect(() => {
    if (!liveListening) {
      liveAbortRef.current?.abort();
      liveAbortRef.current = undefined;
      liveRenderGenerationRef.current += 1;
      liveRenderQueueRef.current = Promise.resolve();
      setLiveConnectionStatus((current) => current === 'error' ? current : 'off');
      liveBoundTaskIdRef.current = undefined;
      return;
    }
    // 原始日志模式不再排除在外：RawLogView 会直接显示流进来的原文，
    // 于是「看原文」和「实时监听」可以同时存在（过去开一个就停另一个）。
    if (workspacePage !== 'logs' || !activeTask?.remoteEnvironmentId || !activeTask.remoteRequest || activeTask.status !== 'ready') {
      setLiveMessage('请选择一个环境和一个组件后开启实时监听。');
      setLiveListening(false);
      return;
    }

    const taskSnapshot = activeTask;
    const remoteRequest = activeTask.remoteRequest;
    const remoteEnvironmentId = activeTask.remoteEnvironmentId;
    const explicitLiveTargets = taskSnapshot.liveTargets ?? remoteRequest.fm_targets ?? [];
    if (explicitLiveTargets.length !== 1) {
      const message = explicitLiveTargets.length > 1
        ? `实时日志监听仅支持单模块，当前任务选择了 ${explicitLiveTargets.length} 个模块，请只选择一个模块后重新查询。`
        : '实时日志监听仅支持单模块，请先选择一个模块并完成日志查询。';
      setLiveConnectionStatus('error');
      setLiveMessage(message);
      setLiveListening(false);
      return;
    }
    const liveTarget = explicitLiveTargets[0];
    const liveRequest: LogWindowRequest = {
      ...remoteRequest,
      subsystems: [liveTarget.subsystem],
      fms: [liveTarget.fm],
      fm_targets: [{ ...liveTarget }],
    };
    const controller = new AbortController();
    liveAbortRef.current?.abort();
    liveAbortRef.current = controller;
    const session = ++liveSessionRef.current;
    setLiveConnectionStatus('connecting');
    setLiveMessage('正在建立实时日志通道…');
    setLiveAddedCount(0);

    const handleEvent = (event: LiveLogStreamEvent) => {
      if (session !== liveSessionRef.current || controller.signal.aborted) return;
      if (event.type === 'ready') {
        setLiveConnectionStatus('connected');
        setLiveMessage(event.transport === 'ssh_tail'
          ? `SSH 长连接已建立 · 持续监听 ${event.artifacts.length} 个当前日志文件`
          : `兼容监听已建立 · ${event.interval_seconds} 秒检查新增日志`);
        return;
      }
      if (event.type === 'reconnecting') {
        setLiveConnectionStatus('connecting');
        setLiveMessage(`SSH 长连接中断 · ${Math.ceil(event.retry_in_seconds)} 秒后自动重连（第 ${event.attempt} 次）`);
        return;
      }
      if (event.type === 'reconnected') {
        setLiveConnectionStatus('connected');
        setLiveMessage('SSH 长连接已自动恢复 · 正在持续监听新增日志');
        return;
      }
      if (event.type === 'fallback') {
        setLiveConnectionStatus('connected');
        setLiveMessage(event.message || '远端不支持 tail -F，已切换兼容监听模式');
        return;
      }
      if (event.type === 'heartbeat') {
        setLiveConnectionStatus('connected');
        return;
      }
      if (event.type === 'error') {
        setLiveConnectionStatus('error');
        setLiveMessage(event.message || '实时日志通道异常');
        setLiveListening(false);
        return;
      }
      if (event.type !== 'logs' || !event.chunks.length) return;
      const text = event.chunks.map((chunk) => chunk.text).join('');
      if (!text) return;
      livePendingFollowRef.current = livePendingFollowRef.current || liveFollowTailRef.current;
      const blob = new Blob([text], { type: 'text/plain' });
      liveImportQueueRef.current = liveImportQueueRef.current
        .then(() => importLiveRemoteBlob(taskSnapshot, blob, `live-${Date.now()}.log`))
        .then((added) => {
          if (session !== liveSessionRef.current || controller.signal.aborted) return;
          if (added === 0) livePendingFollowRef.current = false;
          setLiveAddedCount((current) => current + added);
          setLiveConnectionStatus('connected');
          setLiveMessage(`最近推送 ${event.lines} 行 · 新增 ${added} 条解析日志`);
        })
        .catch((exc) => {
          if (session !== liveSessionRef.current || controller.signal.aborted) return;
          setLiveConnectionStatus('error');
          setLiveMessage(`实时日志解析失败：${exc instanceof Error ? exc.message : String(exc)}`);
        });
    };

    void streamLiveLogWindow(remoteEnvironmentId, liveRequest, {
      signal: controller.signal,
      onEvent: handleEvent,
    }).then(() => {
      if (session !== liveSessionRef.current || controller.signal.aborted) return;
      setLiveConnectionStatus('error');
      setLiveMessage('实时日志连接已结束。');
      setLiveListening(false);
    }).catch((exc) => {
      if (controller.signal.aborted || session !== liveSessionRef.current || (exc instanceof DOMException && exc.name === 'AbortError')) return;
      setLiveConnectionStatus('error');
      setLiveMessage(exc instanceof Error ? exc.message : String(exc));
      setLiveListening(false);
    });

    return () => {
      controller.abort();
      if (liveAbortRef.current === controller) liveAbortRef.current = undefined;
    };
  }, [liveListening, workspacePage, activeTask?.id, rawLogMode]);

  const sourceNames = activeTask?.sourceNames ?? [];
  const parsed = useMemo(() => ({
    entries: activeTask?.entries ?? [],
    issues: activeTask?.issues ?? [],
  }), [activeTask?.entries, activeTask?.issues]);

  useEffect(() => {
    const task = tasks.find((item) => (
      item.status === 'ready'
      && item.remoteEnvironmentId
      && item.remoteRequest
      && !item.semanticAutoStatus
    ));
    if (!task || !task.remoteEnvironmentId || !task.remoteRequest) return;

    const targets = [...(task.remoteRequest.fm_targets || [])].map((item) => ({ subsystem: item.subsystem, module: item.fm }));
    if (!targets.length && task.remoteRequest.subsystems.length === 1 && task.remoteRequest.fms.length === 1) {
      targets.push({ subsystem: task.remoteRequest.subsystems[0], module: task.remoteRequest.fms[0] });
    }
    const unique = new Map<string, { key: string; source_file: string; source_line?: number; function_name?: string }>();
    for (const entry of task.entries) {
      const sourceFile = entry.source.fileName;
      const functionName = entry.boundaryFunctionName || entry.functionName;
      if (!sourceFile || !functionName) continue;
      // 服务端只接受 .py 源码；解析不出 .py 路径的日志（例如只有模块名的日志）会让整个
      // 批次 400，自动源码语义一个也拿不到。这里先过滤，再让下面的空集分支如实说明原因。
      if (!sourceFile.trim().toLowerCase().endsWith('.py')) continue;
      const key = semanticSourceKey(sourceFile, functionName);
      if (!key || unique.has(key)) continue;
      unique.set(key, { key, source_file: sourceFile, source_line: entry.source.lineNumber, function_name: functionName });
      if (unique.size >= 500) break;
    }

    if (!targets.length || unique.size === 0) {
      setTasks((current) => current.map((item) => item.id === task.id ? { ...item, semanticAutoStatus: 'ready', autoSemantics: {}, semanticAutoMessage: '当前任务没有可自动识别的源码函数（需要日志带 .py 源码位置）' } : item));
      return;
    }

    setTasks((current) => current.map((item) => item.id === task.id ? { ...item, semanticAutoStatus: 'loading', semanticAutoMessage: `正在识别 ${unique.size} 个源码函数语义` } : item));
    void recognizeSemanticSourcesBatch(task.remoteEnvironmentId, { items: [...unique.values()], fm_targets: targets })
      .then((payload) => {
        const autoSemantics = Object.fromEntries(payload.results.map((result) => [result.key, result]));
        setTasks((current) => current.map((item) => item.id === task.id ? {
          ...item,
          semanticAutoStatus: 'ready',
          autoSemantics,
          semanticAutoMessage: payload.results.length ? `源码语义 ${payload.results.length} 个` : undefined,
        } : item));
      })
      .catch((error) => {
        setTasks((current) => current.map((item) => item.id === task.id ? { ...item, semanticAutoStatus: 'error', semanticAutoMessage: error instanceof Error ? error.message : String(error) } : item));
      });
  }, [tasks]);
  const classifiedEntries = useMemo(() => parsed.entries.map((entry) => {
    const severity = detectSeverity(entry.level, entry.message, errorRules);
    return severity === entry.severity ? entry : { ...entry, severity };
  }), [errorRules, parsed.entries]);
  const unmaskedEntries = useMemo(() => classifiedEntries.filter((entry) => !isEntryMasked(entry, maskingRules)), [classifiedEntries, maskingRules]);
  const sortedEntries = useMemo(() => unmaskedEntries.slice().sort(compareEntries), [unmaskedEntries]);
  // 折叠规则变化时重建函数树；时间窗和耗时筛选只在既有树上做轻量筛选。
  const allProcesses = useMemo(() => buildProcessTimelines(sortedEntries, effectiveFoldingRules), [effectiveFoldingRules, sortedEntries]);
  const allCrossComponentTraces = useMemo(() => buildCrossComponentTraces(sortedEntries, effectiveFoldingRules), [effectiveFoldingRules, sortedEntries]);
  const assistantFunctionFoldSummary = useMemo(() => {
    type FoldStat = {
      component: string;
      function: string;
      count: number;
      completeCount: number;
      totalMs: number;
      minMs: number;
      maxMs: number;
      logCount: number;
      errorCount: number;
      warningCount: number;
      evidence: string[];
      samples: Array<{ start: string; end: string; duration_ms: number; source: string }>;
    };
    const stats = new Map<string, FoldStat>();
    const recordCall = (call: FunctionNode) => {
      const duration = durationNs(call);
      const durationMs = duration === undefined ? undefined : Number(duration) / 1_000_000;
      const key = `${call.component}::${call.name}`;
      const current = stats.get(key) || {
        component: call.component || '-', function: call.name || '-', count: 0, completeCount: 0,
        totalMs: 0, minMs: Number.POSITIVE_INFINITY, maxMs: 0,
        logCount: 0, errorCount: 0, warningCount: 0, evidence: [], samples: [],
      };
      current.count += 1;
      if (call.endEntry) current.completeCount += 1;
      if (durationMs !== undefined && Number.isFinite(durationMs) && durationMs >= 0) {
        current.totalMs += durationMs;
        current.minMs = Math.min(current.minMs, durationMs);
        current.maxMs = Math.max(current.maxMs, durationMs);
        if (current.samples.length < 2) {
          current.samples.push({
            start: call.startEntry.timestamp,
            end: call.endEntry?.timestamp || '',
            duration_ms: Number(durationMs.toFixed(6)),
            source: `${call.source.fileName || '-'}:${call.source.lineNumber || '-'}`,
          });
        }
      }
      const directLogs = call.children.flatMap((child) => child.kind === 'log' ? [child.entry] : []);
      current.logCount += directLogs.length;
      current.errorCount += directLogs.filter((entry) => entry.severity === 'error').length;
      current.warningCount += directLogs.filter((entry) => entry.severity === 'warning').length;
      const evidenceCandidates = [
        ...directLogs.filter((entry) => entry.severity === 'error'),
        ...directLogs.filter((entry) => entry.severity === 'warning'),
        ...directLogs.slice(0, 1),
      ];
      for (const entry of evidenceCandidates) {
        if (current.evidence.length >= 2) break;
        const compact = `${entry.timestamp} ${entry.level} ${String(entry.message || entry.raw || '').replace(/\s+/g, ' ').slice(0, 180)}`.trim();
        if (compact && !current.evidence.includes(compact)) current.evidence.push(compact);
      }
      stats.set(key, current);
    };
    const walkNode = (node: FunctionNode) => {
      if (node.origin === 'repeated') {
        const calls = node.children.filter((child): child is FunctionNode => child.kind === 'function');
        calls.forEach((call) => {
          recordCall(call);
          call.children.forEach((child) => { if (child.kind === 'function') walkNode(child); });
        });
        return;
      }
      recordCall(node);
      node.children.forEach((child) => { if (child.kind === 'function') walkNode(child); });
    };
    allProcesses.forEach((process) => process.threads.forEach((thread) => thread.traces.forEach((trace) => {
      trace.items.forEach((item) => { if (item.kind === 'function') walkNode(item); });
    })));
    return Array.from(stats.values())
      .map((item) => ({
        component: item.component,
        function: item.function,
        count: item.count,
        complete_count: item.completeCount,
        log_count: item.logCount,
        error_count: item.errorCount,
        warning_count: item.warningCount,
        avg_ms: Number((item.totalMs / Math.max(item.completeCount, 1)).toFixed(6)),
        min_ms: Number((Number.isFinite(item.minMs) ? item.minMs : 0).toFixed(6)),
        max_ms: Number(item.maxMs.toFixed(6)),
        evidence: item.evidence,
        samples: item.samples,
      }))
      .sort((left, right) => (
        right.error_count - left.error_count
        || right.warning_count - left.warning_count
        || right.count - left.count
        || right.max_ms - left.max_ms
      ))
      .slice(0, 28);
  }, [allProcesses]);
  const durationMatchedEntryIds = useMemo(
    () => collectDurationMatchedEntryIds(allProcesses, filters.durationRange),
    [allProcesses, filters.durationRange],
  );
  const durationFilterActive = durationMatchedEntryIds !== undefined;
  const timeScopedEntries = useMemo(
    () => sliceEntriesByRange(sortedEntries, filters.timeRange),
    [filters.timeRange, sortedEntries],
  );
  const durationScopedEntries = useMemo(
    () => durationMatchedEntryIds
      ? timeScopedEntries.filter((entry) => durationMatchedEntryIds.has(entry.id))
      : timeScopedEntries,
    [durationMatchedEntryIds, timeScopedEntries],
  );
  // Timeline 模块复选框同时控制下方日志可见性；这里只过滤当前已加载数据，绝不重新请求远端。
  const timelineVisibleEntries = useMemo(
    () => timelineHiddenComponents.size === 0
      ? durationScopedEntries
      : durationScopedEntries.filter((entry) => !timelineHiddenComponents.has(entry.component)),
    [durationScopedEntries, timelineHiddenComponents],
  );
  const processScopeIndex = useMemo(() => createProcessScopeIndex(allProcesses), [allProcesses]);
  const crossTraceScopeIndex = useMemo(
    () => createCrossTraceScopeIndex(allCrossComponentTraces),
    [allCrossComponentTraces],
  );
  const processes = useMemo(
    () => durationFilterActive
      ? scopeProcessesFromEntries(allProcesses, processScopeIndex, timelineVisibleEntries, true)
      : scopeProcessesForWindow(allProcesses, processScopeIndex, timelineVisibleEntries, filters.timeRange),
    [allProcesses, durationFilterActive, filters.timeRange, processScopeIndex, timelineVisibleEntries],
  );
  const crossComponentTraces = useMemo(
    () => durationFilterActive
      ? scopeCrossTracesFromEntries(allCrossComponentTraces, crossTraceScopeIndex, timelineVisibleEntries, true)
      : scopeCrossTracesForWindow(allCrossComponentTraces, crossTraceScopeIndex, timelineVisibleEntries, filters.timeRange),
    [allCrossComponentTraces, crossTraceScopeIndex, durationFilterActive, filters.timeRange, timelineVisibleEntries],
  );
  const renderFilters = useMemo<Filters>(() => ({ ...filters }), [filters]);
  const visibleCrossComponentTraces = useMemo(() => crossComponentTraces.filter(
    (trace) => trace.components.length > 1 || trace.sourceFiles.length > 1,
  ), [crossComponentTraces]);
  const crossMethodGroups = useMemo(
    () => buildCrossMethodTraceGroups(visibleCrossComponentTraces),
    [visibleCrossComponentTraces],
  );
  const visibleProcesses = processes;

  const mergedTimelineTraces = useMemo(() => {
    const crossEntryIds = new Set(visibleCrossComponentTraces.flatMap((trace) => trace.entries.map((entry) => entry.id)));
    const standaloneTraces = visibleProcesses
      .flatMap((process) => process.threads.flatMap((thread) => thread.traces))
      .filter((trace) => !trace.entries.some((entry) => crossEntryIds.has(entry.id)));
    return [...visibleCrossComponentTraces, ...standaloneTraces]
      .sort((left, right) => compareEntries(left.entries[0], right.entries[0]));
  }, [visibleCrossComponentTraces, visibleProcesses]);

  const ganttProcesses = allProcesses;

  const methodGroupsByProcess = useMemo(
    () => new Map(visibleProcesses.map((process) => [process.id, buildMethodTraceGroups(process)])),
    [visibleProcesses],
  );

  const allThreads = useMemo(() => visibleProcesses.flatMap((process) => process.threads), [visibleProcesses]);
  const selectedCrossTrace = useMemo(
    () => visibleCrossComponentTraces.find((trace) => trace.id === selectedCrossTraceId),
    [selectedCrossTraceId, visibleCrossComponentTraces],
  );
  const selectedProcess = useMemo(
    () => visibleProcesses.find((process) => process.id === selectedProcessId),
    [selectedProcessId, visibleProcesses],
  );
  const selectedThread = useMemo(
    () => allThreads.find((thread) => thread.id === selectedThreadId),
    [allThreads, selectedThreadId],
  );
  const selectedTrace = useMemo(
    () => selectedThread?.traces.find((trace) => trace.id === selectedTraceId),
    [selectedThread, selectedTraceId],
  );
  const visibleTraces = useMemo(() => {
    if (selectedCrossTrace) return [selectedCrossTrace];
    if (selectedTrace) return [selectedTrace];
    if (selectedThread) return selectedThread.traces;
    if (selectedProcess) return selectedProcess.threads.flatMap((thread) => thread.traces);
    if (timelineGroupingMode === 'merged') return mergedTimelineTraces;
    return visibleProcesses.flatMap((process) => process.threads.flatMap((thread) => thread.traces));
  }, [mergedTimelineTraces, selectedCrossTrace, selectedProcess, selectedThread, selectedTrace, timelineGroupingMode, visibleProcesses]);

  const flatEntries = useMemo(() => {
    const scoped = selectedCrossTrace
      ? selectedCrossTrace.entries
      : selectedTrace
        ? selectedTrace.entries
        : selectedThread
          ? selectedThread.traces.flatMap((trace) => trace.entries)
          : selectedProcess
            ? selectedProcess.threads.flatMap((thread) => thread.traces.flatMap((trace) => trace.entries))
            : timelineVisibleEntries;
    return Array.from(new Map(scoped.map((entry) => [entry.id, entry])).values());
  }, [selectedCrossTrace, selectedProcess, selectedThread, selectedTrace, timelineVisibleEntries]);


  const scopedFilteredEntries = useMemo(() => {
    const entries = flatEntries.slice().sort(compareEntries).filter((entry) => matchesEntry(entry, renderFilters));
    return logSortOrder === 'desc' ? entries.reverse() : entries;
  }, [flatEntries, logSortOrder, renderFilters]);
  const timestampNavigationEntries = useMemo(() => {
    const displayIndex = new Map(scopedFilteredEntries.map((entry, index) => [entry.id, index]));
    return scopedFilteredEntries
      .slice()
      .sort(compareEntries)
      .flatMap((entry) => entry.timestampNs === undefined ? [] : [{ entry, index: displayIndex.get(entry.id) ?? 0, timestampNs: entry.timestampNs }]);
  }, [scopedFilteredEntries]);
  const componentTimestampNavigationEntries = useMemo(() => {
    const baseFilters: Filters = { ...renderFilters, components: new Set() };
    const chronological = timelineVisibleEntries.slice().sort(compareEntries).filter((entry) => matchesEntry(entry, baseFilters));
    const visible = logSortOrder === 'desc' ? chronological.slice().reverse() : chronological;
    const displayIndex = new Map(visible.map((entry, index) => [entry.id, index]));
    const grouped = new Map<string, Array<{ entry: LogEntry; index: number; timestampNs: bigint }>>();
    chronological.forEach((entry) => {
      if (entry.timestampNs === undefined) return;
      const rows = grouped.get(entry.component) ?? [];
      rows.push({ entry, index: displayIndex.get(entry.id) ?? 0, timestampNs: entry.timestampNs });
      grouped.set(entry.component, rows);
    });
    return { visible, grouped };
  }, [logSortOrder, renderFilters, timelineVisibleEntries]);
  const logPageCount = Math.max(1, Math.ceil(scopedFilteredEntries.length / logPageSize));
  const safeLogPage = Math.min(logPage, logPageCount);

  useEffect(() => {
    const atPaginationTail = logSortOrder === 'desc' ? safeLogPage === 1 : safeLogPage === logPageCount;
    // 实时监听不使用时间筛选；是否自动跟随只由用户当前是否停留在日志尾页决定。
    liveFollowTailRef.current = atPaginationTail;
  }, [logPageCount, logSortOrder, safeLogPage]);

  useEffect(() => {
    if (!liveListening || !livePendingFollowRef.current) return;
    livePendingFollowRef.current = false;
    setLogPage(logSortOrder === 'desc' ? 1 : logPageCount);
    if (logSortOrder === 'asc') {
      requestAnimationFrame(() => requestAnimationFrame(() => {
        const node = logScrollRef.current;
        if (node) node.scrollTo({ top: node.scrollHeight, behavior: 'auto' });
      }));
    }
  }, [activeTask?.entries.length, liveListening, logPageCount, logSortOrder]);

  useEffect(() => {
    if (!liveListening) return;
    // 实时监听从 current 日志 EOF 开始订阅，只消费新追加内容。
    // 清除任务内时间筛选，避免旧的查询窗口把新增日志隐藏掉。
    setFilters((current) => current.timeRange ? { ...current, timeRange: undefined } : current);
    setCustomStartTime('');
    setCustomEndTime('');
  }, [liveListening]);

  const paginatedEntries = useMemo(() => {
    const start = (safeLogPage - 1) * logPageSize;
    return scopedFilteredEntries.slice(start, start + logPageSize);
  }, [logPageSize, safeLogPage, scopedFilteredEntries]);
  const timelinePageIndex = useMemo(() => createTimelinePageIndex(visibleTraces), [visibleTraces]);
  const paginatedVisibleTraces = useMemo(
    () => buildPaginatedTraces(timelinePageIndex, paginatedEntries.slice().sort(compareEntries)),
    [paginatedEntries, timelinePageIndex],
  );

  const levels = useMemo(() => Array.from(new Set(timelineVisibleEntries.map((entry) => entry.level))), [timelineVisibleEntries]);
  const components = useMemo(() => Array.from(new Set(timelineVisibleEntries.map((entry) => entry.component))), [timelineVisibleEntries]);
  const modes = useMemo(() => Array.from(new Set(timelineVisibleEntries.map((entry) => entry.mode))), [timelineVisibleEntries]);
  const allItems = useMemo(() => paginatedVisibleTraces.flatMap((trace) => trace.items), [paginatedVisibleTraces]);
  const allFunctionIds = useMemo(() => collectFunctionIds(allItems), [allItems]);
  const totalFunctions = useMemo(
    () => processes.reduce(
      (processTotal, process) => processTotal + process.threads.reduce(
        (threadTotal, thread) => threadTotal + thread.traces.reduce(
          (traceTotal, trace) => traceTotal + functionCount(trace.items),
          0,
        ),
        0,
      ),
      0,
    ),
    [processes],
  );
  const errorTraceCount = useMemo(
    () => processes.reduce(
      (total, process) => total + process.threads.reduce(
        (threadTotal, thread) => threadTotal + thread.traces.filter(traceHasError).length,
        0,
      ),
      0,
    ),
    [processes],
  );
  const errorEntryCount = useMemo(
    () => timelineVisibleEntries.filter((entry) => entry.severity === 'error').length,
    [timelineVisibleEntries],
  );
  const metricEntries = useMemo(() => {
    const entries = timelineVisibleEntries.filter((entry) => matchesEntry(entry, renderFilters)).slice().sort(compareEntries);
    return logSortOrder === 'desc' ? entries.reverse() : entries;
  }, [logSortOrder, renderFilters, timelineVisibleEntries]);
  const assistantDataPatternSummary = useMemo(() => {
    type PatternGroup = { pattern: string; count: number; samples: string[]; levels: Set<string>; components: Set<string> };
    const groups = new Map<string, PatternGroup>();
    const normalize = (value: string) => value
      .replace(/0x[0-9a-fA-F]+/g, '<HEX>')
      .replace(/[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}/g, '<UUID>')
      .replace(/[-+]?\d+(?:\.\d+)?(?:e[-+]?\d+)?/gi, '<NUM>')
      .replace(/\s+/g, ' ')
      .trim();
    metricEntries.forEach((entry) => {
      const message = String(entry.message || entry.raw || '').replace(/\s+/g, ' ').trim();
      if (!message) return;
      const pattern = normalize(message).slice(0, 420);
      if (!pattern || pattern.length < 4) return;
      const group = groups.get(pattern) || { pattern, count: 0, samples: [], levels: new Set<string>(), components: new Set<string>() };
      group.count += 1;
      group.levels.add(String(entry.level || entry.severity || ''));
      if (entry.component) group.components.add(String(entry.component));
      if (group.samples.length < 3 && !group.samples.includes(message.slice(0, 520))) group.samples.push(message.slice(0, 520));
      groups.set(pattern, group);
    });
    return Array.from(groups.values())
      .filter((item) => item.count >= 2)
      .sort((a, b) => b.count - a.count)
      .slice(0, 16)
      .map((item) => ({
        pattern: item.pattern,
        count: item.count,
        levels: Array.from(item.levels).filter(Boolean).slice(0, 6),
        components: Array.from(item.components).filter(Boolean).slice(0, 6),
        samples: item.samples,
      }));
  }, [metricEntries]);
  const navigableErrorEntries = useMemo(
    () => metricEntries.filter((entry) => entry.severity === 'error'),
    [metricEntries],
  );

  useEffect(() => {
    const handler = (event: Event) => {
      const custom = event as CustomEvent<{ context?: Record<string, unknown> }>;
      const context = custom.detail?.context;
      if (!context) return;

      const pageLabels: Record<string, string> = {
        resources: '环境资源', logs: '日志定位', reports: 'CPD 测校报告', atlog: '用例分析',
        data: '数据提取', knowledge: '案例分析', audit: '操作审计', 'platform-settings': '平台设置', tools: '工具中心',
      };
      context.page = workspacePage;
      context.page_label = pageLabels[workspacePage] || workspacePage;

      const activeEnvironment = workspacePage === 'reports' ? reportEnvironment : resourceWorkspaceEnvironment;
      if (activeEnvironment) {
        context.environment_id = activeEnvironment.id;
        context.environment_name = activeEnvironment.name;
      } else if (activeTask?.remoteEnvironmentId) {
        context.environment_id = activeTask.remoteEnvironmentId;
      }

      if (workspacePage === 'logs' && resourceWorkspaceFeature === 'resource' && resourceWorkspaceEnvironment) {
        context.page = 'environment-resource-detail';
        context.page_label = '环境资源';
        context.environment_resource = {
          environment_id: resourceWorkspaceEnvironment.id,
          environment_name: resourceWorkspaceEnvironment.name,
          upper_machine: resourceWorkspaceEnvironment.upper_machine.host,
          upper_user: resourceWorkspaceEnvironment.upper_machine.username,
          lower_machine_count: resourceWorkspaceEnvironment.lower_machines.length,
          lower_machines: resourceWorkspaceEnvironment.lower_machines.slice(0, 12).map((item) => ({
            id: item.id, host: item.host, station_name: item.station_name || '', version: item.software_version || '',
          })),
          software_version: resourceWorkspaceEnvironment.software_version || '',
          version_mismatch: Boolean(resourceWorkspaceEnvironment.version_mismatch),
        };
      } else if (workspacePage === 'logs') {
        const currentSelectedEntry = selectedEntry || activeTask?.entries.find((entry) => entry.id === focusedEntryId);
        const request = activeTask?.remoteRequest;
        const viewRange = filters.timeRange
          ? { start: nsToTimestampText(filters.timeRange.startNs), end: nsToQueryEndTimestampText(filters.timeRange.endNs) }
          : undefined;
        const loadedRange = activeTaskLoadedRange
          ? { start: nsToTimestampText(activeTaskLoadedRange.startNs), end: nsToQueryEndTimestampText(activeTaskLoadedRange.endNs) }
          : undefined;
        // The assistant must see what the user is already looking at before it
        // starts another remote query.  Build a bounded excerpt from the *rendered
        // current page* (not merely the task request).  Include the selected row,
        // ERROR rows, the page head and tail so "分析当前页面这些日志" can be
        // answered directly from existing evidence.
        const pageEvidenceCandidates = [
          ...(currentSelectedEntry ? [currentSelectedEntry] : []),
          ...paginatedEntries.filter((entry) => entry.severity === 'error').slice(0, 8),
          ...paginatedEntries.filter((entry) => entry.severity === 'warning').slice(0, 2),
          ...paginatedEntries.slice(0, 3),
          ...paginatedEntries.slice(-2),
        ];
        const pageEvidence = Array.from(new Map(pageEvidenceCandidates.map((entry) => [entry.id, entry])).values())
          .slice(0, 12)
          .map((entry) => {
            const subsystem = entry.logSubsystem || '';
            const moduleName = entry.logModule || '';
            const target = subsystem || moduleName ? `${subsystem || '-'}/${moduleName || '-'}` : '-';
            const source = `${entry.source?.fileName || entry.sourceFile || '-'}:${entry.source?.lineNumber || entry.lineNumber || '-'}`;
            return `${entry.timestamp} | ${entry.level} | ${target} | component=${entry.component || '-'} | ${source} | ${String(entry.message || entry.raw || '').replace(/\s+/g, ' ').slice(0, 260)}`;
          });
        // RemoteLogQueryPanel also contributes its live form selection to this object.
        // Merge it instead of replacing it so the AI receives the component/subsystem
        // the user is currently looking at, not only the snapshot used when the task
        // was originally created. This removes the model-side need to reconstruct
        // fm_targets from display text.
        const liveLocator = context.log_locator && typeof context.log_locator === 'object'
          ? context.log_locator as Record<string, unknown>
          : {};
        context.log_locator = {
          task_id: activeTask?.id || '',
          task_name: activeTask?.name || '',
          task_status: activeTask?.status || 'none',
          source: activeTask?.remoteEnvironmentId ? 'environment' : activeTask ? 'local' : 'none',
          environment_id: activeTask?.remoteEnvironmentId || null,
          query_time_range: request ? { start: request.start_time, end: request.end_time } : null,
          view_time_range: viewRange || null,
          loaded_time_range: loadedRange || null,
          source_categories: request?.source_categories || [],
          targets: (request?.fm_targets || []).slice(0, 12).map((item) => `${item.subsystem}/${item.fm}${item.kind === 'executor' ? ':executor' : ''}`),
          fm_targets: (request?.fm_targets || []).slice(0, 12).map((item) => ({ subsystem: item.subsystem, fm: item.fm, kind: item.kind || 'normal' })),
          component_name: (request?.fm_targets || []).length === 1 ? String(request?.fm_targets?.[0]?.fm || '') : '',
          subsystem_name: (request?.fm_targets || []).length === 1 ? String(request?.fm_targets?.[0]?.subsystem || '') : '',
          keyword: request?.keyword || '',
          ...liveLocator,
          local_filter_query: filters.query || '',
          errors_only: filters.errorsOnly,
          level_filters: Array.from(filters.levels),
          component_filters: Array.from(filters.components).slice(0, 16),
          mode_filters: Array.from(filters.modes).slice(0, 16),
          sort_order: logSortOrder,
          live_monitoring: liveListening,
          live_status: liveConnectionStatus,
          result_count: metricEntries.length,
          loaded_entry_count: activeTask?.entries.length || 0,
          error_count: errorEntryCount,
          source_files: (activeTask?.sourceNames || []).slice(0, 8),
          selected_entry: currentSelectedEntry ? {
            id: currentSelectedEntry.id,
            trace_id: currentSelectedEntry.rpc.traceId,
            span_id: currentSelectedEntry.rpc.spanId,
            error_code: currentSelectedEntry.runEvent?.displayCode || currentSelectedEntry.runEvent?.currentEventCode || '',
            source_file: currentSelectedEntry.remoteSourcePath || currentSelectedEntry.sourceFile,
            line_number: currentSelectedEntry.lineNumber,
            time: currentSelectedEntry.timestamp,
            level: currentSelectedEntry.level,
            component: currentSelectedEntry.component || '',
            source: `${currentSelectedEntry.source.fileName}:${currentSelectedEntry.source.lineNumber}`,
            message: String(currentSelectedEntry.message || currentSelectedEntry.raw || '').replace(/\s+/g, ' ').slice(0, 500),
          } : null,
          displayed_page_number: safeLogPage,
          displayed_page_size: logPageSize,
          displayed_page_row_count: paginatedEntries.length,
          displayed_page_sampled: pageEvidence.length < paginatedEntries.length,
          displayed_evidence_source: 'rendered-current-page',
          displayed_evidence: pageEvidence,
          // 结构化版本（同一批条目）。字符串版给模型读，这一份给后端把案例**绑定到
          // 被分析的这份日志**：保存案例时不需要模型再抄一遍日志行，也不依赖第二次
          // AI 交互。字段与知识库举证契约一致，指纹缺失时由后端补齐。
          displayed_evidence_entries: pageEvidenceCandidates
            .slice(0, 12)
            .map((entry) => {
              const evidence = createAbnormalEvidence(entry, errorRules);
              return {
                ...evidence,
                raw: String(evidence.raw || '').slice(0, 1200),
                message: String(evidence.message || '').slice(0, 600),
                evidence_kind: 'runtime_log',
              };
            }),
          displayed_entries: paginatedEntries.slice(0, 80).map((entry) => ({ id: entry.id, line_number: entry.lineNumber, source_file: entry.sourceFile, timestamp: entry.timestamp, trace_id: entry.rpc.traceId, component: entry.component })),
          folding_enabled: foldingEnabled,
          function_fold_summary: assistantFunctionFoldSummary,
          function_fold_summary_source: 'parsed-function-tree-boundaries',
          data_pattern_summary: assistantDataPatternSummary,
          data_pattern_summary_source: 'normalized-repeated-message-families',
          // Backward-compatible alias for older prompts/tools.
          visible_evidence: pageEvidence,
        };
      }

      let querySkillSummary: Array<Record<string, unknown>> = [];
      try {
        const rawQuerySkillSummary = window.sessionStorage.getItem('tracelens-log-query-skill-summary-v1');
        const parsedQuerySkillSummary = rawQuerySkillSummary ? JSON.parse(rawQuerySkillSummary) : [];
        querySkillSummary = Array.isArray(parsedQuerySkillSummary)
          ? parsedQuerySkillSummary.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object')).slice(0, 80)
          : [];
      } catch {
        querySkillSummary = [];
      }
      context.log_rules = {
        active_tab: workspacePage === 'platform-settings' ? (window.sessionStorage.getItem('tracelens-log-rules-active-tab-v1') || '') : '',
        semantic_count: displayRules.length,
        semantic_enabled: displayRules.filter((rule) => rule.enabled).length,
        semantic_rules: displayRules.slice(0, 24).map((rule) => ({
          name: rule.name, kind: rule.kind, scope: rule.scope, keyword: rule.keyword || '', enabled: rule.enabled,
        })),
        anomaly_count: errorRules.length,
        anomaly_enabled: errorRules.filter((rule) => rule.enabled).length,
        anomaly_rules: errorRules.slice(0, 40).map((rule) => ({ keyword: rule.keyword, enabled: rule.enabled, case_sensitive: rule.caseSensitive, whole_word: rule.wholeWord })),
        data_extraction_count: dataExtractionRules.length,
        data_extraction_enabled: dataExtractionRules.filter((rule) => rule.enabled).length,
        data_extraction_rules: dataExtractionRules.slice(0, 20).map((rule) => ({
          name: rule.name, modules: rule.modules.slice(0, 8), fields: rule.fields.slice(0, 12).map((field) => field.name || field.key), enabled: rule.enabled,
        })),
        query_skill_count: querySkillSummary.length,
        query_skills: querySkillSummary,
        query_skill_scope: 'subsystem',
        masking_count: maskingRules.length,
        folding_count: foldingRules.length,
        source_seed: inlineRuleSeed ? {
          source: inlineRuleSeed.source, function_name: inlineRuleSeed.functionName || '', sample_raw: inlineRuleSeed.sampleRaw.slice(0, 1600), suggested_keyword: inlineRuleSeed.suggestedKeyword || '',
        } : null,
      };
      if (workspacePage === 'platform-settings') context.log_rule_settings = context.log_rules;

      if (workspacePage === 'reports' && reportEnvironment) {
        context.cpd_reports = { environment_id: reportEnvironment.id, environment_name: reportEnvironment.name };
      }
    };
    return registerPageContextReader(handler, 0);
  }, [
    workspacePage, resourceWorkspaceFeature, reportEnvironment, resourceWorkspaceEnvironment, activeTask, activeTaskLoadedRange, filters,
    logSortOrder, liveListening, liveConnectionStatus, metricEntries, errorEntryCount, selectedEntry, focusedEntryId, paginatedEntries, safeLogPage, logPageSize,
    foldingEnabled, assistantFunctionFoldSummary, assistantDataPatternSummary, displayRules, errorRules, dataExtractionRules, maskingRules, foldingRules, inlineRuleSeed,
  ]);
  const focusedErrorIndex = useMemo(
    () => focusedEntryId ? navigableErrorEntries.findIndex((entry) => entry.id === focusedEntryId) : -1,
    [focusedEntryId, navigableErrorEntries],
  );
  const focusedErrorPage = useMemo(() => {
    if (!focusedEntryId || focusedErrorIndex < 0) return undefined;
    const index = metricEntries.findIndex((entry) => entry.id === focusedEntryId);
    return index >= 0 ? Math.floor(index / logPageSize) + 1 : undefined;
  }, [focusedEntryId, focusedErrorIndex, logPageSize, metricEntries]);
  // 手工翻页后，索引自动切换为该页第一条异常；异常跳转时则保持精确目标。
  const selectedErrorIndex = useMemo(() => {
    if (focusedErrorIndex >= 0 && focusedErrorPage === logPage) return focusedErrorIndex;
    const pageStart = (Math.max(1, logPage) - 1) * logPageSize;
    const pageEnd = pageStart + logPageSize;
    const firstErrorOnPage = metricEntries.slice(pageStart, pageEnd).find((entry) => entry.severity === 'error');
    return firstErrorOnPage ? navigableErrorEntries.findIndex((entry) => entry.id === firstErrorOnPage.id) : -1;
  }, [focusedErrorIndex, focusedErrorPage, logPage, logPageSize, metricEntries, navigableErrorEntries]);
  const highlightedEntryId = selectedEntry?.id ?? focusedEntryId;
  const currentSourceCount = useMemo(
    () => new Set(metricEntries.map((entry) => entry.sourceFileId)).size,
    [metricEntries],
  );
  const currentComponentCount = useMemo(
    () => new Set(metricEntries.map((entry) => entry.component)).size,
    [metricEntries],
  );


  useEffect(() => {
    setLogPage(1);
  }, [
    activeTaskId,
    filters.query,
    filters.levels,
    filters.components,
    filters.modes,
    filters.timeRange?.startNs,
    filters.timeRange?.endNs,
    selectedProcessId,
    selectedThreadId,
    selectedTraceId,
    selectedCrossTraceId,
    logSortOrder,
  ]);

  useEffect(() => {
    if (logPage > logPageCount) setLogPage(logPageCount);
  }, [logPage, logPageCount]);

  useEffect(() => {
    timelineCursorTargetRef.current = undefined;
  }, [activeTaskId, filters.timeRange?.endNs, filters.timeRange?.startNs, logPageSize]);

  useEffect(() => {
    logScrollRef.current?.scrollTo({ top: 0, behavior: 'auto' });
  }, [safeLogPage, logPageSize, scopedFilteredEntries]);

  useEffect(() => {
    if (!pendingPageEntryId) return;
    const index = scopedFilteredEntries.findIndex((entry) => entry.id === pendingPageEntryId);
    if (index < 0) return;
    setLogPage(Math.floor(index / logPageSize) + 1);
    setPendingPageEntryId(undefined);
  }, [logPageSize, pendingPageEntryId, scopedFilteredEntries]);

  useEffect(() => {
    if (selectedThreadId && !allThreads.some((thread) => thread.id === selectedThreadId)) {
      setSelectedThreadId(undefined);
      setSelectedTraceId(undefined);
    }
  }, [allThreads, selectedThreadId]);

  useEffect(() => {
    if (selectedProcessId && !visibleProcesses.some((process) => process.id === selectedProcessId)) {
      setSelectedProcessId(undefined);
    }
  }, [selectedProcessId, visibleProcesses]);

  useEffect(() => {
    if (selectedTraceId && !selectedThread?.traces.some((trace) => trace.id === selectedTraceId)) {
      setSelectedTraceId(undefined);
    }
  }, [selectedThread, selectedTraceId]);

  useEffect(() => {
    if (selectedCrossTraceId && !visibleCrossComponentTraces.some((trace) => trace.id === selectedCrossTraceId)) {
      setSelectedCrossTraceId(undefined);
    }
  }, [selectedCrossTraceId, visibleCrossComponentTraces]);

  useEffect(() => {
    setExpandedIds(new Set());
    setTimelineHiddenComponents(new Set());
  }, [activeTaskId]);

  useEffect(() => {
    if (!optimisticTraceId) return;
    if (selectedTraceId === optimisticTraceId || selectedCrossTraceId === optimisticTraceId) setOptimisticTraceId(undefined);
  }, [optimisticTraceId, selectedCrossTraceId, selectedTraceId]);

  useEffect(() => {
    if (!pendingFunctionFocus) return;
    if (!visibleTraces.some((trace) => trace.id === pendingFunctionFocus.traceId)) return;

    setExpandedIds(new Set(pendingFunctionFocus.expandedPath));
    const timer = window.setTimeout(() => {
      const element = document.getElementById(`function-node-${pendingFunctionFocus.nodeId}`);
      if (!element) return;
      element.scrollIntoView({ behavior: 'smooth', block: 'center' });
      element.classList.add('focus-flash');
      window.setTimeout(() => element.classList.remove('focus-flash'), 1500);
      setPendingFunctionFocus(undefined);
    }, 80);

    return () => window.clearTimeout(timer);
  }, [logPage, paginatedVisibleTraces, pendingFunctionFocus, visibleTraces]);

  useEffect(() => {
    if (!pendingEntryFocusId) return;
    if (!paginatedEntries.some((entry) => entry.id === pendingEntryFocusId)) return;

    if (showTimeline) {
      setExpandedIds((current) => new Set([...current, ...allFunctionIds]));
    }
    const timer = window.setTimeout(() => {
      const element = document.getElementById(`log-entry-${pendingEntryFocusId}`);
      if (!element) return;
      if (pendingEntryFocusKind === 'time-cursor' && logScrollRef.current) {
        const container = logScrollRef.current;
        const containerRect = container.getBoundingClientRect();
        const elementRect = element.getBoundingClientRect();
        const targetTop = container.scrollTop
          + (elementRect.top - containerRect.top)
          - Math.max(0, (container.clientHeight - elementRect.height) / 2);
        container.scrollTo({ top: Math.max(0, targetTop), behavior: 'auto' });
      } else {
        element.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
      const focusClass = pendingEntryFocusKind === 'time-cursor' ? 'time-cursor-focus-flash' : 'error-focus-flash';
      element.classList.add(focusClass);
      window.setTimeout(() => element.classList.remove(focusClass), pendingEntryFocusKind === 'time-cursor' ? 950 : 1800);
      setPendingEntryFocusId(undefined);
    }, showTimeline ? 120 : 60);

    return () => window.clearTimeout(timer);
  }, [allFunctionIds, logPage, paginatedEntries, pendingEntryFocusId, pendingEntryFocusKind, showTimeline]);

  useEffect(() => {
    const stack = analysisStackRef.current;
    if (!stack) return;

    const updateStickyTop = () => {
      setTraceHeaderTop(52 + stack.getBoundingClientRect().height + 8);
    };
    updateStickyTop();

    const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(updateStickyTop) : undefined;
    observer?.observe(stack);
    window.addEventListener('resize', updateStickyTop);
    return () => {
      observer?.disconnect();
      window.removeEventListener('resize', updateStickyTop);
    };
  }, []);


  useEffect(() => {
    const groupIds = visibleProcesses.flatMap((process) => (methodGroupsByProcess.get(process.id) ?? []).map((group) => group.id));
    if (groupIds.length === 0) {
      setExpandedMethodGroups(new Set());
      return;
    }

    setExpandedMethodGroups((current) => new Set(Array.from(current).filter((id) => groupIds.includes(id))));
  }, [methodGroupsByProcess, visibleProcesses]);


  useEffect(() => {
    const groupIds = crossMethodGroups.map((group) => group.id);
    if (groupIds.length === 0) {
      setExpandedCrossMethodGroups(new Set());
      return;
    }

    setExpandedCrossMethodGroups((current) => new Set(Array.from(current).filter((id) => groupIds.includes(id))));
  }, [crossMethodGroups]);

  /**
   * Measure the timeline window's content and publish it as a CSS variable.
   *
   * `node.clientHeight - rowsBox` is the chrome (dragbar, gantt header, tools, axis, brush,
   * ribbon) and stays constant no matter how tall the window is, because the rows area flexes
   * to fill it. Adding the rows' *scroll* height — their real content, not their grown box —
   * therefore gives the height the window needs without a feedback loop.
   */
  useLayoutEffect(() => {
    // 固定模式没有悬浮窗，高度由日志区排版决定，不需要再算自然高度。
    if (!showTimeline || timelineDocked) return undefined;
    const node = timelineWindowRef.current;
    if (!node) return undefined;
    const rows = () => node.querySelector('.process-gantt-rows') as HTMLElement | null;
    const box = (selector: string) => {
      const element = node.querySelector(selector) as HTMLElement | null;
      if (!element) return 0;
      const style = window.getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      // Margins are outside the rect and would otherwise be lost from the total.
      return rect.height + parseFloat(style.marginTop || '0') + parseFloat(style.marginBottom || '0');
    };
    const paddingY = (selector: string) => {
      const element = node.querySelector(selector) as HTMLElement | null;
      if (!element) return 0;
      const style = window.getComputedStyle(element);
      return parseFloat(style.paddingTop || '0') + parseFloat(style.paddingBottom || '0');
    };
    let frame = 0;
    const measure = () => {
      frame = 0;
      const rowsNode = rows();
      if (!rowsNode) return;
      // Summed explicitly rather than by subtraction: the stretchy parts (scroll shell, canvas)
      // absorb whatever space is left over, so "window minus rows" is not a constant.
      // dragbar 已经并入 gantt 标题行，不能再算进 chrome，否则窗口会高出 34px。
      const chrome = box('.process-gantt-header')
        + paddingY('.process-gantt-content')
        + box('.process-gantt-tools')
        + paddingY('.process-gantt-canvas')
        + box('.process-gantt-axis')
        + box('.process-gantt-brush-row')
        + box('.live-ribbon')
        + 2; // window borders
      const next = Math.max(0, Math.round(chrome + rowsNode.scrollHeight));
      setTimelineContentHeight((current) => (Math.abs(current - next) < 2 ? current : next));
    };
    const schedule = () => { if (!frame) frame = requestAnimationFrame(measure); };
    schedule();
    const observer = new ResizeObserver(schedule);
    observer.observe(node);
    // Lane count changes without resizing the window when it is already at max-height, so the
    // rows subtree is observed for content changes too.
    const mutations = new MutationObserver(schedule);
    const attach = () => {
      const rowsNode = rows();
      if (rowsNode) mutations.observe(rowsNode, { childList: true, subtree: true });
    };
    attach();
    const attachTimer = window.setTimeout(attach, 400);
    return () => {
      if (frame) cancelAnimationFrame(frame);
      window.clearTimeout(attachTimer);
      observer.disconnect();
      mutations.disconnect();
    };
  }, [showTimeline, timelineDocked, liveListening, ganttProcesses, selectedProcessId]);

  function beginTimelineWindowDrag(event: React.PointerEvent<HTMLDivElement>) {
    const target = event.target as HTMLElement;
    if (target.closest('input, select, a, textarea')) return;
    // 标题行整体是拖动手柄，而折叠开关本身是个 button：它是**唯一**允许从按钮上起拖的，
    // 否则可拖区域只剩标题行几像素的内边距。固定/关闭按钮仍然只响应点击。
    const button = target.closest('button');
    if (button && !button.classList.contains('process-gantt-header-toggle')) return;
    const windowElement = event.currentTarget.closest('.floating-timeline-window') as HTMLElement | null;
    const rect = windowElement?.getBoundingClientRect();
    timelineWindowDragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      left: rect?.left ?? timelineWindowPosition.left,
      top: rect?.top ?? timelineWindowPosition.top,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function moveTimelineWindow(event: React.PointerEvent<HTMLDivElement>) {
    const state = timelineWindowDragRef.current;
    if (!state || state.pointerId !== event.pointerId) return;
    const host = event.currentTarget.closest('.floating-timeline-window') as HTMLElement | null;
    const rect = host?.getBoundingClientRect();
    const width = rect?.width ?? Math.min(1120, window.innerWidth - 36);
    const height = rect?.height ?? 480;
    const nextLeft = state.left + event.clientX - state.startX;
    const nextTop = state.top + event.clientY - state.startY;
    setTimelineWindowPosition({
      left: Math.max(8, Math.min(Math.max(8, window.innerWidth - width - 8), nextLeft)),
      top: Math.max(8, Math.min(Math.max(8, window.innerHeight - Math.min(height, window.innerHeight - 16) - 8), nextTop)),
    });
  }

  function endTimelineWindowDrag(event: React.PointerEvent<HTMLDivElement>) {
    const state = timelineWindowDragRef.current;
    if (!state || state.pointerId !== event.pointerId) return;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    timelineWindowDragRef.current = undefined;
  }

  function resetViewState(resetFilters = true) {
    if (resetFilters) setFilters(cloneFilters(EMPTY_FILTERS));
    setSelectedEntry(undefined);
    setFocusedEntryId(undefined);
    setSelectedProcessId(undefined);
    setSelectedThreadId(undefined);
    setSelectedTraceId(undefined);
    setSelectedCrossTraceId(undefined);
    setExpandedMethodGroups(new Set());
    setExpandedCrossMethodGroups(new Set());
    setExpandedProcessIds(new Set());
    setCrossSectionExpanded(false);
    setExpandedIds(new Set());
    setTimelineHiddenComponents(new Set());
    setCallGraphState(undefined);
    setCallFlowDialogOpen(false);
    setPendingFunctionFocus(undefined);
    setShowIssues(false);
    setPasteDialogOpen(false);
    setImportDialogMode('choose');
  }

  function activateTask(taskId: string) {
    if (taskId === activeTaskId) return;
    const target = tasks.find((task) => task.id === taskId);
    if (!target) return;
    if (activeTaskId) {
      const snapshot = cloneFilters(filters);
      setTasks((current) => current.map((task) => task.id === activeTaskId ? { ...task, viewFilters: snapshot } : task));
    }
    setActiveTaskId(taskId);
    setFilters(cloneFilters(target.viewFilters ?? EMPTY_FILTERS));
    resetViewState(false);
  }

  function stopTask(taskId: string) {
    const running = remoteTaskControllersRef.current.get(taskId);
    if (running) {
      void cancelLogSearch(running.environmentId, running.operationId).catch(() => undefined);
      running.controller.abort();
      remoteTaskControllersRef.current.delete(taskId);
      if (running.workerTaskId) {
        streamWorkerRef.current?.postMessage({ type: 'CANCEL_TASK', taskId: running.workerTaskId });
        importBuffersRef.current.delete(running.workerTaskId);
        const completion = importCompletionRef.current.get(running.workerTaskId);
        importCompletionRef.current.delete(running.workerTaskId);
        completion?.reject(new DOMException('Aborted', 'AbortError'));
      }
    }
    streamWorkerRef.current?.postMessage({ type: 'CANCEL_TASK', taskId });
    importBuffersRef.current.delete(taskId);
    setTasks((current) => current.map((task) => task.id === taskId ? {
      ...task,
      status: 'cancelled',
      errorMessage: undefined,
      remoteProgress: task.remoteProgress ? { ...task.remoteProgress, stage: 'cancelled', done: true, cancelled: true, percent: 100, message: '任务已停止' } : task.remoteProgress,
    } : task));
  }

  function closeTask(taskId: string) {
    const running = remoteTaskControllersRef.current.get(taskId);
    if (running) {
      void cancelLogSearch(running.environmentId, running.operationId).catch(() => undefined);
      running.controller.abort();
      remoteTaskControllersRef.current.delete(taskId);
      if (running.workerTaskId) {
        streamWorkerRef.current?.postMessage({ type: 'CANCEL_TASK', taskId: running.workerTaskId });
        importBuffersRef.current.delete(running.workerTaskId);
        const completion = importCompletionRef.current.get(running.workerTaskId);
        importCompletionRef.current.delete(running.workerTaskId);
        completion?.reject(new DOMException('Aborted', 'AbortError'));
      }
    }
    streamWorkerRef.current?.postMessage({ type: 'CANCEL_TASK', taskId });
    importBuffersRef.current.delete(taskId);
    const index = tasks.findIndex((task) => task.id === taskId);
    const next = tasks.filter((task) => task.id !== taskId);
    setTasks(next);
    if (taskId === activeTaskId) {
      const closed = tasks[index];
      const candidates = closed?.remoteEnvironmentId
        ? next.filter((task) => task.remoteEnvironmentId === closed.remoteEnvironmentId)
        : next.filter((task) => !task.remoteEnvironmentId);
      const replacement = candidates.at(-1);
      setActiveTaskId(replacement?.id ?? '');
      setFilters(cloneFilters(replacement?.viewFilters ?? EMPTY_FILTERS));
      resetViewState(false);
    }
  }

  function startStreamingImport(
    task: LogTask,
    sources: WorkerImportSource[],
    options: StreamingImportOptions = {},
  ) {
    const strategy = options.strategy ?? 'recent-tail';
    const runtimeSources = sources.map((source) => ({ ...source, parserRules: logFormatRulesRef.current }));
    const analyzingTask: LogTask = {
      ...task,
      sources,
      status: 'analyzing',
      entries: [],
      issues: [],
      progress: undefined,
      errorMessage: undefined,
      importStrategy: strategy,
      fullLoaded: false,
    };

    importBuffersRef.current.set(task.id, {
      entryChunks: [],
      issueChunks: [],
      remoteOperationId: task.remoteOperationId,
      remoteKeyword: task.remoteRequest?.keyword || '',
    });
    if (!task.remoteEnvironmentId && !options.replaceTask) {
      setLocalImportWorkspaceOpen(true);
      setResourceWorkspaceEnvironment(undefined);
      setPreferredRemoteEnvironmentId(undefined);
      setPreferredRemotePreset(undefined);
    }
    setTasks((current) => options.replaceTask
      ? current.map((candidate) => candidate.id === task.id ? { ...analyzingTask, viewFilters: candidate.viewFilters ?? analyzingTask.viewFilters } : candidate)
      : [...current, analyzingTask]);
    setActiveTaskId(task.id);

    if (options.replaceTask) {
      setSelectedEntry(undefined);
      setFocusedEntryId(undefined);
      setSelectedProcessId(undefined);
      setSelectedThreadId(undefined);
      setSelectedTraceId(undefined);
      setSelectedCrossTraceId(undefined);
      setExpandedIds(new Set());
      setExpandedMethodGroups(new Set());
      setExpandedCrossMethodGroups(new Set());
      setExpandedProcessIds(new Set());
    } else {
      resetViewState();
    }

    streamWorkerRef.current?.postMessage({
      type: 'IMPORT_TASK',
      taskId: task.id,
      sources: runtimeSources,
      batchSize: 4000,
      strategy,
      recentSeconds: options.recentSeconds ?? DEFAULT_RANGE_SECONDS,
      targetStartNs: options.targetStartNs?.toString(),
      reverseBlockBytes: 2 * 1024 * 1024,
      rangeCheckLines: 5000,
    });
  }

  function importIncrementalRemoteBlob(
    task: LogTask,
    blob: Blob,
    sourceName: string,
    operationId: string,
  ): Promise<number> {
    const worker = streamWorkerRef.current;
    if (!worker) return Promise.reject(new Error('日志解析 Worker 尚未就绪'));
    const workerTaskId = `${task.id}-increment-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const source: WorkerImportSource = {
      kind: 'file',
      // 增量段继续沿用原远端任务的“逻辑日志源”身份，避免每次扩窗都把日志源统计 +1。
      id: task.sources[0]?.id ?? `${task.id}-remote-stream`,
      name: task.sourceNames[0] ?? sourceName,
      file: new File([blob], sourceName, { type: 'text/plain' }),
      logCategories: [...(task.remoteRequest?.source_categories || [])],
      parserRules: logFormatRulesRef.current,
    };
    importBuffersRef.current.set(workerTaskId, {
      entryChunks: [],
      issueChunks: [],
      remoteOperationId: operationId,
      remoteKeyword: task.remoteRequest?.keyword || '',
      mergeIntoTaskId: task.id,
    });
    const running = remoteTaskControllersRef.current.get(task.id);
    if (running) remoteTaskControllersRef.current.set(task.id, { ...running, workerTaskId });

    return new Promise<number>((resolve, reject) => {
      importCompletionRef.current.set(workerTaskId, { resolve, reject });
      worker.postMessage({
        type: 'IMPORT_TASK',
        taskId: workerTaskId,
        sources: [source],
        batchSize: 4000,
        strategy: 'full',
        recentSeconds: DEFAULT_RANGE_SECONDS,
        reverseBlockBytes: 2 * 1024 * 1024,
        rangeCheckLines: 5000,
      });
    });
  }

  function importLiveRemoteBlob(
    task: LogTask,
    blob: Blob,
    sourceName: string,
  ): Promise<number> {
    const worker = streamWorkerRef.current;
    if (!worker) return Promise.reject(new Error('日志解析 Worker 尚未就绪'));
    const workerTaskId = `${task.id}-live-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const source: WorkerImportSource = {
      kind: 'file',
      id: task.sources[0]?.id ?? `${task.id}-remote-live`,
      name: task.sourceNames[0] ?? sourceName,
      file: new File([blob], sourceName, { type: 'text/plain' }),
      logCategories: [...(task.remoteRequest?.source_categories || [])],
      parserRules: logFormatRulesRef.current,
    };
    importBuffersRef.current.set(workerTaskId, {
      entryChunks: [],
      issueChunks: [],
      remoteKeyword: task.remoteRequest?.keyword || '',
      mergeIntoTaskId: task.id,
      live: true,
      liveGeneration: liveRenderGenerationRef.current,
    });
    return new Promise<number>((resolve, reject) => {
      importCompletionRef.current.set(workerTaskId, { resolve, reject });
      worker.postMessage({
        type: 'IMPORT_TASK',
        taskId: workerTaskId,
        sources: [source],
        batchSize: 4000,
        strategy: 'full',
        recentSeconds: DEFAULT_RANGE_SECONDS,
        reverseBlockBytes: 2 * 1024 * 1024,
        rangeCheckLines: 5000,
      });
    });
  }

  function reloadTaskForEarlierRange(task: LogTask, targetStartNs?: bigint, strategy: ImportStrategy = 'recent-tail') {
    if (task.status === 'analyzing' || task.sources.length === 0) return;
    startStreamingImport(task, task.sources, {
      strategy,
      targetStartNs,
      replaceTask: true,
    });
  }

  function createPastedLogTask() {
    const text = pastedLogText.trim();
    if (!text) return;

    setLocalImportWorkspaceOpen(true);
    setResourceWorkspaceEnvironment(undefined);
    setPreferredRemoteEnvironmentId(undefined);
    setPreferredRemotePreset(undefined);

    const taskNumber = taskSequenceRef.current;
    taskSequenceRef.current += 1;
    const taskId = `task-${Date.now()}-${taskNumber}`;
    const sourceName = `粘贴日志-${taskNumber}.log`;
    const sources: WorkerImportSource[] = [{
      kind: 'text',
      id: `${taskId}-pasted-log`,
      name: sourceName,
      text,
    }];
    const nextTask: LogTask = {
      id: taskId,
      name: `任务 ${taskNumber}`,
      sourceNames: [sourceName],
      sources,
      createdAt: Date.now(),
      status: 'analyzing',
      entries: [],
      issues: [],
      viewFilters: cloneFilters(EMPTY_FILTERS),
    };

    startStreamingImport(nextTask, sources);
    setPastedLogText('');
  }

  async function inspectImportedLogUrls() {
    if (importUrlList.length === 0 || urlImportBusy) return;
    setUrlImportBusy(true);
    setUrlImportError('');
    try {
      const inspected = await inspectUrlLogImports(importUrlList);
      setUrlImportItems(inspected);
      setUrlArchiveSelections({});
    } catch (error) {
      setUrlImportItems([]);
      setUrlImportError(error instanceof Error ? error.message : String(error));
    } finally {
      setUrlImportBusy(false);
    }
  }

  async function createUrlLogTask() {
    if (!urlImportReady || urlImportBusy) return;
    setUrlImportBusy(true);
    setUrlImportError('');
    try {
      const taskNumber = taskSequenceRef.current;
      taskSequenceRef.current += 1;
      const taskId = `task-${Date.now()}-${taskNumber}`;
      const sources: WorkerImportSource[] = [];
      const sourceNames: string[] = [];

      for (let index = 0; index < urlImportItems.length; index += 1) {
        const item = urlImportItems[index];
        if (item.status !== 'success') throw new Error(item.message || `${item.filename || item.url} 无法导入`);
        const selectedMemberName = item.kind === 'archive' ? urlArchiveSelections[item.url] : '';
        const selectedMember = selectedMemberName ? item.members.find((member) => member.name === selectedMemberName) : undefined;
        if (item.kind === 'archive' && !selectedMember) throw new Error(`${item.filename} 需要先选择压缩包内日志。`);

        const blob = await fetchUrlLogImportContent(item.url, selectedMember?.name);
        const displayName = selectedMember ? `${item.filename} → ${selectedMember.filename}` : item.filename;
        const file = new File([blob], displayName, { type: 'text/plain', lastModified: Date.now() });
        sourceNames.push(displayName);
        sources.push({
          kind: 'file',
          id: `${taskId}-url-${index}-${item.fm || selectedMember?.fm || 'log'}`,
          name: displayName,
          file,
          logModule: item.fm || selectedMember?.fm || item.filename,
          sourcePath: selectedMember ? `${item.url}::${selectedMember.name}` : item.url,
        });
      }

      const nextTask: LogTask = {
        id: taskId,
        name: `任务 ${taskNumber}`,
        sourceNames,
        sources,
        createdAt: Date.now(),
        status: 'analyzing',
        entries: [],
        issues: [],
        viewFilters: cloneFilters(EMPTY_FILTERS),
      };
      startStreamingImport(nextTask, sources);
      setPasteDialogOpen(false);
      setImportDialogMode('choose');
      setImportUrlText('');
      setUrlImportItems([]);
      setUrlArchiveSelections({});
    } catch (error) {
      setUrlImportError(error instanceof Error ? error.message : String(error));
    } finally {
      setUrlImportBusy(false);
    }
  }

  function loadFiles(files: FileList | File[]) {
    const selectedFiles = Array.from(files);
    if (selectedFiles.length === 0) return;

    setLocalImportWorkspaceOpen(true);
    setResourceWorkspaceEnvironment(undefined);
    setPreferredRemoteEnvironmentId(undefined);
    setPreferredRemotePreset(undefined);

    const taskNumber = taskSequenceRef.current;
    taskSequenceRef.current += 1;
    const taskId = `task-${Date.now()}-${taskNumber}`;
    const sources: WorkerImportSource[] = selectedFiles.map((file, index) => ({
      kind: 'file',
      id: `${taskId}-${file.lastModified}-${index}-${file.name}`,
      name: file.name,
      file,
    }));
    const nextTask: LogTask = {
      id: taskId,
      name: `任务 ${taskNumber}`,
      sourceNames: selectedFiles.map((file) => file.name),
      sources,
      createdAt: Date.now(),
      status: 'analyzing',
      entries: [],
      issues: [],
      viewFilters: cloneFilters(EMPTY_FILTERS),
    };

    startStreamingImport(nextTask, sources);
    if (fileInputRef.current) fileInputRef.current.value = '';
  }

  function selectLatestTaskForEnvironment(environmentId: number) {
    const target = [...tasks].reverse().find((task) => task.remoteEnvironmentId === environmentId);
    if (target) { activateTask(target.id); return; }
    if (activeTaskId) {
      const snapshot = cloneFilters(filters);
      setTasks((current) => current.map((task) => task.id === activeTaskId ? { ...task, viewFilters: snapshot } : task));
    }
    setActiveTaskId('');
    setFilters(cloneFilters(EMPTY_FILTERS));
    resetViewState(false);
  }

  function selectLatestLocalTask() {
    setLocalImportWorkspaceOpen(true);
    const target = [...tasks].reverse().find((task) => !task.remoteEnvironmentId);
    if (target) activateTask(target.id);
    else {
      setActiveTaskId('');
      setFilters(cloneFilters(EMPTY_FILTERS));
      resetViewState(false);
    }
  }

  function rememberResourceWorkspace(environment: EnvironmentSummary) {
    setResourceWorkspaceEnvironment(environment);
    setResourceWorkspaceTabs((current) => current.some((item) => item.id === environment.id) ? current : [...current, environment]);
  }

  function openCurrentEnvironmentResource(environment: EnvironmentSummary) {
    rememberResourceWorkspace(environment);
    setPreferredRemoteEnvironmentId(environment.id);
    setPreferredRemotePreset(undefined);
    setReportEnvironment(undefined);
    setResourceWorkspaceFeature('resource');
    setWorkspacePage('logs');
  }

  function openEnvironmentLogLocator(environment: EnvironmentSummary) {
    rememberResourceWorkspace(environment);
    setPreferredRemoteEnvironmentId(environment.id);
    setPreferredRemotePreset({ token: `env-${environment.id}-${Date.now()}`, environmentId: environment.id });
    setResourceWorkspaceFeature('logs');
    setWorkspacePage('logs');
    selectLatestTaskForEnvironment(environment.id);
  }

  function openEnvironmentCpdReports(environment: EnvironmentSummary) {
    rememberResourceWorkspace(environment);
    setReportEnvironment(environment);
    setWorkspacePage('reports');
  }

  function openGlobalLogLocator() {
    setResourceWorkspaceFeature('logs');
    // 如果日志定位已经处于某个环境资源工作区，主导航切走再回来时保留该一级资源 Tab。
    // 只有当前没有环境资源上下文时，才进入全局/本地日志任务视图。
    setPreferredRemotePreset(undefined);
    setWorkspacePage('logs');
    if (resourceWorkspaceEnvironment) {
      setPreferredRemoteEnvironmentId(resourceWorkspaceEnvironment.id);
      const activeBelongsToResource = tasks.some((task) => task.id === activeTaskId && task.remoteEnvironmentId === resourceWorkspaceEnvironment.id);
      if (!activeBelongsToResource) selectLatestTaskForEnvironment(resourceWorkspaceEnvironment.id);
      return;
    }
    setPreferredRemoteEnvironmentId(undefined);
    selectLatestLocalTask();
  }

  function openLogAudit() {
    setWorkspacePage('audit');
  }

  function retryLogAudit(environment: EnvironmentSummary, request: LogWindowRequest, audit: LogAuditRecord) {
    const retryRequest: LogWindowRequest = {
      start_time: String(request.start_time || audit.start_time).replace('T', ' ').replace(/Z$/, ''),
      end_time: String(request.end_time || audit.end_time).replace('T', ' ').replace(/Z$/, ''),
      source_categories: [...(request.source_categories || audit.source_categories || [])],
      subsystems: [...(request.subsystems || [])],
      fms: [...(request.fms || [])],
      fm_targets: [...(request.fm_targets || audit.targets.map((item) => ({ subsystem: item.subsystem, fm: item.module, kind: item.kind === 'executor' ? 'executor' : 'normal' })))],
      keyword: audit.keyword || request.keyword || '',
    };
    rememberResourceWorkspace(environment);
    setPreferredRemoteEnvironmentId(environment.id);
    setPreferredRemotePreset({
      token: `audit-retry-${audit.id}-${Date.now()}`,
      environmentId: environment.id,
      startTime: retryRequest.start_time,
      endTime: retryRequest.end_time,
      taskName: `审计重试 · ${audit.target_host || environment.name}`,
      request: retryRequest,
    });
    setWorkspacePage('logs');
    startRemoteLogSearch(environment, retryRequest, { taskName: `审计重试 · ${audit.target_host || environment.name}`, force: true });
  }

  function openDataPage(sourceOperationId = '') {
    setDataSourceOperationFilter(sourceOperationId);
    setWorkspacePage('data');
  }

  function createAiSemanticRule(detail: Record<string, unknown>) {
    const spec = detail.rule && typeof detail.rule === 'object' ? detail.rule as Record<string, unknown> : undefined;
    if (!spec) return;
    const kind = String(spec.kind || 'keyword') === 'template' ? 'template' : 'keyword';
    const scopeText = String(spec.scope || 'log');
    const scope: DisplayRule['scope'] = scopeText === 'function' || scopeText === 'both' ? scopeText : 'log';
    const modeText = String(spec.display_mode || spec.displayMode || 'semantic');
    const displayMode: DisplayRule['displayMode'] = modeText === 'label' || modeText === 'both' ? modeText : 'semantic';
    const sampleMessage = String(spec.sample_message || spec.sampleMessage || '');
    const parameters: DisplayRuleParameter[] = Array.isArray(spec.parameters)
      ? spec.parameters.flatMap((item, index): DisplayRuleParameter[] => {
          if (!item || typeof item !== 'object') return [];
          const row = item as Record<string, unknown>;
          const sampleValue = String(row.sample_value || row.sampleValue || '').trim();
          const label = String(row.label || row.name || `参数${index + 1}`).trim();
          if (!sampleValue || !label) return [];
          return [{ id: `ai-semantic-param-${Date.now()}-${index}`, label, sampleValue }];
        })
      : [];
    const marks: ParameterMark[] = [];
    if (kind === 'template' && sampleMessage) {
      const occupied: Array<{ start: number; end: number }> = [];
      parameters.forEach((parameter) => {
        let cursor = 0;
        while (cursor < sampleMessage.length) {
          const start = sampleMessage.indexOf(parameter.sampleValue, cursor);
          if (start < 0) break;
          const end = start + parameter.sampleValue.length;
          const overlaps = occupied.some((item) => start < item.end && end > item.start);
          if (!overlaps) {
            marks.push({ parameterId: parameter.id, start, end });
            occupied.push({ start, end });
            break;
          }
          cursor = end;
        }
      });
      marks.sort((a, b) => a.start - b.start);
    }
    const rule: DisplayRule = {
      id: createDisplayRuleId(),
      name: String(spec.name || 'AI 语义规则').trim() || 'AI 语义规则',
      enabled: spec.enabled !== false,
      kind,
      scope,
      keyword: String(spec.keyword || ''),
      displayTemplate: String(spec.display_template || spec.displayTemplate || ''),
      displayMode,
      customLabelTemplate: String(spec.custom_label_template || spec.customLabelTemplate || ''),
      customLabelColor: String(spec.custom_label_color || spec.customLabelColor || '#2563eb'),
      showLabelOnTimeline: spec.show_label_on_timeline !== false && spec.showLabelOnTimeline !== false,
      supplementalDescription: String(spec.supplemental_description || spec.supplementalDescription || ''),
      sampleMessage,
      parameters,
      patternTokens: kind === 'template' ? buildPatternTokens(sampleMessage, marks) : [],
      createdAt: Date.now(),
    };
    const validationError = validateDisplayRule(rule);
    if (validationError) {
      setRuleEditorRequest({
        requestId: `ai-semantic-${Date.now()}`,
        source: scope === 'function' ? 'function' : 'log',
        sampleRaw: sampleMessage,
        suggestedName: rule.name,
        suggestedKeyword: rule.keyword || '',
        suggestedScope: rule.scope,
        suggestedDisplayTemplate: rule.displayTemplate,
        openSemanticEditor: true,
      });
    } else {
      setDisplayRules((current) => {
        const duplicate = current.some((item) => item.kind === rule.kind
          && String(item.keyword || '').trim().toLowerCase() === String(rule.keyword || '').trim().toLowerCase()
          && item.scope === rule.scope
          && item.displayTemplate.trim() === rule.displayTemplate.trim());
        return duplicate ? current : [rule, ...current];
      });
    }
    if (detail.open_settings !== false) {
      window.sessionStorage.setItem('tracelens-ai-rule-tab-v1', 'semantic');
      setPreferredSettingsTab('rules');
      setWorkspacePage('platform-settings');
    }
  }

  function createAiAnomalyRules(detail: Record<string, unknown>) {
    const values = Array.isArray(detail.keywords)
      ? detail.keywords.map(String).map((item) => item.trim()).filter(Boolean)
      : [String(detail.keyword || '').trim()].filter(Boolean);
    if (!values.length) return;
    setErrorRules((current) => {
      const seen = new Set(current.map((rule) => rule.keyword.trim().toLocaleLowerCase()));
      const created = values.flatMap((keyword): ErrorMatchRule[] => {
        const key = keyword.toLocaleLowerCase();
        if (!keyword || seen.has(key)) return [];
        seen.add(key);
        return [createErrorMatchRule(keyword, {
          caseSensitive: Boolean(detail.case_sensitive),
          wholeWord: Boolean(detail.whole_word),
          enabled: detail.enabled !== false,
        })];
      });
      return created.length ? [...created, ...current] : current;
    });
    if (detail.open_settings !== false) {
      window.sessionStorage.setItem('tracelens-ai-rule-tab-v1', 'anomaly');
      setPreferredSettingsTab('rules');
      setWorkspacePage('platform-settings');
    }
  }

  useEffect(() => {
    const actionRegistry = createTracePilotActionRegistry({
      control_log_view: async (detail) => {
        if (liveListening && (detail.start_time || detail.end_time || detail.components)) throw new Error('实时监听期间不能修改时间范围或监听模块，请先停止监听');
        if (!activeTask || (detail.task_id && detail.task_id !== activeTask.id)) throw new Error('日志任务已变化，请重新读取页面上下文');
        const start = detail.start_time ? timestampTextToNs(String(detail.start_time)) : undefined;
        const end = detail.end_time ? timestampTextToNs(String(detail.end_time)) : undefined;
        if ((detail.start_time || detail.end_time) && (start === undefined || end === undefined || start > end)) throw new Error('时间范围无效');
        const entry = detail.entry_id ? activeTask.entries.find((row) => row.id === detail.entry_id) : undefined;
        if (detail.entry_id && !entry) throw new Error('目标日志已不在当前任务中，未执行定位');
        setWorkspacePage('logs');
        if (typeof detail.folding_enabled === 'boolean') setFoldingEnabled(detail.folding_enabled);
        if (entry) {
          // Reuse the same expansion, pagination and highlight path as user navigation.
          const entries = activeTask.entries.slice().sort(compareEntries);
          if (logSortOrder === 'desc') entries.reverse();
          clearNavigationSelection();
          setFilters((current) => ({ ...current, query: '', levels: new Set(), components: new Set(), modes: new Set(), errorsOnly: false, timeRange: undefined }));
          setSelectedEntry(undefined);
          setFocusedEntryId(entry.id);
          setExpandedIds(new Set(allFunctionIds));
          setShowIssues(false);
          setLogPage(Math.floor(entries.findIndex((row) => row.id === entry.id) / logPageSize) + 1);
          setPendingPageEntryId(entry.id);
          setPendingEntryFocusKind('navigation');
          setPendingEntryFocusId(entry.id);
        } else {
          setFilters((current) => ({
            ...current,
            ...(typeof detail.query === 'string' ? { query: detail.query } : {}),
            ...(typeof detail.errors_only === 'boolean' ? { errorsOnly: detail.errors_only } : {}),
            ...(Array.isArray(detail.components) ? { components: new Set(detail.components.map(String)) } : {}),
            ...(start !== undefined && end !== undefined ? { timeRange: { startNs: start, endNs: end } } : {}),
          }));
          setLogPage(1);
        }
        await afterPaint();
        return { detail: entry ? `已定位 ${entry.sourceFile}:${entry.lineNumber}` : '已更新当前日志视图', entry_id: entry?.id };
      },
      show_atlog_evidence: async (detail) => {
        setWorkspacePage('atlog');
        await afterPaint();
        const request = { ...detail, __claimed: false, __result: undefined as Promise<unknown> | undefined };
        window.dispatchEvent(new CustomEvent('tracelens:atlog-evidence', { detail: request }));
        if (!request.__claimed) throw new Error('目标用例未展开或已切换，证据未覆盖当前页面');
        return await request.__result;
      },
      set_log_semantic_labels: (detail) => {
        setSemanticLabelsEnabled(detail.enabled !== false);
      },
      open_case_editor: (detail) => {
        // The assistant drafted a case; open the real editor prefilled so the user reviews
        // and saves it through the normal path. Nothing is written until they do.
        const draft = (detail.draft && typeof detail.draft === 'object') ? detail.draft as Partial<AbnormalCase> : {};
        const evidences = Array.isArray(detail.evidences) ? detail.evidences as AbnormalCaseEvidence[] : [];
        setCaseImportDraft({ draft, evidences });
        setAbnormalCaseEditorOpen(true);
        return { detail: `已打开案例编辑器并预填「${String(draft.name || '诊断结论')}」，核对后保存` };
      },
      /** 采集面板的勾选写回提取器上的 liveCapture —— 面板与规则页编辑同一个开关。 */
      set_live_capture_items: (detail) => {
        const items = Array.isArray(detail.items) ? detail.items as Array<Record<string, unknown>> : [];
        if (!items.length) throw new Error('没有要更新的采集项');
        const wanted = new Map(items.map((item) => [String(item.id || ''), item.liveCapture === true]));
        let changed = 0;
        setDataExtractionRules((current) => current.map((rule) => {
          if (!wanted.has(rule.id)) return rule;
          const next = wanted.get(rule.id)!;
          if ((rule.liveCapture === true) === next) return rule;
          changed += 1;
          return { ...rule, liveCapture: next, updatedAt: Date.now() };
        }));
        return { detail: `已更新 ${changed} 个采集项`, changed };
      },
      start_live_capture: () => {
        // Same single implementation as the toolbar 实时监听 switch: the page owns the stream.
        const locator = remoteLogLocatorRef.current;
        const targets = locator?.liveTargets ?? [];
        if (!locator?.environment || targets.length !== 1) {
          throw new Error(targets.length > 1
            ? `实时采集仅支持单组件，当前选择了 ${targets.length} 个组件，请只保留一个。`
            : '实时采集需要先在日志定位里选择一个环境和一个组件。');
        }
        return beginLiveMonitoring({
          environment: locator.environment,
          target: targets[0],
          sourceCategories: locator.sourceCategories,
          startTime: locator.startTime,
          endTime: locator.endTime,
        });
      },
      stop_live_capture: () => {
        liveRenderGenerationRef.current += 1;
        liveBoundTaskIdRef.current = undefined;
        setLiveMessage('');
        setLiveListening(false);
        clearLiveCaptureItems();
        return { detail: '已停止实时采集' };
      },
      /**
       * 打开**当前页面**的数据提取弹窗（不是设置页）。
       * 采集面板的「管理提取器」走这条：用户在看的日志就是配置提取器的依据，
       * 把他扔到设置页会丢掉现场。
       */
      open_data_extraction: () => {
        beginDataExtraction();
        return { detail: '已打开当前页面的数据提取弹窗' };
      },
      open_log_rule_settings: (detail) => {
        const tab = String(detail.tab || 'semantic');
        const normalizedTab = tab === 'data' || tab === 'anomaly' ? tab : 'semantic';
        window.sessionStorage.setItem('tracelens-ai-rule-tab-v1', normalizedTab);
        setPreferredSettingsTab('rules');
        setWorkspacePage('platform-settings');
      },
      start_live_monitoring: async (detail) => {
        // The assistant asked for 实时采集; the page owns the actual stream. Fails loudly when
        // the requested component is not available, so the assistant never reports a stream
        // that was never opened.
        const environmentId = Number(detail.environment_id || 0);
        const subsystem = String(detail.subsystem || '').trim();
        const fm = String(detail.fm || '').trim();
        if (!environmentId) throw new Error('开启实时监听需要指定 environment_id');
        if (!subsystem || !fm) throw new Error('开启实时监听需要指定 subsystem 与 fm');
        const environments = await listEnvironments();
        const environment = environments.find((item) => item.id === environmentId);
        if (!environment) throw new Error(`环境 #${environmentId} 不存在，无法开启实时监听`);
        const sourceCategories = Array.isArray(detail.source_categories)
          ? (detail.source_categories as unknown[]).map(String).filter(Boolean)
          : [];
        return beginLiveMonitoring({ environment, target: { subsystem, fm, kind: 'normal' }, sourceCategories });
      },
      create_log_semantic_rule: (detail) => {
        createAiSemanticRule(detail);
      },
      create_log_anomaly_rule: (detail) => {
        createAiAnomalyRules(detail);
      },
      create_data_extraction_capability: (detail) => {
        if (!activeTask || activeTask.status !== 'ready' || !activeTask.entries.length) {
          pendingAiExtractionRef.current = detail;
          setWorkspacePage('logs');
          return;
        }
        void executeAiGeneratedDataCapability(detail);
      },
      run_data_extraction: (detail) => {
        if (!activeTask || activeTask.status !== 'ready' || !activeTask.entries.length) {
          pendingAiExtractionRef.current = detail;
          setWorkspacePage('logs');
          return;
        }
        void executeAiDataExtraction(detail);
      },
      open_workspace_page: (detail) => {
        const page = String(detail.page || '') as WorkspacePage;
        if (WORKSPACE_PAGES.has(page)) setWorkspacePage(page);
      },
      execute_environment_deployment: (detail) => {
        const environmentId = Number(detail.environment_id || 0);
        if (!environmentId) return;
        saveTracePilotAgentContext({
          page: 'environment-resource',
          environmentId,
          deployment: {
            action: 'execute',
            confirmed: detail.confirmed === true,
            targetVersion: String(detail.target_version || ''),
          },
        });
        window.sessionStorage.setItem('tracelens-active-resource-v1', String(environmentId));
        window.sessionStorage.setItem('tracelens-ai-open-deployment-v1', String(environmentId));
        setWorkspacePage('resources');
        window.setTimeout(() => window.dispatchEvent(new CustomEvent('tracelens:assistant-resource', { detail: { ...detail, open_deployment: true, execute_deployment: true } })), 0);
      },
      open_environment_page: (detail) => {
        const environmentId = Number(detail.environment_id || 0);
        if (!environmentId) return;
        window.sessionStorage.setItem('tracelens-active-resource-v1', String(environmentId));
        if (detail.open_deployment) window.sessionStorage.setItem('tracelens-ai-open-deployment-v1', String(environmentId));
        setWorkspacePage('resources');
        window.setTimeout(() => window.dispatchEvent(new CustomEvent('tracelens:assistant-resource', { detail })), 0);
      },
      open_log_locator: async (detail) => {
        const environmentId = Number(detail.environment_id || 0);
        if (!environmentId) throw new Error('缺少目标环境');
        const environments = await listEnvironments();
        (detail.__trace_context as { signal?: AbortSignal } | undefined)?.signal?.throwIfAborted();
        const environment = environments.find((item) => item.id === environmentId);
        if (!environment) throw new Error('目标环境不存在');
        rememberResourceWorkspace(environment);
        setPreferredRemoteEnvironmentId(environment.id);
        const startTime = String(detail.start_time || '').trim();
        const endTime = String(detail.end_time || '').trim();
        const fmTargets = Array.isArray(detail.fm_targets) ? detail.fm_targets as LogWindowRequest['fm_targets'] : [];
        const sourceCategories = Array.isArray(detail.source_categories) ? detail.source_categories.map(String) : [];
        const keyword = String(detail.keyword || '');
        const request = startTime && endTime ? {
          start_time: startTime,
          end_time: endTime,
          source_categories: sourceCategories,
          subsystems: [],
          fms: [],
          fm_targets: fmTargets || [],
          keyword,
        } satisfies LogWindowRequest : undefined;
        setPreferredRemotePreset({
          token: `assistant-${environment.id}-${Date.now()}`,
          environmentId: environment.id,
          startTime: startTime || undefined,
          endTime: endTime || undefined,
          taskName: `TracePilot 日志定位 · ${environment.name}`,
          request,
        });
        setWorkspacePage('logs');
        selectLatestTaskForEnvironment(environment.id);
        if (request && ((request.fm_targets?.length || 0) > 0 || (request.source_categories?.length || 0) > 0)) {
          startRemoteLogSearch(environment, request, { taskName: `TracePilot 日志定位 · ${environment.name}`, force: true });
          if (detail.errors_only !== undefined) {
            setFilters((current) => ({ ...current, errorsOnly: Boolean(detail.errors_only), query: keyword }));
            setLogPage(1);
          }
        }
        return { detail: request ? '已切换环境并提交日志检索；数据加载由日志任务展示' : '已打开日志定位' };
      },
    });

    const handler = (event: Event) => {
      const detail = (event as CustomEvent<Record<string, unknown>>).detail || {};
      // Claim only what this registry can actually run. Claiming every event made the
      // "page did not accept this action" fallback in workstation.ts unreachable, and
      // left no room for a page to own its own action type.
      const type = String(detail.type || '').trim();
      if (!type || !actionRegistry.has(type)) return;
      const complete = detail.__complete as ((receipt: UiReceipt) => void) | undefined;
      detail.__claimed = true;
      void actionRegistry.execute(detail, { signal: detail.__signal as AbortSignal | undefined }).then(async (result) => {
        await afterPaint();
        complete?.({ status: 'success', detail: String((result as { detail?: string } | undefined)?.detail || '页面操作已应用'), result });
      }).catch((error: unknown) => complete?.({ status: 'failed', detail: error instanceof Error ? error.message : String(error) }));
    };
    window.addEventListener('tracelens:assistant-ui', handler as EventListener);
    return () => window.removeEventListener('tracelens:assistant-ui', handler as EventListener);
  }, [tasks, activeTaskId, resourceWorkspaceEnvironment, dataExtractionRules, semanticLabelsEnabled, logSortOrder, logPageSize, allFunctionIds, liveListening]);

  async function openSourceLogFromData(record: DataExtractionRecord) {
    const environmentId = Number(record.environment || 0);
    if (!environmentId || !record.query_snapshot?.start_time || !record.query_snapshot?.end_time) return;
    try {
      const environments = await listEnvironments();
      const environment = environments.find((item) => item.id === environmentId);
      if (!environment) return;
      rememberResourceWorkspace(environment);
      setPreferredRemoteEnvironmentId(environment.id);
      setWorkspacePage('logs');
      startRemoteLogSearch(environment, record.query_snapshot as LogWindowRequest, { taskName: record.task_name || `数据还原 · ${record.name}` });
    } catch {
      // 数据页仍可继续使用；无法恢复环境时不打断当前页面。
    }
  }

  function aiRuleFieldFromSpec(spec: Record<string, unknown>, sampleMessage: string, fieldIndex: number): DataExtractionField | undefined {
    const key = String(spec.key || spec.name || '').trim();
    const name = String(spec.name || key).trim();
    const sampleValue = String(spec.sample_value ?? spec.sampleValue ?? '').trim();
    if (!key || !name || !sampleValue) return undefined;
    const explicitType = String(spec.value_type || spec.valueType || '').trim().toLowerCase();
    const valueType: DataValueType = ['number', 'integer', 'boolean', 'string'].includes(explicitType)
      ? explicitType as DataValueType
      : inferDataValueType(sampleValue);
    const structuredSource = spec.structured_path ?? spec.structuredPath;
    const structuredPath = Array.isArray(structuredSource)
      ? structuredSource.map(String).map((item) => item.trim()).filter(Boolean)
      : [];
    const structuredRootHint = String(spec.structured_root_hint || spec.structuredRootHint || '').trim() || undefined;
    const field: DataExtractionField = {
      id: `ai-field-${Date.now()}-${fieldIndex}-${Math.random().toString(16).slice(2)}`,
      key,
      name,
      sampleValue,
      valueType,
      sourceUnit: String(spec.source_unit || spec.sourceUnit || inferDataSourceUnit(sampleValue) || 'auto').trim() || 'auto',
      plotUnit: String(spec.plot_unit || spec.plotUnit || 'source').trim() || 'source',
      unitConversionEnabled: false,
      unitConversions: [],
      structuredPath: structuredPath.length ? structuredPath : undefined,
      structuredRootHint,
    };

    // If the Agent provided a flat key/value example, derive semantic positions
    // deterministically from the real sample rather than making the model count offsets.
    if (!structuredPath.length) {
      const haystack = sampleMessage.toLowerCase();
      const keyStart = haystack.indexOf(key.toLowerCase());
      const valueStart = sampleMessage.indexOf(sampleValue, keyStart >= 0 ? keyStart + key.length : 0);
      if (keyStart >= 0 && valueStart >= 0) {
        field.keyStart = keyStart;
        field.keyEnd = keyStart + key.length;
        field.valueStart = valueStart;
        field.valueEnd = valueStart + sampleValue.length;
      } else {
        // Structured dict/object logs are common. Match a detected path by
        // display path or final key when possible so the native extractor can reuse it.
        const candidates = detectStructuredDataCandidates(sampleMessage);
        const normalizedKey = key.toLowerCase();
        const candidate = candidates.find((item) => item.displayPath.toLowerCase() === normalizedKey)
          || candidates.find((item) => String(item.path[item.path.length - 1] || '').toLowerCase() === normalizedKey);
        if (candidate) {
          field.structuredPath = [...candidate.path];
          field.structuredRootHint = candidate.rootHint;
          field.sampleValue = candidate.sampleValue;
          field.valueType = candidate.valueType;
          field.sourceUnit = candidate.sourceUnit;
        }
      }
    }
    return field;
  }

  function buildAiGeneratedExtractionRule(detail: Record<string, unknown>): DataExtractionRule | undefined {
    const spec = detail.rule && typeof detail.rule === 'object' ? detail.rule as Record<string, unknown> : undefined;
    if (!spec) return undefined;
    const sampleMessage = String(spec.sample_message || spec.sampleMessage || '').trim();
    if (!sampleMessage) return undefined;
    const rule = createEmptyDataExtractionRule(sampleMessage);
    const requestedFields = Array.isArray(spec.fields) ? spec.fields : [];
    const fields = requestedFields.flatMap((item, index) => {
      if (!item || typeof item !== 'object') return [];
      const field = aiRuleFieldFromSpec(item as Record<string, unknown>, sampleMessage, index);
      return field ? [field] : [];
    });
    if (!fields.length) return undefined;
    const taskRequest = activeTask?.remoteRequest;
    const taskTargets = taskRequest?.fm_targets || [];
    rule.id = `ai-data-extractor-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    rule.name = String(spec.name || `自动提取 · ${fields.map((field) => field.name).slice(0, 3).join(' / ')}`).trim().slice(0, 120);
    rule.description = String(spec.description || 'TracePilot 根据当前真实日志样例自动生成并验证').trim().slice(0, 400);
    rule.matchKeyword = String(spec.match_keyword || spec.matchKeyword || '').trim().slice(0, 240);
    rule.caseSensitive = Boolean(spec.case_sensitive || spec.caseSensitive);
    const sourceCategorySpec = spec.source_categories ?? spec.sourceCategories;
    rule.sourceCategories = Array.isArray(sourceCategorySpec)
      ? sourceCategorySpec.map(String).filter(Boolean)
      : [...(taskRequest?.source_categories || [])];
    rule.subsystems = Array.isArray(spec.subsystems) && spec.subsystems.length
      ? spec.subsystems.map(String).filter(Boolean)
      : Array.from(new Set(taskTargets.map((item) => item.subsystem).filter(Boolean)));
    rule.modules = Array.isArray(spec.modules) && spec.modules.length
      ? spec.modules.map(String).filter(Boolean)
      : Array.from(new Set(taskTargets.map((item) => item.fm).filter(Boolean)));
    rule.fields = fields;
    rule.sampleMessage = sampleMessage;
    rule.outputFormat = 'table';
    rule.enabled = true;
    rule.updatedAt = Date.now();
    return rule;
  }

  function equivalentExtractionRule(candidate: DataExtractionRule): DataExtractionRule | undefined {
    const fieldSignature = (rule: DataExtractionRule) => rule.fields
      .map((field) => `${field.key.toLowerCase()}=>${field.name.toLowerCase()}@${(field.structuredPath || []).join('.')}`)
      .sort().join('|');
    const candidateSignature = fieldSignature(candidate);
    return dataExtractionRules.find((rule) => rule.enabled
      && fieldSignature(rule) === candidateSignature
      && JSON.stringify([...rule.sourceCategories].sort()) === JSON.stringify([...candidate.sourceCategories].sort())
      && JSON.stringify([...rule.subsystems].sort()) === JSON.stringify([...candidate.subsystems].sort())
      && JSON.stringify([...rule.modules].sort()) === JSON.stringify([...candidate.modules].sort()));
  }

  async function executeAiGeneratedDataCapability(detail: Record<string, unknown>) {
    if (!activeTask || activeTask.status !== 'ready' || !activeTask.entries.length) {
      pendingAiExtractionRef.current = detail;
      setWorkspacePage('logs');
      return;
    }
    const candidate = buildAiGeneratedExtractionRule(detail);
    if (!candidate) {
      setDataExtractionDialog({ open: true, phase: 'error', candidates: [], selectedIds: new Set(), error: 'TracePilot 没有生成可验证的数据提取规则；请先提供包含目标数据的真实日志样例。' });
      return;
    }
    const validationError = validateDataExtractionRule(candidate);
    if (validationError) {
      setDataExtractionDialog({ open: true, phase: 'error', candidates: [{ rule: candidate }], selectedIds: new Set(), error: `自动提取规则校验失败：${validationError}` });
      return;
    }
    const existing = equivalentExtractionRule(candidate);
    const rule = existing || candidate;
    const validationSource = metricEntries.length ? metricEntries : activeTask.entries;
    const sampleEntries = validationSource.length > 12000
      ? Array.from({ length: 12000 }, (_, index) => validationSource[Math.min(validationSource.length - 1, Math.floor(index * validationSource.length / 12000))])
      : validationSource;
    const minMatches = Math.max(1, Math.min(Number(detail.min_matches || 1), 100));
    let matchCount = 0;
    const previews: string[] = [];
    for (const entry of sampleEntries) {
      const row = extractDataRow(entry, rule);
      if (!row) continue;
      matchCount += 1;
      if (previews.length < 3) previews.push(Object.entries(row.values).map(([key, value]) => `${key}=${String(value)}`).join(', '));
      if (matchCount >= Math.max(minMatches, 20)) break;
    }
    if (matchCount < minMatches) {
      setDataExtractionDialog({
        open: true,
        phase: 'error',
        candidates: [{ rule }],
        selectedIds: new Set(),
        error: `自动生成的提取能力在当前日志样本中只命中 ${matchCount} 行，未达到验证门槛 ${minMatches}；规则未保存。TracePilot 可以换一条真实样例后重试。`,
      });
      return;
    }
    if (!existing && detail.persist_rule !== false) {
      setDataExtractionRules((current) => [candidate, ...current]);
    }
    if (detail.open_rule_settings === true) {
      window.sessionStorage.setItem('tracelens-ai-rule-tab-v1', 'data');
      setPreferredSettingsTab('rules');
      setWorkspacePage('platform-settings');
      if (detail.auto_start === false) return;
    }
    if (detail.auto_start === false) {
      setDataExtractionDialog({
        open: true,
        phase: 'select',
        candidates: [{ rule }],
        selectedIds: new Set([rule.id]),
        error: previews.length ? `已验证命中至少 ${matchCount} 行；样例：${previews[0]}` : `已验证命中至少 ${matchCount} 行。`,
      });
      return;
    }
    await startSelectedDataExtraction([rule], { openDataPage: detail.open_data_page !== false });
  }

  function resolveAiExtractionRules(detail: Record<string, unknown>): DataExtractionRule[] {
    const requestedIds = new Set(Array.isArray(detail.rule_ids) ? detail.rule_ids.map(String).filter(Boolean) : []);
    const requestedFields = new Set(Array.isArray(detail.field_names) ? detail.field_names.map((item) => String(item).trim().toLowerCase()).filter(Boolean) : []);
    return dataExtractionRules.filter((rule) => {
      if (!rule.enabled) return false;
      if (requestedIds.size && requestedIds.has(rule.id)) return true;
      if (!requestedFields.size) return false;
      const tokens = new Set(rule.fields.flatMap((field) => [field.key, field.name]).map((item) => String(item || '').trim().toLowerCase()).filter(Boolean));
      return [...requestedFields].every((field) => tokens.has(field));
    });
  }

  async function executeAiDataExtraction(detail: Record<string, unknown>) {
    const selectedRules = resolveAiExtractionRules(detail);
    if (!selectedRules.length) {
      window.sessionStorage.setItem('tracelens-ai-rule-tab-v1', 'data');
      setPreferredSettingsTab('rules');
      setWorkspacePage('platform-settings');
      return;
    }
    if (detail.auto_start === false) {
      setDataExtractionDialog({
        open: true, phase: 'select', candidates: selectedRules.map((rule) => ({ rule })),
        selectedIds: new Set(selectedRules.map((rule) => rule.id)), error: undefined, progress: undefined, results: undefined, recordId: undefined, recordSaved: undefined,
      });
      return;
    }
    await startSelectedDataExtraction(selectedRules, { openDataPage: detail.open_data_page !== false });
  }

  /** 实时采集清单的当前内容。弹窗用它判断按钮状态，避免读打开时的旧快照。 */
  const liveCaptureRuleIds = useMemo(
    () => new Set(dataExtractionRules.filter((rule) => rule.liveCapture === true).map((rule) => rule.id)),
    [dataExtractionRules],
  );

  const liveCaptureRules = useMemo(
    () => dataExtractionRules.filter((rule) => liveCaptureRuleIds.has(rule.id)),
    [dataExtractionRules, liveCaptureRuleIds],
  );
  /**
   * 实时采集进度 —— 弹窗里的进度条和工具栏的转圈图标读同一份，不各数一遍。
   * 关闭实时监听时这个 hook 自己会把计数清空。
   */
  const liveCaptureProgress = useLiveCaptureProgress({
    enabled: liveListening,
    environmentId: activeTask?.remoteEnvironmentId,
    rules: liveCaptureRules,
  });
  /** 正在采集：批量采集跑着，或者实时监听开着且确实有采集项。 */
  const collecting = dataExtractionDialog.phase === 'running'
    || (liveListening && liveCaptureRuleIds.size > 0);

  /**
   * 关掉实时监听时清空这一轮的采集项。
   *
   * 只在**用户主动关闭**（工具栏开关、助手停止采集）时调用，不监听 liveListening 的状态变化：
   * 开始监听的过程本身会让 liveListening 短暂地真→假→真（任务切换），
   * 用状态变化触发会把用户刚勾好的采集项悄悄抹掉 —— 实测踩过。
   */
  function clearLiveCaptureItems() {
    setDataExtractionRules((current) => current.some((rule) => rule.liveCapture)
      ? current.map((rule) => rule.liveCapture ? { ...rule, liveCapture: false, updatedAt: Date.now() } : rule)
      : current);
  }

  /**
   * 弹窗里的「开始采集（实时采集）」：勾选项**成为**这一轮的实时采集清单。
   *
   * 用「开始采集」表达采集方式，而不是让人先点一堆「加入实时采集」按钮 ——
   * 采集项本来就是「这一轮要采什么」，和启动动作是同一件事。
   */
  function startLiveCollection(ruleIds: string[]): string | undefined {
    const wanted = new Set(ruleIds);
    setDataExtractionRules((current) => current.map((rule) => {
      const next = wanted.has(rule.id);
      return (rule.liveCapture === true) === next ? rule : { ...rule, liveCapture: next, updatedAt: Date.now() };
    }));
    if (!liveListening) {
      return `已选中 ${ruleIds.length} 项作为实时采集项；打开工具栏的「实时监听」后进度会开始更新。`;
    }
    return undefined;
  }

  /**
   * 数据提取弹窗里的「加入实时采集」：直接写回提取器上的 liveCapture。
   *
   * 和采集面板的勾选是同一个字段、同一份规则列表，所以两边永远一致 ——
   * 这里不新开一份「实时采集清单」，避免同一个概念出现两个说法。
   */
  function setLiveCaptureForRules(ruleIds: string[], enabled: boolean) {
    const wanted = new Set(ruleIds);
    setDataExtractionRules((current) => current.map((rule) => wanted.has(rule.id)
      ? { ...rule, liveCapture: enabled, updatedAt: Date.now() }
      : rule));
  }

  function beginDataExtraction() {
    const enabledRules = dataExtractionRules.filter((rule) => rule.enabled);
    // 实时监听期间当前任务没有已解析日志，但**实时采集照样要能配置**：
    // 采集项的列表来自提取器本身，不依赖当前有没有日志。
    // （之前这里直接返回「当前没有可提取的已解析日志」，于是开了实时监听就一条采集项都看不到。）
    const hasBatchSource = Boolean(activeTask && activeTask.status === 'ready' && activeTask.entries.length);
    setDataExtractionDialog({
      open: true,
      phase: 'select',
      candidates: enabledRules.map((rule) => ({ rule })),
      selectedIds: new Set(),
      batchAvailable: hasBatchSource,
      error: enabledRules.length ? undefined : '还没有启用的数据提取器；先到「设置 → 日志规则 → 数据提取」新建一个。',
      progress: undefined,
      results: undefined,
      recordId: undefined,
      recordSaved: undefined,
    });
  }

  async function startSelectedDataExtraction(forcedRules?: DataExtractionRule[], options?: { openDataPage?: boolean }) {
    if (!activeTask) {
      setDataExtractionDialog({ open: true, phase: 'error', candidates: [], selectedIds: new Set(), error: '当前没有可提取的已解析日志。' });
      return;
    }
    const selectedRules = forcedRules?.length
      ? forcedRules
      : dataExtractionDialog.candidates.filter((item) => dataExtractionDialog.selectedIds.has(item.rule.id)).map((item) => item.rule);
    if (!selectedRules.length) return;
    const controller = new AbortController();
    dataExtractionAbortRef.current = controller;
    setDataExtractionDialog((current) => ({ ...current, phase: 'running', progress: undefined, error: undefined }));

    const startTime = activeTask.remoteRequest?.start_time || activeTask.entries[0]?.timestamp;
    const endTime = activeTask.remoteRequest?.end_time || activeTask.entries.at(-1)?.timestamp;
    if (!startTime || !endTime) {
      setDataExtractionDialog((current) => ({ ...current, phase: 'error', error: '当前任务缺少可用的日志时间范围。' }));
      return;
    }

    let record: DataExtractionRecord | undefined;
    try {
      if (activeTask.remoteEnvironmentId && activeTask.remoteRequest) {
        const environmentName = resourceWorkspaceEnvironment && resourceWorkspaceEnvironment.id === activeTask.remoteEnvironmentId
          ? resourceWorkspaceEnvironment.name
          : '';
        record = await createDataExtractionRecord({
          source_operation_id: activeTask.remoteOperationId || '',
          environment: activeTask.remoteEnvironmentId,
          environment_name: environmentName,
          task_name: activeTask.name,
          name: selectedRules.length === 1 ? selectedRules[0].name : `${selectedRules.length} 项数据提取`,
          query_snapshot: activeTask.remoteRequest,
          rule_snapshots: selectedRules as unknown as Array<Record<string, unknown>>,
          status: 'running',
          matched_rule_count: 0,
          row_count: 0,
          result_summary: [],
          hour_summary: [],
          error_message: '',
        });
      }

      const result = await extractFromLoadedEntries({
        entries: activeTask.entries,
        rules: selectedRules,
        startTime,
        endTime,
        signal: controller.signal,
        onProgress: (progress) => setDataExtractionDialog((current) => ({ ...current, progress })),
      });
      const summary = result.results.map(({ rule, rows }) => ({
        rule_id: rule.id,
        rule_name: rule.name,
        row_count: rows.length,
        fields: rule.fields.map((field) => field.name || field.key),
        output_format: rule.outputFormat,
      }));
      const totalRows = summary.reduce((sum, item) => sum + item.row_count, 0);
      const matchedRuleCount = summary.filter((item) => item.row_count > 0).length;
      if (record) {
        record = await updateDataExtractionRecord(record.id, {
          status: totalRows > 0 ? 'success' : 'no_result',
          matched_rule_count: matchedRuleCount,
          row_count: totalRows,
          result_summary: summary,
          hour_summary: result.hourSummary,
          error_message: '',
          finished_at: new Date().toISOString(),
        });
      }
      const sessionKey = record ? temporarySessionKey(record.id) : temporarySessionKey(`${activeTask.id}-${Date.now()}`);
      saveTemporaryExtractionSession({
        key: sessionKey,
        recordId: record?.id,
        taskName: activeTask.name,
        environmentName: resourceWorkspaceEnvironment?.name,
        startTime,
        endTime,
        createdAt: Date.now(),
        results: result.results,
      });
      setDataExtractionDialog((current) => ({
        ...current,
        phase: 'done',
        results: result.results,
        recordId: record?.id,
        recordSaved: Boolean(record),
        progress: { phase: 'done', hourIndex: result.hourSummary.length, hourCount: result.hourSummary.length, percent: 100, processedEntries: activeTask.entries.length, totalRows, message: `提取完成，共 ${totalRows.toLocaleString()} 行` },
      }));
      if (options?.openDataPage) {
        window.setTimeout(() => {
          setDataExtractionDialog((current) => ({ ...current, open: false }));
          openDataPage(activeTask.remoteOperationId || '');
        }, 250);
      }
    } catch (exc) {
      const cancelled = controller.signal.aborted || (exc instanceof DOMException && exc.name === 'AbortError');
      const message = cancelled ? '数据提取已停止。' : (exc instanceof Error ? exc.message : String(exc));
      if (record) {
        void updateDataExtractionRecord(record.id, { status: cancelled ? 'cancelled' : 'failed', error_message: message, finished_at: new Date().toISOString() }).catch(() => undefined);
      }
      setDataExtractionDialog((current) => ({ ...current, phase: cancelled ? 'error' : 'error', error: message, recordId: record?.id, recordSaved: Boolean(record) }));
    } finally {
      dataExtractionAbortRef.current = undefined;
    }
  }

  useEffect(() => {
    const pending = pendingAiExtractionRef.current;
    if (!pending || !activeTask || activeTask.status !== 'ready' || !activeTask.entries.length) return;
    pendingAiExtractionRef.current = undefined;
    if (String(pending.type || '') === 'create_data_extraction_capability') void executeAiGeneratedDataCapability(pending);
    else void executeAiDataExtraction(pending);
  }, [activeTask?.id, activeTask?.status, activeTask?.entries.length, dataExtractionRules]);

  async function locateLogsFromReport(payload: { environment: EnvironmentSummary; subsystem: string; module: string; startTime?: string; endTime?: string; taskName?: string }) {
    if (payload.startTime && payload.endTime) {
      const startNs = timestampTextToNs(payload.startTime);
      const endNs = timestampTextToNs(payload.endTime);
      if (startNs !== undefined && endNs !== undefined && endNs - startNs > MAX_REMOTE_LOG_RANGE_NS) {
        window.alert('场景还原日志时间范围最多支持 3 天，请缩小报告时间范围。');
        return;
      }
    }
    rememberResourceWorkspace(payload.environment);
    setPreferredRemoteEnvironmentId(payload.environment.id);

    let targets: Array<{ subsystem: string; fm: string; kind?: 'normal' | 'executor' }> = [
      { subsystem: payload.subsystem, fm: payload.module, kind: 'normal' },
    ];
    try {
      const catalog = await getGlobalLogCatalogTree(false);
      const allModules = catalog.flatMap((subsystem) => subsystem.fms.map((fm) => ({ subsystem, fm })));
      const current = allModules.find(({ subsystem, fm }) => subsystem.name === payload.subsystem && fm.name === payload.module);
      if (current) {
        const targetModuleIds = new Set(current.fm.target_module_ids || []);
        targets = [
          { subsystem: current.subsystem.name, fm: current.fm.name, kind: current.fm.kind ?? 'normal' },
          ...allModules
            .filter(({ fm }) => targetModuleIds.has(fm.id) && fm.enabled)
            .map(({ subsystem, fm }) => ({ subsystem: subsystem.name, fm: fm.name, kind: fm.kind ?? 'normal' as const })),
        ];
      }
    } catch {
      // 目标模块配置读取失败不能阻断场景还原；至少检索报告自身模块。
    }
    targets = Array.from(new Map(targets.map((item) => [`${item.subsystem}\u0000${item.fm}\u0000${item.kind ?? 'normal'}`, item])).values());
    const request: LogWindowRequest | undefined = payload.startTime && payload.endTime ? {
      start_time: payload.startTime,
      end_time: payload.endTime,
      source_categories: Array.from(new Set(['debug', ...(targets.some((item) => item.kind === 'executor') ? ['executor'] : [])])),
      subsystems: Array.from(new Set(targets.map((item) => item.subsystem))),
      fms: Array.from(new Set(targets.map((item) => item.fm))),
      fm_targets: targets,
    } : undefined;

    setPreferredRemotePreset({
      token: `report-${payload.environment.id}-${payload.subsystem}-${payload.module}-${payload.startTime || ''}-${payload.endTime || ''}`,
      environmentId: payload.environment.id,
      subsystem: payload.subsystem,
      module: payload.module,
      startTime: payload.startTime,
      endTime: payload.endTime,
      taskName: payload.taskName,
      request,
    });
    setWorkspacePage('logs');
    if (request) startRemoteLogSearch(payload.environment, request, { taskName: payload.taskName });
  }

  function remoteTaskIdentity(environment: EnvironmentSummary, request: LogWindowRequest): string {
    const targets = [...(request.fm_targets || [])]
      .map((item) => `${item.subsystem}/${item.fm}/${item.kind ?? 'normal'}`)
      .sort()
      .join(',');
    const sources = [...(request.source_categories || [])].sort().join(',');
    return [environment.id, sources, targets, request.start_time, request.end_time, request.keyword || ''].join('|');
  }

  function remoteTimeTaskName(request: LogWindowRequest): string {
    const start = request.start_time.replace('T', ' ');
    const end = request.end_time.replace('T', ' ');
    const startDay = start.slice(0, 10);
    const endDay = end.slice(0, 10);
    if (startDay === endDay) return `${start.slice(5, 16)} ~ ${end.slice(11, 19)}`;
    return `${start.slice(5, 16)} ~ ${end.slice(5, 16)}`;
  }

  /**
   * Start 实时监听 for one component — the single implementation behind both the toolbar
   * switch and the assistant's `start_live_monitoring` action.
   *
   * Returns a receipt-shaped result so a UI action can report honestly instead of claiming
   * success for a stream that never opened.
   */
  function beginLiveMonitoring(input: {
    environment: EnvironmentSummary;
    target: NonNullable<LogWindowRequest['fm_targets']>[number];
    sourceCategories?: string[];
    startTime?: string;
    endTime?: string;
  }): { detail: string } {
    const { environment, target } = input;
    setResourceWorkspaceFeature('logs');
    setLiveAddedCount(0);
    const liveRequest: LogWindowRequest = {
      start_time: input.startTime || new Date(Date.now() - 60_000).toISOString(),
      end_time: input.endTime || new Date().toISOString(),
      source_categories: (input.sourceCategories && input.sourceCategories.length) ? [...input.sourceCategories] : ['debug'],
      subsystems: [target.subsystem],
      fms: [target.fm],
      fm_targets: [{ ...target }],
      keyword: '',
    };
    // Live monitoring always gets its own tab. Reusing the queried tab meant enabling
    // 实时监听 *cleared* the logs you were reading, and a streaming task (no time range)
    // does not belong in the same tab as a snapshot.
    const taskNumber = taskSequenceRef.current;
    taskSequenceRef.current += 1;
    const boundTaskId = `live-${environment.id}-${Date.now()}-${taskNumber}`;
    const liveTask: LogTask = {
      id: boundTaskId,
      name: `实时 · ${environment.name} · ${target.fm}`,
      sourceNames: [],
      sources: [],
      createdAt: Date.now(),
      status: 'ready',
      entries: [],
      issues: [],
      remoteEnvironmentId: environment.id,
      remoteRequest: liveRequest,
      liveTargets: [{ ...target }],
      viewFilters: cloneFilters(EMPTY_FILTERS),
    };
    setTasks((current) => [...current, liveTask]);
    setActiveTaskId(boundTaskId);
    rememberResourceWorkspace(environment);
    setPreferredRemoteEnvironmentId(environment.id);
    setWorkspacePage('logs');
    resetViewState(false);
    setFilters(cloneFilters(EMPTY_FILTERS));
    setTasks((current) => current.map((task) => task.id === boundTaskId ? { ...task, liveTargets: [{ ...target }] } : task));

    liveBoundTaskIdRef.current = boundTaskId;
    liveRenderGenerationRef.current += 1;
    liveRenderQueueRef.current = Promise.resolve();
    livePendingFollowRef.current = true;
    liveFollowTailRef.current = true;
    setFilters((current) => ({ ...current, timeRange: undefined }));
    setCustomStartTime('');
    setCustomEndTime('');
    setSelectedEntry(undefined);
    setFocusedEntryId(undefined);
    setSelectedProcessId(undefined);
    setSelectedThreadId(undefined);
    setSelectedTraceId(undefined);
    setSelectedCrossTraceId(undefined);
    setExpandedIds(new Set());
    setLogSortOrder('asc');
    setLogPage(1);
    setLiveMessage('');
    setLiveListening(true);
    return { detail: `已开始实时监听 ${environment.name} · ${target.subsystem}/${target.fm}` };
  }

  function startRemoteLogSearch(environment: EnvironmentSummary, request: LogWindowRequest, meta?: { taskName?: string; force?: boolean; liveTargets?: LogWindowRequest['fm_targets'] }) {
    setResourceWorkspaceFeature('logs');
    const taskKey = remoteTaskIdentity(environment, request);
    const existing = tasks.find((task) => task.remoteTaskKey === taskKey);
    if (existing && !meta?.force) {
      if (meta?.liveTargets) {
        setTasks((current) => current.map((task) => task.id === existing.id ? { ...task, liveTargets: [...meta.liveTargets!] } : task));
      }
      rememberResourceWorkspace(environment);
      setPreferredRemoteEnvironmentId(environment.id);
      setWorkspacePage('logs');
      activateTask(existing.id);
      return;
    }
    const taskNumber = taskSequenceRef.current;
    taskSequenceRef.current += 1;
    const taskId = `remote-${environment.id}-${Date.now()}-${taskNumber}`;
    const operationId = typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID()
      : `search-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const controller = new AbortController();
    const requestedRange = requestTimeRangeNs(request);
    const placeholder: LogTask = {
      id: taskId,
      name: meta?.taskName?.trim() || remoteTimeTaskName(request),
      sourceNames: [],
      sources: [],
      createdAt: Date.now(),
      status: 'analyzing',
      entries: [],
      issues: [],
      remoteEnvironmentId: environment.id,
      remoteOperationId: operationId,
      remoteRequest: request,
      liveTargets: meta?.liveTargets ? [...meta.liveTargets] : undefined,
      remoteTaskKey: taskKey,
      loadedStartNs: requestedRange?.startNs,
      loadedEndNs: requestedRange?.endNs,
      viewFilters: { ...cloneFilters(EMPTY_FILTERS), query: request.keyword || '' },
      remoteProgress: { operation_id: operationId, stage: 'planning', percent: 2, done: false, message: '正在检查日志缓存' },
    };
    remoteTaskControllersRef.current.set(taskId, { controller, environmentId: environment.id, operationId });
    setTasks((current) => [...current, placeholder]);
    setActiveTaskId(taskId);
    rememberResourceWorkspace(environment);
    setWorkspacePage('logs');
    resetViewState(false);
    setFilters({ ...cloneFilters(EMPTY_FILTERS), query: request.keyword || '' });

    void fetchLogWindow(environment.id, request, {
      operationId,
      signal: controller.signal,
      onProgress: (progress) => setTasks((current) => current.map((task) => task.id === taskId ? { ...task, remoteProgress: progress } : task)),
    }).then(({ blob, operationId: serverOperationId }) => {
      remoteTaskControllersRef.current.delete(taskId);
      const sourceName = `${environment.upper_machine.host}-${request.start_time}-${request.end_time}.log`;
      const source: WorkerImportSource = { kind: 'file', id: `${taskId}-${sourceName}`, name: sourceName, file: new File([blob], sourceName, { type: 'text/plain' }), logCategories: [...(request.source_categories || [])] };
      setTasks((current) => current.map((task) => task.id === taskId ? { ...task, remoteOperationId: serverOperationId, sourceNames: [sourceName], sources: [source] } : task));
      const nextTask = placeholder;
      nextTask.remoteOperationId = serverOperationId;
      nextTask.sourceNames = [sourceName];
      nextTask.sources = [source];
      // 远端已经按检索时间裁剪完成；完整读取返回流，确保每个归档/模块的日志类型标记不丢失。
      startStreamingImport(nextTask, [source], { replaceTask: true, strategy: 'full' });
    }).catch((exc) => {
      remoteTaskControllersRef.current.delete(taskId);
      const cancelled = controller.signal.aborted || (exc instanceof DOMException && exc.name === 'AbortError');
      setTasks((current) => current.map((task) => task.id === taskId ? {
        ...task,
        status: cancelled ? 'cancelled' : 'error',
        errorMessage: cancelled ? '检索已停止' : (exc instanceof Error ? exc.message : String(exc)),
        remoteProgress: { ...(task.remoteProgress ?? { operation_id: operationId, stage: 'error', percent: 0, done: true }), stage: cancelled ? 'cancelled' : 'error', done: true, message: cancelled ? '检索已停止' : (exc instanceof Error ? exc.message : String(exc)) },
      } : task));
    });
  }

  useEffect(() => {
    if (workspacePage !== 'reports' || reportEnvironment) return;
    const environmentId = initialRouteEnvironmentIdRef.current;
    if (!environmentId) return;
    void listEnvironments().then((environments) => {
      const environment = environments.find((item) => item.id === environmentId);
      if (environment) setReportEnvironment(environment);
    }).catch(() => undefined);
  }, [workspacePage, reportEnvironment]);

  function currentSharedLogScene(): SharedLogScene | undefined {
    if (!activeTask?.remoteEnvironmentId || !activeTask.remoteRequest) return undefined;
    const selectedError = selectedErrorIndex >= 0 ? navigableErrorEntries[selectedErrorIndex] : undefined;
    return {
      version: 1,
      environmentId: activeTask.remoteEnvironmentId,
      taskName: activeTask.name,
      request: activeTask.remoteRequest,
      filters: {
        query: filters.query,
        levels: [...filters.levels],
        components: [...filters.components],
        modes: [...filters.modes],
        timeRange: filters.timeRange ? {
          startNs: filters.timeRange.startNs.toString(),
          endNs: filters.timeRange.endNs.toString(),
        } : undefined,
        durationRange: filters.durationRange ? { ...filters.durationRange } : undefined,
        errorsOnly: Boolean(filters.errorsOnly),
      },
      view: {
        showTimeline,
        timelineDocked,
        timelineGroupingMode,
        semanticLabelsEnabled,
        rawLogMode,
        processTimelineExpanded,
        hiddenTimelineComponents: [...timelineHiddenComponents],
        logPageSize,
        logSortOrder,
        error: selectedError ? {
          index: selectedErrorIndex,
          timestampNs: selectedError.timestampNs?.toString(),
          component: selectedError.component,
          sourceFile: selectedError.source.fileName,
          sourceLine: selectedError.source.lineNumber,
        } : undefined,
      },
    };
  }

  useEffect(() => {
    const scene = initialSharedScene;
    if (!scene || sharedSceneLaunchRef.current) return;
    sharedSceneLaunchRef.current = true;
    pendingSharedSceneRef.current = scene;
    setWorkspacePage('logs');
    setPreferredRemoteEnvironmentId(scene.environmentId);
    setPreferredRemotePreset({
      token: `url-${scene.environmentId}-${Date.now()}`,
      environmentId: scene.environmentId,
      taskName: scene.taskName,
      request: scene.request,
      startTime: scene.request.start_time,
      endTime: scene.request.end_time,
    });
    // 先恢复显示开关；日志结果若命中最终结果缓存，后端会在文件计划前直接返回。
    setShowTimeline(scene.view.showTimeline !== false);
    setTimelineDocked(Boolean(scene.view.timelineDocked));
    setTimelineGroupingMode(scene.view.timelineGroupingMode === 'process' ? 'process' : 'merged');
    setSemanticLabelsEnabled(scene.view.semanticLabelsEnabled !== false);
    setRawLogMode(Boolean(scene.view.rawLogMode));
    setProcessTimelineExpanded(scene.view.processTimelineExpanded !== false);
    setTimelineHiddenComponents(new Set(scene.view.hiddenTimelineComponents || []));
    setLogPageSize(Math.max(100, Math.min(MAX_LOG_PAGE_SIZE, scene.view.logPageSize || DEFAULT_LOG_PAGE_SIZE)));
    setLogSortOrder(scene.view.logSortOrder === 'desc' ? 'desc' : 'asc');

    void listEnvironments()
      .then((environments) => {
        const environment = environments.find((item) => item.id === scene.environmentId);
        if (!environment) throw new Error(`URL 中的环境资源 #${scene.environmentId} 已不存在`);
        startRemoteLogSearch(environment, scene.request, { taskName: scene.taskName });
      })
      .catch((error) => {
        pendingSharedSceneRef.current = undefined;
        console.error('[url-log-scene] restore failed', error);
      });
  }, [initialSharedScene]);

  useEffect(() => {
    const scene = pendingSharedSceneRef.current;
    if (!scene || !activeTask || activeTask.status !== 'ready' || activeTask.remoteEnvironmentId !== scene.environmentId) return;
    const requestIdentity = JSON.stringify(activeTask.remoteRequest ?? {});
    if (requestIdentity !== JSON.stringify(scene.request)) return;

    setFilters(sharedSceneFilters(scene));
    setShowTimeline(scene.view.showTimeline !== false);
    setTimelineDocked(Boolean(scene.view.timelineDocked));
    setTimelineGroupingMode(scene.view.timelineGroupingMode === 'process' ? 'process' : 'merged');
    setSemanticLabelsEnabled(scene.view.semanticLabelsEnabled !== false);
    setRawLogMode(Boolean(scene.view.rawLogMode));
    setProcessTimelineExpanded(scene.view.processTimelineExpanded !== false);
    setTimelineHiddenComponents(new Set(scene.view.hiddenTimelineComponents || []));
    setLogPageSize(Math.max(100, Math.min(MAX_LOG_PAGE_SIZE, scene.view.logPageSize || DEFAULT_LOG_PAGE_SIZE)));
    setLogSortOrder(scene.view.logSortOrder === 'desc' ? 'desc' : 'asc');
    pendingSharedErrorRef.current = scene.view.error;
    pendingSharedSceneRef.current = undefined;
    setSharedSceneRestoreRevision((value) => value + 1);
  }, [activeTask?.id, activeTask?.status, activeTask?.remoteEnvironmentId, activeTask?.remoteRequest]);

  useEffect(() => {
    const anchor = pendingSharedErrorRef.current;
    if (sharedSceneRestoreRevision <= 0 || !anchor || activeTask?.status !== 'ready') return;
    if (navigableErrorEntries.length === 0) {
      pendingSharedErrorRef.current = undefined;
      return;
    }
    let target: LogEntry | undefined;
    if (anchor.timestampNs) {
      target = navigableErrorEntries.find((entry) => (
        entry.timestampNs?.toString() === anchor.timestampNs
        && (!anchor.component || entry.component === anchor.component)
        && (!anchor.sourceFile || entry.source.fileName === anchor.sourceFile)
        && (!anchor.sourceLine || entry.source.lineNumber === anchor.sourceLine)
      ));
      if (!target) {
        target = navigableErrorEntries.find((entry) => (
          entry.timestampNs?.toString() === anchor.timestampNs
          && (!anchor.component || entry.component === anchor.component)
        ));
      }
    }
    target ??= navigableErrorEntries[Math.max(0, Math.min(navigableErrorEntries.length - 1, anchor.index || 0))];
    pendingSharedErrorRef.current = undefined;
    if (target) jumpToLogEntry(target);
  }, [activeTask?.status, filters, navigableErrorEntries, sharedSceneRestoreRevision, timelineHiddenComponents]);

  useEffect(() => {
    if (typeof window === 'undefined') return;
    if (workspacePage !== 'logs') {
      writeWorkspacePageUrl(workspacePage, workspacePage === 'reports' ? reportEnvironment?.id : undefined, rawLogMode);
      return;
    }
    // 通过 URL 首次恢复时，任务尚未完成前保留原始参数，不要抢先改成空日志页。
    if (pendingSharedSceneRef.current || (initialSharedScene && !sharedSceneLaunchRef.current)) return;
    if (!activeTask?.remoteRequest || activeTask.status !== 'ready') {
      if (!initialSharedScene && activeTask?.status !== 'analyzing') writeWorkspacePageUrl('logs', undefined, rawLogMode);
      return;
    }
    const scene = currentSharedLogScene();
    if (scene) writeSharedLogSceneUrl(scene);
  }, [
    workspacePage,
    reportEnvironment?.id,
    activeTask?.id,
    activeTask?.status,
    activeTask?.remoteEnvironmentId,
    activeTask?.remoteRequest,
    activeTask?.name,
    filters,
    showTimeline,
    timelineDocked,
    timelineGroupingMode,
    semanticLabelsEnabled,
    rawLogMode,
    processTimelineExpanded,
    timelineHiddenComponents,
    logPageSize,
    logSortOrder,
    selectedErrorIndex,
    navigableErrorEntries,
    initialSharedScene,
  ]);

  async function extendRemoteLogTaskRange(task: LogTask, targetRange: TimeRangeFilter) {
    if (!task.remoteEnvironmentId || !task.remoteRequest || task.status !== 'ready' || task.incrementalLoading) return;
    if (targetRange.endNs - targetRange.startNs > MAX_REMOTE_LOG_RANGE_NS) {
      setTasks((current) => current.map((item) => item.id === task.id ? {
        ...item,
        incrementalMessage: '时间窗口最多支持 3 天，请缩小 Brush 选区。',
      } : item));
      return;
    }
    const requestRange = requestTimeRangeNs(task.remoteRequest);
    let loadedStartNs = task.loadedStartNs ?? requestRange?.startNs;
    let loadedEndNs = task.loadedEndNs ?? requestRange?.endNs;
    if (loadedStartNs === undefined || loadedEndNs === undefined) return;

    const missingRanges: TimeRangeFilter[] = [];
    if (targetRange.startNs < loadedStartNs) missingRanges.push({ startNs: targetRange.startNs, endNs: loadedStartNs });
    if (targetRange.endNs > loadedEndNs) missingRanges.push({ startNs: loadedEndNs, endNs: targetRange.endNs });
    if (missingRanges.length === 0) return;

    const controller = new AbortController();
    let addedCount = 0;
    setTasks((current) => current.map((item) => item.id === task.id ? {
      ...item,
      incrementalLoading: true,
      incrementalAddedCount: 0,
      incrementalMessage: missingRanges.length > 1 ? '正在补充时间窗两侧缺失日志…' : '正在补充缺失时间段日志…',
    } : item));

    try {
      for (const missing of missingRanges) {
        if (controller.signal.aborted) throw new DOMException('Aborted', 'AbortError');
        const operationId = typeof crypto !== 'undefined' && 'randomUUID' in crypto
          ? crypto.randomUUID()
          : `increment-${Date.now()}-${Math.random().toString(16).slice(2)}`;
        remoteTaskControllersRef.current.set(task.id, {
          controller,
          environmentId: task.remoteEnvironmentId,
          operationId,
        });
        const incrementalRequest: LogWindowRequest = {
          ...task.remoteRequest,
          start_time: nsToTimestampText(missing.startNs),
          end_time: nsToQueryEndTimestampText(missing.endNs),
        };
        const rangeLabel = `${formatNsTick(missing.startNs, true, false)} ~ ${formatNsTick(missing.endNs, true, false)}`;
        setTasks((current) => current.map((item) => item.id === task.id ? {
          ...item,
          incrementalMessage: `正在增量检索 ${rangeLabel}`,
        } : item));

        const { blob, operationId: serverOperationId } = await fetchLogWindow(task.remoteEnvironmentId, incrementalRequest, {
          operationId,
          signal: controller.signal,
          onProgress: (progress) => setTasks((current) => current.map((item) => item.id === task.id ? {
            ...item,
            incrementalMessage: `正在增量检索 ${rangeLabel} · ${progress.percent}%${progress.message ? ` · ${progress.message}` : ''}`,
          } : item)),
        });
        const sourceName = `${task.remoteEnvironmentId}-increment-${incrementalRequest.start_time}-${incrementalRequest.end_time}.log`;
        const parsedCount = await importIncrementalRemoteBlob(task, blob, sourceName, serverOperationId);
        addedCount += parsedCount;
        loadedStartNs = missing.startNs < loadedStartNs ? missing.startNs : loadedStartNs;
        // 右侧探索允许拖到“现在之后”的可探索区，但未来时间不能被误记为稳定 coverage。
        // 否则用户稍后执行功能、fm.log 新增日志后，再回到这段时间会被错误认为已经查过。
        const nowNs = BigInt(Date.now()) * 1_000_000n;
        const stableEndNs = missing.endNs > nowNs ? nowNs : missing.endNs;
        loadedEndNs = stableEndNs > loadedEndNs ? stableEndNs : loadedEndNs;
        const nextLoadedStart = loadedStartNs;
        const nextLoadedEnd = loadedEndNs;
        const futureRemainder = missing.endNs > nowNs;
        setTasks((current) => current.map((item) => item.id === task.id ? {
          ...item,
          loadedStartNs: nextLoadedStart,
          loadedEndNs: nextLoadedEnd,
          incrementalMessage: parsedCount > 0
            ? `已补充 ${rangeLabel} · 新增 ${parsedCount.toLocaleString()} 条${futureRemainder ? ' · 未来部分仍保持未加载' : ''}`
            : `已检索 ${rangeLabel} · 当前无日志${futureRemainder ? ' · 未来部分仍保持未加载' : ''}`,
        } : item));
      }

      setTasks((current) => current.map((item) => item.id === task.id ? {
        ...item,
        incrementalLoading: false,
        incrementalAddedCount: addedCount,
        incrementalMessage: addedCount > 0 ? `增量更新完成 · 新增 ${addedCount.toLocaleString()} 条日志` : '增量更新完成 · 新扩展区间暂无日志',
      } : item));
    } catch (exc) {
      const cancelled = controller.signal.aborted || (exc instanceof DOMException && exc.name === 'AbortError');
      setTasks((current) => current.map((item) => item.id === task.id ? {
        ...item,
        incrementalLoading: false,
        incrementalMessage: cancelled ? '增量检索已停止' : `增量检索失败：${exc instanceof Error ? exc.message : String(exc)}`,
      } : item));
    } finally {
      const running = remoteTaskControllersRef.current.get(task.id);
      if (running?.controller === controller) remoteTaskControllersRef.current.delete(task.id);
    }
  }

  function toggleFilter(key: 'levels' | 'components' | 'modes', value: string) {
    setFilters((current) => {
      const next = new Set(current[key]);
      if (next.has(value)) next.delete(value);
      else next.add(value);
      return { ...current, [key]: next };
    });
  }

  function toggleExpanded(id: string) {
    setExpandedIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleMethodGroup(id: string) {
    setExpandedMethodGroups((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleCrossMethodGroup(id: string) {
    setExpandedCrossMethodGroups((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleProcess(id: string) {
    setExpandedProcessIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const clearNavigationSelection = useCallback(() => {
    setSelectedProcessId(undefined);
    setSelectedThreadId(undefined);
    setSelectedTraceId(undefined);
    setSelectedCrossTraceId(undefined);
    setSelectedEntry(undefined);
    setFocusedEntryId(undefined);
    setOptimisticTraceId(undefined);
    setExpandedIds(new Set());
  }, []);

  const navigateToTimelineTime = useCallback((timestampNs: bigint) => {
    if (timestampNavigationEntries.length === 0) return;
    let low = 0;
    let high = timestampNavigationEntries.length;
    while (low < high) {
      const mid = Math.floor((low + high) / 2);
      if (timestampNavigationEntries[mid].timestampNs < timestampNs) low = mid + 1;
      else high = mid;
    }
    const right = low < timestampNavigationEntries.length ? timestampNavigationEntries[low] : undefined;
    const left = low > 0 ? timestampNavigationEntries[low - 1] : undefined;
    const distance = (a: bigint, b: bigint) => a >= b ? a - b : b - a;
    const target = !left
      ? right
      : !right
        ? left
        : distance(left.timestampNs, timestampNs) <= distance(right.timestampNs, timestampNs) ? left : right;
    if (!target || timelineCursorTargetRef.current === target.entry.id) return;
    timelineCursorTargetRef.current = target.entry.id;
    setSelectedEntry(undefined);
    setFocusedEntryId(target.entry.id);
    setShowIssues(false);
    setLogPage(Math.floor(target.index / logPageSize) + 1);
    setPendingEntryFocusKind('time-cursor');
    setPendingEntryFocusId(target.entry.id);
  }, [logPageSize, timestampNavigationEntries]);

  const navigateToTimelineComponentTime = useCallback((component: string, timestampNs: bigint) => {
    const candidates = componentTimestampNavigationEntries.grouped.get(component);
    if (!candidates || candidates.length === 0) return;
    let low = 0;
    let high = candidates.length;
    while (low < high) {
      const mid = Math.floor((low + high) / 2);
      if (candidates[mid].timestampNs < timestampNs) low = mid + 1;
      else high = mid;
    }
    const right = low < candidates.length ? candidates[low] : undefined;
    const left = low > 0 ? candidates[low - 1] : undefined;
    const distance = (a: bigint, b: bigint) => a >= b ? a - b : b - a;
    const target = !left
      ? right
      : !right
        ? left
        : distance(left.timestampNs, timestampNs) <= distance(right.timestampNs, timestampNs) ? left : right;
    if (!target) return;

    // Timeline 点击属于“模块 + 时间”事实定位，不继承左侧进程/Trace 的阅读范围。
    clearNavigationSelection();
    setFilters((current) => ({ ...current, components: new Set() }));
    const targetIndex = componentTimestampNavigationEntries.visible.findIndex((entry) => entry.id === target.entry.id);
    if (targetIndex < 0) return;
    timelineCursorTargetRef.current = target.entry.id;
    setSelectedEntry(undefined);
    setFocusedEntryId(target.entry.id);
    setShowIssues(false);
    setLogPage(Math.floor(targetIndex / logPageSize) + 1);
    setPendingEntryFocusKind('time-cursor');
    setPendingEntryFocusId(target.entry.id);
  }, [clearNavigationSelection, componentTimestampNavigationEntries, logPageSize]);

  function safeDownloadSegment(value: string): string {
    return value.trim().replace(/[\\/:*?"<>|]+/g, '_').replace(/\s+/g, '_').slice(0, 80) || 'unknown';
  }

  function downloadVisibleLogs() {
    if (!activeTask || scopedFilteredEntries.length === 0) return;
    const grouped = new Map<string, LogEntry[]>();
    scopedFilteredEntries.forEach((entry) => {
      const subsystem = entry.logSubsystem || '未分类';
      const module = entry.logModule || entry.component || 'unknown';
      const key = `${safeDownloadSegment(subsystem)}/${safeDownloadSegment(module)}.log`;
      const rows = grouped.get(key) ?? [];
      rows.push(entry);
      grouped.set(key, rows);
    });
    const files = Array.from(grouped.entries()).sort(([left], [right]) => left.localeCompare(right)).map(([name, entries]) => ({
      name,
      content: `${entries.slice().sort((left, right) => logSortOrder === 'desc' ? -compareEntries(left, right) : compareEntries(left, right)).map((entry) => entry.raw).join('\n')}\n`,
    }));
    const blob = createStoredZip(files);
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `${safeDownloadSegment(activeTask.name)}-logs.zip`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function jumpToLogEntry(entry: LogEntry) {
    let index = metricEntries.findIndex((candidate) => candidate.id === entry.id);
    if (index < 0) {
      const fallbackEntries = timelineVisibleEntries.slice().sort(compareEntries);
      if (logSortOrder === 'desc') fallbackEntries.reverse();
      index = fallbackEntries.findIndex((candidate) => candidate.id === entry.id);
      if (index < 0) return;
      // 精确日志导航优先保证“点了就到悬浮展示的那一行”。若正文/级别/组件/模式
      // 细筛选隐藏了目标，自动清除这些细筛选，但保留当前时间窗。
      setFilters((current) => ({
        ...current,
        query: '',
        levels: new Set(),
        components: new Set(),
        modes: new Set(),
        errorsOnly: false,
      }));
    }
    clearNavigationSelection();
    setSelectedEntry(undefined);
    setFocusedEntryId(entry.id);
    setShowIssues(false);
    setLogPage(Math.floor(index / logPageSize) + 1);
    setPendingPageEntryId(entry.id);
    setPendingEntryFocusKind('navigation');
    setPendingEntryFocusId(entry.id);
  }

  function jumpToAdjacentError(direction: -1 | 1) {
    if (!navigableErrorEntries.length) return;
    let nextIndex = selectedErrorIndex >= 0 ? selectedErrorIndex + direction : -1;
    if (selectedErrorIndex < 0) {
      const pageStart = (Math.max(1, logPage) - 1) * logPageSize;
      const pageEnd = pageStart + logPageSize;
      if (direction > 0) {
        nextIndex = navigableErrorEntries.findIndex((entry) => metricEntries.findIndex((candidate) => candidate.id === entry.id) >= pageStart);
      } else {
        for (let index = navigableErrorEntries.length - 1; index >= 0; index -= 1) {
          const metricIndex = metricEntries.findIndex((candidate) => candidate.id === navigableErrorEntries[index].id);
          if (metricIndex >= 0 && metricIndex < pageEnd) { nextIndex = index; break; }
        }
      }
    }
    if (nextIndex < 0 || nextIndex >= navigableErrorEntries.length) return;
    jumpToLogEntry(navigableErrorEntries[nextIndex]);
  }

  function jumpToFirstError(entries: readonly LogEntry[]) {
    const entry = entries.filter((candidate) => candidate.severity === 'error').sort(compareEntries)[0];
    if (entry) jumpToLogEntry(entry);
  }

  const selectProcess = useCallback((process: ProcessTimeline) => {
    if (selectedProcessId === process.id && !selectedThreadId && !selectedTraceId && !selectedCrossTraceId) {
      clearNavigationSelection();
      return;
    }
    setSelectedProcessId(process.id);
    setSelectedThreadId(undefined);
    setSelectedTraceId(undefined);
    setSelectedCrossTraceId(undefined);
    setSelectedEntry(undefined);
    setExpandedIds(new Set());
    setExpandedProcessIds((current) => new Set(current).add(process.id));
  }, [clearNavigationSelection, selectedCrossTraceId, selectedProcessId, selectedThreadId, selectedTraceId]);

  function selectThread(thread: ThreadTimeline) {
    if (selectedThreadId === thread.id && !selectedTraceId && !selectedCrossTraceId) {
      clearNavigationSelection();
      return;
    }
    const owner = processes.find((process) => process.threads.some((candidate) => candidate.id === thread.id));
    setSelectedProcessId(undefined);
    setSelectedThreadId(thread.id);
    setSelectedTraceId(undefined);
    setSelectedCrossTraceId(undefined);
    setSelectedEntry(undefined);
    if (owner) setExpandedProcessIds((current) => new Set(current).add(owner.id));
  }

  const selectTrace = useCallback((thread: ThreadTimeline | undefined, trace: TraceTimeline) => {
    const alreadySelected = thread ? selectedTraceId === trace.id : selectedCrossTraceId === trace.id;
    if (alreadySelected) {
      clearNavigationSelection();
      return;
    }
    setOptimisticTraceId(trace.id);
    setSelectedEntry(undefined);
    startNavigationTransition(() => {
      setSelectedProcessId(undefined);
      setSelectedCrossTraceId(thread ? undefined : trace.id);
      setSelectedThreadId(thread?.id);
      setSelectedTraceId(thread ? trace.id : undefined);
      setExpandedIds(new Set());
    });
  }, [clearNavigationSelection, selectedCrossTraceId, selectedTraceId]);

  const selectTimeRange = useCallback((range: TimeRangeFilter) => {
    if (liveListening) return;
    if (activeTask?.remoteEnvironmentId && range.endNs - range.startNs > MAX_REMOTE_LOG_RANGE_NS) {
      setTasks((current) => current.map((item) => item.id === activeTask.id ? {
        ...item,
        incrementalMessage: '时间窗口最多支持 3 天，请缩小 Brush 选区。',
      } : item));
      return;
    }
    setFilters((current) => ({ ...current, timeRange: range }));
    setCustomStartTime(nsToTimestampText(range.startNs));
    setCustomEndTime(nsToTimestampText(range.endNs));
    if (activeTask?.remoteEnvironmentId && activeTask.remoteRequest && activeTask.status === 'ready') {
      void extendRemoteLogTaskRange(activeTask, range);
    }
  }, [activeTask, liveListening, selectedCrossTraceId, selectedProcessId, selectedThreadId, selectedTraceId]);

  const clearTimeRange = useCallback(() => {
    setFilters((current) => ({ ...current, timeRange: undefined }));
    setCustomStartTime('');
    setCustomEndTime('');
  }, []);

  const applyRecentRange = useCallback((seconds?: number) => {
    if (!seconds) {
      clearTimeRange();
      if (activeTask && activeTask.status === 'ready' && !activeTask.fullLoaded) {
        reloadTaskForEarlierRange(activeTask, undefined, 'full');
      }
      return;
    }

    const endNs = activeTask?.latestAvailableNs ?? sortedEntries[sortedEntries.length - 1]?.timestampNs;
    if (endNs === undefined) return;
    const startNs = endNs - BigInt(seconds) * 1_000_000_000n;
    const range = { startNs, endNs };
    setFilters((current) => ({ ...current, timeRange: range }));
    setCustomStartTime(nsToTimestampText(startNs));
    setCustomEndTime(nsToTimestampText(endNs));

    if (
      activeTask
      && activeTask.status === 'ready'
      && !activeTask.fullLoaded
      && (activeTask.earliestImportedNs === undefined || startNs < activeTask.earliestImportedNs)
    ) {
      reloadTaskForEarlierRange(activeTask, startNs);
    }
  }, [activeTask, clearTimeRange, sortedEntries]);

  const applyCustomTimeRange = useCallback(() => {
    const startNs = timestampTextToNs(customStartTime);
    const endNs = timestampTextToNs(customEndTime);
    if (startNs === undefined || endNs === undefined || endNs < startNs) return;
    setFilters((current) => ({ ...current, timeRange: { startNs, endNs } }));

    if (
      activeTask
      && activeTask.status === 'ready'
      && !activeTask.fullLoaded
      && (activeTask.earliestImportedNs === undefined || startNs < activeTask.earliestImportedNs)
    ) {
      reloadTaskForEarlierRange(activeTask, startNs);
    }
  }, [activeTask, customEndTime, customStartTime]);

  useEffect(() => {
    if (!activeTask || activeTask.status !== 'ready') return;
    if (defaultRangeTaskRef.current === activeTask.id) return;

    /*
     * 实时任务不做「默认时间范围」过滤。
     *
     * 实时监听开的是新 tab，它的 remoteRequest 是开启那一刻的 1 分钟窗口；而日志是**从现在往后长**的，
     * 一旦把 filters.timeRange 设成那个固定窗口，之后所有新到的行都落在窗外 → 列表永远 0 条
     * （后端在推、累计计数在涨，界面上却什么都没有）。
     */
    if (liveBoundTaskIdRef.current && activeTask.id === liveBoundTaskIdRef.current) {
      defaultRangeTaskRef.current = activeTask.id;
      setFilters((current) => (current.timeRange ? { ...current, timeRange: undefined } : current));
      setCustomStartTime('');
      setCustomEndTime('');
      return;
    }

    const remoteLoadedStart = activeTask.loadedStartNs ?? requestTimeRangeNs(activeTask.remoteRequest)?.startNs;
    const remoteLoadedEnd = activeTask.loadedEndNs ?? requestTimeRangeNs(activeTask.remoteRequest)?.endNs;
    if (activeTask.remoteRequest && remoteLoadedStart !== undefined && remoteLoadedEnd !== undefined) {
      defaultRangeTaskRef.current = activeTask.id;
      setFilters({
        query: activeTask.remoteRequest.keyword || '',
        levels: new Set(),
        components: new Set(),
        modes: new Set(),
        timeRange: { startNs: remoteLoadedStart, endNs: remoteLoadedEnd },
        errorsOnly: false,
      });
      setCustomStartTime(nsToTimestampText(remoteLoadedStart));
      setCustomEndTime(nsToTimestampText(remoteLoadedEnd));
      return;
    }

    if (sortedEntries.length === 0) return;
    defaultRangeTaskRef.current = activeTask.id;

    const firstNs = sortedEntries[0]?.timestampNs;
    const endNs = sortedEntries[sortedEntries.length - 1]?.timestampNs;
    if (firstNs === undefined || endNs === undefined) return;
    const candidateStart = endNs - BigInt(DEFAULT_RANGE_SECONDS) * 1_000_000_000n;
    const startNs = candidateStart > firstNs ? candidateStart : firstNs;
    setFilters({
      query: '',
      levels: new Set(),
      components: new Set(),
      modes: new Set(),
      timeRange: { startNs, endNs },
      errorsOnly: false,
    });
    setCustomStartTime(nsToTimestampText(startNs));
    setCustomEndTime(nsToTimestampText(endNs));
  }, [activeTask, sortedEntries]);

  const toggleProcessTimeline = useCallback(() => {
    setProcessTimelineExpanded((current) => !current);
  }, []);

  function openProcessGraph(process: ProcessTimeline) {
    const traces = process.threads.flatMap((thread) => thread.traces);
    setCallFlowDialogOpen(true);
    setCallGraphState({
      title: `${process.component} · PID ${process.processId}`,
      subtitle: `${process.threads.length} 个线程 · ${traces.length} 条调用链`,
      traces,
    });
  }

  function openTraceGraph(thread: ThreadTimeline | undefined, trace: TraceTimeline) {
    setCallFlowDialogOpen(true);
    setCallGraphState({
      title: thread ? `${thread.component} · TID ${thread.threadId}` : `跨组件 Trace ${compactTraceId(trace.rpc.traceId)}`,
      subtitle: thread
        ? `${trace.firstFunctionName} · ${trace.entries.length} 条日志`
        : `${trace.components.join(' → ')} · ${trace.entries.length} 条日志`,
      traces: [trace],
    });
  }

  function openCallFlowDialog() {
    setCallFlowDialogOpen(true);
    if (callGraphState) return;
    if (selectedCrossTrace) {
      openTraceGraph(undefined, selectedCrossTrace);
      return;
    }
    if (selectedTrace) {
      openTraceGraph(selectedThread, selectedTrace);
      return;
    }
    if (selectedProcess) {
      openProcessGraph(selectedProcess);
      return;
    }
    const firstProcess = visibleProcesses[0];
    if (firstProcess) {
      openProcessGraph(firstProcess);
      return;
    }
    const firstCrossTrace = visibleCrossComponentTraces[0];
    if (firstCrossTrace) openTraceGraph(undefined, firstCrossTrace);
  }

  /**
   * 把悬浮时间线放到操作工具栏**下方**。
   *
   * 硬编码 offset（曾经是 86）会让窗口正好盖住触发它的那个按钮，
   * 于是「打开了就关不掉」。程序化打开时间线的地方都要先经过这里。
   */
  function positionTimelineBelowToolbar() {
    if (timelineDocked) return; // 固定模式下没有悬浮窗，位置无意义
    const stack = document.querySelector('.analysis-sticky-stack');
    const bottom = stack ? stack.getBoundingClientRect().bottom : 86;
    setTimelineWindowPosition({ left: 8, top: Math.max(8, Math.round(bottom + 8)) });
  }

  /**
   * 时间线只要被打开（按钮、调用导航定位、URL 场景恢复），就保证它不在工具栏**上方**。
   *
   * 悬浮窗盖住「调用导航」按钮时，用户关掉导航就再也打不开了 ——
   * 位置在打开那一刻可能还量不准（日志页尚未渲染），所以这里统一在下一帧兜一次底，
   * 并且只在自己确实压住工具栏时才动，平时不干扰用户拖到别处。
   */
  useEffect(() => {
    if (!showTimeline || timelineDocked) return undefined;
    const correct = () => {
      const stack = document.querySelector('.analysis-sticky-stack');
      // 还没渲染出来就等下一次重试：URL 场景恢复会在日志页就绪之前就把时间线打开，
      // 只看第一帧会量不到工具栏，窗口就留在默认位置上压住按钮。
      if (!stack) return false;
      const bottom = stack.getBoundingClientRect().bottom;
      setTimelineWindowPosition((current) => (
        current.top < bottom + 4 ? { left: current.left, top: Math.round(bottom + 8) } : current
      ));
      return true;
    };
    const frame = window.requestAnimationFrame(() => { correct(); });
    const timers = [120, 400, 900].map((delay) => window.setTimeout(() => { correct(); }, delay));
    return () => {
      window.cancelAnimationFrame(frame);
      timers.forEach((timer) => window.clearTimeout(timer));
    };
  }, [showTimeline, timelineDocked, activeTask?.status]);

  function focusFunctionFromGraph(trace: TraceTimeline, node: FunctionNode) {
    const expandedPath = findFunctionPath(trace.items, node.id) ?? [node.id];
    if (!itemMatches(node, filters)) setFilters(EMPTY_FILTERS);
    // 从调用导航点过来时时间线会被打开：先把它挪到工具栏下方，
    // 否则它会盖住「调用导航」按钮本身，用户关掉导航后就没法再打开。
    positionTimelineBelowToolbar();
    setShowTimeline(true);
    setProcessTimelineExpanded(true);
    setSelectedEntry(undefined);
    setShowIssues(false);

    if (trace.crossComponent) {
      setSelectedProcessId(undefined);
      setSelectedThreadId(undefined);
      setSelectedTraceId(undefined);
      setSelectedCrossTraceId(trace.id);
    } else {
      const owner = processes
        .flatMap((process) => process.threads.map((thread) => ({ process, thread })))
        .find(({ thread }) => thread.traces.some((candidate) => candidate.id === trace.id));
      if (owner) {
        setSelectedProcessId(undefined);
        setSelectedThreadId(owner.thread.id);
        setSelectedTraceId(trace.id);
        setSelectedCrossTraceId(undefined);
      }
    }

    setPendingPageEntryId(node.startEntry.id);
    setPendingFunctionFocus({ nodeId: node.id, traceId: trace.id, expandedPath });
    // 故意**不关闭**导航窗口，也不清空 callGraphState：
    // 在调用导航里点节点是「一边看流程一边定位日志」，关掉窗口等于每点一次就退出一次，
    // 想接着看下一个函数还得重新打开。窗口由用户自己关（右上角 X 或点遮罩）。
  }



  function renderResourceFeatureTabs(active: ResourceWorkspaceFeature) {
    const environment = resourceWorkspaceEnvironment;
    const tabs = resourceWorkspaceTabs;
    const showWorkspaceTabs = localImportWorkspaceOpen || tabs.length > 0 || Boolean(environment);
    if (!showWorkspaceTabs && active !== 'logs') return null;

    function selectWorkspaceResource(item: EnvironmentSummary) {
      setResourceWorkspaceEnvironment(item);
      setPreferredRemoteEnvironmentId(item.id);
      if (active === 'reports') {
        setReportEnvironment(item);
        return;
      }
      if (active === 'resource') {
        setResourceWorkspaceFeature('resource');
        setWorkspacePage('logs');
        return;
      }
      setResourceWorkspaceFeature('logs');
      setWorkspacePage('logs');
      selectLatestTaskForEnvironment(item.id);
    }

    function selectLocalImportWorkspace() {
      setLocalImportWorkspaceOpen(true);
      setResourceWorkspaceEnvironment(undefined);
      setPreferredRemoteEnvironmentId(undefined);
      setPreferredRemotePreset(undefined);
      setReportEnvironment(undefined);
      setResourceWorkspaceFeature('logs');
      setWorkspacePage('logs');
      selectLatestLocalTask();
    }

    function closeLocalImportWorkspace() {
      setLocalImportWorkspaceOpen(false);
      if (environment) return;
      const fallback = tabs.at(-1);
      if (fallback) {
        setResourceWorkspaceFeature('logs');
        setWorkspacePage('logs');
        setResourceWorkspaceEnvironment(fallback);
        setPreferredRemoteEnvironmentId(fallback.id);
        selectLatestTaskForEnvironment(fallback.id);
        return;
      }
      // 只关闭工作区 Tab，不删除本地导入任务。
      setActiveTaskId('');
      setFilters(cloneFilters(EMPTY_FILTERS));
      resetViewState(false);
      setWorkspacePage('resources');
    }

    function closeWorkspaceResource(item: EnvironmentSummary) {
      const index = tabs.findIndex((tab) => tab.id === item.id);
      const remaining = tabs.filter((tab) => tab.id !== item.id);
      setResourceWorkspaceTabs(remaining);
      if (environment?.id !== item.id) return;

      const fallback = remaining[Math.min(Math.max(index, 0), Math.max(remaining.length - 1, 0))];
      if (fallback) {
        selectWorkspaceResource(fallback);
        return;
      }
      if (localImportWorkspaceOpen) {
        selectLocalImportWorkspace();
        return;
      }

      // 关闭最后一个资源工作区时只退出资源页签，不删除已检索任务。
      setResourceWorkspaceEnvironment(undefined);
      setPreferredRemoteEnvironmentId(undefined);
      setPreferredRemotePreset(undefined);
      setReportEnvironment(undefined);
      setWorkspacePage('resources');
    }

    return (
      <div className="resource-workspace-tab-stack">
        <nav className="resource-primary-tabs" aria-label="环境工作区">
          {localImportWorkspaceOpen && (
            <span className={`resource-primary-tab ${!environment && active === 'logs' ? 'active' : ''}`}>
              <button type="button" className="resource-primary-tab-main" onClick={selectLocalImportWorkspace} title="导入日志">
                <FileUp size={14}/><strong>导入日志</strong>
              </button>
              <button type="button" className="resource-primary-tab-close" aria-label="关闭导入日志页签" title="关闭导入日志页签" onClick={closeLocalImportWorkspace}>
                <X size={13}/>
              </button>
            </span>
          )}
          {tabs.map((item) => (
            <span className={`resource-primary-tab ${item.id === environment?.id ? 'active' : ''}`} key={item.id}>
              <button type="button" className="resource-primary-tab-main" onClick={() => selectWorkspaceResource(item)} title={`${item.upper_machine.host}(${item.upper_machine.username})`}>
                <ServerCog size={14}/><strong>{item.upper_machine.host}({item.upper_machine.username})</strong>
              </button>
              <button type="button" className="resource-primary-tab-close" aria-label={`关闭 ${item.upper_machine.host} 环境页签`} title="关闭环境页签" onClick={() => closeWorkspaceResource(item)}>
                <X size={13}/>
              </button>
            </span>
          ))}
        </nav>
        {environment && (
          <nav className="resource-feature-tabs-bar resource-feature-tabs-secondary" aria-label={`${environment.upper_machine.host} 功能`}>
            <button type="button" className={active === 'resource' ? 'active' : ''} onClick={() => openCurrentEnvironmentResource(environment)}><ServerCog size={14}/> 环境资源</button>
            <button type="button" className={active === 'logs' ? 'active' : ''} onClick={() => openEnvironmentLogLocator(environment)}><FileSearch size={14}/> 日志定位</button>
            <button type="button" className={active === 'reports' ? 'active' : ''} onClick={() => openEnvironmentCpdReports(environment)}><BookOpenCheck size={14}/> CPD 测校报告</button>
          </nav>
        )}
      </div>
    );
  }

  const hasActiveDetailFilters = filters.levels.size > 0 || filters.modes.size > 0 || Boolean(filters.timeRange) || Boolean(normalizedDurationBounds(filters.durationRange)) || Boolean(filters.errorsOnly);
  const semanticDisplayValue = useMemo<SemanticDisplayContextValue>(
    () => ({
      enabled: semanticLabelsEnabled,
      rules: displayRules,
      autoSemantics: activeTask?.autoSemantics,
      onEditEntryRule: openEntryRuleEditor,
      onEditFunctionRule: openFunctionRuleEditor,
    }),
    [semanticLabelsEnabled, displayRules, activeTask?.autoSemantics, openEntryRuleEditor, openFunctionRuleEditor],
  );

  function renderWorkspaceNav(active?: 'resources' | 'logs' | 'data' | 'knowledge' | 'atlog' | 'tools' | 'audit' | 'settings') {
    return <nav className="workspace-nav" aria-label="主功能导航">
      <button type="button" className={active === 'resources' ? 'active' : undefined} onClick={() => setWorkspacePage('resources')}><ServerCog size={16} /> 环境资源</button>
      <button type="button" className={active === 'logs' ? 'active' : undefined} onClick={openGlobalLogLocator}><FileSearch size={16} /> 日志定位</button>
      <button type="button" className={active === 'data' ? 'active' : undefined} onClick={() => openDataPage()}><Database size={16} /> 数据提取</button>
      {/* 用例分析 and 案例库 are separate jobs: one triages automation-case reports, the other
          curates the reusable knowledge base. This entry only shows cases, so it says so. */}
      <button type="button" className={active === 'knowledge' ? 'active' : undefined} onClick={() => setWorkspacePage('knowledge')}><BookOpenCheck size={16} /> 案例库</button>
      <button type="button" className={active === 'atlog' ? 'active' : undefined} onClick={() => setWorkspacePage('atlog')}><FlaskConical size={16} /> 用例分析</button>
      <button type="button" className={active === 'audit' ? 'active' : undefined} onClick={openLogAudit}><ClipboardList size={16} /> 审计</button>
      <button type="button" className={active === 'settings' ? 'active' : undefined} onClick={() => { setPreferredSettingsTab('resource'); setWorkspacePage('platform-settings'); }}><Settings size={16} /> 设置</button>
    </nav>;
  }

  /**
   * Global overlays, rendered by *every* return below.
   *
   * TracePilot is mounted beside <App/>, so a UI action it dispatches must reach the same
   * state from any page. The case editor used to live only in the 日志定位 return, so
   * 「整理成案例」 silently did nothing on the other eight pages even though the action
   * reported success. One definition, included everywhere, keeps that from drifting again.
   */
  const globalOverlays = smartAnalysisTab ? (
    <SmartAnalysisDialog
      tab={smartAnalysisTab}
      onTabChange={setSmartAnalysisTab}
      initialDraft={caseImportDraft?.draft}
      presetEvidences={caseImportDraft?.evidences}
      entries={navigableErrorEntries}
      defaultSelectedEntryId={focusedEntryId || selectedEntry?.id}
      environmentId={activeTask?.remoteEnvironmentId ?? preferredRemoteEnvironmentId}
      environmentName={resourceWorkspaceEnvironment && activeTask?.remoteEnvironmentId === resourceWorkspaceEnvironment.id ? resourceWorkspaceEnvironment.name : ''}
      sourceOperationId={activeTask?.remoteOperationId}
      sourceTaskName={activeTask?.name}
      querySnapshot={activeTask?.remoteRequest}
      analysisEntries={metricEntries}
      selectedModules={activeTask?.remoteRequest?.fm_targets?.map((item) => item.fm) || activeTask?.remoteRequest?.fms}
      onLocateEntry={jumpToLogEntry}
      errorRules={errorRules}
      onClose={() => { setSmartAnalysisTab(undefined); setCaseImportDraft(undefined); }}
      onSaved={() => { setSmartAnalysisTab(undefined); setCaseImportDraft(undefined); }}
    />
  ) : null;

  if (workspacePage === 'platform-settings') {
    return (
      <div className="app-shell resource-app-shell">
        <GlobalApiActivity />
        <header className="topbar">
          <div className="brand"><div className="brand-mark"><GitBranch size={22} /></div><div className="brand-title">TraceLens</div></div>
          {renderWorkspaceNav('settings')}
          <div className="topbar-actions"><span className="resource-backend-hint">{APP_VERSION}</span></div>
        </header>
        <PlatformSettingsPage
          initialTab={preferredSettingsTab}
          errorRules={errorRules}
          displayRules={displayRules}
          maskingRules={maskingRules}
          foldingRules={foldingRules}
          dataExtractionRules={dataExtractionRules}
          editorRequest={ruleEditorRequest}
          onErrorRulesChange={setErrorRules}
          onDisplayRulesChange={setDisplayRules}
          onMaskingRulesChange={setMaskingRules}
          onFoldingRulesChange={setFoldingRules}
          onDataExtractionRulesChange={setDataExtractionRules}
          onEditorRequestHandled={() => setRuleEditorRequest(undefined)}
        />
        {globalOverlays}
      </div>
    );
  }
  if (workspacePage === 'catalog') {
    return (
      <div className="app-shell resource-app-shell">
        <GlobalApiActivity />
        <header className="topbar">
          <div className="brand"><div className="brand-mark"><GitBranch size={22} /></div><div className="brand-title">TraceLens</div></div>
          {renderWorkspaceNav('settings')}
          <div className="topbar-actions"><span className="resource-backend-hint">{APP_VERSION}</span></div>
        </header>
        <LogCatalogSettingsPage onBack={() => setWorkspacePage('resources')} />
        {globalOverlays}
      </div>
    );
  }
  if (workspacePage === 'audit') {
    return (
      <div className="app-shell resource-app-shell">
        <GlobalApiActivity />
        <header className="topbar">
          <div className="brand"><div className="brand-mark"><GitBranch size={22} /></div><div className="brand-title">TraceLens</div></div>
          {renderWorkspaceNav('audit')}
          <div className="topbar-actions"><span className="resource-backend-hint">{APP_VERSION}</span></div>
        </header>
        <LogAuditPage onRetry={retryLogAudit} onOpenData={(audit) => openDataPage(audit.operation_id)} />
        {globalOverlays}
      </div>
    );
  }
  if (workspacePage === 'data') {
    return (
      <div className="app-shell resource-app-shell">
        <GlobalApiActivity />
        <header className="topbar">
          <div className="brand"><div className="brand-mark"><GitBranch size={22} /></div><div className="brand-title">TraceLens</div></div>
          {renderWorkspaceNav('data')}
          <div className="topbar-actions"><span className="resource-backend-hint">{APP_VERSION}</span></div>
        </header>
        <ExtractedDataPage
          sourceOperationFilter={dataSourceOperationFilter}
          onClearSourceOperationFilter={() => setDataSourceOperationFilter('')}
          onOpenSourceLog={(record) => void openSourceLogFromData(record)}
        />
        {globalOverlays}
      </div>
    );
  }
  if (workspacePage === 'atlog') {
    return (
      <div className="app-shell resource-app-shell">
        <GlobalApiActivity />
        <header className="topbar">
          <div className="brand"><div className="brand-mark"><GitBranch size={22} /></div><div className="brand-title">TraceLens</div></div>
          {renderWorkspaceNav('atlog')}
          <div className="topbar-actions"><span className="resource-backend-hint">{APP_VERSION}</span></div>
        </header>
        <AtLogAnalysisPage TimelineComponent={ProcessTimelineOverview} errorRules={errorRules} onOpenEnvironmentCpdReports={openEnvironmentCpdReports} onOpenEnvironmentLogLocator={(environment, request) => {
          if (request?.start_time && request?.end_time) {
            rememberResourceWorkspace(environment);
            setPreferredRemoteEnvironmentId(environment.id);
            setPreferredRemotePreset({ token: `atlog-case-${environment.id}-${Date.now()}`, environmentId: environment.id, startTime: request.start_time, endTime: request.end_time, taskName: `用例日志定位 · ${environment.name}`, request });
            setWorkspacePage('logs');
            startRemoteLogSearch(environment, request, { taskName: `用例日志定位 · ${environment.name}`, force: true });
          } else {
            openEnvironmentLogLocator(environment);
          }
        }} />
        {globalOverlays}
      </div>
    );
  }
  if (workspacePage === 'knowledge') {
    return (
      <div className="app-shell resource-app-shell">
        <GlobalApiActivity />
        <header className="topbar">
          <div className="brand"><div className="brand-mark"><GitBranch size={22} /></div><div className="brand-title">TraceLens</div></div>
          {renderWorkspaceNav('knowledge')}
          <div className="topbar-actions"><span className="resource-backend-hint">{APP_VERSION}</span></div>
        </header>
        <KnowledgeBasePage />
        {globalOverlays}
      </div>
    );
  }
  if (workspacePage === 'tools') {
    return (
      <div className="app-shell resource-app-shell">
        <GlobalApiActivity />
        <header className="topbar">
          <div className="brand"><div className="brand-mark"><GitBranch size={22} /></div><div className="brand-title">TraceLens</div></div>
          {renderWorkspaceNav('tools')}
          <div className="topbar-actions"><span className="resource-backend-hint">{APP_VERSION}</span></div>
        </header>
        <ToolCenterPage />
        {globalOverlays}
      </div>
    );
  }
  if (workspacePage === 'resources') {
    return (
      <div className="app-shell resource-app-shell">
        <GlobalApiActivity />
        <header className="topbar">
          <div className="brand"><div className="brand-mark"><GitBranch size={22} /></div><div className="brand-title">TraceLens</div></div>
          {renderWorkspaceNav('resources')}
          <div className="topbar-actions"><span className="resource-backend-hint">{APP_VERSION}</span></div>
        </header>
        <EnvironmentResourcePage initialEnvironmentId={resourceWorkspaceEnvironment?.id} onOpenLogLocator={openEnvironmentLogLocator} onOpenCpdReports={openEnvironmentCpdReports} onOpenCatalogSettings={() => setWorkspacePage('platform-settings')} />
        {globalOverlays}
      </div>
    );
  }
  if (workspacePage === 'reports' && reportEnvironment) {
    return (
      <div className="app-shell resource-app-shell">
        <GlobalApiActivity />
        <header className="topbar">
          <div className="brand"><div className="brand-mark"><GitBranch size={22} /></div><div className="brand-title">TraceLens</div></div>
          {renderWorkspaceNav('logs')}
          <div className="topbar-actions"><span className="resource-backend-hint">{APP_VERSION}</span></div>
        </header>
        {renderResourceFeatureTabs('reports')}
        <CpdReportPage environment={reportEnvironment} onLocateLogs={locateLogsFromReport} />
        {globalOverlays}
      </div>
    );
  }

  /**
   * 时间线内容只有一份：悬浮窗口和「固定到日志区」共用同一个节点，
   * 差别只在 expanded / onToggle —— 固定后标题栏重新变成可折叠的（对应 URL 里的 pex）。
   */
  /** 时间线标题行右侧的「固定 / 关闭」。两种模式共用一份，只有状态文案不同。 */
  const timelineHeaderActions = (
    <span className="timeline-window-actions">
      <button
        type="button"
        className={timelineDocked ? 'active' : ''}
        onClick={() => setTimelineDocked((value) => !value)}
        aria-pressed={timelineDocked}
        aria-label={timelineDocked ? '取消固定时间线' : '固定时间线到日志区'}
        title={timelineDocked ? '取消固定，恢复为可拖动的悬浮窗口' : '固定到日志区上方（不再悬浮，日志区让出位置）'}
      >
        {timelineDocked ? <PinOff size={14} /> : <Pin size={14} />}
      </button>
      <button type="button" className="close" onClick={() => setShowTimeline(false)} aria-label="关闭时间线" title="关闭时间线"><X size={15} /></button>
    </span>
  );

  const timelineOverviewNode = (
    <>
      {/* 实时监听色带属于时间线的信息，固定后不能被藏起来。 */}
      <LiveRibbon environmentId={preferredRemoteEnvironmentId} />
      <ProcessTimelineOverview
        processes={ganttProcesses}
        selectedProcessId={selectedProcessId}
        selectedThreadId={selectedThreadId}
        selectedTraceId={optimisticTraceId ?? selectedTraceId ?? selectedCrossTraceId}
        selectedTimeRange={filters.timeRange}
        loadedTimeRange={activeTaskLoadedRange}
        timeSelectionDisabled={liveListening}
        incrementalLoading={activeTask?.incrementalLoading}
        incrementalMessage={activeTask?.incrementalMessage}
        expanded={timelineDocked ? processTimelineExpanded : true}
        navigationPending={navigationPending}
        onToggle={timelineDocked ? () => setProcessTimelineExpanded((value) => !value) : () => undefined}
        onSelectProcess={selectProcess}
        onSelectTrace={selectTrace}
        onSelectTimeRange={selectTimeRange}
        onClearTimeRange={clearTimeRange}
        onNavigateTime={navigateToTimelineTime}
        onNavigateComponentTime={navigateToTimelineComponentTime}
        onNavigateEntry={jumpToLogEntry}
        onTimeCursorInteractionChange={setTimeCursorScrollLocked}
        onHiddenComponentsChange={setTimelineHiddenComponents}
        restoredHiddenComponents={timelineHiddenComponents}
        filterScopeKey={activeTask?.id ?? 'logs'}
        headerActions={timelineHeaderActions}
        dragHandleProps={timelineDocked ? undefined : {
          onPointerDown: beginTimelineWindowDrag,
          onPointerMove: moveTimelineWindow,
          onPointerUp: endTimelineWindowDrag,
          onPointerCancel: endTimelineWindowDrag,
        }}
      />
    </>
  );

  return (
    <FoldingRuleContext.Provider value={effectiveFoldingRules}>
    <ErrorRuleContext.Provider value={errorRules}>
    <SemanticDisplayContext.Provider value={semanticDisplayValue}>
    <div
      className={classNames('app-shell', dragging && 'dragging')}
      onDragOver={(event: React.DragEvent<HTMLDivElement>) => { event.preventDefault(); setDragging(true); }}
      onDragLeave={(event: React.DragEvent<HTMLDivElement>) => { if (event.currentTarget === event.target) setDragging(false); }}
      onDrop={(event: React.DragEvent<HTMLDivElement>) => { event.preventDefault(); setDragging(false); void loadFiles(event.dataTransfer.files); }}
    >
      <GlobalApiActivity />
      <input
        ref={fileInputRef}
        type="file"
        accept=".log,.txt,.out,text/plain"
        multiple
        hidden
        onChange={(event: React.ChangeEvent<HTMLInputElement>) => event.target.files && void loadFiles(event.target.files)}
      />

      {dragging && (
        <div className="drop-overlay"><UploadCloud size={42} /><strong>释放文件开始解析</strong><span>支持同时导入多个 .log / .txt 文件</span></div>
      )}

      <header className="topbar">
        <div className="brand"><div className="brand-mark"><GitBranch size={22} /></div><div className="brand-title">TraceLens</div></div>
        {renderWorkspaceNav('logs')}
        <div className="topbar-actions"><span className="resource-backend-hint">{APP_VERSION}</span></div>
      </header>
      {renderResourceFeatureTabs(resourceWorkspaceFeature)}

      {resourceWorkspaceFeature === 'resource' && resourceWorkspaceEnvironment ? (
        <CurrentEnvironmentResourceView
          environment={resourceWorkspaceEnvironment}
          onOpenLogLocator={() => openEnvironmentLogLocator(resourceWorkspaceEnvironment)}
          onOpenCpdReports={() => openEnvironmentCpdReports(resourceWorkspaceEnvironment)}
        />
      ) : <>
      <div className="analysis-sticky-stack" ref={analysisStackRef}>
        {scopedTasks.length > 0 && <nav className="task-tabs-bar" aria-label="日志任务">
          <div className="task-tabs">
            {scopedTasks.map((task) => (
              <div className={classNames('task-tab', task.id === activeTaskId && 'active', `status-${task.status}`)} key={task.id}>
                <button
                  type="button"
                  className="task-tab-main"
                  onClick={() => activateTask(task.id)}
                  title={task.name}
                  aria-label={`切换到任务：${task.name}`}
                >
                  <span>{task.name}</span>
                  <small>{task.status === 'analyzing' ? `${task.remoteProgress?.percent ?? task.progress?.percent ?? 0}%` : task.status === 'cancelled' ? '已停止' : `${task.sourceNames.length} 源`}</small>
                </button>
                <button type="button" className="task-tab-close" onClick={() => closeTask(task.id)} aria-label={`关闭${task.name}`}><X size={13} /></button>
              </div>
            ))}
          </div>
        </nav>}

        <section className="global-query-bar unified-query-shell" aria-label="日志定位与任务内筛选">
          <RemoteLogQueryPanel
            initialEnvironmentId={preferredRemoteEnvironmentId}
            initialPreset={preferredRemotePreset}
            activeTaskEnvironmentId={activeTask?.remoteEnvironmentId}
            activeTaskRequest={activeTask?.remoteRequest}
            activeTaskLiveTargets={activeTask?.liveTargets}
            activeTaskViewRange={filters.timeRange ? {
              startTime: nsToTimestampText(filters.timeRange.startNs),
              endTime: nsToTimestampText(filters.timeRange.endNs),
            } : undefined}
            activeTaskLocal={Boolean(activeTask && !activeTask.remoteRequest)}
            taskQuery={filters.query}
            onTaskQueryChange={(value) => setFilters((current) => ({ ...current, query: value }))}
            taskQueryDisabled={activeTask?.status === 'analyzing'}
            timeControlsDisabled={liveListening}
            onLocatorStateChange={setRemoteLogLocator}
            taskFilterControl={(
              <TaskDetailFilterPopover
                components={components}
                levels={levels}
                modes={modes}
                selectedComponents={filters.components}
                selectedLevels={filters.levels}
                selectedModes={filters.modes}
                timeRangeLabel={filters.timeRange
                  ? `${formatNsTick(filters.timeRange.startNs, false, true)}–${formatNsTick(filters.timeRange.endNs, false, true)}`
                  : undefined}
                durationMinMs={filters.durationRange?.minMs ?? ''}
                durationMaxMs={filters.durationRange?.maxMs ?? ''}
                onToggleComponent={(value) => toggleFilter('components', value)}
                onToggleLevel={(value) => toggleFilter('levels', value)}
                onToggleMode={(value) => toggleFilter('modes', value)}
                onClearTimeRange={clearTimeRange}
                onDurationRangeChange={(minValue, maxValue) => {
                  const minMs = minValue.trim() || undefined;
                  const maxMs = maxValue.trim() || undefined;
                  setFilters((current) => ({ ...current, durationRange: minMs !== undefined || maxMs !== undefined ? { minMs, maxMs } : undefined }));
                  setLogPage(1);
                }}
                onClearDurationRange={() => { setFilters((current) => ({ ...current, durationRange: undefined })); setLogPage(1); }}
                onClearAll={() => { setFilters((current) => ({ ...current, levels: new Set(), components: new Set(), modes: new Set(), timeRange: undefined, durationRange: undefined })); setLogPage(1); }}
              />
            )}
            onStartSearch={startRemoteLogSearch}
          />
        </section>

        <div className="metric-strip">
          <div className="metric-strip-actions">
            {/* 左侧固定组：视图开关与三个窗口。它们决定「怎么看日志」，
                和右侧那些「对日志做什么」的动作分开摆，位置固定不随手边的按钮增减而移动。 */}
            <div className="metric-strip-left" aria-label="日志视图工具">
              <SwitchControl
                checked={foldingEnabled}
                label="函数折叠"
                hint={foldingEnabled ? '按函数折叠调用日志，展开查看逐行' : '不折叠，逐行展示日志'}
                onChange={(checked) => setFoldingEnabled(checked)}
              />
            <button
              type="button"
              className={`button ghost metric-action-button ${callFlowDialogOpen ? 'active' : ''}`}
              disabled={!activeTask || activeTask.status !== 'ready' || rawLogMode}
              aria-pressed={callFlowDialogOpen}
              onClick={() => { if (callFlowDialogOpen) setCallFlowDialogOpen(false); else openCallFlowDialog(); }}
              title={
                rawLogMode ? '原始日志模式不显示调用导航'
                : !activeTask || activeTask.status !== 'ready' ? '先查询日志，调用导航会按进程 / 函数 / 调用链展开'
                : callFlowDialogOpen ? '关闭调用导航' : '打开调用导航（进程 / 函数 / 调用链树）'
              }
            >
              <GitBranch size={14} /> 调用导航
            </button>
            <button
              type="button"
              className="button ghost metric-action-button"
              disabled={!remoteLogLocator?.environment || !remoteLogLocator.startTime.trim() || !remoteLogLocator.endTime.trim()}
              onClick={() => {
                const locator = remoteLogLocator;
                if (!locator?.environment || !locator.startTime.trim() || !locator.endTime.trim()) return;
                setEventRestorePreset({
                  environmentId: locator.environment.id,
                  environmentName: locator.environment.name,
                  startTime: locator.startTime.trim(),
                  endTime: locator.endTime.trim(),
                });
                setEventRestoreOpen(true);
              }}
              title="选择时间范围后直接读取运行日志 event.log 并展示根因树"
            >
              <ListTree size={14} /> 根因树
            </button>
            <button
              type="button"
              className={`button ghost metric-action-button ${showTimeline ? 'active' : ''}`}
              disabled={!activeTask || activeTask.status !== 'ready' || rawLogMode}
              aria-pressed={showTimeline}
              onClick={() => {
                const next = !showTimeline;
                if (next) positionTimelineBelowToolbar();
                setShowTimeline(next);
                setProcessTimelineExpanded(true);
              }}
              title={
                rawLogMode ? '原始日志模式不显示时间线'
                : !activeTask || activeTask.status !== 'ready' ? '先查询日志，时间线会展示模块 / 进程 / Trace 的时间分布'
                : showTimeline ? '关闭时间线' : '打开时间线（模块 / 进程 / Trace 时间分布）'
              }
            >
              <Clock size={14} /> 时间线
            </button>
            </div>
            <div className="error-navigation" aria-label="异常导航">
              <button type="button" className="button ghost metric-action-button error-nav-button" disabled={!navigableErrorEntries.length || selectedErrorIndex === 0} onClick={() => jumpToAdjacentError(-1)} title="自动跳到上一条异常所在页并定位日志"><ArrowUp size={14}/> 上一异常</button>
              {/* 只报「第几个异常」。翻页是实现细节：点上一/下一异常会自动跳到它所在的页，
    把页码写在这里反而让人以为要先自己翻页。 */}
              <span className="error-nav-position" title={`当前筛选范围共 ${navigableErrorEntries.length} 条异常；跳转会自动翻页`}>{selectedErrorIndex >= 0 ? selectedErrorIndex + 1 : 0} / {navigableErrorEntries.length}</span>
              <button type="button" className="button ghost metric-action-button error-nav-button" disabled={!navigableErrorEntries.length || selectedErrorIndex === navigableErrorEntries.length - 1} onClick={() => jumpToAdjacentError(1)} title="自动跳到下一条异常所在页并定位日志">下一异常 <ArrowDown size={14}/></button>
            </div>
            <button
              type="button"
              className="button ghost metric-action-button"
              onClick={() => { setLogSortOrder((current) => current === 'asc' ? 'desc' : 'asc'); setLogPage(1); }}
              title={logSortOrder === 'asc' ? '当前按时间升序展示，点击切换为降序' : '当前按时间降序展示，点击切换为升序'}
            >
              {logSortOrder === 'asc' ? <ArrowUp size={14} /> : <ArrowDown size={14} />}
              {logSortOrder === 'asc' ? '升序' : '降序'}
            </button>
            <SwitchControl
              checked={Boolean(filters.errorsOnly)}
              label="只看报错"
              hint={filters.errorsOnly ? '仅展示命中异常规则的日志' : '默认展示完整上下文'}
              onChange={(checked) => { setFilters((current) => ({ ...current, errorsOnly: checked })); setLogPage(1); }}
            />
            {/* 源码语义 与「只看报错 / 原始日志」同类：都决定这一屏日志长什么样，
                所以放在这组开关里，而不是跟右侧的动作按钮混在一起。 */}
            <SwitchControl
              checked={semanticLabelsEnabled}
              label="源码语义"
              hint={semanticLabelsEnabled
                ? '每行右侧显示语义说明与自定义标签，标签颜色同时画到时间线泳道'
                : '关闭语义说明与自定义标签'}
              onChange={(checked) => setSemanticLabelsEnabled(checked)}
            />
            <SwitchControl
              checked={liveListening}
              label="实时监听"
              disabled={!remoteLogLocator?.environment || remoteLogLocator.liveTargets.length !== 1}
              hint={liveConnectionStatus === 'connecting'
                  ? '正在建立后端实时推送通道'
                  : liveListening
                    ? `${liveMessage || 'SSH tail -F 长连接持续推送新增日志'}${liveAddedCount > 0 ? ` · 累计 ${liveAddedCount}` : ''}`
                    : liveConnectionStatus === 'error' && liveMessage
                      ? `上次监听失败：${liveMessage}`
                      : '选择一个组件即可开启 · SSH tail -F → SSE 持续推送新增日志'}
              onChange={(checked) => {
                setLiveAddedCount(0);
                if (checked) {
                  const locator = remoteLogLocator;
                  const targets = locator?.liveTargets ?? [];
                  if (!locator?.environment || targets.length !== 1) {
                    const message = targets.length > 1
                      ? `实时日志监听仅支持单组件，当前选择了 ${targets.length} 个组件，请只保留一个。`
                      : '实时日志监听仅支持单组件，请先选择一个环境和一个组件。';
                    setLiveConnectionStatus('error');
                    setLiveMessage(message);
                    setLiveListening(false);
                    window.alert(message);
                    return;
                  }
                  beginLiveMonitoring({
                    environment: locator.environment,
                    target: targets[0],
                    sourceCategories: locator.sourceCategories,
                    startTime: locator.startTime,
                    endTime: locator.endTime,
                  });
                  return;
                }
                liveRenderGenerationRef.current += 1;
                liveBoundTaskIdRef.current = undefined;
                setLiveMessage('');
                setLiveListening(false);
                // 实时日志开关关掉 → 彻底清除刚才实时采集的项。
                clearLiveCaptureItems();
              }}
            />
            <button
              type="button"
              className={`button ghost compact-button toolbar-icon-button ${collecting ? 'active' : ''}`}
              onClick={() => void beginDataExtraction()}
              title={collecting ? '数据采集中（图标转圈即表示正在采集）· 点开查看每项进度' : '数据采集：勾选要对当前日志采集的数据项'}
            >
              {collecting ? <LoaderCircle className="spin" size={14} /> : <Database size={14} />} 数据采集
            </button>
            <SwitchControl
              checked={rawLogMode}
              label="原始日志"
              hint={rawLogMode ? '直接展示日志原文，不做解析' : '使用结构化日志视图'}
              onChange={setRawLogMode}
            />
            {activeTask && activeTask.status === 'ready' && (
              <div className="log-search-action-tools" aria-label="日志操作">
                {/* 案例录入与相似案例匹配合成一个窗口的两个标签页：同一条工作流的进出两端，
                    分成两个按钮只会让用户先猜哪个是自己要的。 */}
                <button
                  type="button"
                  className={`button ghost compact-button toolbar-icon-button ${smartAnalysisTab ? 'active' : ''}`}
                  disabled={navigableErrorEntries.length === 0}
                  onClick={() => setSmartAnalysisTab(smartAnalysisTab ? undefined : 'analysis')}
                  title={navigableErrorEntries.length === 0 ? '先查询日志，这里会用当前异常日志匹配历史案例' : '案例：相似案例匹配 / 案例录入'}
                >
                  <Wand2 size={14} /> 案例
                </button>
                <button type="button" className="button ghost compact-button toolbar-icon-button" disabled={!activeTask.entries.length} onClick={downloadVisibleLogs} title="下载日志">
                  <Download size={14} /> 下载
                </button>
              </div>
            )}
          </div>
        </div>

      </div>

      {activeTask?.status === 'analyzing' && (
        <section className="analysis-progress-panel" aria-live="polite">
          <div className="analysis-spinner" />
          <div className="analysis-progress-copy">
            {activeTask.remoteProgress && !activeTask.sources.length ? <>
              <strong>{activeTask.remoteProgress.current_action || (activeTask.remoteProgress?.stage === 'reading' ? '正在读取命中日志' : activeTask.remoteProgress?.stage === 'cancelling' ? '正在停止任务' : '正在准备日志检索')}</strong>
              <div className="remote-search-progress-detail">
                {(activeTask.remoteProgress?.current_host || activeTask.remoteProgress.current_source_category) && (
                  <div className="remote-search-progress-row">
                    <span className="remote-search-progress-label">资源</span>
                    <span title={activeTask.remoteProgress.current_root || undefined}>
                      {activeTask.remoteProgress?.current_host || '—'}
                      {activeTask.remoteProgress.current_username ? ` (${activeTask.remoteProgress.current_username})` : ''}
                      {activeTask.remoteProgress.current_source_category ? ` · ${activeTask.remoteProgress.current_source_category}` : ''}
                      {(activeTask.remoteProgress.target_total ?? 0) > 1 ? ` · ${activeTask.remoteProgress.target_current ?? 0}/${activeTask.remoteProgress.target_total}` : ''}
                    </span>
                  </div>
                )}
                {activeTask.remoteProgress?.current_directory && activeTask.remoteProgress?.stage !== 'reading' && (
                  <div className="remote-search-progress-row">
                    <span className="remote-search-progress-label">目录</span>
                    <span className="remote-search-progress-path" title={activeTask.remoteProgress?.current_directory}>{activeTask.remoteProgress?.current_directory}</span>
                  </div>
                )}
                {(activeTask.remoteProgress.current_subsystem || activeTask.remoteProgress.current_module) && (
                  <div className="remote-search-progress-row">
                    <span className="remote-search-progress-label">范围</span>
                    <span title={[activeTask.remoteProgress.current_subsystem, activeTask.remoteProgress.current_module].filter(Boolean).join(' / ')}>
                      {[activeTask.remoteProgress.current_subsystem, activeTask.remoteProgress.current_module].filter(Boolean).join(' / ')}
                    </span>
                  </div>
                )}
                {activeTask.remoteProgress?.stage === 'reading' && activeTask.remoteProgress.current_file ? (
                  <div className="remote-search-progress-row">
                    <span className="remote-search-progress-label">文件</span>
                    <span className="remote-search-progress-path" title={activeTask.remoteProgress.current_file_path || undefined}>
                      {activeTask.remoteProgress.current_file}
                      {(activeTask.remoteProgress.artifact_total ?? 0) > 0 ? ` · ${activeTask.remoteProgress.artifact_current ?? Math.min((activeTask.remoteProgress.artifact_done ?? 0) + 1, activeTask.remoteProgress.artifact_total ?? 0)}/${activeTask.remoteProgress.artifact_total}` : ''}
                    </span>
                  </div>
                ) : (
                  <div className="remote-search-progress-row remote-search-progress-stats">
                    <span className="remote-search-progress-label">进展</span>
                    <span>
                      当前目录候选 {activeTask.remoteProgress?.current_directory_candidates ?? activeTask.remoteProgress.current_subsystem_files ?? 0}
                      {(activeTask.remoteProgress?.current_directory_selected ?? 0) > 0 ? ` · 时间命中 ${activeTask.remoteProgress?.current_directory_selected}` : ''}
                      {(activeTask.remoteProgress.discovered_files ?? 0) > 0 ? ` · 累计检查 ${activeTask.remoteProgress.checked_files ?? 0}/${activeTask.remoteProgress.discovered_files}` : ''}
                      {(activeTask.remoteProgress.selected_files ?? 0) > 0 ? ` · 已定位 ${activeTask.remoteProgress.selected_files}` : ''}
                    </span>
                  </div>
                )}
              </div>
              <div className="remote-search-stage-strip" aria-label="日志检索阶段">
                {['连接资源', '定位目录', '筛选文件', '读取日志'].map((label, index) => {
                  const currentStage = activeTask.remoteProgress?.stage === 'reading' ? 3 : activeTask.remoteProgress?.current_directory_candidates !== undefined ? 2 : activeTask.remoteProgress?.current_directory ? 1 : activeTask.remoteProgress?.current_host ? 0 : -1;
                  return <span key={label} className={index < currentStage ? 'done' : index === currentStage ? 'active' : ''}>{label}</span>;
                })}
              </div>
              <div className="analysis-progress-track"><span style={{ width: `${Math.max(2, activeTask.remoteProgress.percent ?? 0)}%` }} /></div>
            </> : <>
              <strong>{activeTask.progress?.phase === 'planning' ? '正在从文件尾部定位时间范围' : activeTask.importStrategy === 'full' ? '正在解析远程日志' : '正在加载最近时间范围日志'}</strong>
              <span>{activeTask.progress?.currentSource ?? activeTask.sourceNames[0] ?? '准备读取文件'}{' · '}{activeTask.progress?.linesRead ?? 0} 行{' · '}有效 {activeTask.progress?.validCount ?? 0}{' · '}读取 {formatBytes(activeTask.progress?.bytesRead)} / {formatBytes(activeTask.progress?.totalBytes)}</span>
              <div className="analysis-progress-track"><span style={{ width: `${activeTask.progress?.percent ?? 0}%` }} /></div>
            </>}
          </div>
          <strong className="analysis-progress-percent">{activeTask.remoteProgress && !activeTask.sources.length ? `${Math.round(activeTask.remoteProgress.percent ?? 0)}%` : activeTask.progress?.phase === 'planning' ? '定位中' : `${activeTask.progress?.percent ?? 0}%`}</strong>
          <button type="button" className="button danger-outline compact task-stop-button" onClick={() => stopTask(activeTask.id)}>停止</button>
        </section>
      )}

      {activeTask?.status === 'cancelled' && (
        <section className="analysis-progress-panel cancelled" role="status">
          <X size={20} /><div className="analysis-progress-copy"><strong>任务已停止</strong><span>可以调整时间范围或筛选条件后重新搜索。</span></div>
        </section>
      )}

      {activeTask?.status === 'error' && (
        <section className="analysis-progress-panel error" role="alert">
          <AlertTriangle size={20} />
          <div className="analysis-progress-copy"><strong>日志分析失败</strong><span>{activeTask.errorMessage ?? '未知错误'}</span></div>
        </section>
      )}

      <div className="workspace">

        <main className="main-panel">
          <div className={classNames('log-viewer-shell', !activeTask && 'empty-log-viewer')}>
            {showTimeline && activeTask?.status === 'ready' && !rawLogMode && timelineDocked && (
              <section className="docked-timeline-window" aria-label="时间线（已固定到日志区）">
                <div className="docked-timeline-body">{timelineOverviewNode}</div>
              </section>
            )}
            <div
              className="log-scroll-region"
              ref={logScrollRef}
            >
              <div className="timeline-content">
            {!activeTask ? (
              <div className="empty-state large">
                <UploadCloud size={42} />
                <h2>选择环境定位日志，或导入日志开始分析</h2>
                <p className="empty-log-entry-hint">可以从环境资源进入远程日志定位，也可以导入本地日志文件或粘贴日志文本创建独立分析任务。</p>
              </div>
            ) : rawLogMode ? (
              <RawLogView task={activeTask} live={liveListening} />
            ) : foldingEnabled ? (
              paginatedVisibleTraces.length > 0 ? (
                timelineGroupingMode === 'merged' && !selectedProcess && !selectedThread && !selectedTrace && !selectedCrossTrace ? (
                  <MergedFmTimelineView
                    traces={paginatedVisibleTraces}
                    filters={renderFilters}
                    sortOrder={logSortOrder}
                    expandedIds={expandedIds}
                    selectedEntryId={highlightedEntryId}
                    onToggle={toggleExpanded}
                    onSelectEntry={(entry) => { setFocusedEntryId(entry.severity === 'error' ? entry.id : undefined); setSelectedEntry(entry); setShowIssues(false); }}
                  />
                ) : (
                  <ThreadGroupedFlatView
                    traces={paginatedVisibleTraces}
                    filters={renderFilters}
                    sortOrder={logSortOrder}
                    expandedIds={expandedIds}
                    selectedEntryId={highlightedEntryId}
                    onToggle={toggleExpanded}
                    onSelectEntry={(entry) => { setFocusedEntryId(entry.severity === 'error' ? entry.id : undefined); setSelectedEntry(entry); setShowIssues(false); }}
                  />
                )
              ) : (
                <div className="empty-state large">
                  <GitBranch size={42} />
                  <h2>当前条件下没有可折叠的调用日志</h2>
                  <p>可关闭函数折叠查看逐行日志，或调整当前筛选条件。</p>
                </div>
              )
            ) : (
              <FlatLogView
                entries={paginatedEntries}
                filters={renderFilters}
                sortOrder={logSortOrder}
                selectedEntryId={highlightedEntryId}
                onSelectEntry={(entry) => { setFocusedEntryId(entry.severity === 'error' ? entry.id : undefined); setSelectedEntry(entry); setShowIssues(false); }}
                stickyTop={0}
              />
            )}
              </div>
            </div>
            {activeTask && activeTask.status === 'ready' && !rawLogMode && (
              <LogPaginationBar
                page={safeLogPage}
                pageSize={logPageSize}
                total={scopedFilteredEntries.length}
                onPageChange={setLogPage}
                onPageSizeChange={(size) => { setLogPageSize(Math.max(100, Math.min(MAX_LOG_PAGE_SIZE, size))); setLogPage(1); }}
                statistics={{
                  source: currentSourceCount,
                  modules: currentComponentCount,
                  processes: processes.length,
                  threads: processes.flatMap((process) => process.threads).length,
                  functions: totalFunctions,
                  errorTraces: errorTraceCount,
                  errors: errorEntryCount,
                }}
              />
            )}
          </div>
        </main>

        {showTimeline && activeTask?.status === 'ready' && !rawLogMode && !timelineDocked && (
          <section
            className="floating-timeline-window"
            ref={timelineWindowRef}
            style={{
              left: timelineWindowPosition.left,
              top: timelineWindowPosition.top,
              ...(timelineContentHeight ? { '--timeline-content-height': `${timelineContentHeight}px` } : {}),
            } as CSSProperties}
            aria-label="悬浮时间线"
          >
            <div className="floating-timeline-body">{timelineOverviewNode}</div>
          </section>
        )}


        {pasteDialogOpen && (
          <div className="paste-dialog-backdrop" role="presentation" onMouseDown={() => { setPasteDialogOpen(false); setImportDialogMode('choose'); }}>
            <section className="paste-dialog import-log-dialog" role="dialog" aria-modal="true" aria-labelledby="paste-dialog-title" onMouseDown={(event: React.MouseEvent<HTMLElement>) => event.stopPropagation()}>
              <header className="paste-dialog-header">
                <div><div className="eyebrow">IMPORT LOGS</div><h2 id="paste-dialog-title">{importDialogMode === 'choose' ? '导入日志' : importDialogMode === 'paste' ? '粘贴日志文本' : '导入日志链接'}</h2></div>
                <button type="button" className="icon-button" onClick={() => { setPasteDialogOpen(false); setImportDialogMode('choose'); }} aria-label="关闭导入窗口"><X size={18} /></button>
              </header>
              {importDialogMode === 'choose' ? <div className="import-log-choice-grid">
                <button type="button" className="import-log-choice-card file" onClick={() => { setPasteDialogOpen(false); setImportDialogMode('choose'); fileInputRef.current?.click(); }}><FileUp size={28}/><strong>导入日志文件</strong><span>选择一个或多个 .log / .txt 文件作为独立分析任务</span></button>
                <button type="button" className="import-log-choice-card paste" onClick={() => setImportDialogMode('paste')}><ClipboardPaste size={28}/><strong>粘贴日志文本</strong><span>直接粘贴原始日志文本并创建本地分析任务</span></button>
                <button type="button" className="import-log-choice-card url" onClick={() => setImportDialogMode('url')}><Link2 size={28}/><strong>导入日志链接</strong><span>粘贴一个或多个 Nginx 日志 URL；多个链接自动按文件名区分 FM</span></button>
              </div> : importDialogMode === 'paste' ? <>
                <div className="paste-dialog-body">
                  <p>直接粘贴原始日志即可。该任务不绑定环境、日志类型、子系统/模块或时间范围。</p>
                  <textarea autoFocus value={pastedLogText} onChange={(event: React.ChangeEvent<HTMLTextAreaElement>) => setPastedLogText(event.target.value)} onKeyDown={(event: React.KeyboardEvent<HTMLTextAreaElement>) => { if ((event.ctrlKey || event.metaKey) && event.key === 'Enter' && pastedLogText.trim()) { event.preventDefault(); createPastedLogTask(); } }} placeholder="在这里粘贴日志文本……" spellCheck={false}/>
                  <div className="paste-dialog-summary"><span>{pastedLogText ? pastedLogText.split(/\r?\n/).filter((line) => line.trim()).length : 0} 行非空文本</span><span>Ctrl/⌘ + Enter 创建任务</span></div>
                </div>
                <footer className="paste-dialog-footer"><button type="button" className="button secondary" onClick={() => setImportDialogMode('choose')}>返回</button><div><button type="button" className="button secondary" onClick={() => setPastedLogText('')} disabled={!pastedLogText}>清空</button><button type="button" className="button primary" onClick={createPastedLogTask} disabled={!pastedLogText.trim()}>解析并创建任务</button></div></footer>
              </> : <>
                <div className="paste-dialog-body import-url-dialog-body">
                  <p>每行粘贴一个日志链接。普通日志直接解析；压缩包会先读取目录，并要求选择一个内部日志后再创建任务。</p>
                  <textarea
                    autoFocus
                    value={importUrlText}
                    onChange={(event: React.ChangeEvent<HTMLTextAreaElement>) => { setImportUrlText(event.target.value); setUrlImportItems([]); setUrlArchiveSelections({}); setUrlImportError(''); }}
                    placeholder={'http://host/log/moduleA.log\nhttp://host/log/moduleB_20260814.log\nhttp://host/log/moduleC_20260814.tar.gz'}
                    spellCheck={false}
                  />
                  <div className="paste-dialog-summary"><span>{importUrlList.length} 个链接</span><span>FM 按链接文件名自动识别</span></div>
                  {urlImportError && <div className="import-url-error"><AlertTriangle size={14}/><span>{urlImportError}</span></div>}
                  {urlImportItems.length > 0 && <div className="import-url-results">
                    {urlImportItems.map((item) => <article className={classNames('import-url-result', item.status === 'error' && 'error')} key={item.url}>
                      <div className="import-url-result-main">
                        <div><strong>{item.filename || '无法识别文件名'}</strong><span>{item.url}</span></div>
                        <div className="import-url-result-badges"><span className="import-url-fm">FM · {item.fm || '-'}</span><span>{item.kind === 'archive' ? '压缩包' : '日志'}</span></div>
                      </div>
                      {item.status === 'error' ? <div className="import-url-result-error">{item.message || '链接检查失败'}</div> : item.kind === 'archive' ? <label className="import-url-member-select">
                        <span>选择压缩包内日志</span>
                        <select value={urlArchiveSelections[item.url] || ''} onChange={(event) => setUrlArchiveSelections((current) => ({ ...current, [item.url]: event.target.value }))}>
                          <option value="">请选择…</option>
                          {item.members.map((member) => <option value={member.name} key={member.name}>{member.filename} · {member.fm} · {Math.max(1, Math.round(member.size / 1024))} KB</option>)}
                        </select>
                      </label> : null}
                    </article>)}
                  </div>}
                </div>
                <footer className="paste-dialog-footer">
                  <button type="button" className="button secondary" onClick={() => setImportDialogMode('choose')} disabled={urlImportBusy}>返回</button>
                  <div>
                    <button type="button" className="button secondary" onClick={() => { setImportUrlText(''); setUrlImportItems([]); setUrlArchiveSelections({}); setUrlImportError(''); }} disabled={urlImportBusy || (!importUrlText && urlImportItems.length === 0)}>清空</button>
                    <button type="button" className="button secondary" onClick={() => void inspectImportedLogUrls()} disabled={urlImportBusy || importUrlList.length === 0}>{urlImportBusy && urlImportItems.length === 0 ? '检查中…' : '检查链接'}</button>
                    <button type="button" className="button primary" onClick={() => void createUrlLogTask()} disabled={urlImportBusy || !urlImportReady}>{urlImportBusy && urlImportItems.length > 0 ? '读取中…' : '解析并创建任务'}</button>
                  </div>
                </footer>
              </>}
            </section>
          </div>
        )}

        {inlineRuleSeed && (
          <div className="inline-rule-workspace-backdrop" role="presentation" onMouseDown={() => setInlineRuleSeed(undefined)}>
            <aside className="inline-rule-workspace" role="dialog" aria-modal="true" aria-label="日志规则配置" onMouseDown={(event) => event.stopPropagation()}>
              <header className="inline-rule-workspace-header">
                <div><span className="eyebrow">LOG RULES</span><h2>当前日志规则配置</h2><p>直接复用设置中心的日志规则工作区；默认进入语义规则，可切换异常规则、屏蔽规则或数据提取。</p></div>
                <button type="button" className="icon-button" onClick={() => setInlineRuleSeed(undefined)} aria-label="关闭规则配置"><X size={18}/></button>
              </header>
              <div className="inline-rule-workspace-body">
                <LogRulesSettingsPage
                  errorRules={errorRules}
                  displayRules={displayRules}
                  maskingRules={maskingRules}
                  foldingRules={foldingRules}
                  dataExtractionRules={dataExtractionRules}
                  sourceSeed={inlineRuleSeed}
                  onErrorRulesChange={setErrorRules}
                  onDisplayRulesChange={setDisplayRules}
                  onMaskingRulesChange={setMaskingRules}
                  onFoldingRulesChange={setFoldingRules}
                  onDataExtractionRulesChange={setDataExtractionRules}
                />
              </div>
            </aside>
          </div>
        )}

        {eventRestoreOpen && <EventRestoreDialog
          open
          environmentId={eventRestorePreset?.environmentId ?? activeTask?.remoteEnvironmentId}
          environmentName={eventRestorePreset?.environmentName ?? (resourceWorkspaceEnvironment && activeTask?.remoteEnvironmentId && resourceWorkspaceEnvironment.id === activeTask.remoteEnvironmentId ? resourceWorkspaceEnvironment.name : '')}
          startTime={eventRestorePreset?.startTime ?? (filters.timeRange ? nsToTimestampText(filters.timeRange.startNs) : (activeTask?.remoteRequest?.start_time || ''))}
          endTime={eventRestorePreset?.endTime ?? (filters.timeRange ? nsToQueryEndTimestampText(filters.timeRange.endNs) : (activeTask?.remoteRequest?.end_time || ''))}
          formatRules={logFormatRulesRef.current}
          onClose={() => { setEventRestoreOpen(false); setEventRestorePreset(undefined); }}
        />}

        <DataExtractionRunDialog
          open={dataExtractionDialog.open}
          phase={dataExtractionDialog.phase}
          candidates={dataExtractionDialog.candidates}
          selectedIds={dataExtractionDialog.selectedIds}
          progress={dataExtractionDialog.progress}
          results={dataExtractionDialog.results}
          error={dataExtractionDialog.error}
          recordSaved={dataExtractionDialog.recordSaved}
          datasetName={activeTask?.name || '当前提取数据'}
          liveListening={liveListening}
          onStartLiveExtraction={() => { void startSelectedDataExtraction(); }}
          onStartLiveCollection={(ruleIds) => startLiveCollection(ruleIds)}
          liveCaptureIds={liveCaptureRuleIds}
          liveProgress={liveCaptureProgress}
          liveActive={liveListening}
          batchAvailable={dataExtractionDialog.batchAvailable}
          onToggle={(id) => setDataExtractionDialog((current) => { const next = new Set(current.selectedIds); next.has(id) ? next.delete(id) : next.add(id); return { ...current, selectedIds: next }; })}
          onStart={() => void startSelectedDataExtraction()}
          onCancel={() => dataExtractionAbortRef.current?.abort()}
          onClose={() => { if (dataExtractionDialog.phase !== 'running') setDataExtractionDialog((current) => ({ ...current, open: false })); }}
          onDownload={(result) => downloadTemporaryRuleData({ rule: result.rule, rows: result.rows, namePrefix: activeTask?.name || '日志数据' })}
          onDownloadMerged={(results) => downloadMergedTemporaryRuleData({ results, namePrefix: activeTask?.name || '日志数据' })}
          onOpenData={() => { setDataExtractionDialog((current) => ({ ...current, open: false })); openDataPage(activeTask?.remoteOperationId || ''); }}
        />

        {callFlowDialogOpen && createPortal(
          <div className="call-flow-dialog-backdrop" role="presentation" onMouseDown={() => setCallFlowDialogOpen(false)}>
            <section className="call-flow-dialog is-flow-map" role="dialog" aria-modal="true" aria-labelledby="call-flow-dialog-title" onMouseDown={(event: React.MouseEvent<HTMLElement>) => event.stopPropagation()}>
              <header className="call-flow-dialog-header">
                <div>
                  <h2 id="call-flow-dialog-title"><GitBranch size={17} /> 调用导航</h2>
                  <p>进程 / 调用链选择与调用关系图统一在此窗口查看</p>
                </div>
                <button type="button" className="icon-button" onClick={() => setCallFlowDialogOpen(false)} aria-label="关闭调用导航"><X size={18} /></button>
              </header>
              <div className="call-flow-dialog-body">
                <aside className="call-flow-nav-pane">
                  <div className="call-flow-nav-header">
                    <strong>调用范围</strong>
                    {(selectedProcessId || selectedThreadId || selectedTraceId || selectedCrossTraceId) && (
                      <button type="button" className="sidebar-show-all" onClick={() => { clearNavigationSelection(); setCallGraphState(undefined); }}>查看全部</button>
                    )}
                  </div>
              <div className="process-list">
              {visibleCrossComponentTraces.length > 0 && (
              <div className="cross-trace-group">
              <button
              type="button"
              className="cross-trace-group-header"
              onClick={() => setCrossSectionExpanded((current) => !current)}
              >
              <span className="cross-section-chevron">{crossSectionExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</span>
              <GitBranch size={15} />
              <span className="cross-header-copy"><strong>跨组件 RPC</strong><small>{visibleCrossComponentTraces.length} 条全局 Trace</small></span>
              </button>
              {crossSectionExpanded && <div className="cross-method-list">
              {crossMethodGroups.map((group) => {
              const expanded = expandedCrossMethodGroups.has(group.id);
              return (
              <div className={classNames('cross-method-group', `severity-${group.severity}`)} key={group.id}>
              <button
              type="button"
              className="cross-method-group-row"
              onClick={(event) => { const chevron = (event.target as HTMLElement).closest('.cross-method-chevron'); if (chevron || group.errorCount === 0) toggleCrossMethodGroup(group.id); else jumpToFirstError(group.traces.flatMap((trace) => trace.entries)); }}
              title={group.errorCount > 0 ? `${group.name} · 点击函数跳到首个异常；点击箭头展开` : group.name}
              >
              <span className="cross-method-chevron">{expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</span>
              <span className="cross-method-copy">
              <span className="method-entry-time">{group.traces[0] && traceEntryLog(group.traces[0]) ? secondTimestamp(traceEntryLog(group.traces[0])!.timestamp, true) : ''}</span>
              <strong>{group.name}</strong>
              </span>
              {group.errorCount > 0 && <span className="thread-error-count">{group.errorCount}</span>}
              </button>

              {expanded && (
              <div className="cross-method-member-list">
              {group.traces.map((trace) => {
              const severity = traceSeverity(trace);
              const errorCount = trace.entries.filter((entry) => entry.severity === 'error').length;
              return (
              <div
              key={trace.id}
              className={classNames(
              'cross-method-member-row',
              `severity-${severity}`,
              selectedCrossTrace?.id === trace.id && 'active',
              )}
              title={`完整 TraceID ${trace.rpc.traceId} · ${trace.components.join(' → ')}`}
              >
              <button type="button" className="member-select-button" onClick={() => { selectTrace(undefined, trace); openTraceGraph(undefined, trace); }}>
              <GitBranch size={12} />
              <span className="cross-trace-id">{compactTraceId(trace.rpc.traceId)}</span>
              <span className="cross-method-route">{trace.components.join(' → ')}</span>
              <span className="trace-tree-count">{trace.entries.length}</span>
              {errorCount > 0 && <span className="trace-tree-error">{errorCount}</span>}
              </button>
              </div>
              );
              })}
              </div>
              )}
              </div>
              );
              })}
              </div>}
              </div>
              )}

              {visibleProcesses.map((process: ProcessTimeline) => {
              const processExpanded = expandedProcessIds.has(process.id);
              const severity = processSeverity(process);
              const errorCount = processErrorCount(process);
              return (
              <div
              className={classNames(
              'process-group',
              `severity-${severity}`,
              selectedProcess?.id === process.id && 'active',
              )}
              key={process.id}
              >
              <div className="process-row">
              <button
              type="button"
              className="process-expand-button"
              onClick={() => toggleProcess(process.id)}
              aria-label={processExpanded ? '折叠进程' : '展开进程'}
              >
              {processExpanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
              </button>
              <button type="button" className="process-select-button" onClick={() => { selectProcess(process); openProcessGraph(process); }}>
              <span className="process-icon"><ServerCog size={16} /></span>
              <span className="process-row-copy"><strong>{compactEntryRangeSeconds(processEntries(process), true)}</strong></span>
              {errorCount > 0 && (
              <span className="process-error-count" title={`${errorCount} 条 ERROR 日志`}>
              <AlertTriangle size={11} />
              {errorCount}
              </span>
              )}
              <ComponentBadge component={process.component} compact />
              </button>
              </div>
              {processExpanded && <div className="method-group-list">
              {(methodGroupsByProcess.get(process.id) ?? []).map((group) => {
              const expanded = expandedMethodGroups.has(group.id);
              return (
              <div className={classNames('method-group', `severity-${group.severity}`)} key={group.id}>
              <button
              type="button"
              className="method-group-row"
              onClick={(event) => { const chevron = (event.target as HTMLElement).closest('.method-group-chevron'); if (chevron || group.errorCount === 0) toggleMethodGroup(group.id); else jumpToFirstError(group.members.flatMap(({ trace }) => trace.entries)); }}
              title={group.errorCount > 0 ? `${group.name} · 点击函数跳到首个异常；点击箭头展开` : group.name}
              >
              <span className="method-group-chevron">{expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</span>
              <span className="method-group-copy">
              <span className="method-entry-time">{group.members[0] && traceEntryLog(group.members[0].trace) ? secondTimestamp(traceEntryLog(group.members[0].trace)!.timestamp, true) : ''}</span>
              <strong>{group.name}</strong>
              </span>
              {group.errorCount > 0 && <span className="thread-error-count">{group.errorCount}</span>}
              </button>

              {expanded && (
              <div className="method-member-list">
              {group.members.map(({ thread, trace }) => {
              const severity = traceSeverity(trace);
              const traceErrors = trace.entries.filter((entry) => entry.severity === 'error').length;
              return (
              <div
              key={trace.id}
              className={classNames(
              'method-member-row',
              `severity-${severity}`,
              selectedTrace?.id === trace.id && 'active',
              )}
              title={`TID ${thread.threadId} · ${isZeroTrace(trace.traceKey) ? '本地调用链' : `完整 TraceID ${trace.rpc.traceId}`}`}
              >
              <button type="button" className="member-select-button" onClick={() => { selectTrace(thread, trace); openTraceGraph(thread, trace); }}>
              <CircleDot size={12} />
              <span className="member-tid">{thread.threadId}</span>
              <span className="member-trace">{isZeroTrace(trace.traceKey) ? 'LOCAL' : compactTraceId(trace.rpc.traceId)}</span>
              <span className="member-time">{compactEntryRangeSeconds(trace.entries)}</span>
              <span className="trace-tree-count">{trace.entries.length}</span>
              {traceErrors > 0 && <span className="trace-tree-error">{traceErrors}</span>}
              </button>
              </div>
              );
              })}
              </div>
              )}
              </div>
              );
              })}
              </div>}
              </div>
              );
              })}
              {visibleProcesses.length === 0 && (
              <div className="empty-state compact"><Sparkles size={22} /><span>当前时间范围或筛选条件下没有匹配进程</span></div>
              )}
              </div>

              <div className="sidebar-footer">
              <button type="button" className="issue-button" onClick={() => { setShowIssues(true); setCallFlowDialogOpen(false); }}>
              <AlertTriangle size={16} /><span>解析报告</span><strong>{parsed.issues.length}</strong>
              </button>
              </div>
                </aside>
                <section className="call-flow-graph-pane" aria-label="调用关系图">
                  <div className="call-flow-view-switch" role="tablist" aria-label="函数导航视图">
                    <button type="button" role="tab" aria-selected={callFlowView === 'flowmap'} className={callFlowView === 'flowmap' ? 'active' : ''} onClick={() => setCallFlowView('flowmap')} title="按标签和异常着色的横向流程地图，可缩放">
                      <Share2 size={13} /> 流程地图
                    </button>
                    <button type="button" role="tab" aria-selected={callFlowView === 'tree'} className={callFlowView === 'tree' ? 'active' : ''} onClick={() => setCallFlowView('tree')} title="时间轴缩进树：按时间铺开，保留嵌套层级">
                      <ListTree size={13} /> 缩进树
                    </button>
                  </div>
                  {callFlowView === 'flowmap'
                    ? <FlowMapView
                        traces={callGraphState?.traces ?? []}
                        rules={displayRules}
                        semanticEnabled={semanticLabelsEnabled}
                        onSelectNode={focusFunctionFromGraph}
                        onSelectEntry={(entry) => setSelectedEntry(entry)}
                        renderLogRow={(entry) => <LogRow entry={entry} selected={false} onSelect={setSelectedEntry} />}
                      />
                    : <TimeTreeView traces={callGraphState?.traces ?? []} rules={displayRules} onSelectNode={focusFunctionFromGraph} />}
                </section>
              </div>
            </section>
          </div>,
          document.body,
        )}
        {selectedEntry && createPortal(<EntryInspector entry={selectedEntry} onClose={() => setSelectedEntry(undefined)} />, document.body)}
        {showIssues && createPortal(<IssuePanel issues={parsed.issues} onClose={() => setShowIssues(false)} />, document.body)}
      </div>
      </>}
      {globalOverlays}
    </div>
    </SemanticDisplayContext.Provider>
    </ErrorRuleContext.Provider>
    </FoldingRuleContext.Provider>
  );
}
