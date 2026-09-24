import { useEffect, useMemo, useRef, useState } from 'react';
import { Check, ChevronDown, ChevronUp, Database, Download, LoaderCircle, Pause, Play, SlidersHorizontal, Square, X } from 'lucide-react';
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

interface CaptureRow {
  at: string;
  label: string;
  source: string;
  values: Record<string, string | number | boolean>;
}

/**
 * The collector panel — a downloads-style list of what monitoring has captured.
 *
 * Two deliberate decisions:
 *
 * 1. **It is a floating panel, not a strip.** Docking anything above the log workspace
 *    squeezes the timeline and fights the user's layout; a downloads list belongs in a
 *    corner you can close.
 * 2. **The server captures raw windows; extraction happens here.** The extraction engine
 *    already lives in the browser (`dataExtractionRules.ts`) with semantic templates and
 *    unit conversion. Porting it to Python would create a second implementation that drifts.
 *    Capturing raw windows means unattended monitoring still records evidence, and the
 *    extractor becomes a *view* choice — you can re-read old captures with a different
 *    extractor instead of having to pick one before capturing.
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
  const [activeRuleId, setActiveRuleId] = useState('');
  /** Per-extractor capture target, from the watch's capture_config (0 = continuous). */
  const [targets, setTargets] = useState<Record<string, number>>({});
  const [detailOpen, setDetailOpen] = useState(true);
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
  const scrollerRef = useRef<HTMLDivElement | null>(null);
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
   * The opt-in lives on the extraction rule, so this hands the user to that one page
   * instead of growing a second, conflicting set of checkboxes inside the panel.
   */
  function openExtractorPicker() {
    window.dispatchEvent(new CustomEvent('tracelens:assistant-ui', {
      detail: { type: 'open_log_rule_settings', tab: 'data', __claimed: false },
    }));
  }

  const captureTarget = (ruleId: string) => targets[ruleId] || 0;

  /** Extraction runs over captured windows using the app's own engine. */
  const captures = useMemo(() => {
    return rules.map((rule) => {
      const rows: CaptureRow[] = [];
      for (const hit of hits) {
        if (rule.subsystems.length && hit.subsystem && !rule.subsystems.includes(hit.subsystem)) continue;
        if (rule.modules.length && hit.fm && !rule.modules.includes(hit.fm)) continue;
        const message = String(hit.line_text || '');
        if (!message) continue;
        const values = extractDataValues(message, rule);
        if (!values) continue;
        rows.push({ at: hit.matched_at, label: hit.label || hit.watch_name, source: `${hit.subsystem || ''}/${hit.fm || ''}`, values });
      }
      return { rule, rows };
    }).filter((item) => item.rows.length > 0);
  }, [rules, hits]);

  useEffect(() => {
    const node = scrollerRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [hits]);

  if (!open) return null;

  const activeCapture = captures.find((item) => item.rule.id === activeRuleId) || captures[0];
  const totalRows = captures.reduce((total, item) => total + item.rows.length, 0);
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
          {phase === 'prep' ? `${rules.length} 项已勾选` : `${captures.length} 个采集器 · ${totalRows} 条数据`}
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
            <button type="button" className="capture-panel-pick" onClick={openExtractorPicker} title="去「日志规则 → 数据提取」管理提取器本身（字段、Match 等）">
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
                const rowCount = captures.find((item) => item.rule.id === rule.id)?.rows.length || 0;
                const target = captureTarget(rule.id);
                const percent = target ? Math.min(100, Math.round((rowCount / target) * 100)) : 0;
                return (
                  <div className="capture-progress-row" key={`progress-${rule.id}`}>
                    <span className="capture-progress-name" title={rule.name}>{rule.name || rule.matchKeyword || rule.id}</span>
                    <span className="capture-progress-bar" aria-hidden="true">
                      <i className={target ? '' : 'is-live'} style={target ? { width: `${percent}%` } : undefined} />
                    </span>
                    <span className="capture-progress-count">{rowCount}{target ? ` / ${target}` : ' 条'}</span>
                  </div>
                );
              })}
            </div>
          )}

          {phase === 'prep' && (
            <div className="capture-pick-list" aria-label="选择要采集的数据项">
              {allRules.length === 0 && (
                <div className="capture-empty">还没有启用的数据提取器。先去「日志规则 → 数据提取」建一个，再回来勾选。</div>
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

          <div className="capture-list" ref={scrollerRef}>
            {phase === 'running' && captures.length === 0 && (
              <div className="capture-empty">
                {rules.length === 0
                  ? '这次没有勾选任何数据项。停止采集后可以重新勾选。'
                  : hits.length > 0
                    // Hits arriving but nothing extractable is a *different* problem from
                    // "nothing matched", and saying so is the difference between a user
                    // checking their字段配置 and one staring at a silent zero.
                    ? `已命中 ${hits.length} 条日志，但没有任何一条能按当前提取器抽出字段。请检查提取器的 Match 与字段划选是否覆盖这类日志行。`
                    : connected
                      ? '监控中，等待命中…（匹配到提取器的日志行后会自动抽取数据）'
                      : '等待监控通道连接…'}
              </div>
            )}
            {phase === 'running' && captures.length > 0 && rules.map((rule) => {
              const rows = captures.find((item) => item.rule.id === rule.id)?.rows || [];
              const isActive = activeCapture?.rule.id === rule.id;
              // A progress bar only means something against a target. Inventing one that
              // always looks nearly full would be worse than showing "still collecting".
              const target = captureTarget(rule.id);
              const percent = target ? Math.min(100, Math.round((rows.length / target) * 100)) : 0;
              return (
                <button
                  key={rule.id}
                  type="button"
                  className={`capture-item ${isActive ? 'active' : ''} selected`}
                  title="查看这个采集器已采集的数据"
                  onClick={() => {
                    setActiveRuleId(rule.id);
                    setDetailOpen(true);
                  }}
                >
                  <span className="capture-item-check" aria-hidden="true"><Check size={11} /></span>
                  <span className="capture-item-icon"><Database size={15} /></span>
                  <span className="capture-item-body">
                    <strong>{rule.name || rule.matchKeyword || rule.id}</strong>
                    <small>{(rule.fields || []).length} 个字段 · {rule.sourceCategories.length ? rule.sourceCategories.join('/') : '全部日志'}</small>
                    {target ? (
                      <span className="capture-item-bar" aria-hidden="true" title={`目标 ${target} 条`}>
                        <i style={{ width: `${percent}%` }} />
                      </span>
                    ) : (
                      <span className="capture-item-bar is-live" aria-hidden="true" title="持续采集中，未设目标条数">
                        <i />
                      </span>
                    )}
                  </span>
                  <span className="capture-item-count">
                    <strong>{rows.length}</strong>
                    <small>条</small>
                  </span>
                </button>
              );
            })}
          </div>

          {activeCapture && detailOpen && (
            <div className="capture-detail">
              <div className="capture-detail-head">
                <strong>{activeCapture.rule.name} · 采集详情</strong>
                <span>{activeCapture.rows.length} 行</span>
                <button type="button" onClick={() => setDetailOpen(false)} title="收起详情"><Pause size={13} /></button>
              </div>
              <div className="capture-detail-table">
                <table>
                  <thead>
                    <tr>
                      <th>时间</th>
                      {(activeCapture.rule.fields || []).map((field) => <th key={field.id}>{field.name || field.key}</th>)}
                    </tr>
                  </thead>
                  <tbody>
                    {activeCapture.rows.slice(-80).map((row, index) => (
                      <tr key={`${row.at}-${index}`}>
                        <td className="capture-cell-time">{String(row.at).slice(11, 19)}</td>
                        {(activeCapture.rule.fields || []).map((field) => (
                          <td key={field.id} title={row.source}>
                            {String(row.values[field.name || field.key] ?? '')}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      )}
    </aside>
  );
}
