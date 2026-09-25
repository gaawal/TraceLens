/// <reference lib="webworker" />

import {
  appendLogContinuation,
  extractTimestampNsFromLogLine,
  isTimestampLikeLogLine,
  logFormatIsKnown,
  LOG_CONTINUATION_REASON,
  looksLikeLogContinuation,
  parseLogLineByCategories,
} from '../parser/logParser';
import type { LogEntry, ParseIssue } from '../types';
import type {
  ImportProgress,
  ImportStrategy,
  LogStreamWorkerRequest,
  LogStreamWorkerResponse,
  WorkerImportSource,
} from './logStreamProtocol';

const context: DedicatedWorkerGlobalScope = self as unknown as DedicatedWorkerGlobalScope;
const cancelledTasks = new Set<string>();

function post(message: LogStreamWorkerResponse): void {
  context.postMessage(message);
}

function safeIdPart(value: string): string {
  return value.replace(/[^A-Za-z0-9_-]+/g, '-').slice(0, 80) || 'log';
}

const REMOTE_CATEGORY_PREFIX = '__TRACELENS_LOG_CATEGORY__=';
const REMOTE_SUBSYSTEM_PREFIX = '__TRACELENS_LOG_SUBSYSTEM__=';
const REMOTE_MODULE_PREFIX = '__TRACELENS_LOG_MODULE__=';
const REMOTE_SOURCE_PATH_PREFIX = '__TRACELENS_LOG_SOURCE_PATH__=';

interface RemoteLogContext {
  category?: string;
  subsystem?: string;
  module?: string;
  sourcePath?: string;
}

function consumeRemoteMarker(line: string, remote: RemoteLogContext): boolean {
  if (line.startsWith(REMOTE_CATEGORY_PREFIX)) { remote.category = line.slice(REMOTE_CATEGORY_PREFIX.length).trim() || undefined; return true; }
  if (line.startsWith(REMOTE_SUBSYSTEM_PREFIX)) { remote.subsystem = line.slice(REMOTE_SUBSYSTEM_PREFIX.length).trim() || undefined; return true; }
  if (line.startsWith(REMOTE_MODULE_PREFIX)) { remote.module = line.slice(REMOTE_MODULE_PREFIX.length).trim() || undefined; return true; }
  if (line.startsWith(REMOTE_SOURCE_PATH_PREFIX)) { remote.sourcePath = line.slice(REMOTE_SOURCE_PATH_PREFIX.length).trim() || undefined; return true; }
  return false;
}

interface ImportCounters {
  linesRead: number;
  validCount: number;
  issueCount: number;
  bytesRead: number;
  scannedBytes: number;
  earliestImportedNs?: bigint;
  latestImportedNs?: bigint;
}

interface BatchBuffer {
  entries: LogEntry[];
  issues: ParseIssue[];
}

interface SourcePlan {
  source: WorkerImportSource;
  latestNs?: bigint;
  startOffset: number;
  endOffset: number;
  selectedBytes: number;
  skipped: boolean;
}

/**
 * 刷一批给主线程。
 *
 * `force=false` 时**最后一条日志留在缓冲里**：它后面可能还有续行（多行日志），
 * 刷出去以后 worker 再改也传不回去了（postMessage 是结构拷贝）。等下一行到来、
 * 或者整份日志读完（`force=true`）再把它发出去。
 */
function flushBatch(taskId: string, batch: BatchBuffer, force = false): void {
  if (batch.entries.length === 0 && batch.issues.length === 0) return;
  const holdTail = !force && batch.entries.length > 0;
  const outgoingEntries = holdTail ? batch.entries.slice(0, -1) : batch.entries;
  const held = holdTail ? batch.entries[batch.entries.length - 1] : undefined;
  if (outgoingEntries.length === 0 && batch.issues.length === 0) return;
  post({
    type: 'IMPORT_BATCH',
    taskId,
    entries: outgoingEntries,
    issues: batch.issues,
  });
  batch.entries = held ? [held] : [];
  batch.issues = [];
}

function publishProgress(
  taskId: string,
  phase: ImportProgress['phase'],
  strategy: ImportStrategy,
  currentSource: string,
  sourceIndex: number,
  sourceCount: number,
  selectedBytes: number,
  totalFileBytes: number,
  skippedSourceCount: number,
  counters: ImportCounters,
): void {
  const percent = phase === 'planning'
    ? 0
    : selectedBytes > 0
      ? Math.min(99, Math.round((counters.bytesRead / selectedBytes) * 100))
      : phase === 'finalizing' ? 100 : 0;

  const progress: ImportProgress = {
    taskId,
    phase,
    strategy,
    currentSource,
    sourceIndex,
    sourceCount,
    bytesRead: counters.bytesRead,
    totalBytes: selectedBytes,
    scannedBytes: counters.scannedBytes,
    totalFileBytes,
    linesRead: counters.linesRead,
    validCount: counters.validCount,
    issueCount: counters.issueCount,
    skippedSourceCount,
    percent,
  };
  post({ type: 'IMPORT_PROGRESS', progress });
}

function processLine(
  taskId: string,
  source: WorkerImportSource,
  line: string,
  lineNumber: number,
  batch: BatchBuffer,
  counters: ImportCounters,
  batchSize: number,
  remoteContext?: RemoteLogContext,
): void {
  if (!line.trim()) return;
  counters.linesRead += 1;
  const result = parseLogLineByCategories(line, {
    sourceFileId: source.id,
    sourceFileName: source.name,
    lineNumber,
    idPrefix: `${safeIdPart(taskId)}-${safeIdPart(source.id)}`,
    parserSourceFileName: remoteContext?.sourcePath?.split('/').pop() || source.name,
  }, remoteContext?.category ? [remoteContext.category] : (source.logCategories ?? []), source.parserRules ?? []);
  if (result.entry) {
    const resolvedSubsystem = remoteContext?.subsystem ?? source.logSubsystem;
    const resolvedModule = remoteContext?.module ?? source.logModule;
    const resolvedSourcePath = remoteContext?.sourcePath ?? source.sourcePath;
    if (resolvedSubsystem) result.entry.logSubsystem = resolvedSubsystem;
    if (resolvedModule) result.entry.logModule = resolvedModule;
    if (resolvedSourcePath) result.entry.remoteSourcePath = resolvedSourcePath;
    batch.entries.push(result.entry);
    counters.validCount += 1;
    const timestampNs = result.entry.timestampNs;
    if (timestampNs !== undefined) {
      if (counters.earliestImportedNs === undefined || timestampNs < counters.earliestImportedNs) {
        counters.earliestImportedNs = timestampNs;
      }
      if (counters.latestImportedNs === undefined || timestampNs > counters.latestImportedNs) {
        counters.latestImportedNs = timestampNs;
      }
    }
  }
  if (result.issue) {
    // 解析不出来的行有两种可能：① 上一条日志的续行（堆栈、JSON dump、换行正文）；
    // ② 真的是一条格式不支持的新记录。先按续行判定，不满足就维持原来的格式告警。
    const previous = batch.entries[batch.entries.length - 1];
    const sameSource = previous && previous.sourceFileId === source.id;
    const knownFormat = logFormatIsKnown(counters.validCount, counters.issueCount);
    if (sameSource && looksLikeLogContinuation(line, previous, { knownFormat })) {
      appendLogContinuation(previous, line);
      return;
    }
    // 一批（尤其是实时推送的分片）以续行开头时，上一条在更早的一批里：
    // 这里只按「不像新记录」标记出来，主线程拿着任务里最后一条日志再决定要不要并回去
    // （worker 看不到跨批的上一条，判定条件会过严，普通的一行续行会被漏掉）。
    if (!previous && !isTimestampLikeLogLine(line)) {
      batch.issues.push({ ...result.issue, reason: LOG_CONTINUATION_REASON });
      counters.issueCount += 1;
      if (batch.entries.length + batch.issues.length >= batchSize) flushBatch(taskId, batch);
      return;
    }
    batch.issues.push(result.issue);
    counters.issueCount += 1;
  }
  if (batch.entries.length + batch.issues.length >= batchSize) flushBatch(taskId, batch);
}

/**
 * 从文件尾部寻找最后一条可识别日志的时间。
 * 日志按行、按时间升序写入是尾部截取策略的前提。
 */
async function findLatestTimestampInFile(
  taskId: string,
  source: Extract<WorkerImportSource, { kind: 'file' }>,
  blockBytes: number,
  counters: ImportCounters,
): Promise<bigint | undefined> {
  let end = source.file.size;
  let carry = '';

  while (end > 0) {
    if (cancelledTasks.has(taskId)) return undefined;
    const start = Math.max(0, end - blockBytes);
    const text = await source.file.slice(start, end).text();
    counters.scannedBytes += end - start;
    const combined = text + carry;
    const lines = combined.split('\n');
    carry = lines.shift() ?? '';

    for (let index = lines.length - 1; index >= 0; index -= 1) {
      const timestampNs = extractTimestampNsFromLogLine(lines[index].replace(/\r$/, ''), source.parserRules ?? [], source.logCategories ?? [], source.name);
      if (timestampNs !== undefined) return timestampNs;
    }
    end = start;
  }

  return extractTimestampNsFromLogLine(carry.replace(/\r$/, ''), source.parserRules ?? [], source.logCategories ?? [], source.name);
}

function findLatestTimestampInText(source: Extract<WorkerImportSource, { kind: 'text' }>): bigint | undefined {
  const text = source.text;
  let cursor = text.length;
  while (cursor > 0) {
    const newline = text.lastIndexOf('\n', cursor - 1);
    const start = newline + 1;
    const line = text.slice(start, cursor).replace(/\r$/, '');
    const timestampNs = extractTimestampNsFromLogLine(line, source.parserRules ?? [], source.logCategories ?? [], source.name);
    if (timestampNs !== undefined) return timestampNs;
    cursor = newline >= 0 ? newline : 0;
  }
  return undefined;
}

/**
 * 反向按字节块扫描。每累计 rangeCheckLines 行检查一次最早时间，
 * 一旦覆盖目标时间就停止。返回值允许落在行中间，正向读取时会丢弃首个残行。
 */
async function findFileTailStart(
  taskId: string,
  source: Extract<WorkerImportSource, { kind: 'file' }>,
  targetStartNs: bigint,
  blockBytes: number,
  rangeCheckLines: number,
  counters: ImportCounters,
): Promise<number> {
  let end = source.file.size;
  let carry = '';
  let linesSinceCheck = 0;
  let earliestSinceCheck: bigint | undefined;

  while (end > 0) {
    if (cancelledTasks.has(taskId)) return source.file.size;
    const start = Math.max(0, end - blockBytes);
    const text = await source.file.slice(start, end).text();
    counters.scannedBytes += end - start;
    const combined = text + carry;
    const lines = combined.split('\n');
    carry = lines.shift() ?? '';

    for (let index = lines.length - 1; index >= 0; index -= 1) {
      const line = lines[index].replace(/\r$/, '');
      if (!line.trim()) continue;
      linesSinceCheck += 1;
      const timestampNs = extractTimestampNsFromLogLine(line, source.parserRules ?? [], source.logCategories ?? [], source.name);
      if (timestampNs !== undefined && (earliestSinceCheck === undefined || timestampNs < earliestSinceCheck)) {
        earliestSinceCheck = timestampNs;
      }

      if (linesSinceCheck >= rangeCheckLines) {
        if (earliestSinceCheck !== undefined && earliestSinceCheck <= targetStartNs) return Math.max(0, start - 1);
        linesSinceCheck = 0;
        earliestSinceCheck = undefined;
      }
    }

    // 每个字节块末尾也检查一次，避免低密度日志必须等够 5000 行。
    if (earliestSinceCheck !== undefined && earliestSinceCheck <= targetStartNs) return Math.max(0, start - 1);
    end = start;
  }

  return 0;
}

function findTextTailStart(source: Extract<WorkerImportSource, { kind: 'text' }>, targetStartNs: bigint, rangeCheckLines: number): number {
  const text = source.text;
  let cursor = text.length;
  let linesSinceCheck = 0;
  let earliestSinceCheck: bigint | undefined;

  while (cursor > 0) {
    const newline = text.lastIndexOf('\n', cursor - 1);
    const start = newline + 1;
    const line = text.slice(start, cursor).replace(/\r$/, '');
    if (line.trim()) {
      linesSinceCheck += 1;
      const timestampNs = extractTimestampNsFromLogLine(line, source.parserRules ?? [], source.logCategories ?? [], source.name);
      if (timestampNs !== undefined && (earliestSinceCheck === undefined || timestampNs < earliestSinceCheck)) {
        earliestSinceCheck = timestampNs;
      }
      if (linesSinceCheck >= rangeCheckLines) {
        if (earliestSinceCheck !== undefined && earliestSinceCheck <= targetStartNs) return Math.max(0, start - 1);
        linesSinceCheck = 0;
        earliestSinceCheck = undefined;
      }
    }
    cursor = newline >= 0 ? newline : 0;
  }
  return 0;
}

async function buildSourcePlans(
  taskId: string,
  sources: WorkerImportSource[],
  strategy: ImportStrategy,
  recentSeconds: number,
  explicitTargetStartNs: bigint | undefined,
  blockBytes: number,
  rangeCheckLines: number,
  counters: ImportCounters,
): Promise<{
  plans: SourcePlan[];
  latestTimestampNs?: bigint;
  requestedStartNs?: bigint;
  selectedBytes: number;
  totalFileBytes: number;
  skippedSourceCount: number;
}> {
  const encoder = new TextEncoder();
  const totalFileBytes = sources.reduce((sum, source) => (
    sum + (source.kind === 'file' ? source.file.size : encoder.encode(source.text).byteLength)
  ), 0);

  const latestBySource: Array<bigint | undefined> = [];
  for (let index = 0; index < sources.length; index += 1) {
    const source = sources[index];
    publishProgress(taskId, 'planning', strategy, source.name, index + 1, sources.length, 0, totalFileBytes, 0, counters);
    const latestNs = source.kind === 'file'
      ? await findLatestTimestampInFile(taskId, source, blockBytes, counters)
      : findLatestTimestampInText(source);
    latestBySource.push(latestNs);
  }

  const latestTimestampNs = latestBySource.reduce<bigint | undefined>((latest, value) => (
    value !== undefined && (latest === undefined || value > latest) ? value : latest
  ), undefined);
  const requestedStartNs = strategy === 'recent-tail' && latestTimestampNs !== undefined
    ? explicitTargetStartNs ?? latestTimestampNs - BigInt(Math.max(1, recentSeconds)) * 1_000_000_000n
    : undefined;

  const plans: SourcePlan[] = [];
  let selectedBytes = 0;
  let skippedSourceCount = 0;

  for (let index = 0; index < sources.length; index += 1) {
    const source = sources[index];
    const latestNs = latestBySource[index];
    let startOffset = 0;
    let endOffset = source.kind === 'file' ? source.file.size : source.text.length;
    let skipped = false;

    if (strategy === 'recent-tail' && requestedStartNs !== undefined) {
      if (latestNs !== undefined && latestNs < requestedStartNs) {
        skipped = true;
        startOffset = endOffset;
      } else if (source.kind === 'file') {
        startOffset = await findFileTailStart(
          taskId,
          source,
          requestedStartNs,
          blockBytes,
          rangeCheckLines,
          counters,
        );
      } else {
        startOffset = findTextTailStart(source, requestedStartNs, rangeCheckLines);
      }
    }

    const bytes = skipped
      ? 0
      : source.kind === 'file'
        ? Math.max(0, endOffset - startOffset)
        : encoder.encode(source.text.slice(startOffset, endOffset)).byteLength;
    selectedBytes += bytes;
    if (skipped) skippedSourceCount += 1;
    plans.push({ source, latestNs, startOffset, endOffset, selectedBytes: bytes, skipped });
  }

  return {
    plans,
    latestTimestampNs,
    requestedStartNs,
    selectedBytes,
    totalFileBytes,
    skippedSourceCount,
  };
}

async function readFilePlan(
  taskId: string,
  plan: SourcePlan & { source: Extract<WorkerImportSource, { kind: 'file' }> },
  sourceIndex: number,
  sourceCount: number,
  strategy: ImportStrategy,
  selectedBytes: number,
  totalFileBytes: number,
  skippedSourceCount: number,
  counters: ImportCounters,
  batch: BatchBuffer,
  batchSize: number,
): Promise<void> {
  const reader = plan.source.file.slice(plan.startOffset, plan.endOffset).stream().getReader();
  const decoder = new TextDecoder();
  let pending = '';
  let lineNumber = 0;
  let discardLeadingPartial = plan.startOffset > 0;
  let lastProgressAt = performance.now();
  const remoteContext: RemoteLogContext = {};

  while (true) {
    if (cancelledTasks.has(taskId)) {
      await reader.cancel();
      return;
    }
    const { value, done } = await reader.read();
    if (done) break;
    counters.bytesRead += value.byteLength;
    pending += decoder.decode(value, { stream: true });

    if (discardLeadingPartial) {
      const firstNewline = pending.indexOf('\n');
      if (firstNewline < 0) continue;
      pending = pending.slice(firstNewline + 1);
      discardLeadingPartial = false;
    }

    let newlineIndex = pending.indexOf('\n');
    while (newlineIndex >= 0) {
      lineNumber += 1;
      const line = pending.slice(0, newlineIndex).replace(/\r$/, '');
      pending = pending.slice(newlineIndex + 1);
      if (!consumeRemoteMarker(line, remoteContext)) processLine(taskId, plan.source, line, lineNumber, batch, counters, batchSize, remoteContext);
      newlineIndex = pending.indexOf('\n');
    }

    const now = performance.now();
    if (now - lastProgressAt >= 80) {
      publishProgress(
        taskId,
        'reading',
        strategy,
        plan.source.name,
        sourceIndex,
        sourceCount,
        selectedBytes,
        totalFileBytes,
        skippedSourceCount,
        counters,
      );
      lastProgressAt = now;
    }
  }

  pending += decoder.decode();
  if (!discardLeadingPartial && pending.length > 0) {
    lineNumber += 1;
    const line = pending.replace(/\r$/, '');
    if (!consumeRemoteMarker(line, remoteContext)) processLine(taskId, plan.source, line, lineNumber, batch, counters, batchSize, remoteContext);
  }
}

async function readTextPlan(
  taskId: string,
  plan: SourcePlan & { source: Extract<WorkerImportSource, { kind: 'text' }> },
  sourceIndex: number,
  sourceCount: number,
  strategy: ImportStrategy,
  selectedBytes: number,
  totalFileBytes: number,
  skippedSourceCount: number,
  counters: ImportCounters,
  batch: BatchBuffer,
  batchSize: number,
): Promise<void> {
  const encoder = new TextEncoder();
  const chunkSize = 256 * 1024;
  let text = plan.source.text.slice(plan.startOffset, plan.endOffset);
  if (plan.startOffset > 0) {
    const newline = text.indexOf('\n');
    text = newline >= 0 ? text.slice(newline + 1) : '';
  }
  let pending = '';
  let lineNumber = 0;
  let offset = 0;
  const remoteContext: RemoteLogContext = {};

  while (offset < text.length) {
    if (cancelledTasks.has(taskId)) return;
    const textChunk = text.slice(offset, offset + chunkSize);
    offset += textChunk.length;
    counters.bytesRead += encoder.encode(textChunk).byteLength;
    pending += textChunk;

    let newlineIndex = pending.indexOf('\n');
    while (newlineIndex >= 0) {
      lineNumber += 1;
      const line = pending.slice(0, newlineIndex).replace(/\r$/, '');
      pending = pending.slice(newlineIndex + 1);
      if (!consumeRemoteMarker(line, remoteContext)) processLine(taskId, plan.source, line, lineNumber, batch, counters, batchSize, remoteContext);
      newlineIndex = pending.indexOf('\n');
    }

    publishProgress(
      taskId,
      'reading',
      strategy,
      plan.source.name,
      sourceIndex,
      sourceCount,
      selectedBytes,
      totalFileBytes,
      skippedSourceCount,
      counters,
    );
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
  }

  if (pending.length > 0) {
    lineNumber += 1;
    const line = pending.replace(/\r$/, '');
    if (!consumeRemoteMarker(line, remoteContext)) processLine(taskId, plan.source, line, lineNumber, batch, counters, batchSize, remoteContext);
  }
}

async function importTask(message: Extract<LogStreamWorkerRequest, { type: 'IMPORT_TASK' }>): Promise<void> {
  const { taskId, sources } = message;
  const batchSize = Math.max(500, message.batchSize ?? 4000);
  const strategy = message.strategy ?? 'recent-tail';
  const recentSeconds = Math.max(1, message.recentSeconds ?? 7 * 24 * 60 * 60);
  const explicitTargetStartNs = message.targetStartNs ? BigInt(message.targetStartNs) : undefined;
  const blockBytes = Math.max(256 * 1024, message.reverseBlockBytes ?? 2 * 1024 * 1024);
  const rangeCheckLines = Math.max(500, message.rangeCheckLines ?? 5000);
  const batch: BatchBuffer = { entries: [], issues: [] };
  const counters: ImportCounters = {
    linesRead: 0,
    validCount: 0,
    issueCount: 0,
    bytesRead: 0,
    scannedBytes: 0,
  };

  cancelledTasks.delete(taskId);

  try {
    const planning = await buildSourcePlans(
      taskId,
      sources,
      strategy,
      recentSeconds,
      explicitTargetStartNs,
      blockBytes,
      rangeCheckLines,
      counters,
    );
    if (cancelledTasks.has(taskId)) return;

    for (let index = 0; index < planning.plans.length; index += 1) {
      if (cancelledTasks.has(taskId)) return;
      const plan = planning.plans[index];
      if (plan.skipped || plan.selectedBytes === 0) continue;
      if (plan.source.kind === 'file') {
        await readFilePlan(
          taskId,
          plan as SourcePlan & { source: Extract<WorkerImportSource, { kind: 'file' }> },
          index + 1,
          planning.plans.length,
          strategy,
          planning.selectedBytes,
          planning.totalFileBytes,
          planning.skippedSourceCount,
          counters,
          batch,
          batchSize,
        );
      } else {
        await readTextPlan(
          taskId,
          plan as SourcePlan & { source: Extract<WorkerImportSource, { kind: 'text' }> },
          index + 1,
          planning.plans.length,
          strategy,
          planning.selectedBytes,
          planning.totalFileBytes,
          planning.skippedSourceCount,
          counters,
          batch,
          batchSize,
        );
      }
    }

    if (cancelledTasks.has(taskId)) return;
    // 整份读完：缓冲里最后一条不会再等到续行了，强制刷出去。
    flushBatch(taskId, batch, true);
    publishProgress(
      taskId,
      'finalizing',
      strategy,
      sources[sources.length - 1]?.name ?? '日志文件',
      sources.length,
      sources.length,
      planning.selectedBytes,
      planning.totalFileBytes,
      planning.skippedSourceCount,
      counters,
    );

    const fullLoaded = planning.skippedSourceCount === 0 && planning.plans.every((plan) => plan.startOffset === 0);
    post({
      type: 'IMPORT_COMPLETE',
      taskId,
      linesRead: counters.linesRead,
      validCount: counters.validCount,
      issueCount: counters.issueCount,
      strategy,
      fullLoaded,
      requestedStartNs: planning.requestedStartNs?.toString(),
      latestTimestampNs: planning.latestTimestampNs?.toString(),
      earliestImportedNs: counters.earliestImportedNs?.toString(),
      latestImportedNs: counters.latestImportedNs?.toString(),
      selectedBytes: planning.selectedBytes,
      totalFileBytes: planning.totalFileBytes,
      skippedSourceCount: planning.skippedSourceCount,
    });
  } catch (error) {
    post({
      type: 'IMPORT_ERROR',
      taskId,
      message: error instanceof Error ? error.message : String(error),
    });
  } finally {
    cancelledTasks.delete(taskId);
  }
}

context.addEventListener('message', (event: MessageEvent<LogStreamWorkerRequest>) => {
  const message = event.data;
  if (message.type === 'CANCEL_TASK') {
    cancelledTasks.add(message.taskId);
    return;
  }
  if (message.type === 'IMPORT_TASK') void importTask(message);
});
