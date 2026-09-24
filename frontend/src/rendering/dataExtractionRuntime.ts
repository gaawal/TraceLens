import { fetchLogWindow, listRuntimeLogFormatRules, type DataExtractionRecord, type LogWindowRequest } from '../api/resourceApi';
import type { LogEntry } from '../types';
import type { LogStreamWorkerResponse, WorkerImportSource } from '../workers/logStreamProtocol';
import { extractDataRow, type DataExtractionRule, type ExtractedDataRow } from './dataExtractionRules';

export interface ExtractionHourWindow {
  startTime: string;
  endTime: string;
  label: string;
}

export interface ExtractionProgress {
  phase: 'reading' | 'extracting' | 'done';
  hourIndex: number;
  hourCount: number;
  currentHour?: ExtractionHourWindow;
  percent: number;
  processedEntries: number;
  totalRows: number;
  message: string;
}

export interface ExtractionRunResult {
  results: Array<{ rule: DataExtractionRule; rows: ExtractedDataRow[] }>;
  hourSummary: Array<{ start_time: string; end_time: string; row_count: number; status: 'success' | 'no_result' | 'failed' | 'cancelled' }>;
}

function parseLocalDateTime(value: string): Date {
  const text = String(value || '').trim().replace(' ', 'T');
  const date = new Date(text);
  if (!Number.isNaN(date.getTime())) return date;
  throw new Error(`无法解析时间：${value}`);
}

function formatLocalDateTime(date: Date): string {
  const pad = (value: number, length = 2) => String(value).padStart(length, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${pad(date.getMilliseconds(), 3)}`;
}

export function splitIntoHourWindows(startTime: string, endTime: string): ExtractionHourWindow[] {
  const start = parseLocalDateTime(startTime);
  const end = parseLocalDateTime(endTime);
  if (end.getTime() <= start.getTime()) return [];
  const windows: ExtractionHourWindow[] = [];
  let cursor = new Date(start);
  while (cursor.getTime() < end.getTime()) {
    const nextHour = new Date(cursor);
    nextHour.setMinutes(0, 0, 0);
    nextHour.setHours(nextHour.getHours() + 1);
    const logicalEnd = new Date(Math.min(nextHour.getTime(), end.getTime()));
    // 中间小时使用闭区间 [start, nextHour-1ms]，避免整点日志在相邻两个小时重复提取。
    const queryEnd = logicalEnd.getTime() < end.getTime() ? new Date(logicalEnd.getTime() - 1) : logicalEnd;
    windows.push({
      startTime: formatLocalDateTime(cursor),
      endTime: formatLocalDateTime(queryEnd),
      label: `${String(cursor.getHours()).padStart(2, '0')}:${String(cursor.getMinutes()).padStart(2, '0')} ~ ${String(logicalEnd.getHours()).padStart(2, '0')}:${String(logicalEnd.getMinutes()).padStart(2, '0')}`,
    });
    cursor = logicalEnd;
  }
  return windows;
}

function timestampMs(value: string): number {
  const date = new Date(String(value || '').trim().replace(' ', 'T'));
  return date.getTime();
}

function createResultBuckets(rules: readonly DataExtractionRule[]) {
  return new Map(rules.map((rule) => [rule.id, { rule, rows: [] as ExtractedDataRow[] }]));
}

async function extractEntryWindow(
  entries: readonly LogEntry[],
  rules: readonly DataExtractionRule[],
  buckets: ReturnType<typeof createResultBuckets>,
  onChunk?: (processed: number, rowsAdded: number) => void,
  signal?: AbortSignal,
): Promise<number> {
  const chunkSize = 5000;
  let added = 0;
  for (let offset = 0; offset < entries.length; offset += chunkSize) {
    if (signal?.aborted) throw new DOMException('已停止数据提取', 'AbortError');
    const chunk = entries.slice(offset, offset + chunkSize);
    let chunkAdded = 0;
    for (const entry of chunk) {
      for (const rule of rules) {
        const row = extractDataRow(entry, rule);
        if (!row) continue;
        buckets.get(rule.id)?.rows.push(row);
        chunkAdded += 1;
      }
    }
    added += chunkAdded;
    onChunk?.(offset + chunk.length, chunkAdded);
    await new Promise<void>((resolve) => window.setTimeout(resolve, 0));
  }
  return added;
}

export async function extractFromLoadedEntries(input: {
  entries: readonly LogEntry[];
  rules: readonly DataExtractionRule[];
  startTime: string;
  endTime: string;
  onProgress?: (progress: ExtractionProgress) => void;
  signal?: AbortSignal;
}): Promise<ExtractionRunResult> {
  const windows = splitIntoHourWindows(input.startTime, input.endTime);
  const buckets = createResultBuckets(input.rules);
  const hourSummary: ExtractionRunResult['hourSummary'] = [];
  let totalRows = 0;
  let processedEntries = 0;
  const entryTimes = input.entries.map((entry) => ({ entry, time: timestampMs(entry.timestamp) })).filter((item) => Number.isFinite(item.time));

  for (let index = 0; index < windows.length; index += 1) {
    if (input.signal?.aborted) throw new DOMException('已停止数据提取', 'AbortError');
    const window = windows[index];
    const startMs = parseLocalDateTime(window.startTime).getTime();
    const endMs = parseLocalDateTime(window.endTime).getTime();
    const scoped = entryTimes.filter((item) => item.time >= startMs && item.time <= endMs).map((item) => item.entry);
    let hourRows = 0;
    input.onProgress?.({ phase: 'extracting', hourIndex: index, hourCount: windows.length, currentHour: window, percent: Math.round((index / Math.max(1, windows.length)) * 100), processedEntries, totalRows, message: `正在提取 ${window.label}` });
    let previousProcessed = 0;
    await extractEntryWindow(scoped, input.rules, buckets, (processed, added) => {
      hourRows += added;
      processedEntries += Math.max(0, processed - previousProcessed);
      previousProcessed = processed;
      totalRows += added;
    }, input.signal);
    hourSummary.push({ start_time: window.startTime, end_time: window.endTime, row_count: hourRows, status: hourRows > 0 ? 'success' : 'no_result' });
    input.onProgress?.({ phase: 'extracting', hourIndex: index + 1, hourCount: windows.length, currentHour: window, percent: Math.round(((index + 1) / Math.max(1, windows.length)) * 100), processedEntries, totalRows, message: `${window.label} 完成，提取 ${hourRows.toLocaleString()} 行` });
  }

  input.onProgress?.({ phase: 'done', hourIndex: windows.length, hourCount: windows.length, percent: 100, processedEntries, totalRows, message: `提取完成，共 ${totalRows.toLocaleString()} 行` });
  return { results: [...buckets.values()], hourSummary };
}

async function parseRemoteBlob(blob: Blob, sourceName: string, categories: string[], signal?: AbortSignal): Promise<LogEntry[]> {
  const parserRules = await listRuntimeLogFormatRules().catch(() => []);
  return new Promise((resolve, reject) => {
    const worker = new Worker(new URL('../workers/logStream.worker.ts', import.meta.url), { type: 'module' });
    const taskId = `data-restore-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const entries: LogEntry[] = [];
    const cleanup = () => worker.terminate();
    const abort = () => {
      worker.postMessage({ type: 'CANCEL_TASK', taskId });
      cleanup();
      reject(new DOMException('已停止数据提取', 'AbortError'));
    };
    if (signal?.aborted) return abort();
    signal?.addEventListener('abort', abort, { once: true });
    worker.onmessage = (event: MessageEvent<LogStreamWorkerResponse>) => {
      const message = event.data;
      if ('taskId' in message && message.taskId !== taskId) return;
      if (message.type === 'IMPORT_BATCH') entries.push(...message.entries);
      if (message.type === 'IMPORT_COMPLETE') {
        signal?.removeEventListener('abort', abort);
        cleanup();
        resolve(entries);
      }
      if (message.type === 'IMPORT_ERROR') {
        signal?.removeEventListener('abort', abort);
        cleanup();
        reject(new Error(message.message));
      }
    };
    worker.onerror = (event) => {
      signal?.removeEventListener('abort', abort);
      cleanup();
      reject(new Error(event.message || '日志解析 Worker 执行失败'));
    };
    const source: WorkerImportSource = {
      kind: 'file',
      id: `${taskId}-${sourceName}`,
      name: sourceName,
      file: new File([blob], sourceName, { type: 'text/plain' }),
      logCategories: categories,
      parserRules,
    };
    worker.postMessage({ type: 'IMPORT_TASK', taskId, sources: [source], batchSize: 4000, strategy: 'full' });
  });
}

export async function restoreExtractionRecord(input: {
  record: DataExtractionRecord;
  onProgress?: (progress: ExtractionProgress) => void;
  signal?: AbortSignal;
}): Promise<ExtractionRunResult> {
  const environmentId = Number(input.record.environment || 0);
  if (!environmentId) throw new Error('该数据提取记录没有可还原的远程环境。');
  const request = input.record.query_snapshot as LogWindowRequest;
  if (!request?.start_time || !request?.end_time) throw new Error('该记录缺少原始日志查询时间范围。');
  const rules = (input.record.rule_snapshots || []) as unknown as DataExtractionRule[];
  if (!rules.length) throw new Error('该记录没有保存数据提取规则快照。');
  const windows = splitIntoHourWindows(request.start_time, request.end_time);
  const buckets = createResultBuckets(rules);
  const hourSummary: ExtractionRunResult['hourSummary'] = [];
  let totalRows = 0;
  let processedEntries = 0;

  for (let index = 0; index < windows.length; index += 1) {
    if (input.signal?.aborted) throw new DOMException('已停止数据提取', 'AbortError');
    const window = windows[index];
    input.onProgress?.({ phase: 'reading', hourIndex: index, hourCount: windows.length, currentHour: window, percent: Math.round((index / Math.max(1, windows.length)) * 100), processedEntries, totalRows, message: `正在读取 ${window.label} 日志` });
    const slicedRequest: LogWindowRequest = { ...request, start_time: window.startTime, end_time: window.endTime };
    const { blob } = await fetchLogWindow(environmentId, slicedRequest, { signal: input.signal });
    const sourceName = `data-restore-${environmentId}-${window.startTime}-${window.endTime}.log`;
    const entries = await parseRemoteBlob(blob, sourceName, [...(request.source_categories || [])], input.signal);
    let hourRows = 0;
    input.onProgress?.({ phase: 'extracting', hourIndex: index, hourCount: windows.length, currentHour: window, percent: Math.round((index / Math.max(1, windows.length)) * 100), processedEntries, totalRows, message: `正在解析 ${window.label} 数据` });
    let previousProcessed = 0;
    await extractEntryWindow(entries, rules, buckets, (processed, added) => {
      processedEntries += Math.max(0, processed - previousProcessed);
      previousProcessed = processed;
      hourRows += added;
      totalRows += added;
    }, input.signal);
    hourSummary.push({ start_time: window.startTime, end_time: window.endTime, row_count: hourRows, status: hourRows > 0 ? 'success' : 'no_result' });
    input.onProgress?.({ phase: 'extracting', hourIndex: index + 1, hourCount: windows.length, currentHour: window, percent: Math.round(((index + 1) / Math.max(1, windows.length)) * 100), processedEntries, totalRows, message: `${window.label} 完成，提取 ${hourRows.toLocaleString()} 行` });
  }

  input.onProgress?.({ phase: 'done', hourIndex: windows.length, hourCount: windows.length, percent: 100, processedEntries, totalRows, message: `还原完成，共 ${totalRows.toLocaleString()} 行` });
  return { results: [...buckets.values()], hourSummary };
}
