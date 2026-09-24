import { useEffect, useMemo, useState } from 'react';
import { Activity, CheckCircle2, Database, Download, Eye, LoaderCircle, Merge, Plus, Radio, Square, X } from 'lucide-react';
import type { DataExtractionRule, ExtractedDataRow } from '../rendering/dataExtractionRules';
import type { ExtractionProgress } from '../rendering/dataExtractionRuntime';
import { mergeTemporaryRuleData } from '../rendering/extractedDataStore';
import type { VisualizationDataset } from '../rendering/dataVisualization';
import { DataVisualizationDialog } from './DataVisualizationDialog';

export interface DataExtractionCandidate {
  rule: DataExtractionRule;
}

export interface DataExtractionResultView {
  rule: DataExtractionRule;
  rows: ExtractedDataRow[];
}

interface Props {
  open: boolean;
  phase: 'select' | 'running' | 'done' | 'error';
  candidates: DataExtractionCandidate[];
  selectedIds: Set<string>;
  progress?: ExtractionProgress;
  results?: DataExtractionResultView[];
  error?: string;
  recordSaved?: boolean;
  onToggle: (id: string) => void;
  onStart: () => void;
  onCancel: () => void;
  onClose: () => void;
  onDownload: (result: DataExtractionResultView) => void;
  onDownloadMerged: (results: DataExtractionResultView[]) => void;
  onOpenData: () => void;
  datasetName?: string;
  liveListening?: boolean;
  onStartLiveExtraction?: () => void;
  /**
   * 把提取器加入/移出实时采集清单（写回提取器上的 liveCapture）。
   * 之前这个开关只存在于「设置 → 日志规则 → 数据提取」和采集面板里，
   * 在本页面对着的日志上配置提取器时还得跳出去，现在就在这个窗口里完成。
   */
  onSetLiveCapture?: (ruleIds: string[], enabled: boolean) => void | Promise<void>;
  /**
   * 当前**真正**在实时采集清单里的提取器 id。
   *
   * 不能读 `candidates[].rule.liveCapture`：candidates 是打开弹窗那一刻的快照，
   * 加入实时采集只改了规则本身，快照不会跟着变 —— 结果就是点了按钮没有任何反馈，
   * 用户只能靠猜自己到底加没加上。
   */
  liveCaptureIds?: Set<string>;
}

export function DataExtractionRunDialog(props: Props) {
  const [previewResult, setPreviewResult] = useState<DataExtractionResultView>();
  const [visualization, setVisualization] = useState<VisualizationDataset>();
  const matchedResults = useMemo(() => (props.results || []).filter((item) => item.rows.length > 0), [props.results]);
  const resultSignature = matchedResults.map((item) => `${item.rule.id}:${item.rows.length}`).join('|');
  const [mergeSelectedIds, setMergeSelectedIds] = useState<Set<string>>(new Set());
  const [liveBusyId, setLiveBusyId] = useState('');
  const [liveMessage, setLiveMessage] = useState('');

  async function setLiveCapture(ruleIds: string[], enabled: boolean) {
    if (!props.onSetLiveCapture || !ruleIds.length) return;
    setLiveBusyId(enabled ? 'bulk' : ruleIds[0]);
    setLiveMessage('');
    try {
      await props.onSetLiveCapture(ruleIds, enabled);
      setLiveMessage(enabled
        ? `已把 ${ruleIds.length} 个提取器加入实时采集清单，可在「采集」面板查看条数。`
        : `已把 ${ruleIds.length} 个提取器移出实时采集清单。`);
    } catch (error) {
      setLiveMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setLiveBusyId('');
    }
  }

  useEffect(() => {
    if (props.phase !== 'done') return;
    setMergeSelectedIds(new Set(matchedResults.map((item) => item.rule.id)));
    // resultSignature 用于在一次新的提取完成后刷新默认勾选。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.phase, resultSignature]);


  function openVisualization(result: DataExtractionResultView) {
    if (!result.rows.length) return;
    const fields = result.rule.fields.map((field) => field.name || field.key);
    setVisualization({
      name: `${props.datasetName || '当前提取数据'} · ${result.rule.name}`,
      signature: `single:${fields.slice().sort().join('|')}`,
      fields,
      rows: result.rows,
    });
  }

  function openMergedVisualization(results: DataExtractionResultView[]) {
    const selected = results.filter((item) => item.rows.length > 0);
    if (!selected.length) return;
    const merged = mergeTemporaryRuleData(selected);
    setVisualization({
      name: `${props.datasetName || '当前提取数据'} · 合并数据`,
      signature: `merged:${merged.fields.slice().sort().join('|')}`,
      fields: merged.fields,
      rows: merged.rows,
    });
  }

  if (!props.open) return null;
  const mergeResults = matchedResults.filter((item) => mergeSelectedIds.has(item.rule.id));
  const mergeRowCount = mergeResults.reduce((sum, item) => sum + item.rows.length, 0);

  return <>
  <div className="data-extraction-dialog-backdrop" role="presentation" onMouseDown={() => props.phase === 'running' ? undefined : props.onClose()}>
    <section className="data-extraction-dialog" role="dialog" aria-modal="true" aria-label="提取数据" onMouseDown={(event) => event.stopPropagation()}>
      <header>
        <div><span className="eyebrow">DATA EXTRACTION</span><h2>提取数据</h2><p>数据只在当前浏览器内存中生成；后端仅记录本次提取过程，不保存实际数据。</p></div>
        {props.phase !== 'running' && <button className="icon-button" onClick={props.onClose}><X size={18}/></button>}
      </header>

      <div className="data-extraction-dialog-body">
        {props.phase === 'select' && <>
          {!props.candidates.length ? <div className="data-extraction-empty"><Database size={30}/><strong>暂无可用的数据采集能力</strong><span>可以在“设置 → 日志规则 → 数据提取”中新增或启用数据提取器。</span></div> : <>
            <div className="data-extraction-select-head"><strong>已有 {props.candidates.length} 项数据采集能力</strong><span>勾选本次要提取的数据；需要长期盯着的，用右侧「加入实时采集」放进实时采集清单。</span></div>
            <div className="data-extraction-rule-options">
              {props.candidates.map(({ rule }) => {
                const inLiveCapture = props.liveCaptureIds ? props.liveCaptureIds.has(rule.id) : rule.liveCapture === true;
                return <div key={rule.id} className={`data-extraction-rule-option ${props.selectedIds.has(rule.id) ? 'selected' : ''}`}>
                <label>
                  <input type="checkbox" checked={props.selectedIds.has(rule.id)} onChange={() => props.onToggle(rule.id)}/>
                  <span><strong>{rule.name}</strong><small>{rule.description || '数据提取能力'}</small><em>{rule.fields.map((field) => field.name || field.key).join(' / ')}</em></span>
                  <b>{rule.outputFormat === 'text' ? 'TXT' : 'CSV'}</b>
                  <small>{rule.modules.length ? rule.modules.join(' / ') : '通用'}</small>
                </label>
                {props.onSetLiveCapture && <button
                  type="button"
                  className={`data-live-capture-add ${inLiveCapture ? 'on' : ''}`}
                  disabled={Boolean(liveBusyId)}
                  onClick={() => void setLiveCapture([rule.id], !inLiveCapture)}
                  title={inLiveCapture ? '从实时采集清单中移除这个提取器' : '把这个提取器加入实时采集清单'}
                >
                  {liveBusyId === rule.id ? <LoaderCircle className="spin" size={12}/> : inLiveCapture ? <Radio size={12}/> : <Plus size={12}/>}
                  {inLiveCapture ? '已在实时采集' : '加入实时采集'}
                </button>}
              </div>;
              })}
            </div>
            {liveMessage && <div className="data-live-capture-note">{liveMessage}</div>}
          </>}
        </>}

        {props.phase === 'running' && <>
          <div className="data-extraction-progress-card">
            <div className="data-extraction-progress-top"><div><LoaderCircle className="spin" size={19}/><strong>{props.progress?.message || '正在提取数据…'}</strong></div><b>{props.progress?.percent ?? 0}%</b></div>
            <div className="data-extraction-progress-track"><span style={{ width: `${props.progress?.percent ?? 0}%` }}/></div>
            <div className="data-extraction-progress-meta"><span>当前小时：{props.progress?.currentHour?.label || '准备中'}</span><span>小时进度：{Math.min(props.progress?.hourIndex ?? 0, props.progress?.hourCount ?? 0)} / {props.progress?.hourCount ?? 0}</span><span>已提取：{(props.progress?.totalRows ?? 0).toLocaleString()} 行</span></div>
          </div>
          <div className="data-extraction-hour-hint">按小时分片执行，完成一个小时即进入下一个小时；停止后不会继续处理剩余时间段。</div>
        </>}

        {props.phase === 'done' && <>
          <div className="data-extraction-complete"><CheckCircle2 size={24}/><div><strong>数据提取完成</strong><span>共提取 {matchedResults.reduce((sum, item) => sum + item.rows.length, 0).toLocaleString()} 行 · {matchedResults.length} 个数据集</span></div></div>
          <div className="data-extraction-results">
            {(props.results || []).map((result) => <div key={result.rule.id} className={result.rows.length ? 'hit' : 'miss'}>
              <label className="data-merge-checkbox" title={result.rows.length ? '勾选后可合并导出 CSV' : '本次未提取到数据'}>
                <input type="checkbox" disabled={!result.rows.length} checked={result.rows.length > 0 && mergeSelectedIds.has(result.rule.id)} onChange={() => setMergeSelectedIds((current) => { const next = new Set(current); next.has(result.rule.id) ? next.delete(result.rule.id) : next.add(result.rule.id); return next; })}/>
              </label>
              <div><strong>{result.rule.name}</strong><span>{result.rule.fields.map((field) => field.name || field.key).join(' / ')}</span></div>
              <b>{result.rows.length.toLocaleString()} 行</b>
              <button className="button ghost compact" disabled={!result.rows.length} onClick={() => setPreviewResult(result)}><Eye size={14}/> 查看</button>
              <button className="button ghost compact" disabled={!result.rows.length} onClick={() => openVisualization(result)}><Activity size={14}/> 绘图</button>
              <button className="button secondary compact" disabled={!result.rows.length} onClick={() => props.onDownload(result)}><Download size={14}/> 单独下载</button>
            </div>)}
          </div>
          {matchedResults.length > 1 && <div className="data-merge-export-bar">
            <div><Merge size={16}/><span>已选 <strong>{mergeResults.length}</strong> 个数据集 · {mergeRowCount.toLocaleString()} 行，将按时间线合并，字段取并集。</span></div>
            <div className="data-merge-export-actions">
              <button className="button secondary compact" disabled={mergeResults.length < 2} onClick={() => openMergedVisualization(mergeResults)}><Activity size={14}/> 可视化合并</button>
              <button className="button primary compact" disabled={mergeResults.length < 2} onClick={() => props.onDownloadMerged(mergeResults)}><Download size={14}/> 合并下载 CSV</button>
            </div>
          </div>}
          {props.recordSaved && <div className="data-extraction-record-note">本次仅保存了提取记录与规则/查询快照。关闭后实际数据会释放，之后可在“数据”页重新还原并下载。</div>}
        </>}

        {props.phase === 'error' && <div className="data-extraction-empty error"><X size={28}/><strong>数据提取失败</strong><span>{props.error || '未知错误'}</span></div>}
      </div>

      <footer>
        {props.phase === 'select' && <>{props.onSetLiveCapture && <button className="button secondary" disabled={props.selectedIds.size === 0 || Boolean(liveBusyId)} onClick={() => void setLiveCapture([...props.selectedIds], true)} title="把勾选的提取器一次性加入实时采集清单"><Radio size={14}/> 添加实时采集</button>}<span className="data-extraction-footer-spacer"/><button className="button ghost" onClick={props.onClose}>取消</button>{props.liveListening && <button className="button secondary" disabled={props.selectedIds.size === 0} onClick={props.onStartLiveExtraction}>实时提取</button>}<button className="button primary" disabled={!props.candidates.length || props.selectedIds.size === 0} onClick={props.onStart}>开始提取</button></>}
        {props.phase === 'running' && <button className="button danger" onClick={props.onCancel}><Square size={14}/> 停止提取</button>}
        {(props.phase === 'done' || props.phase === 'error') && <><button className="button secondary" onClick={props.onClose}>关闭</button>{props.recordSaved && <button className="button primary" onClick={props.onOpenData}>进入数据</button>}</>}
      </footer>
    </section>
  </div>
  {previewResult && <div className="data-preview-backdrop" onMouseDown={() => setPreviewResult(undefined)}><section className="data-preview-dialog data-preview-dialog-wide" onMouseDown={(event) => event.stopPropagation()}>
    <header><div><span className="eyebrow">DATA PREVIEW</span><h2>{previewResult.rule.name}</h2><p>当前提取结果 · 共 {previewResult.rows.length.toLocaleString()} 行</p></div><button className="icon-button" onClick={() => setPreviewResult(undefined)}><X size={18}/></button></header>
    <div className="data-preview-table-wrap"><table><thead><tr><th>时间</th>{previewResult.rule.fields.map((field) => <th key={field.id}>{field.name || field.key}</th>)}</tr></thead><tbody>{previewResult.rows.slice(0, 1000).map((row, index) => <tr key={`${row.timestamp}-${row.sourceFile}-${row.lineNumber}-${index}`}><td className="data-original-timestamp">{row.timestamp}</td>{previewResult.rule.fields.map((field) => <td key={field.id}>{String(row.values[field.name || field.key] ?? '')}</td>)}</tr>)}</tbody></table></div>
    <footer><span>{previewResult.rows.length > 1000 ? '仅预览前 1000 行，下载可获取全部数据。' : `共 ${previewResult.rows.length} 行。`}</span><div className="data-preview-footer-actions"><button className="button secondary" onClick={() => openVisualization(previewResult)}><Activity size={14}/> 绘图</button><button className="button primary" onClick={() => props.onDownload(previewResult)}><Download size={14}/> 下载</button></div></footer>
  </section></div>}
  {visualization && <DataVisualizationDialog dataset={visualization} onClose={() => setVisualization(undefined)}/>}
  </>;
}
