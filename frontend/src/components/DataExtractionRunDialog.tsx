import { useEffect, useMemo, useRef, useState } from 'react';
import { Activity, CheckCircle2, Database, Download, Eye, LoaderCircle, Merge, Plus, Radio, Square, X } from 'lucide-react';
import type { LiveCaptureProgress } from '../services/liveCaptureProgress';
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
  /**
   * 「开始采集（实时采集）」：把勾选的采集项作为这一轮的实时采集清单。
   * 返回一句话说明结果（例如「已加入，请打开实时监听」）。
   */
  onStartLiveCollection?: (ruleIds: string[]) => void | string | Promise<void | string>;
  /**
   * 当前**真正**在实时采集清单里的提取器 id。
   *
   * 不能读 `candidates[].rule.liveCapture`：candidates 是打开弹窗那一刻的快照，
   * 加入实时采集只改了规则本身，快照不会跟着变 —— 结果就是点了按钮没有任何反馈，
   * 用户只能靠猜自己到底加没加上。
   */
  liveCaptureIds?: Set<string>;
  /**
   * 实时采集的进度。实时监听开着时，每个采集项都显示已采条数；
   * 这就是「统一的进度展示」—— 和一次批量采集共用同一块进度区域。
   */
  liveProgress?: LiveCaptureProgress;
  /** 实时监听是否开着；决定这块进度是在实时计数还是在跑批量采集。 */
  liveActive?: boolean;
  /**
   * 当前有没有可做批量采集的已解析日志。
   * 实时监听期间没有 —— 但**实时采集照常可用**，所以这里只禁用批量那一个按钮，
   * 而不是像以前那样把整个弹窗变成「当前没有可提取的已解析日志」。
   */
  batchAvailable?: boolean;
}

export function DataExtractionRunDialog(props: Props) {
  const [previewResult, setPreviewResult] = useState<DataExtractionResultView>();
  const [visualization, setVisualization] = useState<VisualizationDataset>();
  const matchedResults = useMemo(() => (props.results || []).filter((item) => item.rows.length > 0), [props.results]);
  const resultSignature = matchedResults.map((item) => `${item.rule.id}:${item.rows.length}`).join('|');
  const [mergeSelectedIds, setMergeSelectedIds] = useState<Set<string>>(new Set());
  const [liveBusyId, setLiveBusyId] = useState('');
  /**
   * 弹窗位置。未拖过时为 undefined —— 交给 CSS 默认摆放（右侧对齐），
   * 拖过之后由这里接管，用户可以把它挪到不挡日志的地方。
   */
  const [dragPos, setDragPos] = useState<{ left: number; top: number }>();
  const dragStateRef = useRef<{ pointerId: number; startX: number; startY: number; originLeft: number; originTop: number; width: number; height: number }>();

  function beginDialogDrag(event: React.PointerEvent<HTMLElement>) {
    // 头部里的按钮（关闭）照常点，不要顺手把窗口拖走。
    if ((event.target as HTMLElement).closest('button, input, select, a, textarea')) return;
    const dialog = event.currentTarget.closest('.data-extraction-dialog') as HTMLElement | null;
    if (!dialog) return;
    const rect = dialog.getBoundingClientRect();
    dragStateRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originLeft: rect.left,
      originTop: rect.top,
      width: rect.width,
      height: rect.height,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function moveDialog(event: React.PointerEvent<HTMLElement>) {
    const state = dragStateRef.current;
    if (!state || state.pointerId !== event.pointerId) return;
    // 把**整个**窗口夹在视口内，而不是只留标题栏露头：
    // 拖到只剩一条边的话，底部的「开始采集」就点不到了，用户还得先把它拖回来。
    const maxLeft = Math.max(0, window.innerWidth - state.width);
    const maxTop = Math.max(0, window.innerHeight - state.height);
    setDragPos({
      left: Math.min(maxLeft, Math.max(0, state.originLeft + event.clientX - state.startX)),
      top: Math.min(maxTop, Math.max(0, state.originTop + event.clientY - state.startY)),
    });
  }

  function endDialogDrag() {
    dragStateRef.current = undefined;
  }
  /**
   * 正在实时绘图的采集项。
   *
   * 存的是 **id 而不是数据快照** —— 数据集每帧都从 props.liveProgress.rows 重新算，
   * 新采到的点会自动出现在图上（signature 保持稳定，所以用户的图表配置不会被重置）。
   */
  const [livePlotRuleId, setLivePlotRuleId] = useState<string>();
  const [liveMessage, setLiveMessage] = useState('');

  /** 「开始采集（实时采集）」：把勾选项作为这一轮的实时采集项，并进入进度视图。 */
  async function startLive() {
    const ids = [...props.selectedIds];
    if (!props.onStartLiveCollection || !ids.length) return;
    setLiveBusyId('live');
    setLiveMessage('');
    try {
      const message = await props.onStartLiveCollection(ids);
      // 成功时不弹提示：「已开始实时采集 N 项…」只是复述刚点的按钮，
      // 进度条本身就在跟着涨，多一行字反而是噪声。
      // 只有需要用户做点什么（例如还没开实时监听）时才提示。
      setLiveMessage(typeof message === 'string' ? message : '');
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
  {/* 只有右上角的 X 会关闭；点背景不关，也不压暗/虚化 —— 后面的日志要看得到、点得到。 */}
  <div className="data-collection-backdrop" role="presentation">
    <section
      className="data-extraction-dialog"
      role="dialog"
      aria-label="数据采集"
      onMouseDown={(event) => event.stopPropagation()}
      style={dragPos ? { position: 'fixed', left: dragPos.left, top: dragPos.top, margin: 0 } : undefined}
    >
      <header
        className="data-extraction-dialog-dragbar"
        onPointerDown={beginDialogDrag}
        onPointerMove={moveDialog}
        onPointerUp={endDialogDrag}
        onPointerCancel={endDialogDrag}
      >
        <div>
          <span className="eyebrow">DATA COLLECTION</span>
          <h2>数据采集</h2>
          <p>
            {props.liveActive
              ? '实时监听进行中：勾选采集项后点「开始采集（实时采集）」，进度会随命中实时更新。'
              : '勾选要对当前日志采集的数据项；打开工具栏的「实时监听」后会转为实时采集模式。'}
          </p>
        </div>
        {props.phase !== 'running' && <button className="icon-button" onClick={props.onClose}><X size={18}/></button>}
      </header>

      <div className="data-extraction-dialog-body">
        {props.phase === 'select' && <>
          {!props.candidates.length ? <div className="data-extraction-empty"><Database size={30}/><strong>暂无可用的数据采集能力</strong><span>可以在“设置 → 日志规则 → 数据提取”中新增或启用数据提取器。</span></div> : <>
            <div className="data-extraction-select-head">
              <strong>已有 {props.candidates.length} 项数据采集能力</strong>
            </div>
            <div className="data-extraction-rule-options">
              {props.candidates.map(({ rule }) => {
                const inLiveCapture = props.liveCaptureIds ? props.liveCaptureIds.has(rule.id) : rule.liveCapture === true;
                const liveCount = props.liveProgress?.counts[rule.id] || 0;
                const progressTarget = Number((rule as unknown as { liveTargetRows?: number }).liveTargetRows || 0);
                return <div key={rule.id} className={`data-extraction-rule-option ${props.selectedIds.has(rule.id) ? 'selected' : ''}`}>
                <label>
                  <input type="checkbox" checked={props.selectedIds.has(rule.id)} onChange={() => props.onToggle(rule.id)}/>
                  {/* 一行展示：名称 / 说明 / 字段 / 格式 / 范围。
                      之前这三段各占一行，每个提取器就有三行高，列表稍长一点就得滚动。 */}
                  <span className="data-extraction-rule-text">
                    <strong>{rule.name || rule.matchKeyword || rule.id}</strong>
                    <small>{rule.description || '数据提取能力'}</small>
                    <em>{rule.fields.map((field) => field.name || field.key).join(' / ')}</em>
                  </span>
                  <b>{rule.outputFormat === 'text' ? 'TXT' : 'CSV'}</b>
                  <small className="data-extraction-rule-scope">{rule.modules.length ? rule.modules.join(' / ') : '通用'}</small>
                </label>
                {/* 实时进度直接长在采集项上：不用再去别处对「这一项采到多少」。 */}
                {/* 进度只在实时监听开着时出现：关掉实时按钮就没有「采集中」这回事了。 */}
                {props.liveActive && inLiveCapture && props.liveProgress && (
                  <span className="data-extraction-rule-progress" title={`已采集 ${liveCount} 条`}>
                    <span className="data-extraction-rule-progress-track" aria-hidden="true">
                      <i className={progressTarget ? '' : 'is-live'} style={progressTarget ? { width: `${Math.min(100, Math.round((liveCount / progressTarget) * 100))}%` } : undefined} />
                    </span>
                    <em>{liveCount}{progressTarget ? ` / ${progressTarget}` : ''} 条</em>
                    {/* 按钮一直在：采到第一条之前点它只提示「还没数据」，而不是让按钮忽隐忽现。 */}
                    <button
                      type="button"
                      className="data-extraction-live-plot"
                      onClick={() => {
                        if (!(props.liveProgress?.rows[rule.id] || []).length) {
                          setLiveMessage('还没有采到数据；等第一条采到后就能实时绘图。');
                          return;
                        }
                        setLivePlotRuleId(rule.id);
                      }}
                      title="用已采到的数据实时绘图：新数据进来图会跟着更新"
                    ><Activity size={12}/> 绘图</button>
                  </span>
                )}
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
        {props.phase === 'select' && <>
          <span className="data-extraction-footer-spacer"/>
          <button className="button ghost" onClick={props.onClose}>关闭</button>
          {/* 只有一个开始按钮：采集方式由工具栏的「实时监听」开关决定，
              所以按钮文案跟着它变，而不是让用户先在两颗按钮里选。 */}
          {props.liveActive ? (
            <button
              className="button primary"
              disabled={!props.candidates.length || props.selectedIds.size === 0 || !props.onStartLiveCollection}
              onClick={() => void startLive()}
              title="按实时采集运行：跟着实时监听持续采，进度实时更新，可实时绘图"
            >{liveBusyId === 'live' ? <LoaderCircle className="spin" size={14}/> : <Radio size={14}/>} 开始采集（实时采集）</button>
          ) : (
            <button
              className="button primary"
              disabled={!props.candidates.length || props.selectedIds.size === 0 || props.batchAvailable === false}
              onClick={props.onStart}
              title={props.batchAvailable === false
                ? '当前没有可批量采集的已解析日志；打开工具栏的「实时监听」即可转为实时采集'
                : '对当前日志一次性批量采集选中的项'}
            >开始采集（当前日志）</button>
          )}
        </>}
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
  {livePlotRuleId && (() => {
    const rule = props.candidates.find((item) => item.rule.id === livePlotRuleId)?.rule;
    const rows = props.liveProgress?.rows[livePlotRuleId] || [];
    if (!rule || !rows.length) return null;
    return (
      <DataVisualizationDialog
        dataset={{
          name: `${rule.name} · 实时`,
          // signature 固定 = 用户的图表配置不会被每一批新数据重置。
          signature: `live:${livePlotRuleId}`,
          fields: rule.fields.map((field) => field.name || field.key),
          rows,
        }}
        onClose={() => setLivePlotRuleId(undefined)}
      />
    );
  })()}
  </>;
}
