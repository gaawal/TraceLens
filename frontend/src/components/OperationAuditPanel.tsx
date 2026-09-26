import { Fragment, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle, CheckCircle2, ChevronDown, ChevronLeft, ChevronRight, ChevronUp,
  Filter, LoaderCircle, RefreshCw, Search, ServerCog, Sparkles, X, XCircle,
} from 'lucide-react';
import { registerPageContextReader } from '../assistant/contextRegistry';
import { useImeCompositionGuard } from '../utils/imeComposition';
import {
  listOperationAudits,
  type OperationAuditList,
  type OperationAuditRecord,
} from '../api/resourceApi';
import { SmartDateTimeInput } from './SmartDateTimeInput';

/**
 * 操作审计：**功能级**留痕。
 *
 * 表格里显示的是"用户干了什么功能"（部署环境 / 启动环境进程 / 查询环境列表…），
 * **不显示 URL**；每行给出发起人（浏览器会话）、来源 IP、操作记录、结果和耗时，
 * 展开还能看到脱敏后的请求参数。系统自己跑的（SSE、轮询、前端自动回执）不进这张表，
 * 详见 `backend/apps/audits/features.py` 的 SKIP 名单。
 */

const QUICK_RANGES = [
  { label: '10分钟', seconds: 10 * 60 },
  { label: '1小时', seconds: 60 * 60 },
  { label: '3小时', seconds: 3 * 60 * 60 },
  { label: '12小时', seconds: 12 * 60 * 60 },
  { label: '1天', seconds: 24 * 60 * 60 },
  { label: '3天', seconds: 3 * 24 * 60 * 60 },
  { label: '7天', seconds: 7 * 24 * 60 * 60 },
] as const;

const OUTCOME_OPTIONS = [
  { value: '', label: '全部结果' },
  { value: 'success', label: '成功' },
  { value: 'failed', label: '失败' },
];

const TRIGGER_OPTIONS = [
  { value: '', label: '全部来源' },
  { value: 'user', label: '用户操作' },
  { value: 'ai', label: 'AI 代操作' },
];

interface Filters {
  startTime: string;
  endTime: string;
  group: string;
  feature: string;
  outcome: string;
  trigger: string;
  clientIp: string;
  query: string;
}

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
function durationLabel(value?: number | null): string {
  if (value == null) return '—';
  if (value < 1000) return `${value} ms`;
  if (value < 60_000) return `${(value / 1000).toFixed(1)} s`;
  return `${(value / 60_000).toFixed(1)} min`;
}
function sessionLabel(record: OperationAuditRecord): string {
  const id = String(record.session_id || '').trim();
  if (!id) return '—';
  return id.startsWith('ses_') ? `会话 ${id.slice(4, 10)}` : id.slice(0, 12);
}
function prettyPayload(payload?: Record<string, unknown>): string {
  if (!payload || !Object.keys(payload).length) return '（无参数）';
  try {
    return JSON.stringify(payload, null, 2);
  } catch {
    return String(payload);
  }
}

function OperationDetailPanel({ record }: { record: OperationAuditRecord }) {
  const rows: Array<[string, string]> = [
    ['操作时间', formatMoment(record.created_at)],
    ['功能分组', record.feature_group || '—'],
    ['目标', record.target_name ? `${record.target_kind ? `${record.target_kind} · ` : ''}${record.target_name}` : '—'],
    ['浏览器会话', sessionLabel(record)],
    ['操作 ID', record.operation_id || '—'],
    ['响应', `${record.status_code || '—'} · ${record.outcome_display || record.outcome}`],
    ['耗时', durationLabel(record.duration_ms)],
  ];
  return <div className="operation-detail-panel">
    <div className="operation-detail-grid">
      {rows.map(([label, value]) => <div key={label}><span>{label}</span><strong title={value}>{value}</strong></div>)}
    </div>
    {record.error_message && <div className="operation-detail-error"><XCircle size={13}/><span>{record.error_message}</span></div>}
    <div className="operation-detail-payload">
      <span>请求参数（密码/密钥已脱敏）</span>
      <pre>{prettyPayload(record.request_payload)}</pre>
    </div>
  </div>;
}

function OperationTable({ rows, sortOrder, onSortToggle, onOpenSearchAudit }: {
  rows: OperationAuditRecord[];
  sortOrder: 'asc' | 'desc';
  onSortToggle: () => void;
  onOpenSearchAudit: (auditId: number) => void;
}) {
  const [expandedId, setExpandedId] = useState<number | null>(null);
  return <div className="audit-table-wrap">
    <table className="audit-table operation-audit-table">
      <thead><tr>
        <th><button type="button" className="audit-sort-button" onClick={onSortToggle} title={sortOrder === 'desc' ? '当前倒序：最新记录在前，点击切换为升序' : '当前升序：最早记录在前，点击切换为倒序'}>操作时间 {sortOrder === 'desc' ? <ChevronDown size={13}/> : <ChevronUp size={13}/>}</button></th>
        <th>功能</th>
        <th>操作记录</th>
        <th>用户 / 来源 IP</th>
        <th>结果</th>
        <th>耗时</th>
        <th>操作</th>
      </tr></thead>
      <tbody>
        {rows.map((record) => {
          const failed = record.outcome === 'failed';
          const expanded = expandedId === record.id;
          return <Fragment key={record.id}>
            <tr className={`audit-row operation-audit-row ${failed ? 'audit-failed' : 'audit-success'}${expanded ? ' expanded' : ''}`}>
              <td className="audit-time-cell" title={record.operation_id}>{formatMoment(record.created_at)}</td>
              <td className="operation-feature-cell">
                <strong title={record.feature_name}>{record.feature_name}</strong>
                <span>{record.feature_group}</span>
              </td>
              <td className="operation-summary-cell" title={record.summary}>{record.summary || '—'}</td>
              <td className="operation-actor-cell">
                {record.operator_username ? <span className="operation-user" title="操作用户">{record.operator_username}</span> : null}
                <span className="operation-session" title={record.session_id || ''}>{sessionLabel(record)}</span>
                <code className="operation-ip" title="来源 IP">{record.client_ip || '—'}</code>
                {record.trigger === 'ai' && <span className="operation-ai-badge" title="这一步是 AI 助手代你操作的"><Sparkles size={11}/> AI 代操作</span>}
              </td>
              <td><span className={`audit-result audit-result-${failed ? 'failed' : 'success'}`}>{failed ? <XCircle size={13}/> : <CheckCircle2 size={13}/>}{record.outcome_display || (failed ? '失败' : '成功')}</span></td>
              <td className="audit-single-line">{durationLabel(record.duration_ms)}</td>
              <td><div className="audit-row-actions">
                <button type="button" className="button ghost audit-detail-button" onClick={() => setExpandedId((current) => current === record.id ? null : record.id)}>{expanded ? <ChevronUp size={14}/> : <ChevronDown size={14}/>} 详情</button>
                {record.log_search_audit ? <button type="button" className="button ghost" onClick={() => onOpenSearchAudit(Number(record.log_search_audit))} title="跳到「日志检索审计」看这次检索到底查了什么">检索详情</button> : null}
              </div></td>
            </tr>
            {expanded && <tr className="audit-detail-row"><td colSpan={7}><OperationDetailPanel record={record}/></td></tr>}
          </Fragment>;
        })}
        {!rows.length && <tr><td colSpan={7}><div className="audit-empty"><Search size={26}/><strong>当前条件下没有操作记录</strong><span>用户点出来的功能都会被记录；系统自己的轮询与流式请求不会进这张表。</span></div></td></tr>}
      </tbody>
    </table>
  </div>;
}

export function OperationAuditPanel({ onOpenSearchAudit }: { onOpenSearchAudit?: (auditId: number) => void }) {
  const initialRange = defaultRange(1);
  const imeGuard = useImeCompositionGuard();
  const [filters, setFilters] = useState<Filters>({ ...initialRange, group: '', feature: '', outcome: '', trigger: '', clientIp: '', query: '' });
  const [applied, setApplied] = useState(filters);
  const [quickRange, setQuickRange] = useState('1天');
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [sortOrder, setSortOrder] = useState<'asc' | 'desc'>('desc');
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [data, setData] = useState<OperationAuditList>({ count: 0, results: [] });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function load(nextPage = page, nextApplied = applied, nextPageSize = pageSize, nextSortOrder = sortOrder, auto = false) {
    if (!auto) setBusy(true);
    setError('');
    try {
      const result = await listOperationAudits({
        page: nextPage, pageSize: nextPageSize,
        startTime: nextApplied.startTime, endTime: nextApplied.endTime,
        group: nextApplied.group, feature: nextApplied.feature,
        outcome: nextApplied.outcome, trigger: nextApplied.trigger,
        clientIp: nextApplied.clientIp, query: nextApplied.query,
        order: nextSortOrder, auto,
      });
      setData(result);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally { if (!auto) setBusy(false); }
  }

  useEffect(() => { void load(page, applied, pageSize, sortOrder); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [page, pageSize, applied, sortOrder]);
  useEffect(() => {
    if (!autoRefresh) return undefined;
    // 自动刷新必须标 auto：否则每刷一次都会往审计里写一条"用户操作"。
    const timer = window.setInterval(() => { void load(page, applied, pageSize, sortOrder, true); }, 5000);
    return () => window.clearInterval(timer);
    /* eslint-disable-next-line react-hooks/exhaustive-deps */
  }, [autoRefresh, page, pageSize, applied, sortOrder]);

  function applyQuickRange(seconds: number, label: string) {
    const end = new Date();
    const start = new Date(end.getTime() - seconds * 1000);
    const next = { ...filters, startTime: formatLocal(start), endTime: formatLocal(end) };
    setFilters(next); setQuickRange(label); setPage(1); setApplied(next);
  }
  function applySearch() { setPage(1); setApplied({ ...filters }); setQuickRange(''); }
  function resetFilters() {
    const range = defaultRange(1);
    const next: Filters = { ...range, group: '', feature: '', outcome: '', trigger: '', clientIp: '', query: '' };
    setFilters(next); setApplied(next); setQuickRange('1天'); setPage(1);
  }

  const facets = data.facets;
  const summary = facets?.summary;
  const features = useMemo(() => {
    const all = facets?.features || [];
    return (filters.group ? all.filter((item) => {
      // 分组已选时，只列该分组下的功能：facets 是全量统计，这里补一次分组归属判断。
      const row = data.results.find((record) => record.feature_name === item.value);
      return !row || row.feature_group === filters.group;
    }) : all);
  }, [facets, filters.group, data.results]);
  const totalPages = Math.max(1, Math.ceil(data.count / pageSize));
  const safePage = Math.min(page, totalPages);

  useEffect(() => {
    const handler = (event: Event) => {
      const custom = event as CustomEvent<{ context?: Record<string, unknown> }>;
      const context = custom.detail?.context;
      if (!context) return;
      context.page = 'audit';
      context.page_label = '操作审计';
      context.operation_audit = {
        filters: {
          start_time: applied.startTime,
          end_time: applied.endTime,
          group: applied.group,
          feature: applied.feature,
          outcome: applied.outcome,
          trigger: applied.trigger,
          client_ip: applied.clientIp,
          query: applied.query,
        },
        sort_order: sortOrder,
        result_count: data.count || 0,
        page,
        page_size: pageSize,
        visible_records: (data.results || []).slice(0, 8).map((record) => ({
          feature_group: record.feature_group,
          feature_name: record.feature_name,
          summary: record.summary,
          client_ip: record.client_ip,
          session_id: record.session_id,
          trigger: record.trigger,
          outcome: record.outcome,
          status_code: record.status_code,
          created_at: record.created_at,
          error: record.error_message || '',
        })),
      };
    };
    return registerPageContextReader(handler, 11);
  }, [applied, sortOrder, data, page, pageSize]);

  return <>
    <section className="audit-header">
      <div>
        <div className="eyebrow">OPERATION AUDIT</div>
        <h1>操作审计</h1>
        <p>用户点出来的每一个功能都会留痕：谁（浏览器会话 + 来源 IP）、什么时间、做了什么、结果如何。系统自己的轮询与流式请求不记录。</p>
      </div>
      <div className="audit-header-stats">
        <span><strong>{data.count}</strong> 条记录</span>
        {summary ? <span className="operation-stat-failed" title="失败的操作条数"><strong>{summary.failed}</strong> 失败</span> : null}
        {summary ? <span title="出现过的来源 IP 数"><strong>{summary.client_ips}</strong> 个来源 IP</span> : null}
        <button type="button" className="button ghost" onClick={() => void load()} disabled={busy}><RefreshCw className={busy ? 'spin' : ''} size={15}/> 刷新</button>
      </div>
    </section>

    <section className="audit-query-bar unified-query-shell">
      <div className="audit-query-primary remote-unified-query-row-v150">
        <div className="remote-range-presets" aria-label="快捷时间范围">
          {QUICK_RANGES.map((item) => <button type="button" key={item.label} className={quickRange === item.label ? 'active' : ''} onClick={() => applyQuickRange(item.seconds, item.label)}>{item.label}</button>)}
        </div>
        <SmartDateTimeInput value={filters.startTime} label="开始时间" onChange={(value) => { setFilters((current) => ({ ...current, startTime: value })); setQuickRange(''); }} />
        <span className="remote-time-separator">—</span>
        <SmartDateTimeInput value={filters.endTime} label="结束时间" onChange={(value) => { setFilters((current) => ({ ...current, endTime: value })); setQuickRange(''); }} />
        <div className="remote-unified-text-search audit-unified-text-search">
          <Search size={15}/><input value={filters.query} onChange={(event) => setFilters((current) => ({ ...current, query: event.target.value }))} onCompositionStart={imeGuard.onCompositionStart} onCompositionEnd={imeGuard.onCompositionEnd} onKeyDown={(event) => { if (event.key === 'Enter' && !imeGuard.isComposing(event)) applySearch(); }} placeholder="搜索功能 / 操作记录 / IP / 目标…"/>
          {filters.query && <button type="button" onClick={() => setFilters((current) => ({ ...current, query: '' }))} aria-label="清空搜索"><X size={13}/></button>}
        </div>
        <button type="button" className="button primary remote-search-apply" onClick={applySearch} disabled={busy}><Search size={15}/> 搜索</button>
        <button type="button" className="button ghost audit-reset-button" onClick={resetFilters}><RefreshCw size={14}/> 重置</button>
      </div>
      <div className="audit-query-secondary">
        <label className="audit-select"><ServerCog size={14}/><select value={filters.group} onChange={(event) => setFilters((current) => ({ ...current, group: event.target.value, feature: '' }))}><option value="">全部分组</option>{(facets?.groups || []).map((item) => <option key={item.value} value={item.value}>{item.value}（{item.count}）</option>)}</select></label>
        <label className="audit-select"><Filter size={14}/><select value={filters.feature} onChange={(event) => setFilters((current) => ({ ...current, feature: event.target.value }))}><option value="">全部功能</option>{features.map((item) => <option key={item.value} value={item.value}>{item.value}（{item.count}）</option>)}</select></label>
        <label className="audit-select"><select value={filters.outcome} onChange={(event) => setFilters((current) => ({ ...current, outcome: event.target.value }))}>{OUTCOME_OPTIONS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
        <label className="audit-select"><select value={filters.trigger} onChange={(event) => setFilters((current) => ({ ...current, trigger: event.target.value }))}>{TRIGGER_OPTIONS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
        <label className="audit-select operation-ip-filter"><input value={filters.clientIp} onChange={(event) => setFilters((current) => ({ ...current, clientIp: event.target.value }))} placeholder="来源 IP" onKeyDown={(event) => { if (event.key === 'Enter' && !imeGuard.isComposing(event)) applySearch(); }}/></label>
      </div>
    </section>

    <section className="audit-toolbar">
      <label className="operation-auto-refresh" title="每 5 秒自动刷新一次；自动刷新不会写进审计"><input type="checkbox" checked={autoRefresh} onChange={(event) => setAutoRefresh(event.target.checked)}/> 自动刷新</label>
      <div className="audit-toolbar-right">
        {error && <span className="audit-load-error"><AlertTriangle size={14}/>{error}</span>}
        <span>{data.count ? `${(safePage - 1) * pageSize + 1}-${Math.min(data.count, safePage * pageSize)} / ${data.count}` : '0 条'}</span>
        <select value={pageSize} onChange={(event) => { setPageSize(Number(event.target.value)); setPage(1); }}><option value={50}>50 / 页</option><option value={100}>100 / 页</option><option value={200}>200 / 页</option></select>
      </div>
    </section>

    {busy && !data.results.length
      ? <div className="audit-loading"><LoaderCircle className="spin" size={24}/><span>正在读取操作记录…</span></div>
      : <OperationTable rows={data.results} sortOrder={sortOrder} onSortToggle={() => { setPage(1); setSortOrder((current) => current === 'desc' ? 'asc' : 'desc'); }} onOpenSearchAudit={(id) => onOpenSearchAudit?.(id)}/>}

    <footer className="audit-pagination">
      <button type="button" className="button ghost" disabled={safePage <= 1 || busy} onClick={() => setPage((current) => Math.max(1, current - 1))}><ChevronLeft size={15}/> 上一页</button>
      <span>第 <strong>{safePage}</strong> / {totalPages} 页</span>
      <button type="button" className="button ghost" disabled={safePage >= totalPages || busy} onClick={() => setPage((current) => Math.min(totalPages, current + 1))}>下一页 <ChevronRight size={15}/></button>
    </footer>
  </>;
}
