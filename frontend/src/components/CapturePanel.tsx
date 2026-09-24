import { useEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown, ChevronUp, Download, ExternalLink, LoaderCircle, Play, SlidersHorizontal, Square, X } from 'lucide-react';
import { API_BASE, buildApiHeaders } from '../api/resourceApi';
import { loadDataExtractionRules, extractDataValues, type DataExtractionRule } from '../rendering/dataExtractionRules';
import { executeUiAction } from '../assistant/workstation';
import { subscribeLiveMonitoring } from '../services/liveMonitoring';
import { primeSeq, subscribeWatchHits, subscribeWatchStatus, type WatchHitEvent } from '../services/watchRealtime';

/** At most three collectors run at once so the panel (and the extraction work) stays legible. */
const MAX_ACTIVE_COLLECTORS = 3;

interface LogWatchSummary {
  id: number;
  capture_config?: Record<string, unknown>;
  /** Non-empty for 实时采集 watches; empty for the timeline's semantic watchers. */
  extraction_rule_id?: string;
  name: string;
  level: string;
  trigger_kind: string;
  enabled: boolean;
  hit_count: number;
  dropped_count: number;
  last_error: string;
  source_rule_id: string;
}

/**
 * 数据采集面板 —— 实时采集的**进度台**：每个采集项采到多少条。
 *
 * 刻意只显示条数，不显示采集到的内容：
 * - 采集到的数值属于数据本身，归属「数据提取」页（那里能回看、绘图、下载）；
 *   在进度台里再铺一张表只会让人以为这里是看数据的地方，而它一关就没了。
 * - 面板要能在采集过程中一直挂着，只跑计数比每来一条就渲染一行便宜得多。
 *
 * 采集本身仍是「服务端抓原始窗口、浏览器里按提取器抽取」：抽取引擎只有一份
 * （`dataExtractionRules.ts`），搬到 Python 会多出第二份实现并慢慢跑偏。
 * 抓原始窗口还意味着无人值守时证据仍然在，提取器只是**看的方式**。
 */
export function CapturePanel() {
  // Driven by the single 实时监控 toggle rather than its own switch: the collector is a
  // *consequence* of monitoring, not a second thing to remember to turn on.
  const [open, setOpen] = useState(false);
  const [environmentId, setEnvironmentId] = useState<number | undefined>(undefined);
  const [watches, setWatches] = useState<LogWatchSummary[]>([]);
  const [hits, setHits] = useState<WatchHitEvent[]>([]);
  const [rules, setRules] = useState<DataExtractionRule[]>([]);
  const [connected, setConnected] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  /** Per-extractor capture target, from the watch's capture_config (0 = continuous). */
  const [targets, setTargets] = useState<Record<string, number>>({});
  /**
   * 采集分两个阶段，取代过去「打开实时监听就顺带出面板」的隐式行为：
   * - prep  ：先勾选这次要采集哪些提取器（以及目标条数），再按「开始采集」；
   * - running：展示每个采集项已采集多少、进度条，随时可停止。
   */
  const [phase, setPhase] = useState<'prep' | 'running'>('prep');
  /** 全部启用的提取器（prep 阶段要能勾选还没勾上的），不止已勾选的。 */
  const [allRules, setAllRules] = useState<DataExtractionRule[]>([]);
  const [busy, setBusy] = useState('');
  const [actionError, setActionError] = useState('');
  const lastCaptureTokenRef = useRef(0);

  useEffect(() => subscribeLiveMonitoring((next) => {
    setEnvironmentId(typeof next.environmentId === 'number' ? next.environmentId : undefined);
    // 停止监听后面板仍可保留（用来回看刚采到的数据），只把阶段切回准备态。
    setPhase(next.on ? 'running' : 'prep');
    if (next.on) setOpen(true);
    const request = next.captureRequest;
    if (request && request.token !== lastCaptureTokenRef.current) {
      lastCaptureTokenRef.current = request.token;
      setOpen(true);
      setCollapsed(false);
      if (request.prep) setPhase('prep');
    }
  }), []);

  useEffect(() => {
    if (!open) {
      // Monitoring stopped: drop the stream buffer so a restart does not show stale rows.
      setHits([]);
      return;
    }
    const enabled = loadDataExtractionRules().filter((rule) => rule.enabled);
    setAllRules(enabled);
    // Collect exactly what the user opted into on the extraction rule. Auto-selecting the
    // first N enabled rules made 实时监听 silently capture everything.
    setRules(enabled.filter((rule) => rule.liveCapture === true));
  }, [open, phase]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    const query = environmentId ? `?environment_id=${environmentId}&enabled=1` : '?enabled=1';
    void fetch(`${API_BASE}/log-watches/${query}`, { headers: buildApiHeaders() })
      .then((response) => (response.ok ? response.json() : null))
      .then((payload: { watches?: LogWatchSummary[] } | null) => {
        if (cancelled || !payload) return;
        // 语义规则的监视器 feeds the timeline ribbon. Counting its hits here made the panel
        // read "11 次命中" while collecting nothing at all.
        const extractionWatches = (payload.watches || []).filter((watch) => Boolean(watch.extraction_rule_id));
        setWatches(extractionWatches);
        const next: Record<string, number> = {};
        for (const watch of extractionWatches) {
          const config = (watch as unknown as { capture_config?: Record<string, unknown> }).capture_config || {};
          const target = Number(config.target_rows || config.hourly_quota || 0);
          if (target > 0 && watch.source_rule_id) next[watch.source_rule_id] = target;
        }
        setTargets(next);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [open, environmentId]);

  useEffect(() => {
    if (!open) return;
    const offHit = subscribeWatchHits((hit) => {
      if (environmentId && hit.environment_id && hit.environment_id !== environmentId) return;
      // Only collectors. A symptom rule's hit belongs to the ribbon, and the server marks the
      // surface on every hit so this stays a filter rather than a guess.
      if (hit.display_mode !== 'data') return;
      setHits((current) => [...current, hit].slice(-400));
    });
    const offStatus = subscribeWatchStatus(setConnected);
    return () => {
      offHit();
      offStatus();
    };
  }, [open, environmentId]);

  // Resume from history so reopening the panel does not replay the whole backlog.
  useEffect(() => {
    if (!open || !watches.length) return;
    void Promise.all(
      watches.map((watch) =>
        fetch(`${API_BASE}/log-watches/${watch.id}/hits/?limit=300`, { headers: buildApiHeaders() })
          .then((response) => (response.ok ? response.json() : null))
          .then((payload: { hits?: Array<Record<string, unknown>>; max_seq?: number } | null) => {
            if (!payload) return;
            if (payload.max_seq) primeSeq(watch.id, payload.max_seq);
            const restored: WatchHitEvent[] = (payload.hits || []).map((row) => ({
              type: 'watch.hit',
              watch_id: watch.id,
              watch_name: watch.name,
              level: watch.level,
              seq: Number(row.seq || 0),
              hit_id: Number(row.id || 0),
              matched_at: String(row.matched_at || ''),
              signature: String(row.signature || ''),
              label: watch.name,
              label_color: '#2563eb',
              subsystem: String(row.subsystem || ''),
              fm: String(row.fm || ''),
              machine: String(row.machine || ''),
              line_text: String(row.line_text || ''),
            }));
            setHits((current) => {
              const seen = new Set(current.map((item) => `${item.watch_id}-${item.seq}`));
              const merged = [...restored.filter((item) => !seen.has(`${item.watch_id}-${item.seq}`)), ...current];
              return merged.sort((a, b) => (a.matched_at || '').localeCompare(b.matched_at || '')).slice(-400);
            });
          })
          .catch(() => undefined),
      ),
    );
  }, [open, watches]);

  /**
   * 「管理提取器」打开的是**当前页面的数据提取弹窗**，不是设置页。
   *
   * 之前的做法是把用户扔到「设置 → 日志规则 → 数据提取」，用户丢掉当前的日志现场，
   * 配置完还得自己找回来。提取器的配置、勾选和「加入实时采集」现在都在那个弹窗里，
   * 这里只需要把它打开。
   */
  function openExtractorManager() {
    window.dispatchEvent(new CustomEvent('tracelens:assistant-ui', {
      detail: { type: 'open_data_extraction', __claimed: false },
    }));
  }

  /** 采集结果不在这个面板里，给一个直接去「数据提取」页的入口。 */
  function openExtractedDataPage() {
    window.dispatchEvent(new CustomEvent('tracelens:assistant-ui', {
      detail: { type: 'open_workspace_page', page: 'data', __claimed: false },
    }));
  }

  const captureTarget = (ruleId: string) => targets[ruleId] || 0;

  /**
   * 只数条数：抽取引擎仍然用来判断「这条命中算不算采集到」，
   * 但结果不留在面板里 —— 面板只负责进度。
   */
  const captureCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const rule of rules) {
      let count = 0;
      for (const hit of hits) {
        if (rule.subsystems.length && hit.subsystem && !rule.subsystems.includes(hit.subsystem)) continue;
        if (rule.modules.length && hit.fm && !rule.modules.includes(hit.fm)) continue;
        const message = String(hit.line_text || '');
        if (!message) continue;
        if (extractDataValues(message, rule)) count += 1;
      }
      counts[rule.id] = count;
    }
    return counts;
  }, [rules, hits]);

  if (!open) return null;

  const totalRows = Object.values(captureCounts).reduce((total, count) => total + count, 0);
  const errors = watches.filter((watch) => watch.last_error);
  const targetTotal = rules.reduce((total, rule) => total + captureTarget(rule.id), 0);
  const tooMany = rules.length > MAX_ACTIVE_COLLECTORS ? `（超过建议的 ${MAX_ACTIVE_COLLECTORS} 项，面板会较慢）` : '';
  const limitNote = phase === 'prep'
    ? (rules.length === 0
      ? '勾选这次要采集的数据项，再按「开始采集」。'
      : `已勾选 ${rules.length} 项${tooMany}，可以开始采集`)
    : `采集中 · 共 ${totalRows} 条${targetTotal ? ` / 目标 ${targetTotal}` : ''}`;

  /** 勾选写回提取器上的 liveCapture —— 面板是同一个开关的编辑器，不是第二套选择。 */
  async function toggleRule(rule: DataExtractionRule, next: boolean) {
    setActionError('');
    setBusy(rule.id);
    try {
      const receipt = await executeUiAction({ type: 'set_live_capture_items', items: [{ id: rule.id, liveCapture: next }] });
      if (receipt.status !== 'success') throw new Error(receipt.detail);
      const enabled = loadDataExtractionRules().filter((item) => item.enabled);
      setAllRules(enabled);
      setRules(enabled.filter((item) => item.liveCapture === true));
    } catch (error) {
      setActionError(error instanceof Error ? error.message : '更新采集项失败');
    } finally {
      setBusy('');
    }
  }

  async function startCapture() {
    setActionError('');
    setBusy('start');
    try {
      const receipt = await executeUiAction({ type: 'start_live_capture' });
      if (receipt.status !== 'success') throw new Error(receipt.detail);
      setPhase('running');
    } catch (error) {
      setActionError(error instanceof Error ? error.message : '开始采集失败');
    } finally {
      setBusy('');
    }
  }

  async function stopCapture() {
    setActionError('');
    setBusy('stop');
    try {
      const receipt = await executeUiAction({ type: 'stop_live_capture' });
      if (receipt.status !== 'success') throw new Error(receipt.detail);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : '停止采集失败');
    } finally {
      setBusy('');
    }
  }

  return (
    <aside className={`capture-panel ${collapsed ? 'collapsed' : ''}`} aria-label="数据采集器">
      <header className="capture-panel-head">
        <span className={`capture-panel-status ${connected ? 'on' : 'off'}`}>
          <Download size={14} />
          数据采集
        </span>
        <span className="capture-panel-counts">
          {phase === 'prep' ? `${rules.length} 项已勾选` : `${rules.length} 个采集项 · ${totalRows} 条`}
          {phase === 'running' && hits.length > 0 && <em> · {hits.length} 次命中</em>}
        </span>
        <button type="button" onClick={() => setCollapsed((value) => !value)} title={collapsed ? '展开' : '收起'}>
          {collapsed ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
        </button>
        <button type="button" onClick={() => setOpen(false)} title="关闭（不停止监控）"><X size={14} /></button>
      </header>

      {!collapsed && (
        <>
          {errors.length > 0 && (
            <div className="capture-panel-warn">
              {errors.length} 个监控异常：{errors[0].last_error}
            </div>
          )}
          {!connected && (
            <div className="capture-panel-warn">
              监控通道未连接。服务端仍在采集原始窗口，恢复后会自动补齐。
            </div>
          )}

          <div className="capture-limit-note">
            <span>{limitNote}</span>
            <button type="button" className="capture-panel-pick" onClick={openExtractorManager} title="打开当前页面的数据提取弹窗：在那里配置提取器、勾选数据项，并加入实时采集清单">
              <SlidersHorizontal size={11} /> 管理提取器
            </button>
            {phase === 'prep' ? (
              <button
                type="button"
                className="capture-panel-pick primary"
                disabled={rules.length === 0 || busy !== ''}
                onClick={() => void startCapture()}
                title={rules.length === 0 ? '先勾选至少一个数据项' : '按勾选的数据项开始实时采集'}
              >
                {busy === 'start' ? <LoaderCircle className="spin" size={11} /> : <Play size={11} />} 开始采集
              </button>
            ) : (
              <button type="button" className="capture-panel-pick danger" disabled={busy !== ''} onClick={() => void stopCapture()} title="停止采集（服务端会一并停掉这些监视器）">
                {busy === 'stop' ? <LoaderCircle className="spin" size={11} /> : <Square size={11} />} 停止采集
              </button>
            )}
          </div>
          {actionError && <div className="capture-panel-warn">{actionError}</div>}

          {phase === 'running' && rules.length > 0 && (
            <div className="capture-progress" aria-label="采集进度">
              {rules.map((rule) => {
                const rowCount = captureCounts[rule.id] || 0;
                const target = captureTarget(rule.id);
                // 有目标才画进度条；凭空造一条「总是快满了」的进度比显示「持续采集中」更糟。
                const percent = target ? Math.min(100, Math.round((rowCount / target) * 100)) : 0;
                return (
                  <div className="capture-progress-row" key={`progress-${rule.id}`}>
                    <span className="capture-progress-name" title={rule.name}>
                      {rule.name || rule.matchKeyword || rule.id}
                      <em>{(rule.fields || []).length} 字段</em>
                    </span>
                    <span className="capture-progress-bar" aria-hidden="true">
                      <i className={target ? '' : 'is-live'} style={target ? { width: `${percent}%` } : undefined} />
                    </span>
                    <span className="capture-progress-count">{rowCount}{target ? ` / ${target}` : ' 条'}</span>
                  </div>
                );
              })}
              {totalRows === 0 && (
                <div className="capture-empty">
                  {hits.length > 0
                    // 有命中却一条都抽不出来，和「什么都没命中」是两回事；说清楚区别，
                    // 用户才知道该去查字段配置还是继续等。
                    ? `已命中 ${hits.length} 条日志，但没有任何一条能按当前提取器抽出字段。请检查提取器的 Match 与字段划选是否覆盖这类日志行。`
                    : connected
                      ? '监控中，等待命中…（匹配到提取器的日志行后会自动计数）'
                      : '等待监控通道连接…'}
                </div>
              )}
            </div>
          )}

          {phase === 'prep' && (
            <div className="capture-pick-list" aria-label="选择要采集的数据项">
              {allRules.length === 0 && (
                <div className="capture-empty">还没有启用的数据提取器。点上面的「管理提取器」打开数据提取弹窗，新建或启用一个。</div>
              )}
              {allRules.map((rule) => (
                <label className={`capture-pick-item ${rule.liveCapture ? 'on' : ''}`} key={`pick-${rule.id}`}>
                  <input
                    type="checkbox"
                    checked={rule.liveCapture === true}
                    disabled={busy !== ''}
                    onChange={(event) => void toggleRule(rule, event.target.checked)}
                  />
                  <span className="capture-pick-body">
                    <strong>{rule.name || rule.matchKeyword || rule.id}</strong>
                    <small>{(rule.fields || []).length} 个字段 · {rule.matchKeyword || '未设 Match'}{rule.sourceCategories.length ? ` · ${rule.sourceCategories.join('/')}` : ''}</small>
                  </span>
                  {!rule.matchKeyword && <span className="capture-pick-warn" title="没有 Match 关键字，实时采集无法在日志里定位它">缺 Match</span>}
                </label>
              ))}
            </div>
          )}

          {/* 采集到的数值不在这个面板里：这里只报条数，看数据请去「数据提取」页。 */}
          <div className="capture-result-hint">
            <span>本面板只显示采集条数；采集到的数据请到<strong>数据提取</strong>页查看、绘图和下载。</span>
            <button type="button" className="capture-panel-pick" onClick={openExtractedDataPage} title="打开数据提取页查看已采集的数据">
              <ExternalLink size={11} /> 查看数据
            </button>
          </div>
        </>
      )}
    </aside>
  );
}
