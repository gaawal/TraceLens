import type { LogEntry, ProcessTimeline, ThreadTimeline, TraceTimeline } from '../types';

export interface TimeRangeFilter {
  startNs: bigint;
  endNs: bigint;
}

export interface ProcessScopeIndex {
  processById: Map<string, ProcessTimeline>;
  threadById: Map<string, ThreadTimeline>;
  traceById: Map<string, TraceTimeline>;
}

export interface CrossTraceScopeIndex {
  traceById: Map<string, TraceTimeline>;
}

function lowerBoundTimestamp(entries: LogEntry[], target: bigint): number {
  let low = 0;
  let high = entries.length;
  while (low < high) {
    const middle = (low + high) >>> 1;
    const timestamp = entries[middle].timestampNs;
    if (timestamp === undefined || timestamp < target) low = middle + 1;
    else high = middle;
  }
  return low;
}

function upperBoundTimestamp(entries: LogEntry[], target: bigint): number {
  let low = 0;
  let high = entries.length;
  while (low < high) {
    const middle = (low + high) >>> 1;
    const timestamp = entries[middle].timestampNs;
    if (timestamp !== undefined && timestamp <= target) low = middle + 1;
    else high = middle;
  }
  return low;
}

/**
 * 对已经按时间排序的日志做二分切片。
 * 时间窗变化时无需重新遍历全部日志，复杂度为 O(log n + k)。
 */
export function sliceEntriesByRange(entries: LogEntry[], range?: TimeRangeFilter): LogEntry[] {
  if (!range || entries.length === 0) return entries;
  const start = lowerBoundTimestamp(entries, range.startNs);
  const end = upperBoundTimestamp(entries, range.endNs);
  return entries.slice(start, end);
}

function scopedTraceWithEntries(trace: TraceTimeline, entries: LogEntry[]): TraceTimeline {
  const firstFunctionName = entries.find((entry) => entry.marker === 'start' && entry.functionName)?.functionName
    ?? entries.find((entry) => entry.functionName)?.functionName
    ?? trace.firstFunctionName;
  return {
    ...trace,
    entries,
    // items 复用导入时建立的函数树；渲染器再使用时间窗过滤节点。
    firstFunctionName,
    components: Array.from(new Set(entries.map((entry) => entry.component))).sort(),
    sourceFiles: Array.from(new Set(entries.map((entry) => entry.sourceFile))).sort(),
    participantCount: new Set(entries.map((entry) => `${entry.component}|${entry.processId}|${entry.threadId}`)).size,
  };
}

function processLocationForEntry(entry: LogEntry): { processId: string; threadId: string; traceId: string } {
  const processId = `process-${entry.component}-${entry.processId}`;
  const threadId = `thread-${entry.component}-${entry.processId}-${entry.threadId}`;
  const traceKey = !entry.rpc.isRpc
    ? 'local'
    : entry.rpc.traceId !== '0'
      ? `trace-${entry.rpc.traceId}`
      : `rpc-${entry.rpc.raw}`;
  return {
    processId,
    threadId,
    traceId: `trace-${entry.component}-${entry.processId}-${entry.threadId}-${traceKey}`,
  };
}

/** 建立 Process/Thread/Trace 对象索引，仅在导入日志时执行一次。 */
export function createProcessScopeIndex(processes: ProcessTimeline[]): ProcessScopeIndex {
  const processById = new Map<string, ProcessTimeline>();
  const threadById = new Map<string, ThreadTimeline>();
  const traceById = new Map<string, TraceTimeline>();

  processes.forEach((process) => {
    processById.set(process.id, process);
    process.threads.forEach((thread) => {
      threadById.set(thread.id, thread);
      thread.traces.forEach((trace) => traceById.set(trace.id, trace));
    });
  });

  return { processById, threadById, traceById };
}

/**
 * 根据已经二分切出的当前窗口日志，直接回填原 Process/Thread/Trace 对象。
 * 复杂度与窗口内日志数量 k 成正比，不扫描窗口外的 Trace，也不重建函数树。
 */
export function scopeProcessesFromEntries(
  allProcesses: ProcessTimeline[],
  index: ProcessScopeIndex,
  entries: LogEntry[],
  rangeActive: boolean,
): ProcessTimeline[] {
  if (!rangeActive) return allProcesses;

  const selected = new Map<string, Map<string, Map<string, LogEntry[]>>>();
  entries.forEach((entry) => {
    const location = processLocationForEntry(entry);
    if (!index.traceById.has(location.traceId)) return;
    let threadMap = selected.get(location.processId);
    if (!threadMap) {
      threadMap = new Map();
      selected.set(location.processId, threadMap);
    }
    let traceMap = threadMap.get(location.threadId);
    if (!traceMap) {
      traceMap = new Map();
      threadMap.set(location.threadId, traceMap);
    }
    const traceEntries = traceMap.get(location.traceId) ?? [];
    traceEntries.push(entry);
    traceMap.set(location.traceId, traceEntries);
  });

  return Array.from(selected.entries()).flatMap(([processId, threadMap]) => {
    const process = index.processById.get(processId);
    if (!process) return [];
    const threads = Array.from(threadMap.entries()).flatMap(([threadId, traceMap]) => {
      const thread = index.threadById.get(threadId);
      if (!thread) return [];
      const traces = Array.from(traceMap.entries()).flatMap(([traceId, traceEntries]) => {
        const trace = index.traceById.get(traceId);
        return trace ? [scopedTraceWithEntries(trace, traceEntries)] : [];
      });
      return traces.length > 0
        ? [{ ...thread, traces, entryCount: traces.reduce((sum, trace) => sum + trace.entries.length, 0) }]
        : [];
    });
    return threads.length > 0
      ? [{ ...process, threads, entryCount: threads.reduce((sum, thread) => sum + thread.entryCount, 0) }]
      : [];
  });
}


function scopeProcessesByTraceRanges(
  allProcesses: ProcessTimeline[],
  range: TimeRangeFilter,
): ProcessTimeline[] {
  return allProcesses.flatMap((process) => {
    const threads = process.threads.flatMap((thread) => {
      const traces = thread.traces.flatMap((trace) => {
        const entries = sliceEntriesByRange(trace.entries, range);
        return entries.length > 0 ? [scopedTraceWithEntries(trace, entries)] : [];
      });
      return traces.length > 0
        ? [{ ...thread, traces, entryCount: traces.reduce((sum, trace) => sum + trace.entries.length, 0) }]
        : [];
    });
    return threads.length > 0
      ? [{ ...process, threads, entryCount: threads.reduce((sum, thread) => sum + thread.entryCount, 0) }]
      : [];
  });
}

/**
 * 自适应时间窗算法：
 * - 窄窗口：按窗口内 entry 的确定性 ID 直接分组，避免扫描所有 Trace；
 * - 宽窗口：逐 Trace 二分切片，避免对大量 entry 进行 Map 分组。
 */
export function scopeProcessesForWindow(
  allProcesses: ProcessTimeline[],
  index: ProcessScopeIndex,
  entries: LogEntry[],
  range?: TimeRangeFilter,
): ProcessTimeline[] {
  if (!range) return allProcesses;
  const traceCount = Math.max(index.traceById.size, 1);
  return entries.length <= traceCount * 3
    ? scopeProcessesFromEntries(allProcesses, index, entries, true)
    : scopeProcessesByTraceRanges(allProcesses, range);
}

export function createCrossTraceScopeIndex(traces: TraceTimeline[]): CrossTraceScopeIndex {
  return { traceById: new Map(traces.map((trace) => [trace.id, trace])) };
}

export function scopeCrossTracesFromEntries(
  allTraces: TraceTimeline[],
  index: CrossTraceScopeIndex,
  entries: LogEntry[],
  rangeActive: boolean,
): TraceTimeline[] {
  if (!rangeActive) return allTraces;
  const selected = new Map<string, LogEntry[]>();
  entries.forEach((entry) => {
    if (!entry.rpc.isRpc || entry.rpc.traceId === '0') return;
    const traceId = `cross-trace-${entry.rpc.traceId}`;
    if (!index.traceById.has(traceId)) return;
    const traceEntries = selected.get(traceId) ?? [];
    traceEntries.push(entry);
    selected.set(traceId, traceEntries);
  });
  return Array.from(selected.entries()).flatMap(([traceId, traceEntries]) => {
    const trace = index.traceById.get(traceId);
    return trace ? [scopedTraceWithEntries(trace, traceEntries)] : [];
  });
}


function scopeCrossTracesByRanges(
  allTraces: TraceTimeline[],
  range: TimeRangeFilter,
): TraceTimeline[] {
  return allTraces.flatMap((trace) => {
    const entries = sliceEntriesByRange(trace.entries, range);
    return entries.length > 0 ? [scopedTraceWithEntries(trace, entries)] : [];
  });
}

export function scopeCrossTracesForWindow(
  allTraces: TraceTimeline[],
  index: CrossTraceScopeIndex,
  entries: LogEntry[],
  range?: TimeRangeFilter,
): TraceTimeline[] {
  if (!range) return allTraces;
  const traceCount = Math.max(index.traceById.size, 1);
  return entries.length <= traceCount * 3
    ? scopeCrossTracesFromEntries(allTraces, index, entries, true)
    : scopeCrossTracesByRanges(allTraces, range);
}
