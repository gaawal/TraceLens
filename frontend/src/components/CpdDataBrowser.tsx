import { useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle, FileSpreadsheet, Image as ImageIcon, LoaderCircle, RefreshCw, Search, X,
} from 'lucide-react';
import {
  cpdDataFileUrl, getCpdDataBrowser, getCpdSheet,
  type CpdDataBrowserFile, type CpdDataBrowserIndex, type CpdRawSheet,
  type CpdReportSummary, type EnvironmentSummary,
} from '../api/resourceApi';

interface Props {
  environment: EnvironmentSummary;
  report: CpdReportSummary;
  onClose: () => void;
}

const PAGE_SIZE = 200;

/**
 * 测校数据浏览器 — 左边是检索到的文件路径，右边是它本身。
 *
 * 三条刻意的决定：
 *
 * 1. **左边是路径字符串，不是文件夹树。** 用户要的是「这次检索命中了哪些文件」，
 *    目录层级已经在路径里了；做成可折叠的文件管理器只会多一层点击。
 * 2. **右边直接渲染表格。** 不做「有没有时间列」这类结构判断，也不按时间窗过滤行 ——
 *    测校表格的格式本来就不统一，结构校验的代价是「表就在那儿却显示不出来」。
 * 3. **图片同样在这里看。** 检索结果里带图时，点一下就能在原位看到，不用下载。
 */
export function CpdDataBrowser({ environment, report, onClose }: Props) {
  const subsystem = report.subsystem || '';
  const module = report.module || '';

  const [index, setIndex] = useState<CpdDataBrowserIndex>();
  const [loadingIndex, setLoadingIndex] = useState(true);
  const [indexError, setIndexError] = useState('');
  const [query, setQuery] = useState('');

  const [activeFile, setActiveFile] = useState<CpdDataBrowserFile>();
  const [sheet, setSheet] = useState('');
  const [sheetData, setSheetData] = useState<CpdRawSheet>();
  const [loadingSheet, setLoadingSheet] = useState(false);
  const [sheetError, setSheetError] = useState('');
  const [page, setPage] = useState(1);
  const [sort, setSort] = useState<{ column: string; descending: boolean }>({ column: '', descending: false });
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    let stale = false;
    setLoadingIndex(true);
    setIndexError('');
    void getCpdDataBrowser(environment.id, subsystem, module)
      .then((value) => {
        if (stale) return;
        setIndex(value);
        const first = value.files[0];
        setActiveFile(first);
        setSheet(first?.sheets?.[0] || '');
      })
      .catch((error) => { if (!stale) setIndexError(error instanceof Error ? error.message : String(error)); })
      .finally(() => { if (!stale) setLoadingIndex(false); });
    return () => { stale = true; };
  }, [environment.id, subsystem, module, reloadToken]);

  useEffect(() => {
    if (!activeFile || activeFile.kind !== 'excel' || !sheet) return undefined;
    let stale = false;
    setLoadingSheet(true);
    setSheetError('');
    void getCpdSheet(environment.id, {
      subsystem, module, path: activeFile.path, sheet,
      page: String(page), page_size: String(PAGE_SIZE),
      sort_column: sort.column, descending: String(sort.descending),
    })
      .then((value) => { if (!stale) setSheetData(value); })
      .catch((error) => { if (!stale) setSheetError(error instanceof Error ? error.message : String(error)); })
      .finally(() => { if (!stale) setLoadingSheet(false); });
    return () => { stale = true; };
  }, [environment.id, subsystem, module, activeFile, sheet, page, sort]);

  useEffect(() => {
    const key = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', key);
    return () => window.removeEventListener('keydown', key);
  }, [onClose]);

  const files = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const all = index?.files || [];
    return needle ? all.filter((file) => file.path.toLowerCase().includes(needle)) : all;
  }, [index, query]);

  const root = index?.root || '';
  const relativePath = (path: string) => (root && path.startsWith(root + '/') ? path.slice(root.length + 1) : path);
  const pageCount = Math.max(1, Math.ceil((sheetData?.total || 0) / PAGE_SIZE));

  function pickFile(file: CpdDataBrowserFile) {
    setActiveFile(file);
    setSheet(file.sheets?.[0] || '');
    setPage(1);
    setSort({ column: '', descending: false });
    setSheetData(undefined);
  }

  return (
    <div className="resource-modal-backdrop" onMouseDown={onClose}>
      <section role="dialog" aria-modal="true" aria-label="测校数据" className="cpd-data-browser" onMouseDown={(event) => event.stopPropagation()}>
        <header className="cpd-browser-header">
          <div>
            <h2><FileSpreadsheet size={17} /> 测校数据 · {subsystem}/{module}</h2>
            <p title={root}>{index ? `${index.files.length} 个文件 · ${root}` : '正在读取远端目录…'}</p>
          </div>
          <button type="button" className="icon-button" onClick={() => setReloadToken((value) => value + 1)} title="重新读取文件列表" aria-label="重新读取">
            <RefreshCw className={loadingIndex ? 'spin' : ''} size={17} />
          </button>
          <button type="button" className="icon-button" onClick={onClose} aria-label="关闭"><X size={18} /></button>
        </header>

        <div className="cpd-browser-body">
          <aside className="cpd-browser-files" aria-label="文件列表">
            <label className="cpd-browser-filter">
              <Search size={13} />
              <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="按路径过滤…" />
              {query && <button type="button" onClick={() => setQuery('')} aria-label="清空"><X size={12} /></button>}
            </label>
            {loadingIndex && <div className="cpd-browser-note"><LoaderCircle className="spin" size={14} /> 正在读取远端目录…</div>}
            {indexError && <div className="cpd-browser-note error"><AlertTriangle size={14} /> {indexError}</div>}
            {!loadingIndex && !indexError && files.length === 0 && (
              <div className="cpd-browser-note">{index?.files.length ? '没有匹配该路径的文件。' : '这个子系统/模块下没有检索到文件。'}</div>
            )}
            <div className="cpd-browser-file-list">
              {files.map((file) => (
                <div className={`cpd-browser-file ${activeFile?.path === file.path ? 'active' : ''}`} key={file.path}>
                  <button type="button" className="cpd-browser-file-row" title={file.path} onClick={() => pickFile(file)}>
                    {file.kind === 'image' ? <ImageIcon size={14} /> : <FileSpreadsheet size={14} />}
                    <span>{relativePath(file.path)}</span>
                  </button>
                  {file.kind === 'excel' && (file.sheets || []).length > 0 && (
                    <div className="cpd-browser-sheets">
                      {(file.sheets || []).map((name) => (
                        <button
                          type="button"
                          key={name}
                          className={activeFile?.path === file.path && sheet === name ? 'active' : ''}
                          onClick={() => { setActiveFile(file); setSheet(name); setPage(1); setSort({ column: '', descending: false }); }}
                        >
                          {name}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
            {!!index?.errors.length && (
              <details className="cpd-browser-note">
                <summary>{index.errors.length} 个文件读取失败</summary>
                {index.errors.map((item) => <p key={item.path}>{relativePath(item.path)}：{item.error}</p>)}
              </details>
            )}
          </aside>

          <section className="cpd-browser-view" aria-label="文件内容">
            {!activeFile && <div className="cpd-browser-note">从左侧选一个文件。</div>}

            {activeFile?.kind === 'image' && (
              <div className="cpd-browser-image">
                <img src={cpdDataFileUrl(environment.id, subsystem, module, activeFile.path)} alt={activeFile.name} />
              </div>
            )}

            {activeFile?.kind === 'excel' && (
              <>
                <div className="cpd-browser-view-head">
                  <span title={activeFile.path}>{relativePath(activeFile.path)}</span>
                  {sheetData && <em>{sheet} · {sheetData.total} 行</em>}
                  {loadingSheet && <LoaderCircle className="spin" size={13} />}
                </div>
                {sheetError && <div className="cpd-browser-note error"><AlertTriangle size={14} /> {sheetError}</div>}
                {!sheetError && sheetData?.empty && <div className="cpd-browser-note">这张表是空的。</div>}
                {!sheetError && sheetData && !sheetData.empty && (
                  <div className="cpd-browser-table-scroll">
                    <table className="cpd-browser-table">
                      <thead>
                        <tr>
                          <th className="cpd-browser-line-col">行</th>
                          {sheetData.headers.map((header, column) => (
                            <th key={column}>
                              <button
                                type="button"
                                onClick={() => { setSort({ column: String(column), descending: sort.column === String(column) && !sort.descending }); setPage(1); }}
                              >
                                {header || `列 ${column + 1}`}
                                {sort.column === String(column) ? (sort.descending ? ' ↓' : ' ↑') : ''}
                              </button>
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {sheetData.rows.map((row) => (
                          <tr key={row.line}>
                            <td className="cpd-browser-line-col">{row.line}</td>
                            {sheetData.headers.map((_, column) => (
                              <td key={column}>{row.cells[column] === null || row.cells[column] === undefined ? '' : String(row.cells[column])}</td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                {!sheetError && sheetData && !sheetData.empty && pageCount > 1 && (
                  <footer className="cpd-browser-pager">
                    <button type="button" className="button ghost compact" disabled={page <= 1 || loadingSheet} onClick={() => setPage((value) => value - 1)}>上一页</button>
                    <span>第 {page} / {pageCount} 页 · 共 {sheetData.total} 行</span>
                    <button type="button" className="button ghost compact" disabled={page >= pageCount || loadingSheet} onClick={() => setPage((value) => value + 1)}>下一页</button>
                  </footer>
                )}
              </>
            )}
          </section>
        </div>
      </section>
    </div>
  );
}
