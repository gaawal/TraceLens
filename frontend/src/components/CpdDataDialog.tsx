import { useEffect, useMemo, useState } from 'react';
import { X, LoaderCircle } from 'lucide-react';
import { getCpdDataFiles, getCpdExcelPreview, type CpdDataIndex, type CpdSheetPreview, type CpdReportSummary, type EnvironmentSummary } from '../api/resourceApi';

export function CpdDataDialog({ environment, report, onClose }: {environment: EnvironmentSummary; report: CpdReportSummary; onClose: () => void}) {
  const query = useMemo(() => ({subsystem: report.subsystem || '', module: report.module || '', start_time: report.start_time || '', end_time: report.stop_time || ''}), [report]);
  const [index, setIndex] = useState<CpdDataIndex>();
  const [path, setPath] = useState('');
  const [sheet, setSheet] = useState('');
  const [page, setPage] = useState(1);
  const [sort, setSort] = useState({column: '', descending: false});
  const [preview, setPreview] = useState<CpdSheetPreview>();
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let stale = false;
    getCpdDataFiles(environment.id, query).then(value => {
      if (stale) return;
      setIndex(value); setPath(value.files[0]?.path || ''); setSheet(value.files[0]?.sheets[0]?.name || '');
    }).catch(e => {if (!stale) setError(String(e.message || e));}).finally(() => {if (!stale) setLoading(false);});
    return () => {stale = true;};
  }, [environment.id, query]);
  useEffect(() => {
    if (!path || !sheet) return;
    let stale = false;
    setLoading(true); setPreview(undefined); setError('');
    getCpdExcelPreview(environment.id, {...query, path, sheet, page: String(page), page_size: '100', sort_column: sort.column, descending: String(sort.descending)}).then(value => {if (!stale) setPreview(value);})
      .catch(e => {if (!stale) setError(String(e.message || e));}).finally(() => {if (!stale) setLoading(false);});
    return () => {stale = true;};
  }, [environment.id, query, path, sheet, page, sort]);
  useEffect(() => {const key = (e: KeyboardEvent) => {if (e.key === 'Escape') onClose();}; window.addEventListener('keydown', key); return () => window.removeEventListener('keydown', key);}, [onClose]);
  return <div className="resource-modal-backdrop" onMouseDown={onClose}><section role="dialog" aria-modal="true" aria-label="测校 Excel 数据" className="cpd-report-viewer cpd-data-viewer" onMouseDown={e => e.stopPropagation()}>
    <header><div><h2>测校数据 · {report.subsystem}/{report.module}</h2><p>{query.start_time} — {query.end_time} · 按 Excel 时间列过滤（含起止时刻）</p></div><button className="icon-button" aria-label="关闭测校数据" onClick={onClose}><X size={18}/></button></header>
    {index && <details className="cpd-data-files" open><summary>文件 · {index.files.length} 个 · {index.root}</summary><div role="tablist" aria-label="Excel 文件">{index.files.map(file => <button role="tab" aria-selected={path === file.path} title={file.path} key={file.path} onClick={() => {setPath(file.path); setSheet(file.sheets[0]?.name || ''); setPage(1); setSort({column: '', descending: false});}}>{file.path}</button>)}</div></details>}
    {index?.files.find(f => f.path === path) && <div role="tablist" aria-label="工作表" className="cpd-sheet-tabs">{index.files.find(f => f.path === path)!.sheets.map(s => <button role="tab" aria-selected={sheet === s.name} key={s.name} onClick={() => {setSheet(s.name); setPage(1); setSort({column:'',descending:false});}}>{s.name} · {s.filter_status === 'missing_time_column' ? '缺少时间列' : `${s.matched_rows} 行`}</button>)}</div>}
    <div role="status" className="cpd-data-notice">{loading ? <><LoaderCircle size={14} className="spin"/> 正在读取远端工作簿…</> : error || preview?.warning || (index && !index.files.length ? '此时间范围没有匹配数据。' : preview ? `时间列：${preview.time_column} · ${preview.total} 行匹配 · ${preview.invalid_time_rows || 0} 行时间无法解析` : '')}</div>
    {!!index?.errors.length && <details className="cpd-data-notice"><summary>{index.errors.length} 个文件读取失败</summary>{index.errors.map(e => <p key={e.path}>{e.path}：{e.error}</p>)}</details>}
    <div className="cpd-data-table-scroll" role="tabpanel"><table className="cpd-report-table"><thead><tr><th>原始行</th>{preview?.headers.map((h,i) => <th key={i} aria-sort={sort.column === String(i) ? sort.descending ? 'descending' : 'ascending' : 'none'}><button onClick={() => {setSort({column:String(i),descending:sort.column === String(i) && !sort.descending}); setPage(1);}}>{h || `列 ${i+1}`} {sort.column === String(i) ? sort.descending ? '↓' : '↑' : '↕'}</button></th>)}</tr></thead><tbody>{preview?.rows.map(row => <tr key={row.line}><td>{row.line}</td>{preview.headers.map((_,i) => <td key={i}>{String(row.cells[i] ?? '')}</td>)}</tr>)}</tbody></table></div>
    <footer className="cpd-pagination"><span>{path} · {sheet}</span><button disabled={loading || page <= 1} onClick={() => setPage(p => p-1)}>上一页</button><span>{page} / {Math.max(1, Math.ceil((preview?.total || 0)/100))}</span><button disabled={loading || page*100 >= (preview?.total || 0)} onClick={() => setPage(p => p+1)}>下一页</button></footer>
  </section></div>;
}
