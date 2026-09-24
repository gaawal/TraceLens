import { registerPageContextReader } from '../assistant/contextRegistry';
import { CpdDataDialog } from './CpdDataDialog';
import { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  AlertTriangle, Check, ChevronDown, ChevronRight, FileSearch, FileText,
  CalendarDays, FolderTree, LayoutGrid, List, LoaderCircle, RefreshCw, Search, X,
} from 'lucide-react';
import {
  getCpdReportContent, getCpdReportDataset,
  type CpdReportDataset, type CpdReportSummary, type CpdReportTree, type EnvironmentSummary,
} from '../api/resourceApi';

interface Props {
  environment: EnvironmentSummary;
  onLocateLogs: (payload: { environment: EnvironmentSummary; subsystem: string; module: string; startTime?: string; endTime?: string; taskName?: string }) => void;
}

type ViewMode = 'overview' | 'grouped';
interface ReportFilters {
  subsystem: string;
  module: string;
  result: string;
  validation: string;
  quality: string;
  mcs: string;
  startTime: string;
  endTime: string;
}
interface UiState {
  viewMode: ViewMode;
  filters: ReportFilters;
  quickRange: string;
  appliedStart: string;
  appliedEnd: string;
  page: number;
  pageSize: number;
}

const datasetCache = new Map<string, { data: CpdReportDataset; loadedAt: number }>();
const contentCache = new Map<string, { file: string; path: string; text: string }>();
const uiStateCache = new Map<string, UiState>();
const MAX_REPORT_QUERY_RANGE_MS = 30 * 24 * 60 * 60 * 1000;

function pad(value: number): string { return String(value).padStart(2, '0'); }
function formatDateTime(date: Date): string {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}
function toNativePickerValue(value: string): string {
  const normalized = value.trim().replace(' ', 'T');
  const match = normalized.match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::(\d{2}))?/);
  return match ? `${match[1]}T${match[2]}:${match[3] || '00'}` : '';
}
function parseDateTimeMs(value: string): number | undefined {
  const normalized = value.trim().replace('T', ' ');
  const match = normalized.match(/^(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})(?::(\d{2}))?(?:\.(\d{1,9}))?$/);
  if (!match) return undefined;
  const [, year, month, day, hour, minute, second = '0', fraction = ''] = match;
  const date = new Date(
    Number(year), Number(month) - 1, Number(day), Number(hour), Number(minute), Number(second),
    Number(fraction.slice(0, 3).padEnd(3, '0') || '0'),
  );
  const valueMs = date.getTime();
  return Number.isNaN(valueMs) ? undefined : valueMs;
}
function rangeFilters(days: number): Pick<ReportFilters, 'startTime' | 'endTime'> {
  const end = new Date();
  const start = new Date(end.getTime() - days * 24 * 60 * 60 * 1000);
  return { startTime: formatDateTime(start), endTime: formatDateTime(end) };
}
function defaultFilters(): ReportFilters {
  const range = rangeFilters(7);
  return {
    subsystem: '', module: '', result: '', validation: '', quality: '', mcs: '',
    startTime: range.startTime, endTime: range.endTime,
  };
}
function resourceKey(environment: EnvironmentSummary): string { return `${environment.upper_machine.host}\u0000${environment.upper_machine.username}`; }
function rangeCacheKey(key: string, start: string, end: string): string { return JSON.stringify([key, start.trim(), end.trim()]); }
function reportStem(name: string): string { return name.replace(/\.rpt$/i, ''); }
function reportKey(report: CpdReportSummary): string { return `${report.subsystem || ''}/${report.module || ''}/${report.file_name}`; }
function resultTone(value?: string): string {
  const normalized = (value || '').toUpperCase();
  if (['OK', 'SUCCESS', 'PASSED', 'PASS'].includes(normalized)) return 'ok';
  if (['FAILED', 'FAIL', 'ERROR'].includes(normalized)) return 'failed';
  return 'neutral';
}
function unique(values: Array<string | undefined>): string[] {
  return [...new Set(values.filter((value): value is string => Boolean(value)))].sort();
}
function matchesFilter(actual: string | undefined, expected: string): boolean {
  return !expected || (actual || '') === expected;
}

function HierarchyPicker({ tree, filters, onChange }: { tree?: CpdReportTree; filters: ReportFilters; onChange: (patch: Partial<ReportFilters>) => void }) {
  const [open, setOpen] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [menuPosition, setMenuPosition] = useState({ top: 0, left: 0, width: 480 });
  const triggerRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  function syncMenuPosition() {
    const rect = triggerRef.current?.getBoundingClientRect();
    if (!rect) return;
    const width = Math.min(520, Math.max(460, rect.width + 150));
    const left = Math.max(10, Math.min(rect.left, window.innerWidth - width - 10));
    const availableBelow = window.innerHeight - rect.bottom - 12;
    const desiredHeight = Math.min(590, Math.max(280, availableBelow));
    setMenuPosition({ top: rect.bottom + 6, left, width });
    document.documentElement.style.setProperty('--cpd-hierarchy-max-height', `${desiredHeight}px`);
  }

  useEffect(() => {
    if (!open) return undefined;
    syncMenuPosition();
    const close = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!triggerRef.current?.contains(target) && !menuRef.current?.contains(target)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpen(false); };
    const reposition = () => syncMenuPosition();
    document.addEventListener('pointerdown', close, true);
    document.addEventListener('keydown', escape, true);
    window.addEventListener('resize', reposition);
    window.addEventListener('scroll', reposition, true);
    return () => {
      document.removeEventListener('pointerdown', close, true);
      document.removeEventListener('keydown', escape, true);
      window.removeEventListener('resize', reposition);
      window.removeEventListener('scroll', reposition, true);
    };
  }, [open]);

  const selected = filters.module ? `${filters.subsystem} / ${filters.module}` : filters.subsystem || '全部子系统/模块';
  const menu = open && typeof document !== 'undefined' ? createPortal(
    <div
      ref={menuRef}
      className="cpd-hierarchy-menu cpd-hierarchy-menu-large cpd-hierarchy-menu-portal"
      style={{ position: 'fixed', top: menuPosition.top, left: menuPosition.left, width: menuPosition.width }}
    >
      <button className="cpd-hierarchy-clear" type="button" onClick={() => { onChange({ subsystem: '', module: '' }); setOpen(false); }}>全部子系统/模块</button>
      {tree?.subsystems.map((sub) => {
        const isExpanded = expanded.has(sub.name) || sub.name === filters.subsystem;
        return <div key={sub.name} className="cpd-hierarchy-group">
          <button type="button" className="cpd-hierarchy-sub" onClick={() => setExpanded((current) => {
            const next = new Set(current); next.has(sub.name) ? next.delete(sub.name) : next.add(sub.name); return next;
          })}>
            <ChevronRight className={isExpanded ? 'expanded' : ''} size={15}/><strong>{sub.name}</strong><small>{sub.module_count} 模块 · {sub.report_count} 报告</small>
          </button>
          {isExpanded && <div>{sub.modules.map((item) => <button key={item.name} type="button" className={filters.subsystem === sub.name && filters.module === item.name ? 'selected' : ''} onClick={() => { onChange({ subsystem: sub.name, module: item.name }); setOpen(false); }}>
            <span className="cpd-module-dot"/><span><strong>{item.name}</strong><small>{item.report_count} 份报告</small></span>{filters.subsystem === sub.name && filters.module === item.name && <Check size={14}/>} 
          </button>)}</div>}
        </div>;
      })}
    </div>,
    document.body,
  ) : null;

  return <div className="cpd-hierarchy-picker" ref={triggerRef}>
    <button type="button" className="cpd-hierarchy-trigger" onClick={() => setOpen((value) => !value)}>
      <FolderTree size={16}/><span className="cpd-picker-label">子系统/模块</span><strong title={selected}>{selected}</strong><ChevronDown size={14}/>
    </button>
    {menu}
  </div>;
}

function CpdDateTimeInput({ value, label, onChange }: { value: string; label: string; onChange: (value: string) => void }) {
  const pickerRef = useRef<HTMLInputElement>(null);
  function openPicker() {
    const input = pickerRef.current;
    if (!input) return;
    input.value = toNativePickerValue(value);
    if (typeof input.showPicker === 'function') input.showPicker(); else input.click();
  }
  return <label className="remote-smart-time cpd-smart-time">
    <span>{label}</span>
    <div>
      <input type="text" value={value} onChange={(event) => onChange(event.target.value.replace('T', ' '))} placeholder="2026-05-01 11:05:00"/>
      <button type="button" onClick={openPicker} title={`选择${label}`}><CalendarDays size={14}/></button>
      <input ref={pickerRef} type="datetime-local" step="1" tabIndex={-1} aria-hidden="true" className="remote-native-time-picker" onChange={(event) => {
        const date = new Date(event.target.value);
        if (!Number.isNaN(date.getTime())) onChange(formatDateTime(date));
      }}/>
    </div>
  </label>;
}

export function CpdReportPage({ environment, onLocateLogs }: Props) {
  const key = resourceKey(environment);
  const initial = uiStateCache.get(key);
  const initialFilters = initial?.filters ?? defaultFilters();
  const initialStart = initial?.appliedStart ?? initialFilters.startTime;
  const initialEnd = initial?.appliedEnd ?? initialFilters.endTime;
  const initialCached = datasetCache.get(rangeCacheKey(key, initialStart, initialEnd));

  const [dataset, setDataset] = useState<CpdReportDataset | undefined>(initialCached?.data);
  const [busy, setBusy] = useState(initialCached ? '' : 'dataset');
  const [error, setError] = useState('');
  const [cpdData, setCpdData] = useState<CpdReportSummary>();

  const [content, setContent] = useState<{ file: string; path: string; text: string }>();
  const [viewMode, setViewMode] = useState<ViewMode>(initial?.viewMode ?? 'grouped');
  const [filters, setFilters] = useState<ReportFilters>(initialFilters);
  const [quickRange, setQuickRange] = useState(initial?.quickRange ?? '7天');
  const [appliedStart, setAppliedStart] = useState(initialStart);
  const [appliedEnd, setAppliedEnd] = useState(initialEnd);
  const [page, setPage] = useState(initial?.page ?? 1);
  const [pageSize, setPageSize] = useState(initial?.pageSize ?? 100);

  async function loadDataset(refresh = false, start = filters.startTime, end = filters.endTime) {
    const startText = start.trim();
    const endText = end.trim();
    if (!startText || !endText) { setError('开始时间和结束时间不能为空。'); return; }
    const startMs = parseDateTimeMs(startText);
    const endMs = parseDateTimeMs(endText);
    if (startMs === undefined || endMs === undefined) { setError('时间格式应为 YYYY-MM-DD HH:mm:ss。'); return; }
    if (endMs < startMs) { setError('结束时间不能早于开始时间。'); return; }
    if (endMs - startMs > MAX_REPORT_QUERY_RANGE_MS) { setError('CPD 报告查询时间范围最多支持 30 天。'); return; }
    const cacheKey = rangeCacheKey(key, startText, endText);
    const cached = datasetCache.get(cacheKey);
    if (!refresh && cached) {
      setDataset(cached.data);
      setAppliedStart(startText); setAppliedEnd(endText);
      setBusy(''); setError(''); setPage(1);
      return;
    }
    setBusy('dataset'); setError('');
    try {
      const value = await getCpdReportDataset(environment.id, startText, endText, refresh);
      datasetCache.set(cacheKey, { data: value, loadedAt: Date.now() });
      setDataset(value);
      setAppliedStart(startText); setAppliedEnd(endText);
      setPage(1);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy('');
    }
  }

  useEffect(() => {
    const state = uiStateCache.get(key);
    const nextFilters = state?.filters ?? defaultFilters();
    const nextStart = state?.appliedStart ?? nextFilters.startTime;
    const nextEnd = state?.appliedEnd ?? nextFilters.endTime;
    setFilters(nextFilters); setAppliedStart(nextStart); setAppliedEnd(nextEnd);
    setViewMode(state?.viewMode ?? 'grouped'); setQuickRange(state?.quickRange ?? '7天'); setPage(state?.page ?? 1); setPageSize(state?.pageSize ?? 100);
    const cached = datasetCache.get(rangeCacheKey(key, nextStart, nextEnd));
    if (cached) { setDataset(cached.data); setBusy(''); }
    else { setDataset(undefined); void loadDataset(false, nextStart, nextEnd); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  useEffect(() => {
    uiStateCache.set(key, { viewMode, filters, quickRange, appliedStart, appliedEnd, page, pageSize });
  }, [key, viewMode, filters, quickRange, appliedStart, appliedEnd, page, pageSize]);

  const tree = dataset?.range_tree;
  const allHistoryTree = dataset?.all_history_tree;
  const allRows = dataset?.results ?? [];
  const filteredRows = useMemo(() => allRows.filter((report) => {
    if (filters.subsystem && report.subsystem !== filters.subsystem) return false;
    if (filters.module && report.module !== filters.module) return false;
    if (!matchesFilter(report.test_run_result, filters.result)) return false;
    if (!matchesFilter(report.results_validation, filters.validation)) return false;
    if (!matchesFilter(report.measurement_quality, filters.quality)) return false;
    if (!matchesFilter(report.mcs_status, filters.mcs)) return false;
    return true;
  }), [allRows, filters.subsystem, filters.module, filters.result, filters.validation, filters.quality, filters.mcs]);

  const total = filteredRows.length;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const safePage = Math.min(page, totalPages);
  const pageRows = filteredRows.slice((safePage - 1) * pageSize, safePage * pageSize);
  const options = useMemo(() => ({
    result: unique(['OK', 'FAILED', 'UNKNOWN', ...allRows.map((item) => item.test_run_result)]),
    validation: unique(['OK', 'FAILED', 'UNKNOWN', ...allRows.map((item) => item.results_validation)]),
    quality: unique(['OK', 'FAILED', 'UNKNOWN', ...allRows.map((item) => item.measurement_quality)]),
    mcs: unique(['SAVED', 'NOT SAVED', 'UNKNOWN', ...allRows.map((item) => item.mcs_status)]),
  }), [allRows]);

  useEffect(() => { if (page > totalPages) setPage(totalPages); }, [page, totalPages]);


  useEffect(() => {
    const handler = (event: Event) => {
      const custom = event as CustomEvent<{ context?: Record<string, unknown> }>;
      const context = custom.detail?.context;
      if (!context) return;
      context.page = 'reports';
      context.page_label = 'CPD 测校报告';
      context.environment_id = environment.id;
      context.environment_name = environment.name;
      context.cpd_reports = {
        environment_id: environment.id,
        environment_name: environment.name,
        input_time_range: { start: filters.startTime, end: filters.endTime },
        applied_time_range: { start: appliedStart, end: appliedEnd },
        subsystem: filters.subsystem || '',
        module: filters.module || '',
        result: filters.result || '',
        validation: filters.validation || '',
        quality: filters.quality || '',
        mcs: filters.mcs || '',
        result_count: total,
        candidate_count: dataset?.candidate_count ?? 0,
        report_count: allHistoryTree?.report_count ?? 0,
        loading: busy === 'dataset',
        opened_report: content ? { file: content.file, path: content.path } : null,
        visible_reports: pageRows.slice(0, 8).map((report) =>
          `${report.start_time || '-'} | ${report.subsystem || '-'}/${report.module || '-'} | ${report.file_name} | result=${report.test_run_result || 'UNKNOWN'} | validation=${report.results_validation || '-'} | quality=${report.measurement_quality || '-'} | mcs=${report.mcs_status || '-'}`,
        ),
      };
    };
    return registerPageContextReader(handler, 20);
  }, [environment, filters, appliedStart, appliedEnd, total, dataset, allHistoryTree, busy, content, pageRows]);

  const patchFilters = (patch: Partial<ReportFilters>) => { setFilters((current) => ({ ...current, ...patch })); setPage(1); };

  function showCpdData(report: CpdReportSummary) { setCpdData(report); }

  async function showReport(report: CpdReportSummary) {
    const cacheKey = `${key}:${report.full_path}:${report.modified_at || ""}`;
    const existing = contentCache.get(cacheKey);
    if (existing) { setContent(existing); return; }
    if (!report.subsystem || !report.module) return;
    setBusy(`content:${report.file_name}`); setError('');
    try {
      const value = await getCpdReportContent(environment.id, report.subsystem, report.module, report.file_name);
      const next = { file: value.file_name, path: value.path, text: value.content };
      contentCache.set(cacheKey, next); setContent(next);
    } catch (exc) { setError(exc instanceof Error ? exc.message : String(exc)); }
    finally { setBusy(''); }
  }

  function applyQuickRange(days: number, label: string) {
    const range = rangeFilters(days);
    const next = { ...filters, ...range };
    setFilters(next); setQuickRange(label); setPage(1);
    void loadDataset(false, range.startTime, range.endTime);
  }

  function patchTime(patch: Partial<Pick<ReportFilters, 'startTime' | 'endTime'>>) {
    setQuickRange('');
    patchFilters(patch);
  }

  return <main className="cpd-report-page cpd-report-page-v014 cpd-report-page-v016 cpd-report-page-v018">
    {error && <div className="resource-alert error"><AlertTriangle size={16}/>{error}</div>}
    <section className="cpd-toolbar-v014">
      <div className="cpd-search-row cpd-search-row-rich cpd-search-row-v014 cpd-search-row-v018">
        <div className="cpd-view-switch">
          <button type="button" className={viewMode === 'overview' ? 'active' : ''} onClick={() => setViewMode('overview')}><List size={14}/> 全览</button>
          <button type="button" className={viewMode === 'grouped' ? 'active' : ''} onClick={() => setViewMode('grouped')}><LayoutGrid size={14}/> 按子系统/模块</button>
        </div>
        <HierarchyPicker tree={tree} filters={filters} onChange={patchFilters}/>
        <label className="cpd-top-select"><span>测校结果</span><select value={filters.result} onChange={(event) => patchFilters({ result: event.target.value })}><option value="">全部</option>{options.result.map((value) => <option key={value}>{value}</option>)}</select></label>
        <label className="cpd-top-select"><span>Validation</span><select value={filters.validation} onChange={(event) => patchFilters({ validation: event.target.value })}><option value="">全部</option>{options.validation.map((value) => <option key={value}>{value}</option>)}</select></label>
        <label className="cpd-top-select"><span>Quality</span><select value={filters.quality} onChange={(event) => patchFilters({ quality: event.target.value })}><option value="">全部</option>{options.quality.map((value) => <option key={value}>{value}</option>)}</select></label>
        <label className="cpd-top-select"><span>MCs</span><select value={filters.mcs} onChange={(event) => patchFilters({ mcs: event.target.value })}><option value="">全部</option>{options.mcs.map((value) => <option key={value}>{value}</option>)}</select></label>
        <div className="remote-range-presets cpd-range-presets">
          {[['1天', 1], ['7天', 7], ['30天', 30]].map(([label, days]) => <button key={String(label)} type="button" className={quickRange === label ? 'active' : ''} onClick={() => applyQuickRange(Number(days), String(label))}>{label}</button>)}
        </div>
        <CpdDateTimeInput label="开始" value={filters.startTime} onChange={(value) => patchTime({ startTime: value })}/>
        <CpdDateTimeInput label="结束" value={filters.endTime} onChange={(value) => patchTime({ endTime: value })}/>
        <button className="button primary compact cpd-query-button" type="button" onClick={() => void loadDataset(false)} disabled={busy === 'dataset'}>{busy === 'dataset' ? <LoaderCircle className="spin" size={14}/> : <Search size={14}/>} 查询</button>
        <button className="button secondary compact cpd-refresh-button" type="button" onClick={() => void loadDataset(true)} disabled={busy === 'dataset'}><RefreshCw className={busy === 'dataset' ? 'spin' : ''} size={14}/> 刷新</button>
      </div>
      <div className="cpd-inline-stats">
        <span>全部报告 <strong>{allHistoryTree?.report_count ?? 0}</strong></span><i/>
        <span>当前范围 <strong>{dataset?.candidate_count ?? 0}</strong></span><i/>
        <span>子系统 <strong>{tree?.subsystem_count ?? 0}</strong></span><i/>
        <span>模块 <strong>{tree?.module_count ?? 0}</strong></span><i/>
        <span>筛选结果 <strong>{total}</strong></span>
        {dataset && <><i/><span>数据集 <strong>{dataset.cached_at ? new Date(dataset.cached_at).toLocaleTimeString() : '-'}</strong></span><span className="cpd-cache-state">{dataset.dataset_cache_status?.startsWith('hit') ? '范围缓存命中' : `并行解析 ${dataset.parsed_now}`}</span><span className="cpd-cache-state">摘要缓存 {dataset.summary_cache_hits}</span></>}
      </div>
      {viewMode === 'grouped' && <div className="cpd-group-summary">{tree?.subsystems.map((sub) => <button type="button" key={sub.name} className={filters.subsystem === sub.name ? 'active' : ''} onClick={() => patchFilters({ subsystem: sub.name, module: '' })}><FolderTree size={17}/><strong>{sub.name}</strong><span>{sub.module_count} 模块 · {sub.report_count} 报告</span></button>)}</div>}
    </section>

    <section className="cpd-report-list-panel cpd-report-list-panel-v014">
      <header><div><div><strong>历史报告</strong><span>{appliedStart} ~ {appliedEnd} · 当前条件 {total} 份 · 第 {safePage}/{totalPages} 页</span></div></div></header>
      <div className="cpd-report-table-wrap">
        {busy === 'dataset' && <div className="list-loading-layer"><div className="list-loading-card"><LoaderCircle className="spin" size={17}/>正在并行扫描并解析当前时间范围报告…</div></div>}
        <table className="cpd-report-table">
          <thead><tr><th>开始时间</th><th>结束时间</th><th>子系统</th><th>模块</th><th>报告文件</th><th>测校结果</th><th>Results Validation</th><th>Measurement Quality</th><th>MCs Status</th><th>执行时长</th><th>操作</th></tr></thead>
          <tbody>{pageRows.map((report) => <tr key={reportKey(report)}>
            <td className="cpd-time-cell"><strong>{report.start_time || '-'}</strong></td>
            <td className="cpd-time-cell"><strong>{report.stop_time || '-'}</strong></td>
            <td><strong>{report.subsystem || '-'}</strong></td><td><strong>{report.module || '-'}</strong></td>
            <td><strong>{report.file_name}</strong><small>{report.cpd_name || report.module}</small></td>
            <td><span className={`cpd-result-badge ${resultTone(report.test_run_result)}`}>{report.test_run_result || 'UNKNOWN'}</span></td>
            <td>{report.results_validation || '-'}</td><td>{report.measurement_quality || '-'}</td><td>{report.mcs_status || '-'}</td><td>{report.execution_time || '-'}</td>
            <td><div className="cpd-row-actions"><button type="button" onClick={() => void showReport(report)}><FileText size={14}/> 测校报告</button><button type="button" onClick={() => void showCpdData(report)}>数据</button><button type="button" className="primary" disabled={!report.start_time || !report.stop_time || !report.subsystem || !report.module} onClick={() => onLocateLogs({ environment, subsystem: report.subsystem!, module: report.module!, startTime: report.start_time, endTime: report.stop_time, taskName: reportStem(report.file_name) })}><FileSearch size={14}/> 场景还原</button></div></td>
          </tr>)}{!pageRows.length && busy !== 'dataset' && <tr><td colSpan={11}><div className="resource-empty inline-empty">当前筛选没有匹配报告。</div></td></tr>}</tbody>
        </table>
      </div>
      <footer className="cpd-pagination"><label>每页 <select value={pageSize} onChange={(event) => { setPageSize(Number(event.target.value)); setPage(1); }}><option>50</option><option>100</option><option>200</option></select></label><button disabled={safePage <= 1} onClick={() => setPage((value) => value - 1)}>上一页</button><span>{safePage} / {totalPages}</span><button disabled={safePage >= totalPages} onClick={() => setPage((value) => value + 1)}>下一页</button></footer>
    </section>


    {cpdData && <CpdDataDialog key={`${environment.id}:${cpdData.full_path}`} environment={environment} report={cpdData} onClose={() => setCpdData(undefined)} />}
    {content && <div className="resource-modal-backdrop" onMouseDown={() => setContent(undefined)}><section className="cpd-report-viewer cpd-report-viewer-v014" onMouseDown={(event) => event.stopPropagation()}><header><div><span className="eyebrow">REPORT TEXT</span><h2>{content.file}</h2><p>{content.path}</p></div><button className="icon-button" onClick={() => setContent(undefined)}><X size={18}/></button></header><pre>{content.text}</pre></section></div>}
  </main>;
}
