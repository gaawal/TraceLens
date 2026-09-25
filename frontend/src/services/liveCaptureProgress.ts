import { useEffect, useMemo, useState } from 'react';
import { API_BASE, buildApiHeaders } from '../api/resourceApi';
import { extractDataValues, type DataExtractionRule, type ExtractedDataRow } from '../rendering/dataExtractionRules';
import { primeSeq, subscribeWatchHits, subscribeWatchStatus, type WatchHitEvent } from './watchRealtime';

/**
 * 实时采集的进度来源。
 *
 * 之前这段逻辑住在那个悬浮的采集面板里；面板被「数据采集」弹窗取代之后，
 * 进度必须由**页面上层**提供 —— 工具栏的转圈图标和弹窗里的进度条读的是同一份数据，
 * 两个地方各数一遍迟早会对不上。
 */
export interface LiveCaptureProgress {
  /** 每个提取器已采集到多少条（按当前提取器的字段定义从命中日志里抽出来的才算）。 */
  counts: Record<string, number>;
  /**
   * 每个提取器最近采到的**值**（保留最近 MAX_KEPT_ROWS 条）。
   *
   * 采集进度只需要条数，但「实时绘图联动」需要值本身 —— 面板里不再显示数据表格，
   * 不代表数据可以不留：图就是拿这些行画的，而且新数据进来时图要跟着变。
   */
  rows: Record<string, ExtractedDataRow[]>;
  /** 本次会话累计命中多少条日志（用于区分「没命中」和「命中了但抽不出字段」）。 */
  hits: number;
  /** SSE 通道是否连着；断开时服务端仍在采集原始窗口。 */
  connected: boolean;
}

/** 每个提取器在内存里保留的最近行数：够画图和看趋势，又不会把页面撑爆。 */
const MAX_KEPT_ROWS = 400;

interface Options {
  /** 实时监听是否开着。关闭时清空计数，避免下次打开显示上一轮的残留。 */
  enabled: boolean;
  environmentId?: number;
  /** 已加入实时采集的提取器。只有这些会被计数。 */
  rules: readonly DataExtractionRule[];
}

export function useLiveCaptureProgress({ enabled, environmentId, rules }: Options): LiveCaptureProgress {
  const [hits, setHits] = useState<WatchHitEvent[]>([]);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    if (!enabled) {
      // 实时开关关掉后连命中一起丢掉：用户要的是「彻底清除刚才实时采集的项」，
      // 留着旧命中会让计数看起来还在涨。
      setHits([]);
      return undefined;
    }
    const offHit = subscribeWatchHits((hit) => {
      if (environmentId && hit.environment_id && hit.environment_id !== environmentId) return;
      // 语义规则命中属于时间线色带；只有标记为 data 的命中才是采集器的数据。
      if (hit.display_mode !== 'data') return;
      setHits((current) => [...current, hit].slice(-400));
    });
    const offStatus = subscribeWatchStatus(setConnected);
    return () => {
      offHit();
      offStatus();
    };
  }, [enabled, environmentId]);

  // 从历史恢复：重开弹窗不应该把已经采到的条数清零。
  useEffect(() => {
    if (!enabled) return undefined;
    let cancelled = false;
    void fetch(`${API_BASE}/log-watches/?enabled=1`, { headers: buildApiHeaders() })
      .then((response) => (response.ok ? response.json() : null))
      .then((payload: { watches?: Array<{ id: number; extraction_rule_id?: string }> } | null) => {
        if (cancelled || !payload) return;
        return Promise.all((payload.watches || [])
          .filter((watch) => Boolean(watch.extraction_rule_id))
          .map((watch) => fetch(`${API_BASE}/log-watches/${watch.id}/hits/?limit=300`, { headers: buildApiHeaders() })
            .then((response) => (response.ok ? response.json() : null))
            .then((page: { hits?: Array<Record<string, unknown>>; max_seq?: number } | null) => {
              if (!page) return;
              if (page.max_seq) primeSeq(watch.id, page.max_seq);
              const restored: WatchHitEvent[] = (page.hits || []).map((row) => ({
                type: 'watch.hit',
                watch_id: watch.id,
                watch_name: '',
                level: '',
                seq: Number(row.seq || 0),
                hit_id: Number(row.id || 0),
                matched_at: String(row.matched_at || ''),
                signature: String(row.signature || ''),
                label: '',
                label_color: '',
                display_mode: 'data',
                subsystem: String(row.subsystem || ''),
                fm: String(row.fm || ''),
                machine: String(row.machine || ''),
                line_text: String(row.line_text || ''),
              }));
              setHits((current) => {
                const seen = new Set(current.map((item) => `${item.watch_id}-${item.seq}`));
                return [...restored.filter((item) => !seen.has(`${item.watch_id}-${item.seq}`)), ...current].slice(-400);
              });
            })
            .catch(() => undefined)));
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [enabled]);

  const collected = useMemo(() => {
    const counts: Record<string, number> = {};
    const rows: Record<string, ExtractedDataRow[]> = {};
    for (const rule of rules) {
      const kept: ExtractedDataRow[] = [];
      let count = 0;
      for (const hit of hits) {
        if (rule.subsystems.length && hit.subsystem && !rule.subsystems.includes(hit.subsystem)) continue;
        if (rule.modules.length && hit.fm && !rule.modules.includes(hit.fm)) continue;
        const message = String(hit.line_text || '');
        if (!message) continue;
        // 用抽取引擎判断「这条命中算不算这个提取器采到的一条」：
        // 命中数不等于采集数，配错字段的提取器会一直命中却一条都抽不出来。
        const values = extractDataValues(message, rule);
        if (!values) continue;
        count += 1;
        kept.push({
          timestamp: hit.matched_at,
          sourceFile: `${hit.subsystem || ''}/${hit.fm || ''}`.replace(/^\//, ''),
          lineNumber: 0,
          values,
        });
      }
      counts[rule.id] = count;
      rows[rule.id] = kept.slice(-MAX_KEPT_ROWS);
    }
    return { counts, rows };
  }, [hits, rules]);

  return { counts: collected.counts, rows: collected.rows, hits: hits.length, connected };
}
