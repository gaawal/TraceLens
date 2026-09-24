import { registerPageContextReader } from '../assistant/contextRegistry';
import { useEffect, useMemo, useRef, useState, type ComponentType, type DragEvent, type NamedExoticComponent, type ReactNode } from 'react';
import ExcelJS from 'exceljs';
import { afterPaint } from '../assistant/workstation';
import type { CellValue } from 'exceljs';
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  BookOpenCheck,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  ExternalLink,
  FileSearch,
  FileSpreadsheet,
  FileText,
  ListTree,
  LoaderCircle,
  Search,
  Server,
  ServerCog,
  Sparkles,
  Square,
  UploadCloud,
  WandSparkles,
  XCircle,
} from 'lucide-react';
import {
  analyzeAtLogCase,
  analyzeAtLogReportFile,
  cancelAtLogCaseLogs,
  streamAtLogAiDiagnosis,
  markAbnormalCaseMatched,
  matchAtLogKnowledge,
  queryAtLogCaseLogs,
  startAtLogAiDiagnosis,
  listRuntimeLogFormatRules,
  listEnvironments,
  getEnvironmentRuntimeStatus,
  reportAtLogExcelImportDebug,
  readAtLogCaseFile,
  type AtLogAiDiagnosisJob,
  type AtLogAiDiagnosisResult,
  type AtLogAiDiagnosisStep,
  type AtLogAnomalyRulePayload,
  type AtLogAiJobEvent,
  type AtLogKnowledgeMatchResult,
  type AtLogCaseAnalysis,
  type AtLogCaseLogResult,
  type AtLogCaseLogRow,
  type AtLogReportFileAnalysis,
  type AtLogReportSection,
  type GlobalLogSubsystem,
  type EnvironmentSummary,
  type EnvironmentRuntimeStatus,
  type LogWindowRequest,
} from '../api/resourceApi';
import { normalizedEventLevel, parseEventRestoreStream } from '../rendering/eventRestore';
import { DEFAULT_ERROR_RULES, detectSeverity, matchesErrorRule, parseLogLineByCategories, timestampToNs, type ErrorMatchRule } from '../parser/logParser';
import { buildProcessTimelines } from '../parser/treeBuilder';
import type { TimeRangeFilter } from '../parser/timeIndex';
import { EventRestoreDialog } from './EventRestoreDialog';
import { AbnormalCaseAnalysisDialog } from './AbnormalCaseAnalysisDialog';
import { AbnormalCaseEditorDialog } from './AbnormalCaseEditorDialog';
import { HierarchyModuleSelect, splitTargetKey, targetKey, LogTypeSelect } from './RemoteLogQueryPanel';
import { SmartDateTimeInput } from './SmartDateTimeInput';
import { EnvironmentResourcePreview } from './EnvironmentResourcePage';
import type { ContentSeverity, LogEntry, LogFormatParserRuleConfig, ProcessTimeline, ThreadTimeline, TraceTimeline } from '../types';

interface ImportedCase {
  key: string;
  caseName: string;
  caseId: string;
  caseDescription?: string;
  url: string;
  phase: 'pending' | 'analyzing' | 'done' | 'error';
  analysis?: AtLogCaseAnalysis;
  error?: string;
  expanded?: boolean;
}

interface ImportedWorkbook {
  id: string;
  name: string;
  cases: ImportedCase[];
}

interface AtLogPageCache {
  singleUrl: string;
  workbooks: ImportedWorkbook[];
  activeWorkbookId: string;
  query: string;
  statusFilter: 'all' | 'failed' | 'passed' | 'pending';
  reasonFilter: string;
}

interface AtLogCaseUiCache {
  collectedMessages: AtLogCaseLogRow[];
  aiJob?: AtLogAiDiagnosisJob;
  aiEvents: AtLogAiJobEvent[];
  aiResult?: AtLogAiDiagnosisResult;
  aiError: string;
  knowledge?: AtLogKnowledgeMatchResult;
  usedHistoricalCaseId?: number;
  acceptedAiCaseId?: number;
}

interface AtLogLogWorkspaceCache {
  startTime: string;
  endTime: string;
  keyword: string;
  selectedTargets: string[];
  onlyErrors: boolean;
  descending: boolean;
  result?: AtLogCaseLogResult;
  showRootCause: boolean;
  timelineExpanded: boolean;
  environmentKey: string;
  selectedSources: string[];
}

const MANUAL_WORKBOOK_ID = 'manual-url-workbook';
let ATLOG_PAGE_CACHE: AtLogPageCache = { singleUrl: '', workbooks: [{ id: MANUAL_WORKBOOK_ID, name: '手工用例', cases: [] }], activeWorkbookId: MANUAL_WORKBOOK_ID, query: '', statusFilter: 'all', reasonFilter: '' };
try {
  const saved = JSON.parse(localStorage.getItem('tracelens-atlog-tabs-v1') || 'null');
  if (saved && Array.isArray(saved.workbooks) && saved.workbooks.every((b: ImportedWorkbook) => b && Array.isArray(b.cases))) ATLOG_PAGE_CACHE = {...ATLOG_PAGE_CACHE, ...saved};
} catch { /* Bad browser cache should not hide the workspace. */ }
const ATLOG_CASE_UI_CACHE = new Map<string, AtLogCaseUiCache>();
const ATLOG_LOG_WORKSPACE_CACHE = new Map<string, AtLogLogWorkspaceCache>();

function persistedWorkspaceCache(analysis?: AtLogCaseAnalysis | null): AtLogLogWorkspaceCache | undefined {
  if (!analysis) return undefined;
  const workspace = analysis.saved_state?.workspace;
  const state = workspace?.state;
  const result = workspace?.result;
  if (!state || typeof state !== 'object') return undefined;
  const value = state as Record<string, unknown>;
  const selectedTargets = Array.isArray(value.selectedTargets) ? value.selectedTargets.map(String) : [];
  const selectedSources = Array.isArray(value.selectedSources) ? value.selectedSources.map(String) : ['pytest', 'xytest', 'event'];
  const savedResult = result && typeof result === 'object' && Array.isArray((result as AtLogCaseLogResult).rows) ? result as AtLogCaseLogResult : undefined;
  return {
    startTime: toLocalInput(analysis.start_time || analysis.event_start_time),
    endTime: toLocalInput(analysis.end_time || analysis.event_end_time),
    keyword: String(value.keyword || ''),
    selectedTargets,
    onlyErrors: Boolean(value.onlyErrors),
    descending: Boolean(value.descending),
    result: savedResult,
    showRootCause: Boolean(value.showRootCause),
    timelineExpanded: value.timelineExpanded !== false,
    environmentKey: String(value.environmentKey || ''),
    selectedSources,
  };
}

function persistedAiResult(analysis?: AtLogCaseAnalysis | null): AtLogAiDiagnosisResult | undefined {
  if (!analysis) return undefined;
  const result = analysis.saved_state?.ai?.result;
  if (!result || typeof result !== 'object' || !('report' in result)) return undefined;
  return result as AtLogAiDiagnosisResult;
}

function persistedAiJob(analysis?: AtLogCaseAnalysis | null): AtLogAiDiagnosisJob | undefined {
  if (!analysis) return undefined;
  const ai = analysis.saved_state?.ai;
  const result = persistedAiResult(analysis);
  const thinkingText = String(ai?.thinking_text || '').trim();
  if (!result && !thinkingText) return undefined;
  const completedMs = Date.parse(String(ai?.completed_at || analysis.saved_state?.updated_at || ''));
  const completedAt = Number.isFinite(completedMs) ? completedMs / 1000 : 0;
  const resultUsage = result?.token_usage;
  const savedUsage = ai?.token_usage && 'total_tokens' in ai.token_usage ? ai.token_usage as AtLogAiDiagnosisJob['token_usage'] : undefined;
  return {
    job_id: String(ai?.job_id || `snapshot-${analysis.case_id || 'case'}`),
    url: analysis.base_url,
    status: 'completed',
    created_at: completedAt,
    updated_at: completedAt,
    current_stage: { name: 'done', label: '诊断完成', status: 'completed', detail: '' },
    token_usage: savedUsage || resultUsage || { input_tokens: 0, output_tokens: 0, total_tokens: 0 },
    report_preview: '',
    thinking_text: thinkingText,
    cache_hit: true,
    cache_source: 'database',
    events: [],
    last_seq: 0,
    result: result || null,
    error: '',
  };
}

const ATLOG_ERROR_RULE_STORAGE_KEY = 'tracelens.error-rules.v3';

function currentAtLogAiAnomalyRules(currentRules?: readonly ErrorMatchRule[]): Array<{ id?: string; keyword: string; case_sensitive: boolean; whole_word: boolean; enabled: boolean }> {
  let rules: ErrorMatchRule[] = currentRules !== undefined ? currentRules.map((rule) => ({ ...rule })) : DEFAULT_ERROR_RULES.map((rule) => ({ ...rule }));
  if (currentRules === undefined && typeof window !== 'undefined') {
    try {
      const stored = window.localStorage.getItem(ATLOG_ERROR_RULE_STORAGE_KEY);
      if (stored) {
        const parsed = JSON.parse(stored);
        if (Array.isArray(parsed)) {
          rules = parsed.flatMap((item, index) => {
            if (!item || typeof item !== 'object') return [];
            const keyword = String(item.keyword ?? '').trim();
            if (!keyword) return [];
            return [{
              id: String(item.id || `atlog-error-rule-${index}`),
              keyword,
              caseSensitive: Boolean(item.caseSensitive),
              wholeWord: Boolean(item.wholeWord),
              enabled: item.enabled !== false,
            } satisfies ErrorMatchRule];
          });
        }
      }
    } catch { /* fall back to current built-in rules */ }
  }
  return rules.filter((rule) => rule.enabled && rule.keyword.trim()).map((rule) => ({
    id: rule.id,
    keyword: rule.keyword.trim(),
    case_sensitive: rule.caseSensitive,
    whole_word: rule.wholeWord,
    enabled: true,
  }));
}

function caseUiCache(key: string): AtLogCaseUiCache {
  const existing = ATLOG_CASE_UI_CACHE.get(key);
  if (existing) return existing;
  const created: AtLogCaseUiCache = { collectedMessages: [], aiEvents: [], aiError: '' };
  ATLOG_CASE_UI_CACHE.set(key, created);
  return created;
}

type TimelineRendererProps = {
  processes: ProcessTimeline[];
  selectedProcessId?: string;
  selectedThreadId?: string;
  selectedTraceId?: string;
  selectedTimeRange?: TimeRangeFilter;
  loadedTimeRange?: TimeRangeFilter;
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
};

type TimelineRenderer = ComponentType<TimelineRendererProps> | NamedExoticComponent<TimelineRendererProps>;

interface AtLogAnalysisPageProps {
  TimelineComponent?: TimelineRenderer;
  errorRules?: readonly ErrorMatchRule[];
  onOpenEnvironmentCpdReports?: (environment: EnvironmentSummary) => void;
  onOpenEnvironmentLogLocator?: (environment: EnvironmentSummary, request?: LogWindowRequest) => void;
}


const BATCH_CONCURRENCY = 6;
const HEADER_ALIASES = {
  caseName: ['用例名称', '用例名', 'case名称', 'casename'],
  caseId: ['用例编号', '用例id', 'caseid', '用例ID'],
  url: [
    '日志路径', '日志链接', '日志url', '日志地址', 'logurl', 'url',
    'ATLog', 'ATLog链接', 'ATLog地址', 'ATLog路径', 'ATLogURL',
    '用例链接', '用例URL', 'caseurl', 'reporturl',
  ],
  caseDescription: ['用例描述', '描述', '用例说明', 'case description', 'casedescription', 'description'],
};

function normalizedHeader(value: unknown): string {
  return String(value ?? '').replace(/[\s_\-]/g, '').trim().toLowerCase();
}

function cellText(value: CellValue): string {
  if (value == null) return '';
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return String(value).trim();
  if (value instanceof Date) return value.toISOString();
  if (typeof value === 'object') {
    if ('text' in value && typeof value.text === 'string') return value.text.trim();
    if ('hyperlink' in value && typeof value.hyperlink === 'string') return value.hyperlink.trim();
    if ('richText' in value && Array.isArray(value.richText)) return value.richText.map((item) => item.text).join('').trim();
    if ('result' in value && value.result != null) return String(value.result).trim();
  }
  return String(value).trim();
}

function firstHttpUrl(text: string): string {
  const match = String(text || '').match(/https?:\/\/[^\s"'<>),;]+/i);
  return match?.[0]?.trim() || '';
}

function urlCellDetail(value: CellValue): { raw: string; extracted: string; source: string } {
  if (value == null) return { raw: '', extracted: '', source: 'empty' };
  if (typeof value === 'object') {
    if ('hyperlink' in value && typeof value.hyperlink === 'string') {
      return { raw: cellText(value), extracted: value.hyperlink.trim(), source: 'hyperlink' };
    }
    if ('formula' in value && typeof value.formula === 'string') {
      const fromFormula = firstHttpUrl(value.formula);
      if (fromFormula) return { raw: value.formula, extracted: fromFormula, source: 'formula' };
    }
    if ('richText' in value && Array.isArray(value.richText)) {
      const rich = value.richText.map((item) => item.text).join('').trim();
      const fromRich = firstHttpUrl(rich);
      return { raw: rich, extracted: fromRich || rich, source: 'richText' };
    }
    if ('result' in value && value.result != null) {
      const result = String(value.result).trim();
      const fromResult = firstHttpUrl(result);
      return { raw: result, extracted: fromResult || result, source: 'formula-result' };
    }
  }
  const raw = cellText(value);
  return { raw, extracted: firstHttpUrl(raw) || raw, source: 'text' };
}

function cellUrl(value: CellValue): string {
  return urlCellDetail(value).extracted;
}

function looksLikeUrlHeader(value: string): boolean {
  const normalized = normalizedHeader(value);
  if (!normalized) return false;
  if (HEADER_ALIASES.url.map(normalizedHeader).includes(normalized)) return true;
  if (normalized.includes('atlog') && (normalized.includes('url') || normalized.includes('链接') || normalized.includes('路径') || normalized.includes('地址'))) return true;
  if ((normalized.includes('日志') || normalized.includes('用例')) && (normalized.includes('url') || normalized.includes('链接') || normalized.includes('路径') || normalized.includes('地址'))) return true;
  return false;
}

function caseIdFromUrl(url: string): string {
  try {
    const parts = new URL(url).pathname.replace(/\/$/, '').split('/').filter(Boolean);
    return decodeURIComponent(parts.at(-1) || '');
  } catch {
    return '';
  }
}

/** Report artefacts the backend dispatches on by type. */
const REPORT_FILE_RE = /(?:summary_report\.xml|xytest\.log|event\.log|[\w.-]+\.(?:rpt|xml|ini|xlsx|xlsm|html))$/i;

/**
 * One input has to tell two destinations apart, so decide by what the URL points at.
 *
 * A report URL is a concrete artefact and must keep its path and extension exactly — the
 * case-URL normaliser appends a trailing slash, which would corrupt
 * `.../summary_report.xml` into a directory.
 */
function looksLikeReportFile(value: string): boolean {
  try {
    return REPORT_FILE_RE.test(new URL(value.trim()).pathname);
  } catch {
    return false;
  }
}

function normalizeCaseUrl(value: string): string {
  const text = value.trim();
  if (!text) return '';
  try {
    const url = new URL(text);
    if (!/^https?:$/.test(url.protocol)) return '';
    url.search = '';
    url.hash = '';
    if (!url.pathname.endsWith('/')) url.pathname += '/';
    return url.toString();
  } catch {
    return '';
  }
}

function toLocalInput(value: string): string {
  const text = String(value || '').trim().replace(' ', 'T');
  const match = text.match(/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})(?::(\d{2}))?(?:\.(\d{1,3}))?/);
  if (!match) return text;
  return `${match[1]}:${match[2] || '00'}${match[3] ? `.${match[3].padEnd(3, '0')}` : ''}`;
}

function apiTime(value: string): string {
  return String(value || '').trim().replace('T', ' ');
}

function statusLabel(item: ImportedCase): string {
  if (item.phase === 'analyzing') return '分析中';
  if (item.phase === 'error') return '解析失败';
  if (item.analysis?.status === 'passed') return '通过';
  if (item.analysis?.status === 'failed') return '失败';
  return item.phase === 'done' ? '待确认' : '待分析';
}

function statusClass(item: ImportedCase): string {
  if (item.phase === 'analyzing') return 'analyzing';
  if (item.phase === 'error') return 'error';
  return item.analysis?.status || 'pending';
}

function statusIcon(item: ImportedCase) {
  if (item.phase === 'analyzing') return <LoaderCircle className="spin" size={16}/>;
  if (item.phase === 'error' || item.analysis?.status === 'failed') return <XCircle size={16}/>;
  if (item.analysis?.status === 'passed') return <CheckCircle2 size={16}/>;
  return <FileSearch size={16}/>;
}

function linkLabel(key: string): string {
  const map: Record<string, string> = {
    pytest_xml: 'pytest XML',
    'xytest.log': 'xytest.log',
    'event.log': 'event.log',
    test_html: 'pytest HTML',
    'summary.ini': 'summary.ini',
    'details_report.html': 'details_report.html',
    'failures_report.html': 'failures_report.html',
    'summary_report.html': 'summary_report.html',
    '详细日志链接.html': '详细日志链接.html',
    full_logs: 'full_logs',
  };
  return map[key] || key;
}

function relativePathFromLink(baseUrl: string, linkUrl: string): string {
  try {
    const base = new URL(baseUrl);
    const link = new URL(linkUrl);
    if (base.origin !== link.origin || !link.pathname.startsWith(base.pathname)) return '';
    return decodeURIComponent(link.pathname.slice(base.pathname.length));
  } catch {
    return '';
  }
}

function compactLogTime(value: string): string {
  const match = value.match(/(\d{2}:\d{2}:\d{2}(?:\.\d+)?)/);
  return match?.[1] || value;
}

function simpleLineSeverity(line: string): ContentSeverity {
  return detectSeverity('', line);
}

function ParsedLogRow({ entry }: { entry: LogEntry }) {
  return <div className={`log-row atlog-reused-log-row severity-${entry.severity}`} title={entry.raw}>
    <span className="log-time">{compactLogTime(entry.timestamp)}</span>
    <span className="component-badge compact">{entry.component || '—'}</span>
    <span className={`level-badge level-${String(entry.level || '').toLowerCase()}`}>{entry.level || '—'}</span>
    <span className="log-summary">{entry.message || entry.raw}</span>
  </div>;
}

function LogTextViewer({ text, fileName, formatRules, categories = [] }: { text: string; fileName: string; formatRules: readonly LogFormatParserRuleConfig[]; categories?: string[] }) {
  const rows = useMemo(() => text.split(/\r?\n/).filter((line) => line.length > 0).map((line, index) => {
    const parsed = parseLogLineByCategories(line, {
      sourceFileId: `atlog-${fileName}`,
      sourceFileName: fileName,
      parserSourceFileName: fileName,
      lineNumber: index + 1,
      idPrefix: `atlog-${index}`,
    }, categories, formatRules);
    return { line, index, entry: parsed.entry };
  }), [text, fileName, categories.join(','), formatRules]);

  return <div className="atlog-log-text-viewer">
    {rows.map((row) => row.entry ? <ParsedLogRow entry={row.entry} key={row.entry.id}/> : <div className={`atlog-plain-log-row severity-${simpleLineSeverity(row.line)}`} key={row.index}>
      <span className="atlog-line-number">{row.index + 1}</span><code>{row.line}</code>
    </div>)}
    {!rows.length && <div className="atlog-empty-inline">没有可展示的文本内容。</div>}
  </div>;
}

function buildDiagnosisAssertionText(analysis: AtLogCaseAnalysis): string {
  const lines: string[] = [];
  const summary = analysis.assertion_summary || analysis.assertion || analysis.conclusion || '暂无明确断言';
  lines.push(`[ERROR] ${summary}`);
  if (analysis.assertion) lines.push(`[ERROR] 原始断言: ${analysis.assertion}`);
  if (analysis.failure_location) {
    lines.push(`[ERROR] 断言位置: ${analysis.failure_location.file}:${analysis.failure_location.line}${analysis.failure_location.function ? ` · ${analysis.failure_location.function}` : ''}`);
  }
  if (analysis.assertion_meta?.caller) lines.push(`[INFO] 检查函数: ${analysis.assertion_meta.caller}`);
  if (analysis.call_chain.length) lines.push(`[INFO] 调用链: ${analysis.call_chain.map((frame) => `${frame.file}:${frame.line}${frame.function ? `(${frame.function})` : ''}`).join(' → ')}`);
  return lines.join('\n');
}

function isExplicitErrorLine(line: string): boolean {
  return /(?:\bERROR\b|\bFATAL\b|\bCRITICAL\b|\bALARM\b|AssertionError|\bFAILED\b|<failure\b|\bFAILURE\b)/i.test(line);
}

function NavigableLogTextViewer({ text, fileName, formatRules, categories = [], errorRules, emptyText = '没有可展示的内容。' }: { text: string; fileName: string; formatRules: readonly LogFormatParserRuleConfig[]; categories?: string[]; errorRules?: readonly ErrorMatchRule[]; emptyText?: string }) {
  const rows = useMemo(() => text.split(/\r?\n/).filter((line) => line.length > 0).map((line, index) => {
    const parsed = parseLogLineByCategories(line, {
      sourceFileId: `atlog-nav-${fileName}`,
      sourceFileName: fileName,
      parserSourceFileName: fileName,
      lineNumber: index + 1,
      idPrefix: `atlog-nav-${index}`,
    }, categories, formatRules);
    const severity = parsed.entry?.severity || simpleLineSeverity(line);
    const ruleMatched = Boolean(errorRules?.some((rule) => matchesErrorRule(line, rule)));
    return { line, index, entry: parsed.entry, severity, isError: ruleMatched || (!errorRules?.length && (severity === 'error' || isExplicitErrorLine(line))) };
  }), [text, fileName, categories.join(','), formatRules, errorRules]);
  const errorIndexes = useMemo(() => rows.filter((row) => row.isError).map((row) => row.index), [rows]);
  const [errorCursor, setErrorCursor] = useState(0);
  const rowRefs = useRef(new Map<number, HTMLDivElement>());

  const focusError = (nextCursor: number) => {
    if (!errorIndexes.length) return;
    const normalized = (nextCursor + errorIndexes.length) % errorIndexes.length;
    setErrorCursor(normalized);
    window.setTimeout(() => rowRefs.current.get(errorIndexes[normalized])?.scrollIntoView({ block: 'center', behavior: 'smooth' }), 0);
  };

  useEffect(() => {
    setErrorCursor(0);
    if (!errorIndexes.length) return;
    const timer = window.setTimeout(() => rowRefs.current.get(errorIndexes[0])?.scrollIntoView({ block: 'center' }), 30);
    return () => window.clearTimeout(timer);
  }, [text, errorIndexes.join(',')]);

  const activeRow = errorIndexes[errorCursor];
  return <div className="atlog-navigable-viewer">
    <div className="atlog-pane-error-nav">
      <span>{errorIndexes.length ? `异常 ${errorCursor + 1} / ${errorIndexes.length}` : '未识别到异常行'}</span>
      <span className="spacer"/>
      <button type="button" onClick={() => focusError(errorCursor - 1)} disabled={!errorIndexes.length}><ArrowUp size={13}/> 上一异常</button>
      <button type="button" onClick={() => focusError(errorCursor + 1)} disabled={!errorIndexes.length}>下一异常 <ArrowDown size={13}/></button>
    </div>
    <div className="atlog-independent-log-scroll">
      {rows.map((row) => <div ref={(node) => { if (node) rowRefs.current.set(row.index, node); else rowRefs.current.delete(row.index); }} className={`atlog-navigable-row ${row.isError ? 'is-error' : row.severity === 'warning' ? 'is-warning' : ''} ${activeRow === row.index ? 'is-current' : ''}`} key={row.index}>
        {row.entry ? <ParsedLogRow entry={row.entry}/> : <div className={`atlog-plain-log-row severity-${row.severity}`}><span className="atlog-line-number">{row.index + 1}</span><code>{row.line}</code></div>}
      </div>)}
      {!rows.length && <div className="atlog-empty-inline">{emptyText}</div>}
    </div>
  </div>;
}

function readableAtLogDocument(text: string, key: string): string {
  if (!/html/i.test(key) && !/^\s*</.test(text)) return text;
  if (typeof window === 'undefined') return text.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
  try {
    const doc = new DOMParser().parseFromString(text, 'text/html');
    doc.querySelectorAll('script,style,noscript').forEach((node) => node.remove());
    return (doc.body?.innerText || doc.documentElement?.textContent || '').replace(/\n{3,}/g, '\n\n').trim();
  } catch { return text; }
}

function DiagnosticReportPane({ analysis, formatRules, errorRules }: { analysis: AtLogCaseAnalysis; formatRules: readonly LogFormatParserRuleConfig[]; errorRules?: readonly ErrorMatchRule[] }) {
  const reportLinks = useMemo(() => Object.entries(analysis.links).filter(([key]) => ['pytest_xml', 'test_html', 'failures_report.html', 'details_report.html'].includes(key)), [analysis.links]);
  const [activeKey, setActiveKey] = useState(() => reportLinks.find(([key]) => key === 'test_html')?.[0] || reportLinks.find(([key]) => key === 'pytest_xml')?.[0] || reportLinks[0]?.[0] || '');
  const [content, setContent] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const load = async (key: string) => {
    setActiveKey(key); setLoading(true); setError(''); setContent('');
    const url = analysis.links[key];
    const relative = url ? relativePathFromLink(analysis.base_url, url) : '';
    if (!relative) { setLoading(false); return; }
    try {
      const result = await readAtLogCaseFile({ url: analysis.base_url, relative_path: relative, max_bytes: 5 * 1024 * 1024 });
      setContent(readableAtLogDocument(result.content, key));
    } catch (exc) { setError(exc instanceof Error ? exc.message : String(exc)); }
    finally { setLoading(false); }
  };

  useEffect(() => { if (activeKey) void load(activeKey); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, []);
  return <div className="atlog-diagnostic-pane">
    <div className="atlog-pane-tabs"><strong>pytest 报告</strong>{reportLinks.map(([key]) => <button type="button" className={activeKey === key ? 'active' : ''} onClick={() => void load(key)} key={key}>{linkLabel(key)}</button>)}<span className="spacer"/>{activeKey && analysis.links[activeKey] && <a href={analysis.links[activeKey]} target="_blank" rel="noreferrer" title="打开原始报告"><ExternalLink size={14}/></a>}</div>
    {loading && <div className="atlog-empty-inline"><LoaderCircle className="spin" size={16}/> 正在读取 pytest 报告…</div>}
    {error && <div className="atlog-inline-error"><AlertTriangle size={15}/>{error}</div>}
    {!loading && !error && <NavigableLogTextViewer text={content} fileName={activeKey || 'pytest-report'} formatRules={formatRules} errorRules={errorRules} emptyText="没有可展示的 pytest 报告内容。"/>}
  </div>;
}

function DiagnosticWorkspace({ analysis, formatRules }: { analysis: AtLogCaseAnalysis; formatRules: readonly LogFormatParserRuleConfig[] }) {
  const failureScene = useMemo(() => {
    const blocks = [buildDiagnosisAssertionText(analysis)];
    if (analysis.xytest_errors.length) {
      blocks.push('[INFO] —— xytest 失败现场 ——');
      blocks.push(analysis.xytest_errors.map((row) => row.raw).join('\n'));
    }
    return blocks.filter(Boolean).join('\n');
  }, [analysis]);
  return <div className="atlog-diagnostic-workspace">
    <section className="atlog-diagnostic-pane">
      <div className="atlog-pane-tabs"><strong>失败现场</strong><span className="atlog-pane-hint">断言结论 + xytest 上下文</span></div>
      <div className="atlog-diagnosis-inline-facts">
        {analysis.reason_category && <span><b>失败原因</b>{analysis.reason_category}</span>}
        {analysis.failure_time && <span><b>失败时间</b>{analysis.failure_time}</span>}
        {analysis.assertion_meta?.caller && <span><b>检查函数</b>{analysis.assertion_meta.caller}</span>}
      </div>
      <NavigableLogTextViewer text={failureScene} fileName="failure-scene.log" formatRules={formatRules}/>
    </section>
    <DiagnosticReportPane analysis={analysis} formatRules={formatRules}/>
  </div>;
}

function rowSeverity(row: AtLogCaseLogRow): ContentSeverity {
  const level = String(row.level || '').toUpperCase();
  if (/ERROR|FATAL|CRITICAL|ALARM|FAIL/.test(level) || isExplicitErrorLine(row.raw)) return 'error';
  if (/WARN/.test(level)) return 'warning';
  return simpleLineSeverity(row.raw);
}

const ATLOG_PATH_MARKERS = new Set(['full_logs', 'log', 'debug', 'elog', 'executor', 'exec', 'run', 'operation']);

function isAtLogHierarchyName(value: string | undefined): boolean {
  const text = String(value || '').trim();
  if (!text || ATLOG_PATH_MARKERS.has(text.toLowerCase())) return false;
  if (/^(?:\d{1,3}\.){3}\d{1,3}$/.test(text)) return false;
  return true;
}

function isAtLogTargetKeyValid(value: string): boolean {
  const target = splitTargetKey(value);
  return isAtLogHierarchyName(target.subsystem) && isAtLogHierarchyName(target.fm);
}

function atLogHierarchyCatalog(result?: AtLogCaseLogResult): GlobalLogSubsystem[] {
  return (result?.log_catalog || [])
    .filter((group) => isAtLogHierarchyName(group.subsystem))
    .map((group) => ({ ...group, modules: (group.modules || []).filter((module) => isAtLogHierarchyName(module)) }))
    .filter((group) => group.modules.length > 0)
    .map((group, groupIndex) => ({
      id: 900000 + groupIndex,
      name: group.subsystem,
      display_name: '',
      effective_name: group.subsystem,
      enabled: true,
      sort_order: groupIndex,
      description: 'ATLog full_logs 自动发现',
      fm_count: group.modules.length,
      fms: group.modules.map((module, moduleIndex) => ({
        id: 900000000 + groupIndex * 1000 + moduleIndex,
        subsystem: 900000 + groupIndex,
        name: module,
        kind: 'normal' as const,
        display_name: '',
        effective_name: module,
        enabled: true,
        sort_order: moduleIndex,
        description: 'ATLog full_logs 自动发现',
        query_priority: 100,
        target_module_ids: [],
        target_module_names: [],
        event_component: false,
        event_config_files: [],
        event_display_codes: [],
        event_code_count: 0,
        matched_count: 0,
      })),
    }));
}

function atLogAiTargetKeys(targets?: Array<{ subsystem: string; module: string }>): string[] {
  return (targets || []).flatMap((item) => {
    const subsystem = String(item?.subsystem || '').trim();
    const module = String(item?.module || '').trim();
    return isAtLogHierarchyName(subsystem) && isAtLogHierarchyName(module) ? [targetKey(subsystem, module, 'normal')] : [];
  });
}

function atLogAiTargetSignature(targets?: Array<{ subsystem: string; module: string }>): string {
  return atLogAiTargetKeys(targets).sort().join('\u0001');
}

function atLogFallbackEntry(row: AtLogCaseLogRow, index: number): LogEntry {
  const timestamp = row.time || '';
  const component = row.component || row.source_path.split('/').at(-1)?.replace(/\.(?:log|txt|out)$/i, '') || 'ATLog';
  return {
    id: `atlog-fallback-${index}`,
    sourceFile: row.source_path || 'atlog.log',
    sourceFileId: `atlog-${row.source_path || 'log'}`,
    lineNumber: row.line_number || index + 1,
    timestamp,
    timestampNs: timestampToNs(timestamp),
    level: row.level || '',
    component,
    processId: component,
    threadId: 'main',
    source: { raw: row.source || '', fileName: row.source_path || 'atlog.log', lineNumber: row.line_number || index + 1 },
    mode: '',
    rpc: { traceId: '', spanId: '', fatherSpanId: '', raw: '', isRpc: false },
    message: row.message || row.raw,
    marker: 'none',
    severity: rowSeverity(row),
    summary: row.message || row.raw,
    raw: row.raw,
    logCategory: row.source_kind === 'event' ? 'run' : 'debug',
    logSubsystem: '',
    logModule: component,
    remoteSourcePath: row.source_path,
  };
}

function parsedAtLogEntries(rows: AtLogCaseLogRow[], formatRules: readonly LogFormatParserRuleConfig[]): Array<{ row: AtLogCaseLogRow; entry: LogEntry; index: number }> {
  return rows.map((row, index) => {
    const parsed = parseLogLineByCategories(row.raw, {
      sourceFileId: `atlog-unified-${row.source_path}`,
      sourceFileName: row.source_path || 'atlog.log',
      parserSourceFileName: row.source_path || 'atlog.log',
      lineNumber: row.line_number || index + 1,
      idPrefix: `atlog-unified-${index}`,
    }, row.source_kind === 'event' ? ['run'] : ['debug'], formatRules).entry;
    const entry = parsed ? {
      ...parsed,
      component: row.component || parsed.component,
      logCategory: row.source_kind === 'event' ? 'run' : 'debug',
      remoteSourcePath: row.source_path,
    } : atLogFallbackEntry(row, index);
    return { row, entry, index };
  });
}

function entryRange(entries: LogEntry[]): TimeRangeFilter | undefined {
  const timed = entries.filter((entry) => entry.timestampNs !== undefined);
  if (!timed.length) return undefined;
  const startNs = timed.reduce((value, entry) => entry.timestampNs! < value ? entry.timestampNs! : value, timed[0].timestampNs!);
  const endNs = timed.reduce((value, entry) => entry.timestampNs! > value ? entry.timestampNs! : value, timed[0].timestampNs!);
  return { startNs, endNs: endNs > startNs ? endNs : startNs + 1n };
}

function nearestTimelineEntryIndex(entries: Array<{ entry: LogEntry; index: number }>, timestampNs: bigint, component?: string): number {
  let winner = -1;
  let distance: bigint | undefined;
  entries.forEach(({ entry, index }) => {
    if (entry.timestampNs === undefined) return;
    if (component && entry.component !== component) return;
    const nextDistance = entry.timestampNs > timestampNs ? entry.timestampNs - timestampNs : timestampNs - entry.timestampNs;
    if (distance === undefined || nextDistance < distance) {
      distance = nextDistance;
      winner = index;
    }
  });
  return winner;
}

function environmentLogLocatorHref(analysis: AtLogCaseAnalysis, startTime: string, endTime: string): string {
  const environmentId = analysis.environment?.environment_id;
  if (!environmentId) return '';
  const params = new URLSearchParams();
  params.set('page', 'logs');
  params.set('env', String(environmentId));
  params.set('st', apiTime(startTime));
  params.set('et', apiTime(endTime));
  params.set('type', 'run');
  params.set('raw', '0');
  return `?${params.toString()}`;
}

function environmentChoices(analysis: AtLogCaseAnalysis): Array<{ key: string; label: string; host: string }> {
  const env = analysis.environment;
  if (!env) return [];
  const rows: Array<{ key: string; label: string; host: string }> = [];
  if (env.upper?.environment_host) rows.push({ key: 'upper', label: `SCH · ${env.upper.environment_host}`, host: env.upper.environment_host });
  if (env.dhh?.environment_host) rows.push({ key: 'dhh', label: `DHH · ${env.dhh.environment_host}`, host: env.dhh.environment_host });
  (env.lowers || []).forEach((node, index) => {
    if (node.environment_host) rows.push({ key: `lower-${index}`, label: `${node.label || '下位机'} · ${node.environment_host}`, host: node.environment_host });
  });
  return rows;
}

const QUICK_RANGES = [{ label: '3分钟', seconds: 180 }, { label: '10分钟', seconds: 600 }, { label: '1小时', seconds: 3600 }, { label: '3小时', seconds: 10800 }, { label: '12小时', seconds: 43200 }, { label: '1天', seconds: 86400 }, { label: '2天', seconds: 172800 }, { label: '3天', seconds: 259200 }] as const;

const ATLOG_SOURCE_TYPES = [
  { value: 'pytest', label: 'pytest HTML / 断言', hint: '自动化用例专属，不进入时间线' },
  { value: 'xytest', label: 'xytest.log', hint: '自动化用例专属，不进入时间线' },
  { value: 'event', label: 'event.log', hint: '运行事件日志' },
  { value: 'debug', label: '调试日志', hint: 'full_logs/log/debug' },
  { value: 'executor', label: '执行器日志', hint: 'full_logs/log/debug/elog/<IP>' },
] as const;

const CASE_LOG_SOURCE_TYPES = ATLOG_SOURCE_TYPES.filter((item) => ['event', 'debug', 'executor'].includes(item.value));

function AtLogSourceTypeSelect({ selected, onChange }: { selected: Set<string>; onChange: (next: Set<string>) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => { if (!ref.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener('pointerdown', close, true);
    return () => document.removeEventListener('pointerdown', close, true);
  }, [open]);
  const labels = ATLOG_SOURCE_TYPES.filter((item) => selected.has(item.value)).map((item) => item.label);
  return <div className="remote-logtype-select atlog-source-type-select" ref={ref}>
    <button type="button" className={`remote-logtype-trigger ${open ? 'open' : ''}`} onClick={() => setOpen((value) => !value)}>
      <span className="remote-control-label">类型</span><strong>{labels.length <= 2 ? labels.join('、') || '未选择' : `${labels.length} 类日志`}</strong><ChevronDown size={13}/>
    </button>
    {open && <div className="remote-logtype-menu">
      {ATLOG_SOURCE_TYPES.map((item, index) => {
        const checked = selected.has(item.value);
        return <button type="button" key={item.value} className={checked ? 'selected' : ''} onClick={() => {
          const next = new Set(selected); if (checked) next.delete(item.value); else next.add(item.value); onChange(next);
        }}>
          <span className="remote-option-check">{checked && <Check size={12}/>}</span><span className={`logtype-tone tone-${index % 5}`}/><span><strong>{item.label}</strong><small>{item.hint}</small></span>
        </button>;
      })}
    </div>}
  </div>;
}

function PytestLogPane({ analysis, formatRules, errorRules, onlyErrors = false }: { analysis: AtLogCaseAnalysis; formatRules: readonly LogFormatParserRuleConfig[]; errorRules: readonly ErrorMatchRule[]; onlyErrors?: boolean }) {
  const assertionText = useMemo(() => buildDiagnosisAssertionText(analysis), [analysis]);
  const [reportText, setReportText] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    const key = analysis.links.test_html ? 'test_html' : analysis.links.pytest_xml ? 'pytest_xml' : '';
    const url = key ? analysis.links[key] : '';
    const relative = url ? relativePathFromLink(analysis.base_url, url) : '';
    if (!relative) { setReportText(analysis.report_excerpt || analysis.failure_text || ''); return; }
    let active = true;
    setLoading(true); setError('');
    void readAtLogCaseFile({ url: analysis.base_url, relative_path: relative, max_bytes: 5 * 1024 * 1024 })
      .then((value) => { if (active) setReportText(readableAtLogDocument(value.content, key)); })
      .catch((exc) => { if (active) setError(exc instanceof Error ? exc.message : String(exc)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [analysis.base_url, analysis.links.test_html, analysis.links.pytest_xml, analysis.report_excerpt, analysis.failure_text]);
  const displayedReportText = onlyErrors ? filterAtLogErrorLines(reportText, errorRules) : reportText;
  return <div className="atlog-pytest-source-pane">
    <section className="atlog-diagnostic-pane atlog-pytest-assertion-pane">
      <div className="atlog-pane-tabs"><strong>pytest 断言</strong><span className="atlog-pane-hint">已有断言解析</span></div>
      <NavigableLogTextViewer text={assertionText} fileName="pytest-assertion.log" formatRules={formatRules} errorRules={errorRules} emptyText="没有 pytest 断言信息。"/>
    </section>
    <section className="atlog-diagnostic-pane">
      <div className="atlog-pane-tabs"><strong>pytest HTML</strong><span className="atlog-pane-hint">已提取为可读文本</span></div>
      {loading && <div className="atlog-empty-inline"><LoaderCircle className="spin" size={15}/> 正在读取 pytest HTML…</div>}
      {error && <div className="atlog-inline-error"><AlertTriangle size={14}/>{error}</div>}
      {!loading && !error && <NavigableLogTextViewer text={displayedReportText} fileName="pytest.html" formatRules={formatRules} errorRules={errorRules} emptyText="没有可展示的 pytest HTML 内容。"/>}
    </section>
  </div>;
}

function XytestLogPane({ analysis, formatRules, errorRules, startTime, endTime, onlyErrors = false }: { analysis: AtLogCaseAnalysis; formatRules: readonly LogFormatParserRuleConfig[]; errorRules: readonly ErrorMatchRule[]; startTime: string; endTime: string; onlyErrors?: boolean }) {
  const [content, setContent] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    const url = analysis.links['xytest.log'];
    const relative = url ? relativePathFromLink(analysis.base_url, url) : '';
    if (!relative) { setContent(analysis.xytest_errors.map((row) => row.raw).join('\n')); return; }
    let active = true;
    setLoading(true); setError('');
    void readAtLogCaseFile({ url: analysis.base_url, relative_path: relative, max_bytes: 6 * 1024 * 1024 })
      .then((value) => { if (active) setContent(value.content); })
      .catch((exc) => { if (active) setError(exc instanceof Error ? exc.message : String(exc)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [analysis.base_url, analysis.links['xytest.log']]);
  const filteredContent = useMemo(() => {
    const startNs = timestampToNs(apiTime(startTime));
    const endNs = timestampToNs(apiTime(endTime));
    if (startNs === undefined || endNs === undefined) return content;
    return content.split(/\r?\n/).filter((line) => {
      const match = line.match(/(20\d{2}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)/);
      if (!match) return true;
      const value = timestampToNs(match[1].replace('T', ' '));
      return value === undefined || (value >= startNs && value <= endNs);
    }).join('\n');
  }, [content, startTime, endTime]);
  if (loading) return <div className="atlog-empty-inline"><LoaderCircle className="spin" size={15}/> 正在读取 xytest.log…</div>;
  if (error) return <div className="atlog-inline-error"><AlertTriangle size={14}/>{error}</div>;
  const displayedContent = onlyErrors ? filterAtLogErrorLines(filteredContent, errorRules) : filteredContent;
  return <NavigableLogTextViewer text={displayedContent} fileName="xytest.log" formatRules={formatRules} categories={['run']} errorRules={errorRules} emptyText="xytest.log 没有可展示内容。"/>;
}

interface WorkstationEvidence {
  result: AtLogCaseLogResult;
  sourceKind: string;
  targets: Array<{ subsystem: string; module: string }>;
  signal?: AbortSignal;
  complete: (result: unknown) => void;
}

function UnifiedCaseLogWorkspace({ onSelectedEntryChange, aiEvidence, analysis, formatRules, TimelineComponent, errorRules, aiSelectedTargets, aiRefreshToken, onRowsChange, onEntriesChange, onOpenCaseAnalysis, onOpenEnvironmentLogLocator, onQueryStateChange }: { onSelectedEntryChange?: (entry: AtLogCaseLogRow | undefined) => void; aiEvidence?: WorkstationEvidence; analysis: AtLogCaseAnalysis; formatRules: readonly LogFormatParserRuleConfig[]; TimelineComponent?: TimelineRenderer; errorRules: readonly ErrorMatchRule[]; aiSelectedTargets?: Array<{ subsystem: string; module: string }>; aiRefreshToken?: string; onRowsChange?: (rows: AtLogCaseLogRow[]) => void; onEntriesChange?: (entries: LogEntry[]) => void; onOpenCaseAnalysis?: () => void; onOpenEnvironmentLogLocator?: (environment: EnvironmentSummary, request?: LogWindowRequest) => void; onQueryStateChange?: (request: LogWindowRequest) => void }) {
  const cached = ATLOG_LOG_WORKSPACE_CACHE.get(analysis.base_url) || persistedWorkspaceCache(analysis);
  const [startTime, setStartTime] = useState(() => toLocalInput(analysis.start_time || analysis.event_start_time) || cached?.startTime || '');
  const [endTime, setEndTime] = useState(() => toLocalInput(analysis.end_time || analysis.event_end_time) || cached?.endTime || '');
  const [keyword, setKeyword] = useState(() => cached?.keyword || '');
  const restoredAiTargetKeys = atLogAiTargetKeys(aiSelectedTargets);
  const [selectedTargets, setSelectedTargets] = useState<Set<string>>(() => new Set((cached?.selectedTargets?.length ? cached.selectedTargets : restoredAiTargetKeys).filter(isAtLogTargetKeyValid)));
  const [onlyErrors, setOnlyErrors] = useState(() => cached?.onlyErrors || false);
  const [descending, setDescending] = useState(() => cached?.descending || false);
  const [result, setResult] = useState<AtLogCaseLogResult | undefined>(() => cached?.result);
  const [evidenceChain, setEvidenceChain] = useState<WorkstationEvidence[]>([]);
  const [loading, setLoading] = useState(false);
  const [stoppingSearch, setStoppingSearch] = useState(false);
  const [activeSearchOperationId, setActiveSearchOperationId] = useState('');
  const activeSearchOperationRef = useRef('');
  const activeSearchAbortRef = useRef<AbortController>();
  const [searchProgress, setSearchProgress] = useState<{ percent: number; message: string; currentFile: string; matchedFiles: number; readFiles: number; totalFiles: number }>({ percent: 0, message: '', currentFile: '', matchedFiles: 0, readFiles: 0, totalFiles: 0 });
  const [error, setError] = useState('');
  const [activeIndex, setActiveIndex] = useState(-1);
  const [errorCursor, setErrorCursor] = useState(0);
  const [rootCauseOpen, setRootCauseOpen] = useState(false);
  const [timelineExpanded, setTimelineExpanded] = useState(() => cached?.timelineExpanded ?? true);
  const [timelineRange, setTimelineRange] = useState<TimeRangeFilter>();
  const [environmentKey, setEnvironmentKey] = useState(() => cached?.environmentKey === 'atlog' || cached?.environmentKey?.startsWith('upper:') ? cached.environmentKey : 'atlog');
  const [resourceEnvironments, setResourceEnvironments] = useState<EnvironmentSummary[]>([]);
  const [environmentMenuOpen, setEnvironmentMenuOpen] = useState(false);
  const [activeQuickRange, setActiveQuickRange] = useState('');
  const [selectedSources, setSelectedSources] = useState<Set<string>>(() => new Set((cached?.selectedSources || ['event']).filter((value) => ['event', 'debug', 'executor'].includes(value))));
  const rowRefs = useRef(new Map<number, HTMLDivElement>());
  const workspaceRef = useRef<HTMLDivElement>(null);
  const toolbarRef = useRef<HTMLDivElement>(null);
  const initialRef = useRef(false);
  const aiTargetSignature = atLogAiTargetSignature(aiSelectedTargets);
  const appliedAiTargetSignatureRef = useRef(aiTargetSignature);
  const appliedAiRefreshTokenRef = useRef(aiRefreshToken || '');

  useEffect(() => {
    if (!environmentMenuOpen) return;
    const close = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!(target instanceof Node)) return;
      const menu = document.querySelector('.atlog-case-environment-select');
      if (menu && !menu.contains(target)) setEnvironmentMenuOpen(false);
    };
    document.addEventListener('pointerdown', close, true);
    return () => document.removeEventListener('pointerdown', close, true);
  }, [environmentMenuOpen]);

  useEffect(() => {
    let active = true;
    void listEnvironments().then((items) => { if (active) setResourceEnvironments(items); }).catch(() => undefined);
    return () => { active = false; };
  }, []);

  useEffect(() => {
    const workspace = workspaceRef.current;
    const toolbar = toolbarRef.current;
    if (!workspace || !toolbar) return;
    const caseItem = workspace.closest('.atlog-case-item') as HTMLElement | null;
    const summary = caseItem?.querySelector(':scope > .atlog-case-summary-wrap') as HTMLElement | null;
    const detail = workspace.closest('.atlog-case-unified-detail') as HTMLElement | null;
    const detailTabs = detail?.querySelector(':scope > .atlog-case-detail-tabs') as HTMLElement | null;
    const updateStickyOffsets = () => {
      const summaryHeight = Math.ceil(summary?.getBoundingClientRect().height || 54);
      const detailTabsHeight = Math.ceil(detailTabs?.getBoundingClientRect().height || 44);
      const toolbarHeight = Math.ceil(toolbar.getBoundingClientRect().height || 42);
      const targets = [caseItem, detail, workspace].filter(Boolean) as HTMLElement[];
      for (const target of targets) {
        target.style.setProperty('--atlog-case-summary-sticky-height', `${summaryHeight}px`);
        target.style.setProperty('--atlog-detail-tabs-sticky-height', `${detailTabsHeight}px`);
        target.style.setProperty('--atlog-toolbar-sticky-height', `${toolbarHeight}px`);
      }
    };
    updateStickyOffsets();
    const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(updateStickyOffsets) : undefined;
    if (summary) observer?.observe(summary);
    if (detailTabs) observer?.observe(detailTabs);
    observer?.observe(toolbar);
    window.addEventListener('resize', updateStickyOffsets);
    return () => { observer?.disconnect(); window.removeEventListener('resize', updateStickyOffsets); };
  }, []);

  const runQuery = async (targetsOverride?: Set<string>, sourcesOverride?: Set<string>) => {
    const activeTargets = targetsOverride ?? selectedTargets;
    const activeSources = sourcesOverride ?? selectedSources;
    if (environmentKey.startsWith('upper:')) {
      const upperEnvironment = currentResourceEnvironment;
      if (!upperEnvironment) { setError('当前用例未找到对应的环境资源。'); return; }
      const fmTargets = Array.from(activeTargets)
        .map(splitTargetKey)
        .filter((item) => item.subsystem || item.fm)
        .map((item) => ({ subsystem: item.subsystem, fm: item.fm, kind: item.kind }));
      const request: LogWindowRequest = {
        start_time: apiTime(startTime),
        end_time: apiTime(endTime),
        source_categories: Array.from(activeSources).filter((value) => ['event', 'debug', 'executor'].includes(value)),
        subsystems: [...new Set(fmTargets.map((item) => item.subsystem).filter(Boolean))],
        fms: [...new Set(fmTargets.map((item) => item.fm).filter(Boolean))],
        fm_targets: fmTargets,
        keyword: keyword.trim(),
      };
      onQueryStateChange?.(request);
      setError('');
      onOpenEnvironmentLogLocator?.(upperEnvironment, request);
      return;
    }
    activeSearchAbortRef.current?.abort();
    const searchController = new AbortController();
    activeSearchAbortRef.current = searchController;
    setLoading(true); setError('');
    setStoppingSearch(false);
    setActiveSearchOperationId('');
    activeSearchOperationRef.current = '';
    try {
      const runtimeSources = Array.from(activeSources).filter((item) => ['event', 'debug', 'executor'].includes(item));
      const allowFullLogs = activeSources.has('debug') || activeSources.has('executor');
      const targets = allowFullLogs
        ? Array.from(activeTargets)
          .map(splitTargetKey)
          .filter((item) => item.subsystem || item.fm)
          .map((item) => ({ subsystem: item.subsystem, module: item.fm }))
        : [];
      const next = await queryAtLogCaseLogs({
        url: analysis.base_url,
        start_time: apiTime(startTime),
        end_time: apiTime(endTime),
        targets,
        source_categories: runtimeSources,
        anomaly_rules: onlyErrors ? currentAtLogAiAnomalyRules(errorRules) : [],
        keyword,
        max_lines: 16000,
        signal: searchController.signal,
        onStarted: (operationId) => { activeSearchOperationRef.current = operationId; setActiveSearchOperationId(operationId); },
        onProgress: (progress) => setSearchProgress({ percent: Number(progress.percent || 0), message: String(progress.message || ''), currentFile: String(progress.current_file || ''), matchedFiles: Number(progress.matched_files || 0), readFiles: Number(progress.read_files || 0), totalFiles: Number(progress.total_files || 0) }),
        workspace_state: {
          startTime, endTime, keyword, selectedTargets: Array.from(activeTargets), onlyErrors, descending,
          showRootCause: false, timelineExpanded, environmentKey, selectedSources: Array.from(activeSources),
        },
      });
      searchController.signal.throwIfAborted();
      setResult(next); setActiveIndex(-1); setErrorCursor(0); setTimelineRange(undefined);
    } catch (exc) {
      const message = exc instanceof Error ? exc.message : String(exc);
      const aborted = exc instanceof DOMException && exc.name === 'AbortError';
      if (!aborted && !/日志检索已停止/.test(message)) setError(message);
    }
    finally {
      if (activeSearchAbortRef.current === searchController) activeSearchAbortRef.current = undefined;
      setLoading(false);
      setStoppingSearch(false);
      setActiveSearchOperationId('');
      activeSearchOperationRef.current = '';
      window.setTimeout(() => setSearchProgress({ percent: 0, message: '', currentFile: '', matchedFiles: 0, readFiles: 0, totalFiles: 0 }), 1200);
    }
  };

  const stopQuery = () => {
    const operationId = activeSearchOperationRef.current;
    if (!operationId || stoppingSearch) return;
    setStoppingSearch(true);
    // Same stop contract as the log-positioning task: mark the shared backend
    // operation cancelled, abort client polling immediately, and transition the
    // visible task state to cancelled without waiting for another poll cycle.
    void cancelAtLogCaseLogs(operationId).catch((exc) => {
      if (!(exc instanceof DOMException && exc.name === 'AbortError')) setError(exc instanceof Error ? exc.message : String(exc));
    });
    activeSearchAbortRef.current?.abort();
    setSearchProgress((current) => ({ ...current, percent: 100, message: '任务已停止', currentFile: '' }));
  };

  useEffect(() => () => {
    const operationId = activeSearchOperationRef.current;
    if (operationId) void cancelAtLogCaseLogs(operationId).catch(() => undefined);
    activeSearchAbortRef.current?.abort();
  }, []);

  useEffect(() => {
    ATLOG_LOG_WORKSPACE_CACHE.set(analysis.base_url, {
      startTime, endTime, keyword, selectedTargets: Array.from(selectedTargets), onlyErrors, descending, result,
      showRootCause: false, timelineExpanded, environmentKey, selectedSources: Array.from(selectedSources),
    });
  }, [analysis.base_url, startTime, endTime, keyword, selectedTargets, onlyErrors, descending, result, timelineExpanded, environmentKey, selectedSources]);

  useEffect(() => {
    if (!aiTargetSignature) {
      appliedAiTargetSignatureRef.current = '';
      return;
    }
    if (appliedAiTargetSignatureRef.current === aiTargetSignature) return;
    const next = new Set(atLogAiTargetKeys(aiSelectedTargets));
    appliedAiTargetSignatureRef.current = aiTargetSignature;
    if (!next.size) return;
    setSelectedTargets(next);
  }, [aiSelectedTargets, aiTargetSignature]);

  useEffect(() => {
    const refreshToken = String(aiRefreshToken || '');
    if (!refreshToken || appliedAiRefreshTokenRef.current === refreshToken) return;
    appliedAiRefreshTokenRef.current = refreshToken;
    const aiTargets = new Set(atLogAiTargetKeys(aiSelectedTargets));
    const nextTargets = aiTargets.size ? aiTargets : selectedTargets;
    const nextSources = new Set(selectedSources);
    if (aiTargets.size) {
      appliedAiTargetSignatureRef.current = aiTargetSignature;
      setSelectedTargets(aiTargets);
      // AI component resolution points at debug modules. Include debug in the
      // refreshed evidence workspace so the newly diagnosed component's real
      // log rows are visible immediately instead of leaving the old evidence set.
      nextSources.add('debug');
      setSelectedSources(nextSources);
    }
    // Every completed AI diagnosis represents a new evidence snapshot. Refresh
    // the workspace even when the resolved module set did not change, so the
    // evidence rows and the case editor never keep the previous diagnosis data.
    void runQuery(nextTargets, nextSources);
    /* eslint-disable-next-line react-hooks/exhaustive-deps */
  }, [aiRefreshToken]);

  useEffect(() => {
    if (initialRef.current) return;
    initialRef.current = true;
    const cachedEventQuery = cached?.result?.event_query;
    const needsEventRefresh = selectedSources.has('event') && (
      !cached?.result || !cachedEventQuery?.requested || cachedEventQuery?.found === false
    );
    if (!aiEvidence && (!cached?.result || needsEventRefresh)) void runQuery(new Set());
    /* eslint-disable-next-line react-hooks/exhaustive-deps */
  }, []);

  useEffect(() => {
    const fmTargets = Array.from(selectedTargets).map((value) => {
      const target = splitTargetKey(value);
      return { subsystem: target.subsystem, fm: target.fm, kind: target.kind };
    }).filter((item) => item.subsystem && item.fm);
    onQueryStateChange?.({
      start_time: apiTime(startTime),
      end_time: apiTime(endTime),
      source_categories: Array.from(selectedSources).filter((value) => ['event', 'debug', 'executor'].includes(value)),
      subsystems: [...new Set(fmTargets.map((item) => item.subsystem))],
      fms: [...new Set(fmTargets.map((item) => item.fm))],
      fm_targets: fmTargets,
      keyword: keyword.trim(),
    });
  }, [startTime, endTime, keyword, selectedSources, selectedTargets, onQueryStateChange]);

  const rows = useMemo(() => {
    const values = result?.rows || [];
    return descending ? [...values].reverse() : values;
  }, [result?.rows, descending]);
  useEffect(() => { onRowsChange?.((result?.rows || []).slice(-300)); }, [result?.rows, onRowsChange]);
  useEffect(() => { onEntriesChange?.(parsedAtLogEntries(result?.rows || [], formatRules).map((item) => item.entry)); }, [result?.rows, formatRules, onEntriesChange]);
  const parsedRows = useMemo(() => parsedAtLogEntries(rows, formatRules), [rows, formatRules]);
  const timelineEntries = useMemo(() => parsedRows.map((item) => item.entry).slice().sort((left, right) => {
    if (left.timestampNs !== undefined && right.timestampNs !== undefined) return left.timestampNs < right.timestampNs ? -1 : left.timestampNs > right.timestampNs ? 1 : 0;
    return left.timestamp.localeCompare(right.timestamp);
  }), [parsedRows]);
  const processes = useMemo(() => buildProcessTimelines(timelineEntries, []), [timelineEntries]);
  const loadedTimeRange = useMemo(() => entryRange(timelineEntries), [timelineEntries]);
  const hierarchyCatalog = useMemo(() => atLogHierarchyCatalog(result), [result]);
  const errorIndexes = useMemo(() => parsedRows.map((item, index) => item.entry.severity === 'error' || rowSeverity(item.row) === 'error' ? index : -1).filter((index) => index >= 0), [parsedRows]);
  const parsedEvents = useMemo(() => result?.event_raw_text ? parseEventRestoreStream(result.event_raw_text, `atlog-case-${analysis.case_id}`, formatRules) : [], [result?.event_raw_text, analysis.case_id, formatRules]);
  const rootEntries = useMemo(() => parsedEvents.filter((entry) => ['ERROR', 'EVENT'].includes(normalizedEventLevel(entry))), [parsedEvents]);
  const currentResourceEnvironment = useMemo(() => {
    const id = analysis.environment?.environment_id;
    return id ? resourceEnvironments.find((item) => item.id === id) : undefined;
  }, [analysis.environment?.environment_id, resourceEnvironments]);
  const upperEnvironmentHost = analysis.environment?.upper?.environment_host || currentResourceEnvironment?.upper_machine?.host || '';
  const environmentSelectOptions = useMemo(() => ([
    { key: 'atlog', label: 'ATLog · 测试路径' },
    ...(currentResourceEnvironment && upperEnvironmentHost ? [{ key: `upper:${currentResourceEnvironment.id}`, label: `上位机 · ${upperEnvironmentHost}` }] : []),
  ]), [currentResourceEnvironment, upperEnvironmentHost]);

  useEffect(() => {
    if (!aiEvidence || aiEvidence.signal?.aborted) return;
    activeSearchAbortRef.current?.abort();
    setLoading(false);
    setResult(aiEvidence.result);
    setEnvironmentKey('atlog');
    setStartTime(toLocalInput(aiEvidence.result.start_time));
    setEndTime(toLocalInput(aiEvidence.result.end_time));
    setKeyword(''); setOnlyErrors(false); setTimelineRange(undefined);
    setSelectedTargets(new Set(atLogAiTargetKeys(aiEvidence.targets)));
    setSelectedSources(new Set([aiEvidence.sourceKind]));
    setTimelineExpanded(true);
    setEvidenceChain((current) => [...current, aiEvidence].slice(-6));
    void afterPaint().then(() => {
      if (!aiEvidence.signal?.aborted) aiEvidence.complete({ detail: `已在当前用例展示 ${aiEvidence.result.rows.length} 条 ${aiEvidence.sourceKind} 证据`, count: aiEvidence.result.rows.length });
    });
  }, [aiEvidence]);

  useEffect(() => { onSelectedEntryChange?.(rows[activeIndex]); }, [activeIndex, rows, onSelectedEntryChange]);

  const focusRow = (index: number) => {
    if (index < 0 || index >= rows.length) return;
    setActiveIndex(index);
    window.setTimeout(() => rowRefs.current.get(index)?.scrollIntoView({ block: 'center', behavior: 'smooth' }), 0);
  };
  const focusError = (nextCursor: number) => {
    if (!errorIndexes.length) return;
    const normalized = (nextCursor + errorIndexes.length) % errorIndexes.length;
    setErrorCursor(normalized); focusRow(errorIndexes[normalized]);
  };
  useEffect(() => {
    if (!errorIndexes.length) return;
    const timer = window.setTimeout(() => focusError(0), 40);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [result?.rows, descending]);

  const navigateEntry = (entry: LogEntry) => {
    const direct = parsedRows.find((item) => item.entry.id === entry.id)?.index;
    if (direct !== undefined) focusRow(direct);
  };
  const navigateTime = (timestampNs: bigint, component?: string) => {
    const index = nearestTimelineEntryIndex(parsedRows, timestampNs, component);
    if (index >= 0) focusRow(index);
  };

  return <div ref={workspaceRef} className="atlog-unified-log-workspace">
    {evidenceChain.length > 0 && <section className="workstation-evidence-chain" aria-label="诊断证据链">
      <strong>断言 → event → 组件日志</strong>
      <span>时间邻近仅代表候选关联 · 点击步骤回看原始证据</span>
      <div>{evidenceChain.map((step, index) => <button type="button" key={index} onClick={() => {
        setResult(step.result); setKeyword(''); setOnlyErrors(false); setTimelineRange(undefined);
        setStartTime(toLocalInput(step.result.start_time)); setEndTime(toLocalInput(step.result.end_time));
        setSelectedSources(new Set([step.sourceKind])); setSelectedTargets(new Set(atLogAiTargetKeys(step.targets)));
      }}><b>{step.sourceKind === 'event' ? '业务事件' : '组件调试日志'}</b><small>{step.result.count} 条{step.result.truncated ? ' · 已截断' : ''} · {step.result.start_time} — {step.result.end_time}</small></button>)}</div>
    </section>}
    <div ref={toolbarRef} className="remote-unified-query-row remote-unified-query-row-v150 atlog-case-log-searchbar" aria-label="用例日志定位搜索">
      <div className="remote-resource-select atlog-case-environment-select">
        <button type="button" className={`remote-resource-trigger ${environmentMenuOpen ? 'open' : ''}`} onClick={() => setEnvironmentMenuOpen((value) => !value)}>
          <ServerCog size={15}/><span className="remote-control-label">环境</span><strong title={environmentSelectOptions.find((item) => item.key === environmentKey)?.label}>{environmentSelectOptions.find((item) => item.key === environmentKey)?.label || 'ATLog · 测试路径'}</strong><ChevronDown size={13}/>
        </button>
        {environmentMenuOpen && <div className="remote-resource-menu atlog-case-environment-menu">
          {environmentSelectOptions.map((item) => <button type="button" className={item.key === environmentKey ? 'selected' : ''} key={item.key} onClick={() => { setEnvironmentKey(item.key); setEnvironmentMenuOpen(false); setActiveQuickRange(''); setError(''); }}>
            <span className="remote-option-check">{item.key === environmentKey && <Check size={12}/>}</span><span><strong>{item.label}</strong><small>{item.key === 'atlog' ? analysis.base_url : '环境资源 · 上位机日志'}</small></span>
          </button>)}
        </div>}
      </div>
      <HierarchyModuleSelect catalog={hierarchyCatalog} selected={selectedTargets} onChange={setSelectedTargets} label="子系统/模块" compact/>
      <LogTypeSelect
        values={CASE_LOG_SOURCE_TYPES.map((item) => ({ value: item.value, label: item.label, hint: item.hint }))}
        selected={selectedSources}
        onChange={setSelectedSources}
      />
      <div className="remote-range-presets" aria-label="快捷时间范围">
        {QUICK_RANGES.map((item) => <button type="button" className={activeQuickRange === item.label ? 'active' : ''} key={item.label} onClick={() => { const end = new Date(); setEndTime(toLocalInput(end.toISOString())); setStartTime(toLocalInput(new Date(end.getTime() - item.seconds * 1000).toISOString())); setActiveQuickRange(item.label); }}>{item.label}</button>)}
      </div>
      <SmartDateTimeInput value={startTime} label="开始时间" onChange={(value) => { setStartTime(value); setActiveQuickRange(''); }}/>
      <span className="remote-time-separator">—</span>
      <SmartDateTimeInput value={endTime} label="结束时间" onChange={(value) => { setEndTime(value); setActiveQuickRange(''); }}/>
      <div className="remote-unified-text-search">
        <Search size={15}/><input value={keyword} onChange={(event) => setKeyword(event.target.value)} placeholder="搜索正文 / 函数 / TraceID…"/>
        {keyword && <button type="button" onClick={() => setKeyword('')} aria-label="清空全文搜索"><XCircle size={13}/></button>}
      </div>
      {loading ? <button className="button danger-outline remote-search-apply" type="button" onClick={() => void stopQuery()} disabled={stoppingSearch || !activeSearchOperationId}>{stoppingSearch ? <LoaderCircle className="spin" size={15}/> : <Square size={14}/>} {stoppingSearch ? '停止中' : '停止'}</button> : <button className="button primary remote-search-apply" type="button" onClick={() => void runQuery()} disabled={!startTime || !endTime}><Search size={15}/> 搜索</button>}
      <button type="button" className="button ghost atlog-intelligent-analysis-button" onClick={onOpenCaseAnalysis} disabled={!onOpenCaseAnalysis} title={analysis.saved_state?.ai?.result ? '打开智能分析（该用例已有 AI 结论）' : '打开智能分析'}><BookOpenCheck size={14}/> 智能分析</button>
    </div>

    {loading && <div className="atlog-search-progress" role="status" aria-live="polite">
      <div className="atlog-search-progress-head"><LoaderCircle className="spin" size={15}/><strong>{searchProgress.message || '正在检索日志'}</strong><span>{Math.max(0, Math.min(100, searchProgress.percent))}%</span></div>
      <div className="atlog-search-progress-track"><div className="atlog-search-progress-bar" style={{ width: `${Math.max(4, Math.min(100, searchProgress.percent))}%` }}/></div>
      <div className="atlog-search-progress-meta">
        <span>目标文件 {searchProgress.matchedFiles || 0}</span><span>已读取 {searchProgress.readFiles || 0}{searchProgress.totalFiles ? ` / ${searchProgress.totalFiles}` : ''}</span>
        {searchProgress.currentFile && <span title={searchProgress.currentFile}>{searchProgress.currentFile}</span>}
      </div>
    </div>}

    {TimelineComponent ? <TimelineComponent
      processes={processes}
      selectedTimeRange={timelineRange}
      loadedTimeRange={loadedTimeRange}
      expanded={timelineExpanded}
      navigationPending={false}
      onToggle={() => setTimelineExpanded((value) => !value)}
      onSelectProcess={(process) => { const entry = process.threads.flatMap((thread) => thread.traces.flatMap((trace) => trace.entries))[0]; if (entry) navigateEntry(entry); }}
      onSelectTrace={(_thread, trace) => { const entry = trace.entries[0]; if (entry) navigateEntry(entry); }}
      onSelectTimeRange={setTimelineRange}
      onClearTimeRange={() => setTimelineRange(undefined)}
      onNavigateTime={(timestampNs) => navigateTime(timestampNs)}
      onNavigateComponentTime={(component, timestampNs) => navigateTime(timestampNs, component)}
      onNavigateEntry={navigateEntry}
      filterScopeKey={`atlog:${analysis.base_url}:${rows.length}`}
    /> : <div className="atlog-empty-inline">日志时间线组件未加载。</div>}

    <div className="atlog-log-actionbar"><span><strong>{rows.length}</strong> 条 · {result?.sources.length || 0} 个文件{result?.truncated ? ' · 已达到读取阈值' : ''}</span><span className="spacer"/><button type="button" onClick={() => focusError(errorCursor - 1)} disabled={!errorIndexes.length}><ArrowUp size={13}/> 上一异常</button><button type="button" onClick={() => focusError(errorCursor + 1)} disabled={!errorIndexes.length}>下一异常 <ArrowDown size={13}/></button><button type="button" onClick={() => setDescending((value) => !value)}>{descending ? '时间降序' : '时间升序'}</button><button type="button" onClick={() => setRootCauseOpen(true)} disabled={!rootEntries.length}><ListTree size={14}/> 根因树</button></div>
    {error && <div className="atlog-inline-error"><AlertTriangle size={15}/>{error}</div>}
    {rootCauseOpen && <EventRestoreDialog
      open
      sourceEntries={parsedEvents}
      startTime={startTime}
      endTime={endTime}
      formatRules={formatRules}
      onClose={() => setRootCauseOpen(false)}
    />}
    <div className="atlog-unified-log-list">
      {parsedRows.map(({ row, entry }, index) => {
        const severity = entry.severity || rowSeverity(row);
        return <div ref={(node) => { if (node) rowRefs.current.set(index, node); else rowRefs.current.delete(index); }} data-atlog-entry-id={entry.id} className={`atlog-unified-log-row ${severity === 'error' ? 'is-error' : severity === 'warning' ? 'is-warning' : ''} ${activeIndex === index ? 'is-current' : ''}`} key={`${row.source_path}-${row.line_number}-${index}`} onClick={() => setActiveIndex(index)} title={row.source_path}>
          <span className="atlog-source-dot" title={row.source_kind === 'event' ? 'event.log' : row.source_path}/>
          <span className="log-time">{compactLogTime(row.time || entry.timestamp || '')}</span>
          <span className="component-badge compact">{row.component || entry.component || '—'}</span>
          <span className={`level-badge level-${String(row.level || entry.level || '').toLowerCase()}`}>{row.level || entry.level || '—'}</span>
          <span className="log-summary">{row.message || entry.message || row.raw}</span>
        </div>;
      })}
      {!loading && !rows.length && <div className="atlog-empty-inline">{selectedSources.has('event')
        ? (result?.event_query?.found
          ? '当前时间范围内 event.log 没有匹配日志；event.log 已从当前用例根目录直接加载。'
          : `未读取到当前用例根目录的 event.log${result?.event_query?.error ? `：${result.event_query.error}` : '。'}`)
        : (selectedSources.has('debug') || selectedSources.has('executor')
          ? `当前时间范围没有日志。调试日志/执行器日志需要选择对应子系统和模块后检索。${(result?.sources || []).length ? `
检索日志URL：${(result?.sources || []).map((item: any) => item.url || item.path || item.source_path || '').filter(Boolean).join('\n')}` : '\n未发现可访问日志URL，请检查当前环境日志目录映射。'}`
          : '当前没有可展示的运行日志。')}</div>}
    </div>
    {environmentKey && <div className="atlog-environment-inline-note">当前环境：{environmentSelectOptions.find((item) => item.key === environmentKey)?.label || environmentKey}。</div>}
  </div>;
}

function formatTokenCount(value: number | undefined): string {
  const count = Number(value || 0);
  return count > 0 ? count.toLocaleString() : '—';
}

function TypewriterText({ text, className = '' }: { text: string; className?: string }) {
  const [visible, setVisible] = useState('');
  useEffect(() => {
    const content = String(text || '');
    setVisible('');
    if (!content) return;
    let cursor = 0;
    const timer = window.setInterval(() => {
      cursor += 1;
      setVisible(content.slice(0, cursor));
      if (cursor >= content.length) window.clearInterval(timer);
    }, 10);
    return () => window.clearInterval(timer);
  }, [text]);
  return <span className={`atlog-ai-typewriter ${className}`}>{visible}<i aria-hidden="true"/></span>;
}

function mergeAiEvents(current: AtLogAiJobEvent[], incoming: AtLogAiJobEvent[]): AtLogAiJobEvent[] {
  const bySeq = new Map<number, AtLogAiJobEvent>();
  [...current, ...incoming].forEach((item) => bySeq.set(Number(item.seq || 0), item));
  return Array.from(bySeq.values()).sort((left, right) => Number(left.seq || 0) - Number(right.seq || 0)).slice(-240);
}

function GrowingTypewriterText({ text }: { text: string }) {
  const [visible, setVisible] = useState('');
  const visibleRef = useRef('');
  const targetRef = useRef(String(text || ''));

  useEffect(() => {
    const next = String(text || '');
    if (!next.startsWith(visibleRef.current)) {
      visibleRef.current = '';
      setVisible('');
    }
    targetRef.current = next;
  }, [text]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      const target = targetRef.current;
      const current = visibleRef.current;
      if (current.length >= target.length) return;
      const next = target.slice(0, Math.min(target.length, current.length + 1));
      visibleRef.current = next;
      setVisible(next);
    }, 14);
    return () => window.clearInterval(timer);
  }, []);

  return <span className="atlog-ai-growing-typewriter">{visible}<i aria-hidden="true"/></span>;
}

function compactReasonTranscript(job: AtLogAiDiagnosisJob | undefined, events: AtLogAiJobEvent[]): string {
  const reasons = events
    .filter((event) => event.type === 'thinking' && event.kind === 'reason' && event.stage_name !== 'report' && String(event.text || '').trim())
    .map((event) => String(event.text || '').trim());
  if (reasons.length) return reasons.join('\n\n');

  // Redis-restored jobs may only have the accumulated snapshot available.
  // Keep only the public "依据" blocks and strip Agent/tool headings so the UI
  // reads like one continuous diagnosis conversation instead of a workflow log.
  const snapshot = String(job?.thinking_text || '').trim();
  if (!snapshot) return '';
  const blocks = snapshot.split(/\n\s*\n/).map((item) => item.trim()).filter(Boolean);
  const reasonBlocks = blocks
    .filter((item) => /·\s*依据\]/.test(item) && !/^\[Report Agent\s*·/u.test(item))
    .map((item) => item.replace(/^\[[^\]]+\]\s*/u, '').trim())
    .filter(Boolean);
  return reasonBlocks.join('\n\n');
}

const ATLOG_AI_STAGE_PROGRESS: Record<string, number> = {
  queued: 2, prepare: 7, skill_load: 8, agent_decision: 18, synthesis: 92, final: 100,
  context: 12, component_map: 28, triage: 42, retrieve: 60, review: 74, history: 86, report: 96, cache: 100, done: 100,
};

function diagnosisStageProgress(job?: AtLogAiDiagnosisJob): number {
  const name = String(job?.current_stage?.name || '');
  if (job?.status === 'completed') return 100;
  const explicit = Number(job?.current_stage?.progress);
  if (Number.isFinite(explicit) && explicit >= 0) return Math.max(0, Math.min(100, explicit));
  if (name.startsWith('tool:')) return 48;
  return ATLOG_AI_STAGE_PROGRESS[name] ?? (job?.status === 'running' ? 6 : 0);
}

function diagnosisStageTimeline(events: AtLogAiJobEvent[]): AtLogAiDiagnosisStep[] {
  const stages = new Map<string, AtLogAiDiagnosisStep>();
  for (const event of events) {
    if (event.type !== 'stage' || !event.stage?.name) continue;
    const key = String(event.stage.name);
    stages.set(key, { ...((stages.get(key) || {}) as AtLogAiDiagnosisStep), ...event.stage });
  }
  return Array.from(stages.values());
}

function AiDiagnosisLiveStages({ events, job }: { events: AtLogAiJobEvent[]; job?: AtLogAiDiagnosisJob }) {
  const stages = diagnosisStageTimeline(events);
  if (!stages.length && !job?.current_stage?.name) return null;
  const currentName = String(job?.current_stage?.name || '');
  const items = stages.length ? stages : [job!.current_stage];
  return <div className="atlog-ai-live-stages" aria-live="polite">
    {items.map((stage, index) => {
      const status = String(stage.status || (stage.name === currentName ? 'running' : 'completed'));
      const running = status === 'running';
      return <div className={`atlog-ai-live-stage ${running ? 'running' : 'completed'}`} key={`${stage.name}-${index}`}>
        <span className="atlog-ai-live-stage-icon">{running ? <LoaderCircle className="spin" size={13}/> : <Check size={13}/>}</span>
        <span className="atlog-ai-live-stage-copy"><strong>{stage.label || stage.name}</strong>{stage.detail && <small>{stage.detail}</small>}</span>
      </div>;
    })}
  </div>;
}

function AiEvidenceCards({ evidence }: { evidence: Array<{ time?: string; component?: string; source?: string; message?: string; why?: string }> }) {
  if (!evidence.length) return null;
  return <div className="atlog-ai-chat-evidence">
    <strong>证据</strong>
    {evidence.map((item, index) => <article key={`${item.source || ''}-${item.time || ''}-${index}`}>
      <header><span>{item.time || '—'}</span><b>{item.component || '—'}</b>{item.source && <code>{item.source}</code>}</header>
      <p>{item.message || '—'}</p>
      {item.why && <small>{item.why}</small>}
    </article>)}
  </div>;
}

function AiDiagnosisConversation({
  job,
  events,
  result,
  knowledge,
  usedHistoricalCaseId,
  onUseHistorical,
  onAccept,
  accepting,
  acceptedCaseId,
}: {
  job?: AtLogAiDiagnosisJob;
  events: AtLogAiJobEvent[];
  result?: AtLogAiDiagnosisResult;
  knowledge?: AtLogKnowledgeMatchResult;
  usedHistoricalCaseId?: number;
  onUseHistorical: (caseId: number) => void;
  onAccept: () => void;
  accepting: boolean;
  acceptedCaseId?: number;
}) {
  const [processOpen, setProcessOpen] = useState(false);
  const running = ['queued', 'running'].includes(job?.status || '');
  const transcript = compactReasonTranscript(job, events);
  const liveStage = job?.current_stage;
  const liveProgress = diagnosisStageProgress(job);
  useEffect(() => { setProcessOpen(false); }, [job?.job_id]);
  const historical = knowledge?.strong_match ? knowledge.matches?.[0] : undefined;
  const usage = result?.token_usage || job?.token_usage;
  const targetCaseId = historical?.case_id;
  const displayedEvidence = result?.case_evidences?.length
    ? result.case_evidences.map((evidence) => ({
        time: evidence.timestamp,
        component: evidence.module || evidence.component,
        source: evidence.source_file,
        message: evidence.message || evidence.raw,
        why: evidence.anomaly_rules?.length ? `命中异常规则：${evidence.anomaly_rules.map((rule) => rule.keyword).join(', ')}` : evidence.source_category === 'case_report' ? '用例报告证据' : '',
      }))
    : (result?.report.evidence || []);

  return <div className="atlog-ai-chat">
    {historical && !result && <div className="atlog-ai-chat-row assistant historical">
      <div className="atlog-ai-chat-avatar"><BookOpenCheck size={17}/></div>
      <div className="atlog-ai-chat-bubble">
        <div className="atlog-ai-chat-title"><strong>案例库已找到相似问题</strong><span>{historical.score.toFixed(1)}% 相似</span></div>
        <p className="atlog-ai-chat-conclusion">{historical.root_cause || historical.symptom || historical.name}</p>
        <AiEvidenceCards evidence={(historical.evidence_preview || []).map((item) => ({ ...item }))}/>
        <div className="atlog-ai-chat-actions">
          <button type="button" className="button secondary" disabled={usedHistoricalCaseId === historical.case_id} onClick={() => onUseHistorical(historical.case_id)}>
            {usedHistoricalCaseId === historical.case_id ? <Check size={14}/> : <BookOpenCheck size={14}/>} {usedHistoricalCaseId === historical.case_id ? '已采用该案例' : '采用该案例结论'}
          </button>
        </div>
      </div>
    </div>}

    {(job || running) && <div className={`atlog-ai-chat-row assistant live process-collapsible ${processOpen ? 'open' : 'collapsed'}`}>
      <div className="atlog-ai-chat-avatar">{running ? <LoaderCircle className="spin" size={18}/> : <CheckCircle2 size={17}/>}</div>
      <div className="atlog-ai-chat-bubble">
        <button type="button" className="atlog-ai-process-toggle" aria-expanded={processOpen} onClick={() => setProcessOpen((current) => !current)}>
          <span>{processOpen ? <ChevronDown size={16}/> : <ChevronRight size={16}/>}<strong>{running ? (liveStage?.label || '分析过程') : '分析过程'}</strong></span>
          <small>{running ? <><LoaderCircle className="spin" size={13}/>{Math.max(1, Math.round(liveProgress))}%</> : job?.cache_hit ? '已恢复' : '已完成'}</small>
        </button>
        {running && <div className="atlog-ai-process-progress" aria-label={`诊断进度 ${Math.round(liveProgress)}%`}><i style={{ width: `${Math.max(2, Math.min(100, liveProgress))}%` }}/></div>}
        {processOpen && <div className="atlog-ai-chat-stream">
          <AiDiagnosisLiveStages events={events} job={job}/>
          {transcript ? <div className="atlog-ai-live-reason"><GrowingTypewriterText text={transcript}/></div> : !running && <span>本轮没有额外过程信息。</span>}
        </div>}
      </div>
    </div>}

    {result && <div className="atlog-ai-chat-row assistant final">
      <div className="atlog-ai-chat-avatar"><Sparkles size={17}/></div>
      <div className="atlog-ai-chat-bubble">
        <div className="atlog-ai-chat-title"><strong>诊断结论</strong><span>{result.report.confidence ?? 0}%</span></div>
        <p className="atlog-ai-chat-conclusion"><TypewriterText text={result.report.root_cause || result.report.summary || '当前证据不足，未形成明确根因。'}/></p>
        <AiEvidenceCards evidence={displayedEvidence}/>
        <div className="atlog-ai-chat-footer">
          {usage && <span>Token：{formatTokenCount(usage.input_tokens)} 输入 / {formatTokenCount(usage.output_tokens)} 输出</span>}
          <button type="button" className="button primary" disabled={accepting || Boolean(acceptedCaseId)} onClick={onAccept}>
            {accepting ? <LoaderCircle className="spin" size={14}/> : acceptedCaseId ? <Check size={14}/> : <BookOpenCheck size={14}/>} {acceptedCaseId ? `已保存到案例 #${acceptedCaseId}` : accepting ? '正在保存…' : targetCaseId ? `保存案例 · 补充 #${targetCaseId}` : '保存案例'}
          </button>
        </div>
      </div>
    </div>}
  </div>;
}


function filterAtLogErrorLines(text: string, errorRules: readonly ErrorMatchRule[]): string {
  return text.split(/\r?\n/).filter((line) => !line.trim() || errorRules.some((rule) => matchesErrorRule(line, rule)) || detectSeverity(line, '') === 'error').join('\n');
}

function CaseReportTab({ analysis, formatRules, errorRules, startTime, endTime }: { analysis: AtLogCaseAnalysis; formatRules: readonly LogFormatParserRuleConfig[]; errorRules: readonly ErrorMatchRule[]; startTime: string; endTime: string }) {
  const [onlyErrors, setOnlyErrors] = useState(true);
  return <section className="atlog-case-tab-panel atlog-case-report-tab-panel">
    <div className="atlog-case-tab-toolbar"><strong>测试报告</strong><label className="atlog-inline-check"><input type="checkbox" checked={onlyErrors} onChange={(event) => setOnlyErrors(event.target.checked)}/>只看报错</label></div>
    <div className="atlog-case-report-tab-grid">
      <section className="atlog-diagnostic-pane"><div className="atlog-pane-tabs"><strong>pytest</strong>{analysis.links.test_html && <a href={analysis.links.test_html} target="_blank" rel="noreferrer" title="打开原始 pytest HTML"><ExternalLink size={14}/></a>}</div><PytestLogPane analysis={analysis} formatRules={formatRules} errorRules={errorRules} onlyErrors={onlyErrors}/></section>
      <section className="atlog-diagnostic-pane"><div className="atlog-pane-tabs"><strong>xytest.log</strong>{analysis.links['xytest.log'] && <a href={analysis.links['xytest.log']} target="_blank" rel="noreferrer" title="打开原始 xytest.log"><ExternalLink size={14}/></a>}</div><XytestLogPane analysis={analysis} formatRules={formatRules} errorRules={errorRules} startTime={startTime} endTime={endTime} onlyErrors={onlyErrors}/></section>
    </div>
  </section>;
}

function CaseEnvironmentTab({ analysis, onOpenLogLocator, onOpenCpdReports, logRequest }: { analysis: AtLogCaseAnalysis; onOpenLogLocator?: (environment: EnvironmentSummary, request?: LogWindowRequest) => void; onOpenCpdReports?: (environment: EnvironmentSummary) => void; logRequest?: LogWindowRequest }) {
  const environmentId = analysis.environment?.environment_id;
  const [environment, setEnvironment] = useState<EnvironmentSummary>();
  const [runtime, setRuntime] = useState<EnvironmentRuntimeStatus>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    if (!environmentId) { setEnvironment(undefined); setRuntime(undefined); setError('当前用例没有关联环境资源。'); return; }
    let active = true;
    setLoading(true); setError('');
    void Promise.all([listEnvironments(), getEnvironmentRuntimeStatus(environmentId, false)])
      .then(([items, status]) => { if (!active) return; const found = items.find((item) => item.id === environmentId); setEnvironment(found); setRuntime(status); if (!found) setError(`未找到环境资源 #${environmentId}。`); })
      .catch((exc) => { if (active) setError(exc instanceof Error ? exc.message : String(exc)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [environmentId]);
  if (loading) return <div className="atlog-empty-inline"><LoaderCircle className="spin" size={16}/> 正在读取环境资源…</div>;
  if (error) return <div className="atlog-inline-error"><AlertTriangle size={14}/>{error}</div>;
  if (!environment) return <div className="atlog-empty-inline">当前用例没有关联环境资源。</div>;
  return <section className="atlog-case-tab-panel atlog-case-environment-tab-panel"><EnvironmentResourcePreview environment={environment} runtime={runtime} onOpenLogLocator={() => onOpenLogLocator?.(environment, logRequest)} onOpenCpdReports={() => onOpenCpdReports?.(environment)}/></section>;
}

function compactAssistantEvidenceRows(rows: readonly AtLogCaseLogRow[], kind: 'event' | 'runtime', limit: number): Array<Record<string, unknown>> {
  const candidates = rows.filter((row) => kind === 'event' ? row.source_kind === 'event' : row.source_kind !== 'event');
  const priority = candidates.filter((row) => /^(?:ERROR|FATAL|CRITICAL|WARN|WARNING)$/i.test(String(row.level || '')));
  const ordered = [...priority, ...candidates.slice(-Math.max(limit * 2, 24))];
  const seen = new Set<string>();
  const result: Array<Record<string, unknown>> = [];
  for (const row of ordered) {
    const key = `${row.source_path || row.source}|${row.line_number || ''}|${row.raw || row.message}`;
    if (seen.has(key)) continue;
    seen.add(key);
    result.push({
      time: String(row.time || '').slice(0, 64),
      component: String(row.component || '').slice(0, 100),
      level: String(row.level || '').slice(0, 24),
      source: String(row.source_path || row.source || '').slice(0, 220),
      line: row.line_number,
      record_id: row.record_id, trace_id: row.trace_id, error_code: row.error_code, correlation_kind: row.correlation_kind,
      message: String(row.message || row.raw || '').slice(0, 1100),
    });
    if (result.length >= limit) break;
  }
  return result;
}

function compactAtLogDiagnosisMessages(rows: readonly AtLogCaseLogRow[], limit = 160): AtLogCaseLogRow[] {
  const priority = rows.filter((row) => /^(?:ERROR|FATAL|CRITICAL|ALARM)$/i.test(String(row.level || '')));
  const ordered = [...priority, ...rows.slice(-Math.max(80, limit))];
  const seen = new Set<string>();
  const result: AtLogCaseLogRow[] = [];
  for (const row of ordered) {
    const key = `${row.source_path || row.source}|${row.line_number || ''}|${row.raw || row.message}`;
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(row);
    if (result.length >= limit) break;
  }
  return result;
}

function buildAtLogAssistantCaseContext(
  item: ImportedCase,
  analysis: AtLogCaseAnalysis,
  logRequest: LogWindowRequest | undefined,
  collectedMessages: readonly AtLogCaseLogRow[],
  parsedLogEntries: number,
  aiResult?: AtLogAiDiagnosisResult,
  anomalyRules: readonly AtLogAnomalyRulePayload[] = [],
): Record<string, unknown> {
  const caseId = item.caseId || analysis.case_id || '';
  const caseName = item.caseName || analysis.case_name || caseId || '自动化用例';
  const eventEvidence = compactAssistantEvidenceRows(collectedMessages, 'event', 12);
  const runtimeEvidence = compactAssistantEvidenceRows(collectedMessages, 'runtime', 24);
  const authoritativeCaseUrl = String(analysis.base_url || item.url || '').trim();
  const selectedTargets = (logRequest?.fm_targets || []).slice(0, 8);
  return {
    type: 'atlog_case',
    selected: true,
    case_url: authoritativeCaseUrl,
    source_case_url: String(item.url || authoritativeCaseUrl).trim(),
    case_id: caseId,
    case_name: caseName,
    case_description: item.caseDescription || analysis.case_description || '',
    status: analysis.status || '',
    assertion_summary: analysis.assertion_summary || analysis.conclusion || '',
    failure_time: analysis.failure_time || '',
    start_time: logRequest?.start_time || analysis.start_time || analysis.event_start_time || '',
    end_time: logRequest?.end_time || analysis.end_time || analysis.event_end_time || '',
    environment: analysis.environment ? {
      environment_id: analysis.environment.environment_id,
      environment_name: analysis.environment.environment_name,
    } : null,
    report_facts: {
      assertion_summary: analysis.assertion_summary || analysis.assertion || '',
      failure_text: String(analysis.failure_text || '').slice(-6000),
      report_excerpt: analysis.status === 'failed' ? String(analysis.report_excerpt || '').slice(-6000) : '',
      failure_location: analysis.failure_location || null,
      call_chain: (analysis.call_chain || []).slice(-12),
      xytest_errors: (analysis.xytest_errors || []).slice(-10),
      links: {
        pytest_xml: analysis.links?.pytest_xml || '',
        test_html: analysis.links?.test_html || '',
        xytest_log: analysis.links?.['xytest.log'] || '',
        summary_report: analysis.links?.['summary_report.xml'] || analysis.links?.summary_report || '',
        event_log: analysis.links?.['event.log'] || '',
      },
      summary: analysis.summary || {},
      evidence: (analysis.evidence || []).slice(0, 12),
    },
    current_log_request: logRequest ? {
      start_time: logRequest.start_time,
      end_time: logRequest.end_time,
      source_categories: logRequest.source_categories,
      subsystems: logRequest.subsystems,
      fms: logRequest.fms,
      fm_targets: logRequest.fm_targets,
      keyword: logRequest.keyword,
    } : null,
    selected_targets: selectedTargets,
    anomaly_rules: anomalyRules.filter((rule) => rule.enabled).slice(0, 64),
    event_evidence: eventEvidence,
    runtime_evidence: runtimeEvidence,
    loaded_evidence: runtimeEvidence,
    selected_log_context: {
      rows: compactAtLogDiagnosisMessages(collectedMessages, 120).map((row) => ({
        level: row.level,
        source: row.source_path || row.source,
        line: row.line_number,
        message: row.raw || row.message,
      })),
      description: '用户当前选择/加载的日志上下文，优先用于AI分析',
    },
    loaded_log_rows: collectedMessages.length,
    parsed_log_entries: parsedLogEntries,
    persisted_ai_summary: aiResult ? {
      root_cause: aiResult.report?.root_cause || '',
      summary: aiResult.report?.summary || '',
      confidence: aiResult.report?.confidence || 0,
      selected_targets: aiResult.selected_targets || [],
    } : null,
  };
}

function CaseExpandedPanel({ item, formatRules, TimelineComponent, errorRules, onAiConclusionReady, onOpenEnvironmentLogLocator, onOpenEnvironmentCpdReports }: { item: ImportedCase; formatRules: readonly LogFormatParserRuleConfig[]; TimelineComponent?: TimelineRenderer; errorRules?: readonly ErrorMatchRule[]; onAiConclusionReady?: () => void; onOpenEnvironmentLogLocator?: (environment: EnvironmentSummary, request?: LogWindowRequest) => void; onOpenEnvironmentCpdReports?: (environment: EnvironmentSummary) => void }) {
  const analysis = item.analysis;
  const cacheKey = analysis?.base_url || item.url;
  const cached = caseUiCache(cacheKey);
  const effectiveErrorRules = useMemo(() => errorRules?.length ? [...errorRules] : DEFAULT_ERROR_RULES, [errorRules]);
  const [collectedMessages, setCollectedMessages] = useState<AtLogCaseLogRow[]>(() => cached.collectedMessages || []);
  const [caseEntries, setCaseEntries] = useState<LogEntry[]>([]);
  const [caseAnalysisOpen, setCaseAnalysisOpen] = useState(false);
  const [aiJob, setAiJob] = useState<AtLogAiDiagnosisJob | undefined>(() => cached.aiJob || persistedAiJob(analysis));
  const [aiEvents, setAiEvents] = useState<AtLogAiJobEvent[]>(() => cached.aiEvents || []);
  const [aiResult, setAiResult] = useState<AtLogAiDiagnosisResult | undefined>(() => cached.aiResult || cached.aiJob?.result || persistedAiResult(analysis) || undefined);
  const [aiError, setAiError] = useState(() => cached.aiError || '');
  const [knowledge, setKnowledge] = useState<AtLogKnowledgeMatchResult | undefined>(() => cached.knowledge);
  const [knowledgeLoading, setKnowledgeLoading] = useState(false);
  const [acceptedAiCaseId, setAcceptedAiCaseId] = useState<number | undefined>(() => cached.acceptedAiCaseId);
  const [aiEvidenceRevision, setAiEvidenceRevision] = useState(() => Number(analysis?.saved_state?.ai?.revision || 0));
  const accepting = false;
  const [caseEditorOpen, setCaseEditorOpen] = useState(false);
  const lastSeqRef = useRef(aiJob?.last_seq || 0);
  const aiAnomalyRules = useMemo(() => currentAtLogAiAnomalyRules(effectiveErrorRules), [effectiveErrorRules]);
  const aiAnomalyRuleKey = useMemo(() => JSON.stringify(aiAnomalyRules), [aiAnomalyRules]);
  const [detailTab, setDetailTab] = useState<'logs' | 'environment' | 'report'>('logs');
  const [aiEvidence, setAiEvidence] = useState<WorkstationEvidence>();
  const [selectedLogRow, setSelectedLogRow] = useState<AtLogCaseLogRow>();
  const [logRequest, setLogRequest] = useState<LogWindowRequest>();
  const assistantCaseContext = useMemo(() => analysis ? buildAtLogAssistantCaseContext(item, analysis, logRequest, collectedMessages, caseEntries.length, aiResult, aiAnomalyRules) : undefined, [analysis, item.caseId, item.caseName, item.caseDescription, logRequest, collectedMessages, caseEntries.length, aiResult, aiAnomalyRuleKey]);

  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent).detail;
      if (!analysis || String(detail.case_url || '').replace(/\/+$/, '') !== analysis.base_url.replace(/\/+$/, '') || detail.__claimed) return;
      detail.__claimed = true;
      detail.__result = new Promise((resolve, reject) => {
        const signal = detail.__signal as AbortSignal | undefined;
        if (signal?.aborted) { reject(new DOMException('已接管', 'AbortError')); return; }
        if (!detail.result || !Array.isArray(detail.result.rows)) { reject(new Error('证据格式无效')); return; }
        const abort = () => reject(new DOMException('已接管', 'AbortError'));
        signal?.addEventListener('abort', abort, { once: true });
        setDetailTab('logs');
        setAiEvidence({ result: detail.result, targets: detail.targets || [], sourceKind: detail.source_kind || 'debug', signal,
          complete: (value) => { signal?.removeEventListener('abort', abort); resolve(value); } });
      });
    };
    window.addEventListener('tracelens:atlog-evidence', handler);
    return () => window.removeEventListener('tracelens:atlog-evidence', handler);
  }, [analysis]);

  useEffect(() => {
    ATLOG_CASE_UI_CACHE.set(cacheKey, { collectedMessages, aiJob, aiEvents, aiResult, aiError, knowledge, acceptedAiCaseId });
  }, [cacheKey, collectedMessages, aiJob, aiEvents, aiResult, aiError, knowledge, acceptedAiCaseId]);

  useEffect(() => {
    if (!assistantCaseContext) return;
    const handler = (event: Event) => {
      const custom = event as CustomEvent<{ context?: Record<string, unknown> }>;
      const context = custom.detail?.context;
      if (!context) return;
      const page = context.atlog_page && typeof context.atlog_page === 'object' ? context.atlog_page as Record<string, unknown> : {};
      const existing = page.expanded_case && typeof page.expanded_case === 'object' ? page.expanded_case as Record<string, unknown> : {};
      context.page = 'atlog';
      context.page_label = '用例分析';
      const selectedCase = { ...existing, ...assistantCaseContext, selected_entry: selectedLogRow };
      context.selected_atlog_case = selectedCase;
      context.atlog_page = { ...page, expanded_case: selectedCase };
    };
    return registerPageContextReader(handler, 10);
  }, [assistantCaseContext, selectedLogRow]);

  useEffect(() => {
    if (!analysis || !caseAnalysisOpen || knowledge || knowledgeLoading) return;
    let active = true;
    setKnowledgeLoading(true);
    void matchAtLogKnowledge(analysis.base_url, aiAnomalyRules)
      .then((next) => { if (active) setKnowledge(next); })
      .catch(() => { /* knowledge pre-check is advisory */ })
      .finally(() => { if (active) setKnowledgeLoading(false); });
    return () => { active = false; };
  }, [analysis?.base_url, aiAnomalyRuleKey, caseAnalysisOpen]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const jobId = aiJob?.job_id;
    if (!jobId || !['queued', 'running'].includes(aiJob?.status || '')) return;
    const controller = new AbortController();
    let active = true;
    void streamAtLogAiDiagnosis(jobId, lastSeqRef.current, {
      signal: controller.signal,
      onEvent: (event) => {
        if (!active) return;
        if (event.type === 'error') {
          setAiError(event.message || 'AI 诊断失败。');
          return;
        }
        const next = event.job;
        lastSeqRef.current = Math.max(lastSeqRef.current, next.last_seq || 0, event.type === 'event' ? Number(event.event.seq || 0) : 0);
        if (event.type === 'event') setAiEvents((current) => mergeAiEvents(current, [event.event]));
        else if (event.type === 'snapshot') setAiEvents((current) => mergeAiEvents(current, next.events || []));
        setAiJob((current) => ({ ...(current || next), ...next, events: [] }));
        if (next.result) {
          setAiResult(next.result);
          setAiEvidenceRevision((current) => current + 1);
          onAiConclusionReady?.();
        }
        if (next.status === 'error') setAiError(next.error || 'AI 诊断失败。');
      },
    }).catch((exc) => {
      if (!active || controller.signal.aborted) return;
      setAiError(exc instanceof Error ? exc.message : String(exc));
    });
    return () => { active = false; controller.abort(); };
  }, [aiJob?.job_id]);

  if (item.phase === 'pending' || item.phase === 'analyzing') return <div className="atlog-case-loading"><LoaderCircle className="spin" size={18}/> 正在解析用例报告…</div>;
  if (item.phase === 'error') return <div className="atlog-inline-error"><AlertTriangle size={17}/>{item.error}</div>;
  if (!analysis) return <div className="atlog-empty-inline">尚未分析此用例。</div>;

  const openTracePilotCaseAnalysis = () => {
    const caseId = item.caseId || analysis.case_id || '';
    const caseName = item.caseName || analysis.case_name || caseId || '自动化用例';
    const caseContext = assistantCaseContext || buildAtLogAssistantCaseContext(item, analysis, logRequest, collectedMessages, caseEntries.length, aiResult, aiAnomalyRules);
    window.dispatchEvent(new CustomEvent('tracelens:assistant-open', {
      detail: {
        skill_id: 'atlog',
        scope_key: `atlog:${analysis.base_url}`,
        scope_label: `用例 · ${caseId || caseName}`,
        title: caseId || caseName,
        context: { atlog_case: caseContext },
        prompt: `分析当前自动化用例 ${caseId || caseName} 的失败原因。先只根据当前页面已经选择和加载的测试报告、pytest/HTML、xytest、event、日志与时间线证据直接给出结论，不要自动扩展日志范围或调用额外工具。只有我后续明确要求继续查日志、扩大时间范围、切换模块或补充取证时，再使用对应 Skill/Tool。`,
        auto_send: true,
      },
    }));
  };

  const runAiDiagnosis = async () => {
    const force = Boolean(aiResult);
    setAiError(''); setAiResult(undefined); setAiEvents([]); setAcceptedAiCaseId(undefined); setCaseEditorOpen(false); setKnowledge(undefined);
    lastSeqRef.current = 0;
    try {
      const diagnosisContext = assistantCaseContext || buildAtLogAssistantCaseContext(
        item, analysis, logRequest, collectedMessages, caseEntries.length, aiResult, aiAnomalyRules,
      );
      const next = await startAtLogAiDiagnosis({
        url: analysis.base_url,
        messages: compactAtLogDiagnosisMessages(collectedMessages),
        anomaly_rules: aiAnomalyRules,
        case_context: diagnosisContext,
        force,
      });
      setAiJob(next); setAiEvents(next.events || []); lastSeqRef.current = next.last_seq || 0;
      if (next.result) {
        setAiResult(next.result);
        setAiEvidenceRevision((current) => current + 1);
        onAiConclusionReady?.();
      }
    } catch (exc) { setAiError(exc instanceof Error ? exc.message : String(exc)); }
  };

  const acceptDiagnosis = () => {
    if (!aiResult) return;
    setAiError('');
    setCaseEditorOpen(true);
  };

  const aiRunning = ['queued', 'running'].includes(aiJob?.status || '');
  const aiPanel = <section className="knowledge-ai-diagnosis-panel">
    <header>
      <div><Sparkles size={17}/><span><strong>AI 诊断</strong>{aiJob?.cache_source === 'database' && aiResult && <small>已恢复 · {analysis.saved_state?.ai?.completed_at || analysis.saved_state?.updated_at || ''}</small>}</span>{knowledgeLoading && <LoaderCircle className="spin" size={13}/>}</div>
      <button type="button" className="button primary" onClick={() => void runAiDiagnosis()} disabled={aiRunning}>{aiRunning ? <LoaderCircle className="spin" size={14}/> : <WandSparkles size={14}/>} {aiRunning ? '诊断中…' : aiResult ? '重新诊断' : '一键诊断'}</button>
    </header>
    {aiError && <div className="atlog-inline-error"><AlertTriangle size={15}/>{aiError}</div>}
    <AiDiagnosisConversation
      job={aiJob}
      events={aiEvents}
      result={aiResult}
      knowledge={undefined}
      onUseHistorical={() => undefined}
      onAccept={acceptDiagnosis}
      accepting={accepting}
      acceptedCaseId={acceptedAiCaseId}
    />

  </section>;

  return <div className="atlog-case-detail atlog-case-unified-detail">
    <nav className="resource-feature-tabs-bar resource-feature-tabs-secondary atlog-case-detail-tabs" aria-label="用例详情">
      <button type="button" className={detailTab === 'logs' ? 'active' : ''} onClick={() => setDetailTab('logs')}><FileSearch size={14}/> 日志</button>
      <button type="button" className={detailTab === 'environment' ? 'active' : ''} onClick={() => setDetailTab('environment')}><Server size={14}/> 环境</button>
      <button type="button" className={detailTab === 'report' ? 'active' : ''} onClick={() => setDetailTab('report')}><FileText size={14}/> 报告</button>
    </nav>
    {detailTab === 'logs' && <UnifiedCaseLogWorkspace
      aiEvidence={aiEvidence}
      onSelectedEntryChange={setSelectedLogRow}
      key={`${analysis.case_id}-${analysis.failure_time}`}
      analysis={analysis}
      formatRules={formatRules}
      TimelineComponent={TimelineComponent}
      errorRules={effectiveErrorRules}
      aiSelectedTargets={aiResult?.selected_targets}
      aiRefreshToken={aiResult ? `${aiJob?.job_id || 'persisted'}:${aiEvidenceRevision}` : ''}
      onRowsChange={setCollectedMessages}
      onEntriesChange={setCaseEntries}
      onOpenCaseAnalysis={openTracePilotCaseAnalysis}
      onOpenEnvironmentLogLocator={onOpenEnvironmentLogLocator}
      onQueryStateChange={setLogRequest}
    />}
    {detailTab === 'environment' && <CaseEnvironmentTab analysis={analysis} logRequest={logRequest} onOpenLogLocator={(environment, request) => { onOpenEnvironmentLogLocator?.(environment, request); if (!onOpenEnvironmentLogLocator) setDetailTab('logs'); }} onOpenCpdReports={onOpenEnvironmentCpdReports} />}
    {detailTab === 'report' && <CaseReportTab analysis={analysis} formatRules={formatRules} errorRules={effectiveErrorRules} startTime={logRequest?.start_time || analysis.start_time || analysis.event_start_time || ''} endTime={logRequest?.end_time || analysis.end_time || analysis.event_end_time || ''} />}
    {caseAnalysisOpen && <AbnormalCaseAnalysisDialog
      entries={caseEntries}
      selectedModules={Array.from(new Set(caseEntries.map((entry) => entry.logModule || entry.component).filter(Boolean)))}
      environmentId={analysis.environment?.environment_id || undefined}
      errorRules={effectiveErrorRules}
      onClose={() => setCaseAnalysisOpen(false)}
      onLocateEntry={(entry) => {
        const element = document.querySelector(`[data-atlog-entry-id="${CSS.escape(entry.id)}"]`) as HTMLElement | null;
        element?.scrollIntoView({ block: 'center', behavior: 'smooth' });
      }}
      aiPanel={aiPanel}
      refreshToken={`${aiEvidenceRevision}:${acceptedAiCaseId || 0}`}
    />}
    {caseEditorOpen && aiResult && <AbnormalCaseEditorDialog
      key={`ai-case-editor-${aiJob?.job_id || 'persisted'}-${aiEvidenceRevision}`}
      initialDraft={{
        name: aiResult.case_draft?.name || item.caseName || analysis.case_name,
        category: aiResult.case_draft?.category || aiResult.report.root_cause_category || '',
        symptom: aiResult.case_draft?.symptom || analysis.assertion_summary || analysis.conclusion || '',
        root_cause: aiResult.case_draft?.root_cause || aiResult.report.root_cause || '',
        solution: aiResult.case_draft?.solution || (aiResult.report.recommendations || []).join('\n'),
        description: aiResult.case_draft?.description || aiResult.report.summary || item.caseDescription || '',
        tags: aiResult.case_draft?.tags || [],
        environment: analysis.environment?.environment_id || null,
        environment_name: analysis.environment?.environment_name || '',
        source_task_name: item.caseName || analysis.case_name,
        query_snapshot: { base_url: analysis.base_url, ai_job_id: aiJob?.job_id || '', ai_evidence_revision: aiEvidenceRevision, ai_confidence: aiResult.report.confidence, ai_evidence_count: aiResult.case_evidences?.length || 0, token_usage: aiResult.token_usage },
      }}
      presetEvidences={aiResult.case_evidences || []}
      entries={caseEntries}
      environmentId={analysis.environment?.environment_id || undefined}
      environmentName={analysis.environment?.environment_name || ''}
      sourceTaskName={item.caseName || analysis.case_name}
      errorRules={effectiveErrorRules}
      onClose={() => setCaseEditorOpen(false)}
      onSaved={(saved) => {
        setAcceptedAiCaseId(saved.id);
        setCaseEditorOpen(false);
        void matchAtLogKnowledge(analysis.base_url, aiAnomalyRules).then(setKnowledge).catch(() => undefined);
      }}
    />}
  </div>;
}

function FailureReasonStats({ total, passed, failed, pending, reasons, activeReason, onReason }: { total: number; passed: number; failed: number; pending: number; reasons: Array<{ category: string; count: number }>; activeReason: string; onReason: (reason: string) => void }) {
  const failureRate = total > 0 ? Math.round((failed / total) * 1000) / 10 : 0;
  return <div className="atlog-failure-reason-stats">
    <div className="atlog-failure-reason-summary"><strong>失败原因统计</strong><span>总计 <b>{total}</b></span><span>通过 <b className="passed">{passed}</b></span><span>失败 <b className="failed">{failed}</b></span><span>失败率 <b className="failed">{failureRate}%</b></span><span>原因分类 <b>{reasons.length}</b></span><span>待确认 <b>{pending}</b></span>{activeReason && <button type="button" onClick={() => onReason('')}>清除筛选 ×</button>}</div>
    <div className="atlog-failure-reason-buttons">
      {reasons.map((item) => <button type="button" className={activeReason === item.category ? 'active' : ''} key={item.category} onClick={() => onReason(activeReason === item.category ? '' : item.category)}>
        <strong>{item.category}</strong><span>{item.count} 用例</span>
      </button>)}
      {!reasons.length && <div className="atlog-failure-reason-empty">当前没有失败原因可统计。</div>}
    </div>
  </div>;
}

/** 「报告文件 URL 分析」：粘贴单个报告文件链接（.rpt/.xml/.html/.log/.ini/.xlsx）直接解析。 */
const REPORT_KIND_LABELS: Record<string, string> = {
  junit_xml: 'JUnit XML 报告',
  summary_xml: 'summary 报告',
  pytest_html: 'pytest HTML 报告',
  html: 'HTML 页面',
  cpd_report: 'CPD 测校报告',
  spreadsheet: '测校数据表格',
  ini: '配置 ini',
  xytest_log: 'xytest 日志',
  debug_log: '调试日志',
  event_log: '运行事件日志',
  log: '日志',
  text: '文本',
};

const CPD_FIELD_LABELS: Array<[string, string]> = [
  ['cpd_name', '测校名称'],
  ['operator', '操作员'],
  ['software_ver', '软件版本'],
  ['report_date', '报告日期'],
  ['report_time', '报告时刻'],
  ['start_time', '开始时间'],
  ['stop_time', '结束时间'],
  ['execution_time', '执行时长'],
  ['measure_log', '测校日志'],
  ['test_run_result', 'Test Run Result'],
  ['results_validation', 'Results Validation'],
  ['measurement_quality', 'Measurement Quality'],
  ['mcs_status', 'MCs Status'],
];

function formatReportBytes(size: number): string {
  if (!Number.isFinite(size) || size <= 0) return '0 B';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(2)} MB`;
}

function reportStatusMeta(status: string): { cls: string; text: string; icon: ReactNode } {
  if (status === 'passed') return { cls: 'passed', text: '判定通过', icon: <CheckCircle2 size={16}/> };
  if (status === 'failed') return { cls: 'failed', text: '判定失败', icon: <XCircle size={16}/> };
  return { cls: 'unknown', text: '未判定', icon: <FileSearch size={16}/> };
}

function ReportFileSectionTable({ section }: { section: AtLogReportSection }) {
  const table = section.table;
  if (table && table.length) {
    return <div className="atlog-report-file-table-wrap">
      <div className="atlog-mini-heading">{section.name || '数据表'} · {table.length} 行{section.truncated ? '（已截断）' : ''}</div>
      <div className="atlog-report-file-table-scroll">
        <table className="atlog-report-file-table">
          <thead><tr>{(section.columns || []).map((column, index) => <th key={`${column}-${index}`}>{column}</th>)}</tr></thead>
          <tbody>{table.slice(0, 200).map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, cellIndex) => {
            const text = String(cell ?? '');
            const flagged = text.trim().toUpperCase() === 'FAILED' || text.trim().toUpperCase() === 'NG';
            return <td className={flagged ? 'flagged' : ''} key={cellIndex}>{text}</td>;
          })}</tr>)}</tbody>
        </table>
      </div>
    </div>;
  }
  const rows = section.rows || [];
  if (!rows.length) return null;
  return <div className="atlog-report-file-kv">
    <div className="atlog-mini-heading">{section.name || '配置项'} · {rows.length} 项</div>
    <div className="atlog-report-file-kv-body">{rows.slice(0, 200).map((row, index) => <div key={index}><span>{String(row.key ?? '')}</span><code>{String(row.value ?? '')}</code></div>)}</div>
  </div>;
}

function ReportFilePanel({ result, onClose }: { result: AtLogReportFileAnalysis; onClose: () => void }) {
  const meta = reportStatusMeta(result.status);
  const cpd = result.cpd_report;
  const summary = result.summary;
  const pytest = result.pytest;
  // 只有报告类才有"断言"可言；日志类把首条异常行当断言展示会误导，交给下面异常行表即可
  const showsAssertion = ['junit_xml', 'summary_xml', 'pytest_html'].includes(result.kind);
  const summaryFacts = summary
    ? ([['tests', '用例数'], ['failures', '失败'], ['errors', '错误'], ['skipped', '跳过']] as const)
      .map(([key, label]) => [label, summary[key]] as const)
      .filter(([, value]) => value !== undefined && value !== null)
    : [];
  return <section className="atlog-report-file-panel">
    <header className="atlog-report-file-head">
      <span className={`atlog-result-pill ${meta.cls}`}>{meta.icon}{meta.text}</span>
      <strong title={result.file_name}>{result.file_name}</strong>
      <code>{REPORT_KIND_LABELS[result.kind] || result.kind}</code>
      {result.case_id && <code title="识别出的用例编号">{result.case_id}</code>}
      <span className="atlog-report-file-metric">行数 {result.line_count}</span>
      <span className="atlog-report-file-metric">大小 {formatReportBytes(result.char_count)}</span>
      <a href={result.url} target="_blank" rel="noreferrer">原文<ExternalLink size={13}/></a>
      <span className="spacer"/>
      <button type="button" onClick={onClose} aria-label="关闭报告文件分析结果"><XCircle size={15}/></button>
    </header>
    <p className="atlog-report-file-conclusion">{result.conclusion}</p>
    {result.assertion && showsAssertion && <div className="atlog-raw-assertion inline"><span>断言</span><code>{result.assertion}</code></div>}
    {cpd && <div className="atlog-report-file-kv">
      <div className="atlog-mini-heading">CPD 测校报告字段</div>
      <div className="atlog-report-file-kv-body">{CPD_FIELD_LABELS
        .filter(([key]) => cpd[key] !== undefined && cpd[key] !== null && String(cpd[key]).trim() !== '')
        .map(([key, label]) => <div key={key}><span>{label}</span><code>{String(cpd[key])}</code></div>)}</div>
    </div>}
    {summaryFacts.length > 0 && <div className="atlog-report-file-facts">{summaryFacts.map(([label, value]) => <span key={label}>{label} <b>{String(value)}</b></span>)}</div>}
    {(pytest?.call_chain?.length || 0) > 0 && <div className="atlog-report-file-chain">
      <div className="atlog-mini-heading">失败调用链</div>
      <ol>{(pytest?.call_chain || []).map((frame, index) => <li key={index}><code>{frame.file}:{frame.line}</code>{frame.function && <span> in {frame.function}</span>}</li>)}</ol>
    </div>}
    {result.findings.length > 0 && <div className="atlog-report-file-findings">
      <div className="atlog-mini-heading">异常行 · {result.findings.length}</div>
      <div className="atlog-report-file-table-scroll">
        <table className="atlog-report-file-table findings">
          <thead><tr><th>时间</th><th>级别</th><th>说明</th></tr></thead>
          <tbody>{result.findings.slice(0, 100).map((item, index) => <tr key={index}>
            <td>{item.time || '—'}</td><td className="flagged">{item.level}</td><td>{item.message}</td>
          </tr>)}</tbody>
        </table>
      </div>
    </div>}
    {result.sections.filter((section) => section.table?.length || section.rows?.length).map((section, index) => <ReportFileSectionTable section={section} key={index}/>)}
    {result.text_excerpt && <details className="atlog-report-file-excerpt"><summary>原文摘录（{result.text_excerpt.split('\n').length} 行）</summary><pre>{result.text_excerpt}</pre></details>}
    {result.notes.length > 0 && <ul className="atlog-report-file-notes">{result.notes.map((note, index) => <li key={index}><AlertTriangle size={13}/>{note}</li>)}</ul>}
  </section>;
}

export function AtLogAnalysisPage({ TimelineComponent, errorRules, onOpenEnvironmentCpdReports, onOpenEnvironmentLogLocator }: AtLogAnalysisPageProps) {
  const [singleUrl, setSingleUrl] = useState(() => ATLOG_PAGE_CACHE.singleUrl);
  const [workbooks, setWorkbooks] = useState<ImportedWorkbook[]>(() => {
    const cached = ATLOG_PAGE_CACHE.workbooks?.length ? ATLOG_PAGE_CACHE.workbooks : [{ id: MANUAL_WORKBOOK_ID, name: '手工用例', cases: [] }];
    return cached.map((book) => ({ ...book, cases: book.cases.map((item) => ({ ...item, phase: item.phase === 'analyzing' && !item.analysis ? 'pending' : item.phase })) }));
  });
  const [activeWorkbookId, setActiveWorkbookId] = useState(() => ATLOG_PAGE_CACHE.activeWorkbookId || MANUAL_WORKBOOK_ID);
  const [importError, setImportError] = useState('');
  const [reportResult, setReportResult] = useState<AtLogReportFileAnalysis | null>(null);
  const [reportError, setReportError] = useState('');
  const [reportRunning, setReportRunning] = useState(false);
  const [batchRunning, setBatchRunning] = useState(false);
  const [query, setQuery] = useState(() => ATLOG_PAGE_CACHE.query);
  const [statusFilter, setStatusFilter] = useState<'all' | 'failed' | 'passed' | 'pending'>(() => ATLOG_PAGE_CACHE.statusFilter);
  const [reasonFilter, setReasonFilter] = useState(() => ATLOG_PAGE_CACHE.reasonFilter);
  const [formatRules, setFormatRules] = useState<LogFormatParserRuleConfig[]>([]);
  const [aiCompletedCaseKeys, setAiCompletedCaseKeys] = useState<Set<string>>(() => new Set());
  const fileInputRef = useRef<HTMLInputElement>(null);
  const lastAutoUrlRef = useRef('');
  const caseRefs = useRef(new Map<string, HTMLElement>());

  const activeWorkbook = useMemo(() => workbooks.find((item) => item.id === activeWorkbookId) || workbooks[0], [workbooks, activeWorkbookId]);
  const cases = activeWorkbook?.cases || [];

  const updateWorkbookCases = (workbookId: string, updater: ImportedCase[] | ((current: ImportedCase[]) => ImportedCase[])) => {
    setWorkbooks((current) => current.map((book) => {
      if (book.id !== workbookId) return book;
      const next = typeof updater === 'function' ? updater(book.cases) : updater;
      return { ...book, cases: next };
    }));
  };
  const setCases = (updater: ImportedCase[] | ((current: ImportedCase[]) => ImportedCase[])) => updateWorkbookCases(activeWorkbook?.id || MANUAL_WORKBOOK_ID, updater);

  useEffect(() => { void listRuntimeLogFormatRules().then(setFormatRules).catch(() => setFormatRules([])); }, []);  useEffect(() => {
    ATLOG_PAGE_CACHE = { singleUrl, workbooks, activeWorkbookId, query, statusFilter, reasonFilter };
    try { localStorage.setItem('tracelens-atlog-tabs-v1', JSON.stringify(ATLOG_PAGE_CACHE)); }
    catch { setImportError('浏览器存储空间不足，用例仍在当前页面；刷新前请保留用例 URL。'); }
  }, [singleUrl, workbooks, activeWorkbookId, query, statusFilter, reasonFilter]);

  const analyzeItems = async (items: ImportedCase[], workbookId: string) => {
    if (!items.length) return;
    setBatchRunning(true);
    updateWorkbookCases(workbookId, (current) => current.map((row) => items.some((item) => item.key === row.key) ? { ...row, phase: 'analyzing', error: undefined } : row));
    let cursor = 0;
    const worker = async () => {
      while (cursor < items.length) {
        const target = items[cursor++];
        try {
          const analysis = await analyzeAtLogCase(target.url, { case_id: target.caseId, case_name: target.caseName, case_description: target.caseDescription || '' });
          updateWorkbookCases(workbookId, (current) => current.map((row) => row.key === target.key ? { ...row, phase: 'done', analysis, caseId: row.caseId || analysis.case_id, caseName: row.caseName || analysis.case_name, error: undefined } : row));
        } catch (exc) {
          const message = exc instanceof Error ? exc.message : String(exc);
          updateWorkbookCases(workbookId, (current) => current.map((row) => row.key === target.key ? { ...row, phase: 'error', error: message } : row));
        }
      }
    };
    try { await Promise.all(Array.from({ length: Math.min(BATCH_CONCURRENCY, items.length) }, worker)); }
    finally { setBatchRunning(false); }
  };

  useEffect(() => {
    workbooks.forEach((book) => {
      const pending = book.cases.filter((item) => item.phase === 'pending' && !item.analysis);
      if (pending.length) void analyzeItems(pending, book.id);
    });
    // Resume only the in-memory workspace snapshot once on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const addSingle = (url: string) => {
    const manual = workbooks.find((book) => book.id === MANUAL_WORKBOOK_ID) || { id: MANUAL_WORKBOOK_ID, name: '手工用例', cases: [] };
    const existing = manual.cases.find((row) => row.url === url);
    if (!workbooks.some((book) => book.id === MANUAL_WORKBOOK_ID)) setWorkbooks((current) => [manual, ...current]);
    setActiveWorkbookId(MANUAL_WORKBOOK_ID);
    if (existing) {
      updateWorkbookCases(MANUAL_WORKBOOK_ID, (current) => current.map((row) => row.key === existing.key ? { ...row, expanded: true } : { ...row, expanded: false }));
      return;
    }
    const caseId = caseIdFromUrl(url);
    const item: ImportedCase = { key: `${Date.now()}-${Math.random().toString(36).slice(2)}`, caseName: caseId, caseId, url, phase: 'pending', expanded: true };
    updateWorkbookCases(MANUAL_WORKBOOK_ID, (current) => [item, ...current.map((row) => ({ ...row, expanded: false }))]);
    void analyzeItems([item], MANUAL_WORKBOOK_ID);
  };

  useEffect(() => {
    const raw = singleUrl.trim();
    if (!raw) return;
    // A report file keeps its exact URL; a case URL goes through the normaliser.
    const target = looksLikeReportFile(raw) ? raw : normalizeCaseUrl(raw);
    if (!target || target === lastAutoUrlRef.current) return;
    const timer = window.setTimeout(() => {
      lastAutoUrlRef.current = target;
      setImportError('');
      setReportError('');
      if (looksLikeReportFile(target)) {
        void analyzeReportFileUrl(target);
      } else {
        addSingle(target);
      }
    }, 650);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [singleUrl]);

  const importWorkbook = async (file: File) => {
    setImportError('');
    const debug: Record<string, unknown> = {
      file_name: file.name,
      file_size: file.size,
      last_modified: file.lastModified,
      sheets: [],
      selected_sheet: '',
      header_scan: [],
      header_row: 0,
      indexes: {},
      headers: {},
      row_samples: [],
      accepted_count: 0,
      rejected_counts: {},
      fallback_scan: false,
      error: '',
    };
    const rowSamples: Array<Record<string, unknown>> = [];
    const rejectedCounts: Record<string, number> = {};
    const addRejected = (reason: string) => { rejectedCounts[reason] = (rejectedCounts[reason] || 0) + 1; };
    const addSample = (sample: Record<string, unknown>) => {
      if (rowSamples.length < 60) rowSamples.push(sample);
    };
    const rejectionReason = (candidate: string): string => {
      const text = String(candidate || '').trim();
      if (!text) return 'empty';
      try {
        const parsed = new URL(text);
        if (!/^https?:$/.test(parsed.protocol)) return `unsupported-protocol:${parsed.protocol || 'unknown'}`;
        return 'normalization-rejected';
      } catch {
        return /^https?:/i.test(text) ? 'invalid-http-url' : 'not-http-url';
      }
    };
    const reportDebug = (error = '') => {
      debug.row_samples = rowSamples;
      debug.rejected_counts = rejectedCounts;
      debug.error = error;
      try {
        console.groupCollapsed(`[ATLog Excel Import] ${file.name}`);
        console.info('summary', debug);
        if (Array.isArray(debug.header_scan)) console.table(debug.header_scan);
        console.table(rowSamples);
        console.groupEnd();
      } catch { /* console diagnostics are best-effort only */ }
      void reportAtLogExcelImportDebug(debug).catch((reportError) => {
        console.warn('[ATLog Excel Import] failed to report diagnostics to backend', reportError);
      });
    };
    try {
      const workbook = new ExcelJS.Workbook();
      await workbook.xlsx.load(await file.arrayBuffer() as never);
      debug.sheets = workbook.worksheets.map((item) => ({ name: item.name, row_count: item.rowCount, column_count: item.columnCount }));
      const sheet = workbook.worksheets[0];
      if (!sheet) throw new Error(`${file.name} 中没有可读取的工作表。`);
      debug.selected_sheet = sheet.name;

      let headerRow = 0;
      let indexes = { caseName: 0, caseId: 0, caseDescription: 0, url: 0 };
      const headerScan: Array<Record<string, unknown>> = [];
      for (let rowNumber = 1; rowNumber <= Math.min(sheet.rowCount, 30); rowNumber += 1) {
        const row = sheet.getRow(rowNumber);
        const rawValues = Array.from({ length: Math.max(row.cellCount, 1) }, (_, index) => cellText(row.getCell(index + 1).value));
        const values = rawValues.map(normalizedHeader);
        const find = (aliases: string[]) => values.findIndex((value) => aliases.map(normalizedHeader).includes(value)) + 1;
        const urlIndex = rawValues.findIndex((value) => looksLikeUrlHeader(value)) + 1;
        const urlLikeColumns = rawValues.flatMap((value, index) => looksLikeUrlHeader(value) ? [index + 1] : []);
        if (rowNumber <= 12 || urlLikeColumns.length) {
          headerScan.push({
            row_number: rowNumber,
            values: rawValues.slice(0, 30).map((value) => String(value || '').slice(0, 160)),
            url_like_columns: urlLikeColumns,
          });
        }
        if (urlIndex) {
          headerRow = rowNumber;
          indexes = {
            caseName: find(HEADER_ALIASES.caseName),
            caseId: find(HEADER_ALIASES.caseId),
            caseDescription: find(HEADER_ALIASES.caseDescription),
            url: urlIndex,
          };
          break;
        }
      }
      debug.header_scan = headerScan;
      debug.header_row = headerRow;
      debug.indexes = indexes;
      if (headerRow) {
        const row = sheet.getRow(headerRow);
        debug.headers = {
          case_name: indexes.caseName ? cellText(row.getCell(indexes.caseName).value) : '',
          case_id: indexes.caseId ? cellText(row.getCell(indexes.caseId).value) : '',
          case_description: indexes.caseDescription ? cellText(row.getCell(indexes.caseDescription).value) : '',
          url: indexes.url ? cellText(row.getCell(indexes.url).value) : '',
        };
      } else {
        debug.fallback_scan = true;
      }

      const seen = new Set<string>();
      const imported: ImportedCase[] = [];
      const firstDataRow = headerRow ? headerRow + 1 : 1;
      for (let rowNumber = firstDataRow; rowNumber <= sheet.rowCount; rowNumber += 1) {
        const row = sheet.getRow(rowNumber);
        let selectedColumn = indexes.url || 0;
        let detail = selectedColumn ? urlCellDetail(row.getCell(selectedColumn).value) : { raw: '', extracted: '', source: 'no-url-column' };
        let url = normalizeCaseUrl(detail.extracted);

        // Fallback: scan every cell in this row for a real ATLog URL. This handles
        // unconventional headers, merged cells, HYPERLINK() formulas, and files
        // where the URL column moved without updating its title.
        if (!url) {
          for (let column = 1; column <= Math.max(row.cellCount, sheet.columnCount); column += 1) {
            if (column === selectedColumn) continue;
            const candidateDetail = urlCellDetail(row.getCell(column).value);
            const candidateUrl = normalizeCaseUrl(candidateDetail.extracted);
            if (!candidateUrl) continue;
            selectedColumn = column;
            detail = candidateDetail;
            url = candidateUrl;
            break;
          }
        }

        if (!url) {
          const reason = rejectionReason(detail.extracted);
          addRejected(reason);
          if (detail.raw || detail.extracted || rowNumber <= firstDataRow + 8) {
            addSample({
              row_number: rowNumber,
              column: selectedColumn,
              source: detail.source,
              raw: detail.raw,
              extracted: detail.extracted,
              normalized: '',
              reason,
              case_id: indexes.caseId ? cellText(row.getCell(indexes.caseId).value) : '',
            });
          }
          continue;
        }
        if (seen.has(url)) {
          addRejected('duplicate');
          addSample({ row_number: rowNumber, column: selectedColumn, source: detail.source, raw: detail.raw, extracted: detail.extracted, normalized: url, reason: 'duplicate', case_id: indexes.caseId ? cellText(row.getCell(indexes.caseId).value) : '' });
          continue;
        }
        seen.add(url);
        const caseId = indexes.caseId ? cellText(row.getCell(indexes.caseId).value) : caseIdFromUrl(url);
        const caseName = indexes.caseName ? cellText(row.getCell(indexes.caseName).value) : caseId;
        const caseDescription = indexes.caseDescription ? cellText(row.getCell(indexes.caseDescription).value) : '';
        imported.push({ key: `${Date.now()}-${rowNumber}-${Math.random().toString(36).slice(2)}`, caseName: caseName || caseIdFromUrl(url), caseId: caseId || caseIdFromUrl(url), caseDescription, url, phase: 'pending' });
        addSample({ row_number: rowNumber, column: selectedColumn, source: detail.source, raw: detail.raw, extracted: detail.extracted, normalized: url, reason: 'accepted', case_id: caseId || caseIdFromUrl(url) });
      }
      debug.accepted_count = imported.length;
      debug.rejected_counts = rejectedCounts;
      if (!imported.length) {
        const error = `${file.name} 中没有解析到有效 ATLog 用例链接。请查看后端 [ATLog Excel Import] 详细日志。`;
        reportDebug(error);
        throw new Error(error);
      }
      const workbookId = `excel-${Date.now()}-${Math.random().toString(36).slice(2)}`;
      const name = file.name.replace(/\.(?:xlsx|xlsm)$/i, '') || `用例文件 ${workbooks.length}`;
      setWorkbooks((current) => [...current, { id: workbookId, name, cases: imported }]);
      setActiveWorkbookId(workbookId); setReasonFilter(''); setStatusFilter('all'); setQuery('');
      reportDebug('');
      void analyzeItems(imported, workbookId);
    } catch (exc) {
      const message = exc instanceof Error ? exc.message : String(exc);
      if (!String(debug.error || '')) reportDebug(message);
      setImportError(message);
    }
  };

  const handleDrop = (event: DragEvent<HTMLElement>) => {
    event.preventDefault();
    const files = Array.from(event.dataTransfer.files || []).filter((file) => /\.(?:xlsx|xlsm)$/i.test(file.name));
    if (files.length) { files.forEach((file) => void importWorkbook(file)); return; }
    const url = normalizeCaseUrl(event.dataTransfer.getData('text/plain'));
    if (url) { setSingleUrl(url); lastAutoUrlRef.current = url; addSingle(url); }
  };

  /** 单文件报告分析：直接把 URL 交给后端按类型分派解析，不进入用例列表。 */
  const analyzeReportFileUrl = async (explicitTarget?: string) => {
    const target = (explicitTarget ?? singleUrl).trim();
    if (!target) { setReportError('请粘贴报告文件的完整 URL（如 http://…/summary_report.xml）。'); return; }
    setReportRunning(true);
    setReportError('');
    try {
      const result = await analyzeAtLogReportFile(target);
      setReportResult(result);
    } catch (error) {
      setReportResult(null);
      setReportError(error instanceof Error ? error.message : String(error));
    } finally {
      setReportRunning(false);
    }
  };

  const closeWorkbook = (id: string) => {
    if (id === MANUAL_WORKBOOK_ID) return;
    setWorkbooks((current) => current.filter((book) => book.id !== id));
    if (activeWorkbookId === id) setActiveWorkbookId(MANUAL_WORKBOOK_ID);
  };

  const toggleExpanded = (key: string) => {
    const target = cases.find((item) => item.key === key);
    const opening = !target?.expanded;
    setCases((current) => current.map((item) => item.key === key ? { ...item, expanded: opening } : { ...item, expanded: false }));
    if (opening) window.requestAnimationFrame(() => window.requestAnimationFrame(() => caseRefs.current.get(key)?.scrollIntoView({ block: 'start', behavior: 'smooth' })));
  };
  const markAiConclusionReady = (key: string) => {
    setAiCompletedCaseKeys((current) => {
      if (current.has(key)) return current;
      const next = new Set(current);
      next.add(key);
      return next;
    });
  };
  const stats = useMemo(() => ({ total: cases.length, passed: cases.filter((item) => item.analysis?.status === 'passed').length, failed: cases.filter((item) => item.analysis?.status === 'failed').length, pending: cases.filter((item) => item.phase === 'pending' || item.phase === 'analyzing' || item.phase === 'error' || item.analysis?.status === 'unknown').length }), [cases]);
  const reasonStats = useMemo(() => { const counts = new Map<string, number>(); cases.forEach((item) => { if (item.analysis?.status !== 'failed') return; const category = item.analysis.reason_category || '未归类'; counts.set(category, (counts.get(category) || 0) + 1); }); return Array.from(counts.entries()).map(([category, count]) => ({ category, count })).sort((a, b) => b.count - a.count || a.category.localeCompare(b.category)); }, [cases]);
  const filteredCases = useMemo(() => { const needle = query.trim().toLowerCase(); return cases.filter((item) => { if (statusFilter === 'failed' && item.analysis?.status !== 'failed') return false; if (statusFilter === 'passed' && item.analysis?.status !== 'passed') return false; if (statusFilter === 'pending' && !(item.phase === 'pending' || item.phase === 'analyzing' || item.phase === 'error' || item.analysis?.status === 'unknown')) return false; if (reasonFilter && item.analysis?.reason_category !== reasonFilter) return false; if (!needle) return true; return `${item.caseName} ${item.caseId} ${item.analysis?.reason_category || ''} ${item.analysis?.assertion_summary || ''}`.toLowerCase().includes(needle); }); }, [cases, query, statusFilter, reasonFilter]);

  const expandedCase = useMemo(() => cases.find((item) => item.expanded), [cases]);

  useEffect(() => {
    const handler = (event: Event) => {
      const custom = event as CustomEvent<{ context?: Record<string, unknown> }>;
      const context = custom.detail?.context;
      if (!context) return;
      context.page = 'atlog';
      context.page_label = '用例分析';
      const existingPage = context.atlog_page && typeof context.atlog_page === 'object' ? context.atlog_page as Record<string, unknown> : {};
      const existingExpanded = existingPage.expanded_case && typeof existingPage.expanded_case === 'object' ? existingPage.expanded_case as Record<string, unknown> : {};
      context.atlog_page = {
        ...existingPage,
        workbook: activeWorkbook ? { id: activeWorkbook.id, name: activeWorkbook.name, case_count: cases.length } : null,
        filters: { query, status: statusFilter, reason: reasonFilter },
        visible_case_count: filteredCases.length,
        stats,
        expanded_case: expandedCase ? {
          ...existingExpanded,
          key: expandedCase.key,
          case_id: expandedCase.caseId,
          case_name: expandedCase.caseName,
          description: expandedCase.caseDescription || '',
          url: expandedCase.url,
          case_url: String(existingExpanded.case_url || expandedCase.analysis?.base_url || expandedCase.url || ''),
          phase: expandedCase.phase,
          status: expandedCase.analysis?.status || 'unknown',
          reason_category: expandedCase.analysis?.reason_category || '',
          assertion_summary: expandedCase.analysis?.assertion_summary || '',
          conclusion: expandedCase.analysis?.conclusion || '',
          failure_time: expandedCase.analysis?.failure_time || '',
          start_time: String(existingExpanded.start_time || expandedCase.analysis?.start_time || expandedCase.analysis?.event_start_time || ''),
          end_time: String(existingExpanded.end_time || expandedCase.analysis?.end_time || expandedCase.analysis?.event_end_time || ''),
          environment_name: expandedCase.analysis?.environment?.environment_name || '',
          environment_id: expandedCase.analysis?.environment?.environment_id || null,
          ai_conclusion_ready: Boolean(persistedAiResult(expandedCase.analysis) || ATLOG_CASE_UI_CACHE.get(expandedCase.analysis?.base_url || expandedCase.url)?.aiResult || aiCompletedCaseKeys.has(expandedCase.key)),
        } : null,
        visible_cases: filteredCases.slice(0, 8).map((item) => ({
          case_id: item.caseId,
          case_name: item.caseName,
          status: item.analysis?.status || item.phase,
          reason: item.analysis?.reason_category || '',
          failure_time: item.analysis?.failure_time || '',
        })),
      };
      const liveExpanded = context.atlog_page && typeof context.atlog_page === 'object'
        ? (context.atlog_page as Record<string, unknown>).expanded_case
        : undefined;
      if (liveExpanded && typeof liveExpanded === 'object') context.selected_atlog_case = liveExpanded;
    };
    return registerPageContextReader(handler, 10);
  }, [activeWorkbook, cases.length, query, statusFilter, reasonFilter, filteredCases, stats, expandedCase, aiCompletedCaseKeys]);

  return <main className="atlog-analysis-page compact-v2 atlog-page-drop-zone" onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; }} onDrop={handleDrop}>
    {/* One ingest row: paste a link or drop a report anywhere on the page. The URL's shape
        decides whether it is an ATLog case or a report artefact, so there is nothing to pick. */}
    <section className="atlog-ingest-bar">
      <div className="atlog-url-ingest">
        <FileSearch size={17}/>
        <input
          value={singleUrl}
          onChange={(event) => setSingleUrl(event.target.value)}
          onKeyDown={(event) => {
            if (event.key !== 'Enter') return;
            event.preventDefault();
            const raw = singleUrl.trim();
            if (!raw) return;
            if (looksLikeReportFile(raw)) { lastAutoUrlRef.current = raw; void analyzeReportFileUrl(raw); }
            else { const url = normalizeCaseUrl(raw); if (url) { lastAutoUrlRef.current = url; addSingle(url); } }
          }}
          placeholder="粘贴 ATLog 用例 URL 或报告文件 URL（.rpt / summary_report.xml / xytest.log / event.log / .html / .ini / .xlsx），或直接把文件拖到页面"
          aria-label="用例或报告链接"
        />
      </div>
      {reportRunning && <span className="atlog-auto-running"><LoaderCircle className="spin" size={15}/> 解析报告</span>}
      {batchRunning && <span className="atlog-auto-running"><LoaderCircle className="spin" size={15}/> 自动分析中</span>}
      <input ref={fileInputRef} className="visually-hidden" type="file" accept=".xlsx,.xlsm" multiple onChange={(event) => { const files = Array.from(event.target.files || []); files.forEach((file) => void importWorkbook(file)); event.currentTarget.value = ''; }}/>
      <button type="button" className="atlog-drop-button" onClick={() => fileInputRef.current?.click()} title="选择 Excel 用例文件（也可直接把文件拖到页面任意位置）">
        <FileSpreadsheet size={17}/><span>选择 Excel</span>
      </button>
    </section>
    {reportError && <div className="atlog-inline-error"><AlertTriangle size={16}/>{reportError}</div>}
    {reportResult && <ReportFilePanel result={reportResult} onClose={() => { setReportResult(null); setReportError(''); }}/>}
    {importError && <div className="atlog-inline-error"><AlertTriangle size={16}/>{importError}</div>}

    <nav className="atlog-workbook-tabs" aria-label="已导入用例文件">
      {workbooks.map((book) => <button type="button" className={book.id === activeWorkbook?.id ? 'active' : ''} onClick={() => { setActiveWorkbookId(book.id); setReasonFilter(''); }} key={book.id}>
        <FileSpreadsheet size={14}/><span>{book.name}</span><small>{book.cases.length}</small>{book.id !== MANUAL_WORKBOOK_ID && <i role="button" tabIndex={0} aria-label={`关闭 ${book.name}`} onClick={(event) => { event.stopPropagation(); closeWorkbook(book.id); }} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); event.stopPropagation(); closeWorkbook(book.id); } }}><XCircle size={13}/></i>}
      </button>)}
      <span className="atlog-workbook-drop-hint"><UploadCloud size={14}/> 可把多个 Excel 直接拖到页面任意位置</span>
    </nav>

    {cases.length > 0 && <section className="atlog-failure-reason-panel"><FailureReasonStats total={stats.total} passed={stats.passed} failed={stats.failed} pending={stats.pending} reasons={reasonStats} activeReason={reasonFilter} onReason={(reason) => { setReasonFilter(reason); if (reason) setStatusFilter('failed'); }}/></section>}

    {cases.length > 0 && <section className="atlog-list-toolbar compact"><label><Search size={15}/><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索用例或结论"/></label><div className="atlog-status-filter">{([['all', '全部'], ['failed', '失败'], ['passed', '通过'], ['pending', '待确认']] as const).map(([value, label]) => <button type="button" className={statusFilter === value ? 'active' : ''} onClick={() => setStatusFilter(value)} key={value}>{label}</button>)}</div>{reasonFilter && <button type="button" className="atlog-active-reason-filter" onClick={() => setReasonFilter('')}>{reasonFilter} ×</button>}<span>{filteredCases.length} / {cases.length}</span></section>}

    <section className="atlog-case-list compact">
      {filteredCases.map((item) => {
        const fullCaseName = item.caseName || item.caseId || '未命名用例';
        const caseConclusion = item.analysis?.assertion_summary || item.analysis?.conclusion || item.error || (item.phase === 'pending' ? '等待自动分析' : '正在解析报告…');
        const aiAnalyzed = Boolean(persistedAiResult(item.analysis) || ATLOG_CASE_UI_CACHE.get(item.analysis?.base_url || item.url)?.aiResult || aiCompletedCaseKeys.has(item.key));
        return <article ref={(node) => { if (node) caseRefs.current.set(item.key, node); else caseRefs.current.delete(item.key); }} className={`atlog-case-item ${statusClass(item)} ${item.expanded ? 'expanded' : ''}`} key={item.key}>
          <div className="atlog-case-summary-wrap">
            <button type="button" className="atlog-case-summary" onClick={() => toggleExpanded(item.key)} title={item.url}><span className="atlog-case-toggle">{item.expanded ? <ChevronDown size={17}/> : <ChevronRight size={17}/>}</span><span className={`atlog-case-status ${statusClass(item)}`}>{statusIcon(item)} {statusLabel(item)}</span><span className="atlog-case-identity" title={fullCaseName}><strong title={fullCaseName}>{fullCaseName}</strong>{item.caseId && item.caseName !== item.caseId && <code title={item.caseId}>{item.caseId}</code>}</span><span className="atlog-case-conclusion">{item.analysis?.status === 'failed' && item.analysis.reason_category && <em className="atlog-case-error-point" title={item.analysis.reason_category}>{item.analysis.reason_category}</em>}{item.analysis?.failure_time && <span className="atlog-case-time">{item.analysis.failure_time}</span>}<span className="atlog-case-check-result" title={caseConclusion}>{caseConclusion}</span></span></button>
            <button type="button" className={`atlog-case-ai-state ${aiAnalyzed ? 'completed' : 'pending'}`} onClick={(event) => { event.stopPropagation(); if (!item.expanded) toggleExpanded(item.key); }} title={aiAnalyzed ? '智能分析已有结论，点击展开用例查看' : '待智能分析，点击展开用例后可执行智能分析'}>{aiAnalyzed ? <><Check size={13}/> OK</> : '待分析'}</button>
            <a className="atlog-case-origin-link" href={item.url} target="_blank" rel="noreferrer" title="打开原始 ATLog 用例路径" aria-label={`打开 ${item.caseId || item.caseName} 原始 ATLog 路径`}><ExternalLink size={15}/></a>
          </div>
          {item.expanded && <CaseExpandedPanel item={item} formatRules={formatRules} TimelineComponent={TimelineComponent} errorRules={errorRules} onAiConclusionReady={() => markAiConclusionReady(item.key)} onOpenEnvironmentLogLocator={onOpenEnvironmentLogLocator} onOpenEnvironmentCpdReports={onOpenEnvironmentCpdReports}/>} 
        </article>;
      })}
      {cases.length === 0 && <div className="atlog-empty-state compact"><UploadCloud size={28}/><strong>{activeWorkbook?.id === MANUAL_WORKBOOK_ID ? '粘贴一个用例链接，或把 Excel 拖到页面任意位置' : `${activeWorkbook?.name || '当前文件'} 暂无用例`}</strong><span>多个 Excel 会分别保留在上方 Tab 中，切换后不会丢失各自的分析结果。</span></div>}
      {cases.length > 0 && filteredCases.length === 0 && <div className="atlog-empty-inline">当前筛选条件下没有用例。</div>}
    </section>
  </main>;
}

