import { registerPageContextReader } from '../assistant/contextRegistry';
import { useImeCompositionGuard } from '../utils/imeComposition';
import { Fragment, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle, CheckCircle2, ChevronDown, ChevronLeft, ChevronRight, ChevronUp,
  Filter, FolderTree, LayoutGrid, List, LoaderCircle, RefreshCw,
  Copy, RotateCcw, Search, ServerCog, X, XCircle,
} from 'lucide-react';
import {
  listEnvironments, listLogAudits,
  type EnvironmentSummary, type LogAuditList, type LogAuditRecord, type LogWindowRequest,
} from '../api/resourceApi';
import { SmartDateTimeInput } from './SmartDateTimeInput';

interface Props {
  onRetry: (environment: EnvironmentSummary, request: LogWindowRequest, audit: LogAuditRecord) => void;
  onOpenData?: (audit: LogAuditRecord) => void;
}

type ViewMode = 'overview' | 'grouped';

interface Filters {
  startTime: string;
  endTime: string;
  result: string;
  environmentId?: number;
  subsystem: string;
  module: string;
  sourceCategory: string;
  query: string;
}

const SOURCE_LABELS: Record<string, string> = {
  debug: '调试日志', run: '运行日志', executor: '执行器日志', helf: 'HELF日志', sil: 'SIL仿真日志',
};

const AUDIT_QUICK_RANGES = [
  { label: '10分钟', seconds: 10 * 60 },
  { label: '1小时', seconds: 60 * 60 },
  { label: '3小时', seconds: 3 * 60 * 60 },
  { label: '12小时', seconds: 12 * 60 * 60 },
  { label: '1天', seconds: 24 * 60 * 60 },
  { label: '2天', seconds: 2 * 24 * 60 * 60 },
  { label: '3天', seconds: 3 * 24 * 60 * 60 },
] as const;

function pad(value: number): string { return String(value).padStart(2, '0'); }
function formatLocal(date: Date): string {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}
function formatMoment(value?: string | null): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).replace('T', ' ').slice(0, 19);
  return date.toLocaleString('zh-CN', { hour12: false });
}
function defaultRange(days = 1): Pick<Filters, 'startTime' | 'endTime'> {
  const end = new Date();
  const start = new Date(end.getTime() - days * 24 * 60 * 60 * 1000);
  return { startTime: formatLocal(start), endTime: formatLocal(end) };
}
function resultMeta(result: string) {
  if (result === 'success') return { label: '成功', className: 'success', icon: <CheckCircle2 size={14}/> };
  if (result === 'no_result') return { label: '无结果', className: 'no-result', icon: <Search size={14}/> };
  if (result === 'running') return { label: '执行中', className: 'running', icon: <LoaderCircle className="spin" size={14}/> };
  if (result === 'cancelled') return { label: '已停止', className: 'cancelled', icon: <XCircle size={14}/> };
  return { label: '失败', className: 'failed', icon: <AlertTriangle size={14}/> };
}
function targetLabel(item: LogAuditRecord): string {
  if (!item.targets.length) return '无需子系统/模块';
  if (item.targets.length === 1) return `${item.targets[0].subsystem} / ${item.targets[0].module}`;
  const subsystems = new Set(item.targets.map((target) => target.subsystem));
  if (subsystems.size === 1) return `${item.targets[0].subsystem} / 多模块(${item.targets.length})`;
  return `多子系统(${subsystems.size}) / 多模块(${item.targets.length})`;
}
function durationLabel(value?: number | null): string {
  if (value == null) return '—';
  if (value < 1000) return `${value} ms`;
  if (value < 60_000) return `${(value / 1000).toFixed(1)} s`;
  return `${(value / 60_000).toFixed(1)} min`;
}

function basename(value: string): string {
  const normalized = String(value || '').replace(/\\/g, '/');
  return normalized.split('/').filter(Boolean).pop() || normalized || '未知文件';
}
function auditFileLabel(file: LogAuditRecord['matched_files'][number]): string {
  const archive = basename(file.path);
  return file.member ? `${archive} → ${basename(file.member)}` : archive;
}
function auditFileCopyText(file: LogAuditRecord['matched_files'][number]): string {
  return file.member ? `${file.path} :: ${file.member}` : file.path;
}
function auditFileTitle(item: LogAuditRecord): string {
  return (item.matched_files || []).map((file) => {
    const module = [file.subsystem, file.module].filter(Boolean).join('/');
    const location = auditFileCopyText(file);
    return `${module ? `[${module}] ` : ''}${location}`;
  }).join('\n');
}
async function copyAuditText(value: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const input = document.createElement('textarea');
  input.value = value;
  input.style.position = 'fixed';
  input.style.opacity = '0';
  document.body.appendChild(input);
  input.focus();
  input.select();
  document.execCommand('copy');
  document.body.removeChild(input);
}

function auditFileTimeLabel(file: LogAuditRecord['matched_files'][number]): string {
  const start = file.indexed_start ? formatMoment(file.indexed_start) : '未知';
  const end = file.indexed_end
    ? formatMoment(file.indexed_end)
    : (file.boundary_time ? `归档边界 ${formatMoment(file.boundary_time)}` : '未知');
  return `${start} → ${end}`;
}

function AuditMatchedFilesPanel({ item }: { item: LogAuditRecord }) {
  const files = item.matched_files || [];
  const imeGuard = useImeCompositionGuard();
  const [copiedKey, setCopiedKey] = useState('');

  async function copyFile(file: LogAuditRecord['matched_files'][number], key: string) {
    try {
      await copyAuditText(auditFileCopyText(file));
      setCopiedKey(key);
      window.setTimeout(() => setCopiedKey((current) => current === key ? '' : current), 1200);
    } catch {
      setCopiedKey('');
    }
  }

  return <div className="audit-detail-panel">
    <div className="audit-detail-title"><strong>命中文件</strong><span>{files.length} 个</span></div>
    {files.length ? <div className="audit-detail-file-list">
      {files.map((file, index) => {
        const key = `${file.path}:${file.member || ''}:${index}`;
        return <div className="audit-detail-file-row" key={key} title={auditFileCopyText(file)}>
          <strong>{[file.subsystem, file.module].filter(Boolean).join(' / ') || auditFileLabel(file)}</strong>
          <button type="button" className={`audit-detail-file-path${copiedKey === key ? ' copied' : ''}`} onClick={() => void copyFile(file, key)} title="点击复制完整路径"><Copy size={11}/><code>{copiedKey === key ? '已复制' : auditFileCopyText(file)}</code></button>
          <span>文件时间：{auditFileTimeLabel(file)}</span>
        </div>;
      })}
    </div> : <div className="audit-detail-empty">本次查询没有命中文件。</div>}
  </div>;
}

function AuditTable({ rows, environments, onRetry, onOpenData, sortOrder, onSortToggle }: { rows: LogAuditRecord[]; environments: EnvironmentSummary[]; onRetry: Props['onRetry']; onOpenData?: Props['onOpenData']; sortOrder: 'asc' | 'desc'; onSortToggle: () => void }) {
  const [expandedAuditId, setExpandedAuditId] = useState<number | null>(null);

  return <div className="audit-table-wrap">
    <table className="audit-table">
      <thead><tr>
        <th><button type="button" className="audit-sort-button" onClick={onSortToggle} title={sortOrder === 'desc' ? '当前倒序：最新记录在前，点击切换为升序' : '当前升序：最早记录在前，点击切换为倒序'}>操作时间 {sortOrder === 'desc' ? <ChevronDown size={13}/> : <ChevronUp size={13}/>}</button></th><th>操作用户 / IP</th><th>目标环境</th><th>子系统 / 模块</th><th>日志类型</th>
        <th>日志时间范围</th><th>关键字</th><th>状态</th><th>数据提取</th><th>命中文件</th><th>错误信息</th><th>耗时</th><th>操作</th>
      </tr></thead>
      <tbody>
        {rows.map((item) => {
          const meta = resultMeta(item.result);
          const environment = environments.find((env) => env.id === item.environment);
          const expanded = expandedAuditId === item.id;
          return <Fragment key={item.id}><tr className={`audit-row audit-${meta.className}${expanded ? ' expanded' : ''}`}>
            <td className="audit-time-cell" title={item.operation_id}>{formatMoment(item.created_at)}</td>
            <td className="audit-single-line" title={item.operator_username || item.client_ip || ''}>{item.operator_username || item.client_ip || '—'}</td>
            <td className="audit-single-line" title={`${item.target_host || item.environment_name}${item.target_username ? `(${item.target_username})` : ''}`}>{item.target_host || item.environment_name}{item.target_username ? `(${item.target_username})` : ''}</td>
            <td><div className="audit-target-badges" title={item.targets.map((target) => `${target.subsystem}/${target.module}${target.kind === 'executor' ? '(执行器)' : ''}`).join('\n')}>
              {item.targets.length ? item.targets.slice(0, 3).map((target, index) => <span key={`${target.subsystem}-${target.module}-${target.kind}-${index}`}>{target.subsystem}/{target.module}{target.kind === 'executor' ? ' · 执行器' : ''}</span>) : <span className="muted">无需限定</span>}
              {item.targets.length > 3 && <em>+{item.targets.length - 3}</em>}
            </div></td>
            <td><div className="audit-source-badges">{item.source_categories.map((category) => <span key={category}>{SOURCE_LABELS[category] || category}</span>)}</div></td>
            <td className="audit-range-cell" title={`${formatMoment(item.start_time)} 至 ${formatMoment(item.end_time)}`}>{formatMoment(item.start_time)} 至 {formatMoment(item.end_time)}</td>
            <td className="audit-keyword-cell" title={item.keyword || ''}>{item.keyword || '—'}</td>
            <td><span className={`audit-result audit-result-${meta.className}`}>{meta.icon}{meta.label}</span></td>
            <td>{(item.data_extraction_count || 0) > 0 ? <button type="button" className="audit-data-link" onClick={() => onOpenData?.(item)}>已提取 {item.data_extraction_count} 次</button> : <span className="muted">—</span>}</td>
            <td className="audit-files-cell" title={auditFileTitle(item)}>{(item.matched_files || []).length ? `${item.matched_files.length} 个文件` : '—'}</td>
            <td className={item.error_message ? 'audit-error-cell has-error' : 'audit-error-cell'} title={item.error_message || ''}>{item.error_message || '—'}</td>
            <td className="audit-single-line">{durationLabel(item.duration_ms)}</td>
            <td><div className="audit-row-actions"><button type="button" className="button ghost audit-detail-button" onClick={() => setExpandedAuditId((current) => current === item.id ? null : item.id)}>{expanded ? <ChevronUp size={14}/> : <ChevronDown size={14}/>} 详情</button><button type="button" className="button ghost audit-retry-button" disabled={!environment || item.result === 'running'} onClick={() => environment && onRetry(environment, item.request_payload, item)} title={environment ? '带入原检索参数并重新发起查询' : '原环境已不存在，无法重试'}><RotateCcw size={14}/> 重试</button></div></td>
          </tr>{expanded && <tr className="audit-detail-row"><td colSpan={13}><AuditMatchedFilesPanel item={item}/></td></tr>}</Fragment>;
        })}
        {!rows.length && <tr><td colSpan={13}><div className="audit-empty"><Search size={26}/><strong>当前条件下没有日志审计记录</strong><span>默认展示最近 1 天的日志检索操作。</span></div></td></tr>}
      </tbody>
    </table>
  </div>;
}

export function LogAuditPage({ onRetry, onOpenData }: Props) {
  const initialRange = defaultRange(1);
  const imeGuard = useImeCompositionGuard();
  const [filters, setFilters] = useState<Filters>({ ...initialRange, result: '', subsystem: '', module: '', sourceCategory: '', query: '' });
  const [applied, setApplied] = useState(filters);
  const [quickRange, setQuickRange] = useState('1天');
  const [viewMode, setViewMode] = useState<ViewMode>('overview');
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [sortOrder, setSortOrder] = useState<'asc' | 'desc'>('desc');
  const [data, setData] = useState<LogAuditList>({ count: 0, results: [] });
  const [environments, setEnvironments] = useState<EnvironmentSummary[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function load(nextPage = page, nextApplied = applied, nextPageSize = pageSize, nextSortOrder = sortOrder) {
    setBusy(true); setError('');
    try {
      const result = await listLogAudits({
        page: nextPage, pageSize: nextPageSize,
        startTime: nextApplied.startTime, endTime: nextApplied.endTime,
        result: nextApplied.result, environmentId: nextApplied.environmentId,
        subsystem: nextApplied.subsystem, module: nextApplied.module,
        sourceCategory: nextApplied.sourceCategory, query: nextApplied.query, order: nextSortOrder,
      });
      setData(result);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally { setBusy(false); }
  }

  useEffect(() => { void listEnvironments().then(setEnvironments).catch(() => setEnvironments([])); }, []);
  useEffect(() => { void load(page, applied, pageSize, sortOrder); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [page, pageSize, applied, sortOrder]);
  useEffect(() => {
    if (!data.results.some((item) => item.result === 'running')) return undefined;
    const timer = window.setInterval(() => { void load(page, applied, pageSize, sortOrder); }, 1800);
    return () => window.clearInterval(timer);
    /* eslint-disable-next-line react-hooks/exhaustive-deps */
  }, [data.results, page, pageSize, applied, sortOrder]);

  function applyQuickRange(seconds: number, label: string) {
    const end = new Date();
    const start = new Date(end.getTime() - seconds * 1000);
    const next = { ...filters, startTime: formatLocal(start), endTime: formatLocal(end) };
    setFilters(next); setQuickRange(label); setPage(1); setApplied(next);
  }
  function applySearch() { setPage(1); setApplied({ ...filters }); setQuickRange(''); }
  function resetFilters() {
    const range = defaultRange(1);
    const next: Filters = { ...range, result: '', subsystem: '', module: '', sourceCategory: '', query: '' };
    setFilters(next); setApplied(next); setQuickRange('1天'); setPage(1);
  }

  const facets = data.facets;
  const targetTree = useMemo(() => {
    const map = new Map<string, Set<string>>();
    (facets?.targets || []).forEach((item) => {
      if (!item.targets__subsystem || !item.targets__module) return;
      if (!map.has(item.targets__subsystem)) map.set(item.targets__subsystem, new Set());
      map.get(item.targets__subsystem)!.add(item.targets__module);
    });
    return [...map.entries()].map(([subsystem, modules]) => ({ subsystem, modules: [...modules].sort() })).sort((a, b) => a.subsystem.localeCompare(b.subsystem));
  }, [facets]);
  const modules = targetTree.find((item) => item.subsystem === filters.subsystem)?.modules || [];
  const totalPages = Math.max(1, Math.ceil(data.count / pageSize));
  const safePage = Math.min(page, totalPages);
  const grouped = useMemo(() => {
    const map = new Map<string, LogAuditRecord[]>();
    data.results.forEach((item) => {
      const keys = item.targets.length
        ? Array.from(new Set(item.targets.map((target) => `${target.subsystem} / ${target.module}${target.kind === 'executor' ? '（执行器）' : ''}`)))
        : ['无需子系统/模块'];
      keys.forEach((key) => {
        if (!map.has(key)) map.set(key, []);
        map.get(key)!.push(item);
      });
    });
    return [...map.entries()].sort(([left], [right]) => left.localeCompare(right));
  }, [data.results]);


  useEffect(() => {
    const handler = (event: Event) => {
      const custom = event as CustomEvent<{ context?: Record<string, unknown> }>;
      const context = custom.detail?.context;
      if (!context) return;
      const rows = data.results || [];
      context.page = 'audit';
      context.page_label = '操作审计';
      context.audit_page = {
        filters: {
          start_time: applied.startTime,
          end_time: applied.endTime,
          result: applied.result,
          environment_id: applied.environmentId || null,
          subsystem: applied.subsystem,
          module: applied.module,
          source_category: applied.sourceCategory,
          query: applied.query,
        },
        view_mode: viewMode,
        sort_order: sortOrder,
        result_count: data.count || rows.length,
        page,
        page_size: pageSize,
        visible_records: rows.slice(0, 8).map((item) => ({
          id: item.id,
          operation_id: item.operation_id,
          environment_id: item.environment || null,
          environment_name: item.environment_name || '',
          target_host: item.target_host || '',
          result: item.result,
          start_time: item.start_time,
          end_time: item.end_time,
          keyword: item.keyword || '',
          source_categories: item.source_categories,
          targets: item.targets.slice(0, 6).map((target) => `${target.subsystem}/${target.module}`),
          matched_file_count: item.matched_files?.length || 0,
          data_extraction_count: item.data_extraction_count || 0,
          error: item.error_message || '',
        })),
      };
    };
    return registerPageContextReader(handler, 10);
  }, [applied, viewMode, sortOrder, data, page, pageSize]);

  return <main className="audit-page">
    <section className="audit-header">
      <div><div className="eyebrow">LOG AUDIT</div><h1>日志审计</h1><p>记录谁在什么时间检索了哪个环境的日志、使用了哪些条件，以及最终成功、失败或停止的原因。</p></div>
      <div className="audit-header-stats"><span><strong>{data.count}</strong> 条记录</span><button type="button" className="button ghost" onClick={() => void load()} disabled={busy}><RefreshCw className={busy ? 'spin' : ''} size={15}/> 刷新</button></div>
    </section>

    <section className="audit-query-bar unified-query-shell">
      <div className="audit-query-primary remote-unified-query-row-v150">
        <div className="remote-range-presets" aria-label="快捷时间范围">
          {AUDIT_QUICK_RANGES.map((item) => <button type="button" key={item.label} className={quickRange === item.label ? 'active' : ''} onClick={() => applyQuickRange(item.seconds, item.label)}>{item.label}</button>)}
        </div>
        <SmartDateTimeInput value={filters.startTime} label="开始时间" onChange={(value) => { setFilters((current) => ({ ...current, startTime: value })); setQuickRange(''); }} />
        <span className="remote-time-separator">—</span>
        <SmartDateTimeInput value={filters.endTime} label="结束时间" onChange={(value) => { setFilters((current) => ({ ...current, endTime: value })); setQuickRange(''); }} />
        <div className="remote-unified-text-search audit-unified-text-search">
          <Search size={15}/><input value={filters.query} onChange={(event) => setFilters((current) => ({ ...current, query: event.target.value }))} onCompositionStart={imeGuard.onCompositionStart} onCompositionEnd={imeGuard.onCompositionEnd} onKeyDown={(event) => { if (event.key === 'Enter' && !imeGuard.isComposing(event)) applySearch(); }} placeholder="搜索操作IP / 环境 / 关键字 / 错误…"/>
          {filters.query && <button type="button" onClick={() => setFilters((current) => ({ ...current, query: '' }))} aria-label="清空搜索"><X size={13}/></button>}
        </div>
        <button type="button" className="button primary remote-search-apply" onClick={applySearch} disabled={busy}><Search size={15}/> 搜索</button>
        <button type="button" className="button ghost audit-reset-button" onClick={resetFilters}><RefreshCw size={14}/> 重置</button>
      </div>
      <div className="audit-query-secondary">
        <label className="audit-select"><ServerCog size={14}/><select value={filters.environmentId ?? ''} onChange={(event) => setFilters((current) => ({ ...current, environmentId: event.target.value ? Number(event.target.value) : undefined }))}><option value="">全部环境</option>{(facets?.environments || []).map((item) => <option key={`${item.environment_id}-${item.target_host}-${item.target_username}`} value={item.environment_id ?? ''}>{item.target_host || item.environment_name}{item.target_username ? ` (${item.target_username})` : ''}</option>)}</select></label>
        <label className="audit-select"><FolderTree size={14}/><select value={filters.subsystem} onChange={(event) => setFilters((current) => ({ ...current, subsystem: event.target.value, module: '' }))}><option value="">全部子系统</option>{targetTree.map((item) => <option key={item.subsystem} value={item.subsystem}>{item.subsystem}</option>)}</select></label>
        <label className="audit-select"><Filter size={14}/><select value={filters.module} disabled={!filters.subsystem} onChange={(event) => setFilters((current) => ({ ...current, module: event.target.value }))}><option value="">全部模块</option>{modules.map((module) => <option key={module} value={module}>{module}</option>)}</select></label>
        <label className="audit-select"><select value={filters.sourceCategory} onChange={(event) => setFilters((current) => ({ ...current, sourceCategory: event.target.value }))}><option value="">全部日志类型</option>{Object.entries(SOURCE_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        <label className="audit-select"><select value={filters.result} onChange={(event) => setFilters((current) => ({ ...current, result: event.target.value }))}><option value="">全部结果</option><option value="success">成功</option><option value="no_result">无结果</option><option value="failed">失败/停止</option><option value="running">执行中</option></select></label>
      </div>
    </section>

    <section className="audit-toolbar">
      <div className="audit-view-toggle"><button type="button" className={viewMode === 'overview' ? 'active' : ''} onClick={() => setViewMode('overview')}><List size={14}/> 全览</button><button type="button" className={viewMode === 'grouped' ? 'active' : ''} onClick={() => setViewMode('grouped')}><LayoutGrid size={14}/> 按子系统/模块</button></div>
      <div className="audit-toolbar-right">{error && <span className="audit-load-error"><AlertTriangle size={14}/>{error}</span>}<span>{data.count ? `${(safePage - 1) * pageSize + 1}-${Math.min(data.count, safePage * pageSize)} / ${data.count}` : '0 条'}</span><select value={pageSize} onChange={(event) => { setPageSize(Number(event.target.value)); setPage(1); }}><option value={50}>50 / 页</option><option value={100}>100 / 页</option><option value={200}>200 / 页</option></select></div>
    </section>

    {busy && !data.results.length ? <div className="audit-loading"><LoaderCircle className="spin" size={24}/><span>正在读取审计记录…</span></div> : viewMode === 'overview' ? <AuditTable rows={data.results} environments={environments} onRetry={onRetry} onOpenData={onOpenData} sortOrder={sortOrder} onSortToggle={() => { setPage(1); setSortOrder((current) => current === 'desc' ? 'asc' : 'desc'); }}/> : <div className="audit-groups">{grouped.map(([name, rows]) => <section className="audit-group" key={name}><header><div><FolderTree size={16}/><strong>{name}</strong><span>{rows.length} 条</span></div></header><AuditTable rows={rows} environments={environments} onRetry={onRetry} onOpenData={onOpenData} sortOrder={sortOrder} onSortToggle={() => { setPage(1); setSortOrder((current) => current === 'desc' ? 'asc' : 'desc'); }}/></section>)}{!grouped.length && <AuditTable rows={[]} environments={environments} onRetry={onRetry} onOpenData={onOpenData} sortOrder={sortOrder} onSortToggle={() => { setPage(1); setSortOrder((current) => current === 'desc' ? 'asc' : 'desc'); }}/>}</div>}

    <footer className="audit-pagination"><button type="button" className="button ghost" disabled={safePage <= 1 || busy} onClick={() => setPage((current) => Math.max(1, current - 1))}><ChevronLeft size={15}/> 上一页</button><span>第 <strong>{safePage}</strong> / {totalPages} 页</span><button type="button" className="button ghost" disabled={safePage >= totalPages || busy} onClick={() => setPage((current) => Math.min(totalPages, current + 1))}>下一页 <ChevronRight size={15}/></button></footer>
  </main>;
}
