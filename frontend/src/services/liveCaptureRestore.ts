/**
 * 从**服务端已落库的实时采集命中**还原采集数据。
 *
 * 实时采集的数据不是凭空来的：命中日志由后端 `LogWatchHit` 落库（见 watch_capture），
 * 浏览器只是拿这些命中按提取器字段抽值画图和计数。所以「关掉弹窗 / 关掉实时监听」
 * 之后数据不应该算丢 —— 只要把这一轮的 watch 记进数据记录，就能随时把行还原出来，
 * 不需要重读日志（这也是它比批量提取更快的原因）。
 */
import { API_BASE, buildApiHeaders, type DataExtractionRecord } from '../api/resourceApi';
import { extractDataValues, type DataExtractionRule } from '../rendering/dataExtractionRules';
import type { ExtractionRunResult } from '../rendering/dataExtractionRuntime';
import type { TemporaryRuleData } from '../rendering/extractedDataStore';

/** 数据记录里保存实时采集信息的位置（query_snapshot 是自由 JSON，不需要迁移）。 */
export interface LiveCaptureRecordSnapshot {
  started_at?: string;
  ended_at?: string;
  /** 这一轮实时采集命中过多少条日志（含抽不出字段的）。 */
  hits?: number;
  rules?: Array<{ rule_id: string; rule_name?: string; row_count?: number; watch_ids?: number[] }>;
}

/** 记录是不是实时采集落下来的。 */
export function liveCaptureSnapshot(record: DataExtractionRecord): LiveCaptureRecordSnapshot | undefined {
  const snapshot = record.query_snapshot as Record<string, unknown> | undefined;
  const live = snapshot?.live_capture;
  return live && typeof live === 'object' ? live as LiveCaptureRecordSnapshot : undefined;
}

export function isLiveCaptureRecord(record: DataExtractionRecord): boolean {
  return Boolean(liveCaptureSnapshot(record));
}

interface WatchHitRow {
  id?: number;
  seq?: number;
  matched_at?: string;
  subsystem?: string;
  fm?: string;
  line_text?: string;
  display_mode?: string;
}

async function fetchWatchHits(watchId: number, limit: number, signal?: AbortSignal): Promise<WatchHitRow[]> {
  const query = new URLSearchParams({ limit: String(limit) });
  const response = await fetch(`${API_BASE}/log-watches/${watchId}/hits/?${query.toString()}`, {
    headers: buildApiHeaders(),
    signal,
  });
  if (!response.ok) return [];
  const payload = await response.json() as { hits?: WatchHitRow[] } | null;
  return Array.isArray(payload?.hits) ? payload!.hits! : [];
}

/** 按提取规则列表去 watch 列表里找它对应的 watch（一个提取器一条 watch）。 */
export async function listCaptureWatches(signal?: AbortSignal): Promise<Array<{ id: number; extraction_rule_id: string; enabled: boolean; hit_count?: number }>> {
  const response = await fetch(`${API_BASE}/log-watches/`, { headers: buildApiHeaders(), signal });
  if (!response.ok) return [];
  const payload = await response.json() as { watches?: Array<Record<string, unknown>> } | Array<Record<string, unknown>> | null;
  const rows = Array.isArray(payload) ? payload : (payload?.watches || []);
  return rows
    .map((row) => ({
      id: Number(row.id || 0),
      extraction_rule_id: String(row.extraction_rule_id || ''),
      enabled: Boolean(row.enabled),
      hit_count: Number(row.hit_count || 0),
    }))
    .filter((row) => row.id > 0 && row.extraction_rule_id);
}

/**
 * 还原一条实时采集记录：命中 → 按字段抽值 → 行。
 *
 * @param limit 每个 watch 最多取多少条命中（默认按记录里的行数放大一点取，够还原当时的视图）。
 */
export async function restoreLiveCaptureRecord(input: {
  record: DataExtractionRecord;
  signal?: AbortSignal;
  onProgress?: (progress: { current: number; total: number; rows: number; message: string }) => void;
}): Promise<ExtractionRunResult> {
  const snapshot = liveCaptureSnapshot(input.record);
  const rules = (input.record.rule_snapshots || []) as unknown as DataExtractionRule[];
  if (!snapshot || !rules.length) throw new Error('这条记录没有可还原的实时采集信息。');

  const watched = await listCaptureWatches(input.signal);
  const byRule = new Map<string, number[]>();
  for (const row of watched) {
    const list = byRule.get(row.extraction_rule_id) || [];
    list.push(row.id);
    byRule.set(row.extraction_rule_id, list);
  }
  for (const entry of snapshot.rules || []) {
    const ids = (entry.watch_ids || []).filter((id) => Number(id) > 0);
    if (!ids.length) continue;
    const list = byRule.get(entry.rule_id) || [];
    byRule.set(entry.rule_id, Array.from(new Set([...list, ...ids.map(Number)])));
  }

  const limit = Math.max(200, Math.min(20000, Math.max(0, Number(input.record.row_count || 0)) * 3 + 200));
  const results: TemporaryRuleData[] = [];
  let current = 0;
  const total = rules.length;
  for (const rule of rules) {
    current += 1;
    input.onProgress?.({ current, total, rows: 0, message: `正在读取 ${rule.name || rule.id} 的实时采集命中` });
    const watchIds = byRule.get(rule.id) || [];
    const hits: WatchHitRow[] = [];
    for (const watchId of watchIds) {
      hits.push(...await fetchWatchHits(watchId, limit, input.signal));
    }
    hits.sort((left, right) => String(left.matched_at || '').localeCompare(String(right.matched_at || '')));
    const rows = [];
    for (const hit of hits) {
      const message = String(hit.line_text || '');
      if (!message) continue;
      const values = extractDataValues(message, rule);
      if (!values) continue;
      rows.push({
        timestamp: String(hit.matched_at || ''),
        sourceFile: `${hit.subsystem || ''}/${hit.fm || ''}`.replace(/^\//, ''),
        lineNumber: 0,
        values,
      });
    }
    results.push({ rule, rows });
    input.onProgress?.({ current, total, rows: rows.length, message: `${rule.name || rule.id} 还原 ${rows.length} 行` });
  }
  return { results, hourSummary: [] };
}
