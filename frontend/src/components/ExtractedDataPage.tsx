import { registerPageContextReader } from '../assistant/contextRegistry';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Database, Download, Eye, FileSearch, LoaderCircle, Merge, RefreshCw, RotateCcw, Search, Trash2, X } from 'lucide-react';
import { DataVisualizationDialog } from './DataVisualizationDialog';
import {
  deleteDataExtractionRecord,
  listDataExtractionRecords,
  updateDataExtractionRecord,
  type DataExtractionRecord,
} from '../api/resourceApi';
import type { DataExtractionRule, ExtractedDataRow } from '../rendering/dataExtractionRules';
import { restoreExtractionRecord, type ExtractionProgress } from '../rendering/dataExtractionRuntime';
import { isLiveCaptureRecord, liveCaptureSnapshot, restoreLiveCaptureRecord } from '../services/liveCaptureRestore';
import {
  downloadMergedTemporaryRuleData,
  downloadTemporaryRuleData,
  getTemporaryExtractionSession,
  mergeTemporaryRuleData,
  saveTemporaryExtractionSession,
  temporarySessionKey,
  type MergedTemporaryDataRow,
  type TemporaryExtractionSession,
  type TemporaryRuleData,
} from '../rendering/extractedDataStore';
import type { VisualizationDataset } from '../rendering/dataVisualization';

interface Props {
  sourceOperationFilter?: string;
  onClearSourceOperationFilter?: () => void;
  onOpenSourceLog?: (record: DataExtractionRecord) => void;
}

type DataPreview =
  | { mode: 'single'; record: DataExtractionRecord; session: TemporaryExtractionSession; rule: DataExtractionRule; rows: ExtractedDataRow[] }
  | { mode: 'merged'; record: DataExtractionRecord; session: TemporaryExtractionSession; results: TemporaryRuleData[]; fields: string[]; rows: MergedTemporaryDataRow[] };

interface VisualizationState {
  record: DataExtractionRecord;
  dataset: VisualizationDataset;
}

function formatMoment(value?: string | null): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).replace('T', ' ').slice(0, 19);
  return date.toLocaleString('zh-CN', { hour12: false });
}

/** 实时采集落下来的记录：还原走 watch 命中，不重读日志。 */
function liveCaptureLabel(record: DataExtractionRecord): string {
  const snapshot = liveCaptureSnapshot(record);
  if (!snapshot) return '';
  const window = [snapshot.started_at, snapshot.ended_at]
    .map((value) => String(value || '').replace('T', ' ').slice(11, 19))
    .filter(Boolean);
  return `实时采集${window.length === 2 ? ` ${window[0]}–${window[1]}` : ''}${snapshot.hits ? ` · 命中 ${snapshot.hits.toLocaleString()} 条` : ''}`;
}

/** 采集时段只在真的跨了时间时才占用表格一格；一两秒内结束的采集显示「—」，别重复名字里的信息。 */
function liveCaptureWindow(record: DataExtractionRecord): string {
  const snapshot = liveCaptureSnapshot(record);
  if (!snapshot?.started_at || !snapshot?.ended_at) return '';
  const start = new Date(String(snapshot.started_at)).getTime();
  const end = new Date(String(snapshot.ended_at)).getTime();
  if (!Number.isFinite(start) || !Number.isFinite(end) || end - start < 2000) return '';
  return `${String(snapshot.started_at).replace('T', ' ').slice(5, 19)} → ${String(snapshot.ended_at).replace('T', ' ').slice(11, 19)}`;
}

function resultLabel(record: DataExtractionRecord) {
  if (record.status === 'success') return { text: `命中 ${record.matched_rule_count} 项 · ${record.row_count.toLocaleString()} 行`, cls: 'success' };
  if (record.status === 'no_result') return { text: '未命中数据', cls: 'no-result' };
  if (record.status === 'running') return { text: '提取中', cls: 'running' };
  if (record.status === 'cancelled') return { text: '已停止', cls: 'cancelled' };
  return { text: '失败', cls: 'failed' };
}

export function ExtractedDataPage({ sourceOperationFilter, onClearSourceOperationFilter, onOpenSourceLog }: Props) {
  const [records, setRecords] = useState<DataExtractionRecord[]>([]);
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [restoreRecord, setRestoreRecord] = useState<DataExtractionRecord>();
  const [restoreProgress, setRestoreProgress] = useState<ExtractionProgress>();
  const [restoreError, setRestoreError] = useState('');
  const [restoreMergeIds, setRestoreMergeIds] = useState<Set<string>>(new Set());
  const [preview, setPreview] = useState<DataPreview>();
  const [visualization, setVisualization] = useState<VisualizationState>();
  const [downloadDialog, setDownloadDialog] = useState<{ record: DataExtractionRecord; session: TemporaryExtractionSession }>();
  const abortRef = useRef<AbortController>();

  async function refresh() {
    setLoading(true); setError('');
    try {
      const payload = await listDataExtractionRecords({ pageSize: 200, sourceOperationId: sourceOperationFilter, query: query.trim() || undefined });
      setRecords(payload.results);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally { setLoading(false); }
  }

  useEffect(() => { void refresh(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [sourceOperationFilter]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return records;
    return records.filter((item) => `${item.name} ${item.task_name} ${item.environment_name} ${item.source_operation_id}`.toLowerCase().includes(needle));
  }, [query, records]);

  function sessionFor(record: DataExtractionRecord) {
    return getTemporaryExtractionSession(temporarySessionKey(record.id));
  }

  async function restore(record: DataExtractionRecord) {
    const controller = new AbortController();
    abortRef.current = controller;
    setRestoreRecord(record); setRestoreProgress(undefined); setRestoreError(''); setRestoreMergeIds(new Set());
    try {
      // 实时采集落下来的记录：命中已经由后端落库，直接按 watch 命中还原行，不用重读日志。
      const result = isLiveCaptureRecord(record)
        ? await restoreLiveCaptureRecord({
            record,
            signal: controller.signal,
            onProgress: (progress) => setRestoreProgress({
              phase: 'extracting',
              hourIndex: progress.current,
              hourCount: progress.total,
              percent: Math.round((progress.current / Math.max(1, progress.total)) * 100),
              processedEntries: 0,
              totalRows: progress.rows,
              message: progress.message,
            }),
          })
        : await restoreExtractionRecord({ record, signal: controller.signal, onProgress: setRestoreProgress });
      const rules = record.rule_snapshots as unknown as DataExtractionRule[];
      const session: TemporaryExtractionSession = {
        key: temporarySessionKey(record.id),
        recordId: record.id,
        taskName: record.task_name || record.name,
        environmentName: record.environment_name,
        startTime: String(record.query_snapshot?.start_time || ''),
        endTime: String(record.query_snapshot?.end_time || ''),
        createdAt: Date.now(),
        results: result.results,
      };
      saveTemporaryExtractionSession(session);
      setRestoreMergeIds(new Set(result.results.filter((item) => item.rows.length > 0).map((item) => item.rule.id)));
      const summary = result.results.map(({ rule, rows }) => ({
        rule_id: rule.id,
        rule_name: rule.name,
        row_count: rows.length,
        fields: rule.fields.map((field) => field.name || field.key),
        output_format: rule.outputFormat,
      }));
      const rowCount = summary.reduce((sum, item) => sum + item.row_count, 0);
      const matchedRuleCount = summary.filter((item) => item.row_count > 0).length;
      const updated = await updateDataExtractionRecord(record.id, {
        status: rowCount > 0 ? 'success' : 'no_result',
        matched_rule_count: matchedRuleCount,
        row_count: rowCount,
        result_summary: summary,
        hour_summary: result.hourSummary,
        error_message: '',
        finished_at: new Date().toISOString(),
      });
      setRecords((current) => current.map((item) => item.id === updated.id ? updated : item));
      setRestoreRecord(updated);
    } catch (exc) {
      const cancelled = controller.signal.aborted || (exc instanceof DOMException && exc.name === 'AbortError');
      const message = cancelled ? '数据还原已停止。' : (exc instanceof Error ? exc.message : String(exc));
      setRestoreError(message);
      void updateDataExtractionRecord(record.id, {
        status: cancelled ? 'cancelled' : 'failed',
        error_message: message,
        finished_at: new Date().toISOString(),
      }).then((updated) => setRecords((current) => current.map((item) => item.id === updated.id ? updated : item))).catch(() => undefined);
    } finally {
      abortRef.current = undefined;
    }
  }

  function openMergedPreview(record: DataExtractionRecord, session: TemporaryExtractionSession, results: TemporaryRuleData[]) {
    const selected = results.filter((item) => item.rows.length > 0);
    if (!selected.length) return;
    const merged = mergeTemporaryRuleData(selected);
    setPreview({ mode: 'merged', record, session, results: selected, fields: merged.fields, rows: merged.rows });
  }

  function openSinglePreview(record: DataExtractionRecord, session: TemporaryExtractionSession, result: TemporaryRuleData) {
    if (!result.rows.length) return;
    setPreview({ mode: 'single', record, session, rule: result.rule, rows: result.rows });
  }

  function openSingleVisualization(record: DataExtractionRecord, result: TemporaryRuleData) {
    if (!result.rows.length) return;
    const fields = result.rule.fields.map((field) => field.name || field.key);
    setPreview(undefined);
    setVisualization({
      record,
      dataset: {
        name: `${record.name} · ${result.rule.name}`,
        signature: `single:${fields.slice().sort().join('|')}`,
        fields,
        rows: result.rows,
      },
    });
  }

  function openMergedVisualization(record: DataExtractionRecord, results: TemporaryRuleData[]) {
    const selected = results.filter((item) => item.rows.length > 0);
    if (!selected.length) return;
    const merged = mergeTemporaryRuleData(selected);
    setPreview(undefined);
    setVisualization({
      record,
      dataset: {
        name: `${record.name} · 合并数据`,
        signature: `merged:${merged.fields.slice().sort().join('|')}`,
        fields: merged.fields,
        rows: merged.rows,
      },
    });
  }

  function openSessionVisualization(record: DataExtractionRecord) {
    const session = sessionFor(record);
    if (!session) return;
    const matched = session.results.filter((item) => item.rows.length > 0);
    if (matched.length > 1) openMergedVisualization(record, matched);
    else if (matched[0]) openSingleVisualization(record, matched[0]);
  }

  function openSession(record: DataExtractionRecord) {
    const session = sessionFor(record);
    if (!session) return;
    const matched = session.results.filter((item) => item.rows.length > 0);
    if (matched.length > 1) {
      openMergedPreview(record, session, matched);
      return;
    }
    const result = matched[0] || session.results[0];
    if (!result) return;
    openSinglePreview(record, session, result);
  }


  useEffect(() => {
    const handler = (event: Event) => {
      const custom = event as CustomEvent<{ context?: Record<string, unknown> }>;
      const context = custom.detail?.context;
      if (!context) return;
      const activeRecord = visualization?.record || preview?.record || restoreRecord || downloadDialog?.record;
      context.page = 'data';
      context.page_label = '数据提取';
      context.data_page = {
        query,
        source_operation_filter: sourceOperationFilter || '',
        record_count: filtered.length,
        total_row_count: filtered.reduce((sum, item) => sum + Number(item.row_count || 0), 0),
        restored_record_count: filtered.filter((item) => Boolean(sessionFor(item))).length,
        active_record: activeRecord ? {
          id: activeRecord.id,
          name: activeRecord.name,
          task_name: activeRecord.task_name || '',
          environment_id: activeRecord.environment || null,
          environment_name: activeRecord.environment_name || '',
          status: activeRecord.status,
          row_count: activeRecord.row_count,
          matched_rule_count: activeRecord.matched_rule_count,
          start_time: String(activeRecord.query_snapshot?.start_time || ''),
          end_time: String(activeRecord.query_snapshot?.end_time || ''),
          source_operation_id: activeRecord.source_operation_id || '',
          rules: activeRecord.rule_snapshots.slice(0, 12).map((raw) => String((raw as { name?: string }).name || '')),
        } : null,
        preview: preview ? {
          mode: preview.mode,
          row_count: preview.rows.length,
          fields: preview.mode === 'merged' ? preview.fields.slice(0, 24) : preview.rule.fields.map((field) => field.name || field.key).slice(0, 24),
        } : null,
        visualization: visualization ? {
          name: visualization.dataset.name,
          fields: visualization.dataset.fields.slice(0, 24),
          row_count: visualization.dataset.rows.length,
        } : null,
      };
      if (activeRecord?.environment) context.environment_id = activeRecord.environment;
      if (activeRecord?.environment_name) context.environment_name = activeRecord.environment_name;
    };
    return registerPageContextReader(handler, 10);
  }, [query, sourceOperationFilter, filtered, preview, visualization, restoreRecord, downloadDialog]);

  async function remove(record: DataExtractionRecord) {
    if (!window.confirm(`确认删除数据提取记录“${record.name}”吗？实际数据本身并未保存在后端。`)) return;
    await deleteDataExtractionRecord(record.id);
    setRecords((current) => current.filter((item) => item.id !== record.id));
  }

  return <main className="data-page">
    <section className="data-page-header">
      <div><span className="eyebrow">DATA EXTRACTION</span><h1>数据提取</h1><p>这里只保存数据提取过程与可重放快照；实际数据需要时由浏览器重新还原，生成后可预览或下载。</p></div>
      <div className="data-page-actions"><label><Search size={15}/><input value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') void refresh(); }} placeholder="搜索提取记录 / 环境 / 任务…"/></label><button className="button secondary compact" onClick={() => void refresh()}><RefreshCw className={loading ? 'spin' : ''} size={14}/> 刷新</button></div>
    </section>

    {sourceOperationFilter && <div className="data-filter-banner"><Database size={15}/><span>仅显示来源日志任务 <strong>{sourceOperationFilter.slice(0, 16)}</strong> 的提取记录</span><button onClick={onClearSourceOperationFilter}><X size={13}/> 清除筛选</button></div>}
    {error && <div className="resource-alert error">{error}</div>}

    <section className="data-summary-strip"><div><Database size={18}/><strong>{filtered.length}</strong><span>条提取记录</span></div><div><strong>{filtered.reduce((sum, item) => sum + Number(item.row_count || 0), 0).toLocaleString()}</strong><span>上次命中行数</span></div><div><strong>{filtered.filter((item) => sessionFor(item)).length}</strong><span>当前浏览器可直接下载</span></div></section>

    <div className="data-table-wrap"><table className="data-table"><thead><tr><th>提取记录</th><th>来源任务</th><th>环境</th><th>原日志时间范围</th><th>规则</th><th>上次结果</th><th>执行时间</th><th>操作</th></tr></thead><tbody>
      {filtered.map((record) => {
        const result = resultLabel(record);
        const session = sessionFor(record);
        // 记录行只留一眼有用的信息：名字 + 编号/审计号 + 任务/环境/时间窗/规则/结果/时间/操作。
        // 其余细节（实时采集时段、命中数、回放方式等）放进悬停提示，不在表格里重复一遍名字。
        const liveDetail = liveCaptureLabel(record);
        const rowTitle = [
          record.name,
          liveDetail ? `${liveDetail}（还原按命中回放，不重读日志）` : '',
          record.task_name ? `来源任务：${record.task_name}` : '',
          record.source_operation_id ? `远程审计：${record.source_operation_id}` : '',
        ].filter(Boolean).join('\n');
        return <tr key={record.id} title={rowTitle}>
          <td><strong>{record.name}</strong><small>#{record.id}{record.source_operation_id ? ` · ${record.source_operation_id.slice(0, 12)}` : ''}</small></td>
          <td>{record.task_name || '—'}</td>
          <td>{record.environment_name || (record.environment ? `环境 #${record.environment}` : '—')}</td>
          <td>{isLiveCaptureRecord(record)
            ? <small title={liveDetail}>{liveCaptureWindow(record) || '—'}</small>
            : <><small>{String(record.query_snapshot?.start_time || '—')}</small><span className="data-range-separator">→</span><small>{String(record.query_snapshot?.end_time || '—')}</small></>}</td>
          <td><div className="data-field-chips">{record.rule_snapshots.slice(0, 4).map((raw, index) => <span key={String((raw as { id?: string }).id || index)}>{String((raw as { name?: string }).name || `规则${index + 1}`)}</span>)}{record.rule_snapshots.length > 4 && <span>+{record.rule_snapshots.length - 4}</span>}</div></td>
          <td><span className={`data-record-status ${result.cls}`}>{result.text}</span>{session && <small className="data-session-ready">当前浏览器已还原</small>}</td>
          <td>{formatMoment(record.updated_at)}</td>
          <td><div className="data-row-actions">
            <button onClick={() => void restore(record)}><RotateCcw size={13}/> {isLiveCaptureRecord(record) ? '回放数据' : '还原'}</button>
            <button disabled={!session} onClick={() => openSession(record)}><Eye size={13}/> 查看</button>
            <button disabled={!session} onClick={() => openSessionVisualization(record)}><Eye size={13}/> 可视化</button>
            <button disabled={!session} onClick={() => {
              const current = sessionFor(record); if (!current) return;
              setDownloadDialog({ record, session: current });
            }}><Download size={13}/> 下载</button>
            {onOpenSourceLog && <button onClick={() => onOpenSourceLog(record)}><FileSearch size={13}/> 原日志</button>}
            <button className="danger" onClick={() => void remove(record)}><Trash2 size={13}/></button>
          </div></td>
        </tr>;
      })}
      {!loading && !filtered.length && <tr><td colSpan={8}><div className="resource-empty inline-empty">暂无数据提取记录。请先在日志定位中点击“提取数据”。</div></td></tr>}
    </tbody></table>{loading && <div className="list-loading-layer"><div className="list-loading-card"><LoaderCircle className="spin" size={16}/>正在读取数据提取记录…</div></div>}</div>

    {downloadDialog && (() => {
      const matched = downloadDialog.session.results.filter((item) => item.rows.length > 0);
      const totalRows = matched.reduce((sum, item) => sum + item.rows.length, 0);
      return <div className="data-download-choice-backdrop" onMouseDown={() => setDownloadDialog(undefined)}>
        <section className="data-download-choice-dialog" onMouseDown={(event) => event.stopPropagation()}>
          <header>
            <div><span className="eyebrow">DOWNLOAD DATA</span><h2>下载 · {downloadDialog.record.name}</h2><p>选择按数据集单独下载，或将本次已命中的数据按时间线合并后下载。</p></div>
            <button className="icon-button" onClick={() => setDownloadDialog(undefined)}><X size={18}/></button>
          </header>
          <div className="data-download-choice-body">
            <section className="data-download-choice-section">
              <div className="data-download-choice-title"><div><Download size={17}/><strong>单个下载</strong></div><span>{matched.length} 个数据集</span></div>
              <div className="data-download-single-list">
                {matched.map((item) => <div key={item.rule.id} className="data-download-single-row">
                  <div><strong>{item.rule.name}</strong><span>{item.rule.fields.map((field) => field.name || field.key).join(' / ')}</span></div>
                  <b>{item.rows.length.toLocaleString()} 行</b>
                  <button className="button secondary compact" onClick={() => downloadTemporaryRuleData({ ...item, namePrefix: downloadDialog.record.name })}><Download size={14}/> 下载</button>
                </div>)}
                {!matched.length && <div className="data-download-empty">当前没有可下载的命中数据。</div>}
              </div>
            </section>
            <section className="data-download-choice-section merged">
              <div className="data-download-choice-title"><div><Merge size={17}/><strong>合并下载</strong></div><span>{totalRows.toLocaleString()} 行</span></div>
              <p>把本次命中的全部数据集合并为一个 CSV，并按原日志时间线排序。</p>
              <button className="button primary" disabled={matched.length < 2} onClick={() => downloadMergedTemporaryRuleData({ results: matched, namePrefix: downloadDialog.record.name })}><Download size={14}/> 合并下载</button>
              {matched.length === 1 && <small>当前只有 1 个命中数据集，可直接使用上方单个下载。</small>}
            </section>
          </div>
        </section>
      </div>;
    })()}

    {restoreRecord && <div className="data-extraction-dialog-backdrop" onMouseDown={() => { if (!abortRef.current) setRestoreRecord(undefined); }}><section className="data-extraction-dialog" onMouseDown={(event) => event.stopPropagation()}>
      <header><div><span className="eyebrow">RESTORE DATA</span><h2>还原数据 · {restoreRecord.name}</h2><p>按小时重新读取原查询范围日志，并在当前浏览器中重新执行当时的数据提取规则。</p></div>{!abortRef.current && <button className="icon-button" onClick={() => setRestoreRecord(undefined)}><X size={18}/></button>}</header>
      <div className="data-extraction-dialog-body">
        {abortRef.current ? <div className="data-extraction-progress-card"><div className="data-extraction-progress-top"><div><LoaderCircle className="spin" size={19}/><strong>{restoreProgress?.message || '正在还原…'}</strong></div><b>{restoreProgress?.percent || 0}%</b></div><div className="data-extraction-progress-track"><span style={{ width: `${restoreProgress?.percent || 0}%` }}/></div><div className="data-extraction-progress-meta"><span>当前小时：{restoreProgress?.currentHour?.label || '准备中'}</span><span>{restoreProgress?.hourIndex || 0} / {restoreProgress?.hourCount || 0}</span><span>已提取 {(restoreProgress?.totalRows || 0).toLocaleString()} 行</span></div></div>
          : restoreError ? <div className="data-extraction-empty error"><strong>还原失败</strong><span>{restoreError}</span></div>
          : (() => {
            const session = sessionFor(restoreRecord);
            if (!session) return <div className="data-extraction-empty"><Database size={28}/><strong>准备重新还原</strong><span>点击下方“开始还原”后才会读取日志。实际数据仍不会保存到后端。</span></div>;
            const mergeResults = session.results.filter((item) => item.rows.length > 0 && restoreMergeIds.has(item.rule.id));
            return <>
              <div className="data-extraction-results">{session.results.map((item) => <div key={item.rule.id} className={item.rows.length ? 'hit' : 'miss'}>
                <label className="data-merge-checkbox"><input type="checkbox" disabled={!item.rows.length} checked={item.rows.length > 0 && restoreMergeIds.has(item.rule.id)} onChange={() => setRestoreMergeIds((current) => { const next = new Set(current); next.has(item.rule.id) ? next.delete(item.rule.id) : next.add(item.rule.id); return next; })}/></label>
                <div><strong>{item.rule.name}</strong><span>{item.rule.fields.map((field) => field.name || field.key).join(' / ')}</span></div><b>{item.rows.length.toLocaleString()} 行</b>
                <button className="button secondary compact" disabled={!item.rows.length} onClick={() => downloadTemporaryRuleData({ ...item, namePrefix: restoreRecord.name })}><Download size={14}/> 单独下载</button>
                <button className="button ghost compact" disabled={!item.rows.length} onClick={() => openSinglePreview(restoreRecord, session, item)}><Eye size={14}/> 查看</button>
                <button className="button ghost compact" disabled={!item.rows.length} onClick={() => openSingleVisualization(restoreRecord, item)}><Eye size={14}/> 绘图</button>
              </div>)}</div>
              {session.results.filter((item) => item.rows.length > 0).length > 1 && <div className="data-merge-export-bar"><div><Merge size={16}/><span>已选 <strong>{mergeResults.length}</strong> 个数据集，合并后按时间线排序。</span></div><div className="data-merge-export-actions"><button className="button secondary compact" disabled={mergeResults.length < 2} onClick={() => openMergedPreview(restoreRecord, session, mergeResults)}><Eye size={14}/> 查看合并</button><button className="button secondary compact" disabled={mergeResults.length < 2} onClick={() => openMergedVisualization(restoreRecord, mergeResults)}><Eye size={14}/> 可视化合并</button><button className="button primary compact" disabled={mergeResults.length < 2} onClick={() => downloadMergedTemporaryRuleData({ results: mergeResults, namePrefix: restoreRecord.name })}><Download size={14}/> 合并下载 CSV</button></div></div>}
            </>;
          })()}
      </div>
      <footer>{abortRef.current ? <button className="button danger" onClick={() => abortRef.current?.abort()}>停止还原</button> : <><button className="button secondary" onClick={() => setRestoreRecord(undefined)}>关闭</button>{!sessionFor(restoreRecord) && !restoreError && <button className="button primary" onClick={() => void restore(restoreRecord)}>开始还原</button>}{restoreError && <button className="button primary" onClick={() => void restore(restoreRecord)}>重新还原</button>}</>}</footer>
    </section></div>}

    {preview && <div className="data-preview-backdrop" onMouseDown={() => setPreview(undefined)}><section className="data-preview-dialog data-preview-dialog-wide" onMouseDown={(event) => event.stopPropagation()}><header><div><span className="eyebrow">DATA PREVIEW</span><h2>{preview.mode === 'merged' ? '合并数据' : preview.rule.name}</h2><p>{preview.mode === 'merged' ? `已合并 ${preview.results.length} 个数据集 · 按原日志时间线排序 · 共 ${preview.rows.length.toLocaleString()} 行` : `当前浏览器临时数据 · 共 ${preview.rows.length.toLocaleString()} 行`}</p></div><button className="icon-button" onClick={() => setPreview(undefined)}><X size={18}/></button></header><div className="data-preview-table-wrap"><table><thead>{preview.mode === 'merged' ? <tr><th>时间</th><th>采集规则</th><th>子系统</th><th>模块</th><th>PID</th><th>TID</th><th>TraceID</th><th>源文件</th><th>行号</th>{preview.fields.map((field) => <th key={field}>{field}</th>)}</tr> : <tr><th>时间</th>{preview.rule.fields.map((field) => <th key={field.id}>{field.name || field.key}</th>)}</tr>}</thead><tbody>{preview.mode === 'merged' ? preview.rows.slice(0, 1000).map((row, index) => <tr key={`${row.ruleId}-${row.sourceFile}-${row.lineNumber}-${row.timestamp}-${index}`}><td className="data-original-timestamp">{row.timestamp}</td><td>{row.ruleName}</td><td>{row.subsystem || ''}</td><td>{row.module || ''}</td><td>{row.processId || ''}</td><td>{row.threadId || ''}</td><td>{row.traceId || ''}</td><td>{row.sourceFile}</td><td>{row.lineNumber}</td>{preview.fields.map((field) => <td key={field}>{String(row.values[field] ?? '')}</td>)}</tr>) : preview.rows.slice(0, 1000).map((row, index) => <tr key={`${row.timestamp}-${index}`}><td className="data-original-timestamp">{row.timestamp}</td>{preview.rule.fields.map((field) => <td key={field.id}>{String(row.values[field.name || field.key] ?? '')}</td>)}</tr>)}</tbody></table></div><footer><span>{preview.rows.length > 1000 ? '仅预览前 1000 行，下载可获取本次全部数据。' : `共 ${preview.rows.length} 行。`}</span><div className="data-preview-footer-actions"><button className="button secondary" onClick={() => preview.mode === 'merged' ? openMergedVisualization(preview.record, preview.results) : openSingleVisualization(preview.record, { rule: preview.rule, rows: preview.rows })}><Eye size={14}/> 可视化</button>{preview.mode === 'merged' ? <button className="button primary" onClick={() => downloadMergedTemporaryRuleData({ results: preview.results, namePrefix: preview.record.name })}><Download size={14}/> 下载合并 CSV</button> : <button className="button primary" onClick={() => downloadTemporaryRuleData({ rule: preview.rule, rows: preview.rows, namePrefix: preview.record.name })}><Download size={14}/> 下载</button>}</div></footer></section></div>}
    {visualization && <DataVisualizationDialog dataset={visualization.dataset} onClose={() => setVisualization(undefined)} onOpenSourceLog={onOpenSourceLog ? () => onOpenSourceLog(visualization.record) : undefined}/>}
  </main>;
}
