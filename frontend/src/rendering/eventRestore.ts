import { parseLogLineByCategories } from '../parser/logParser';
import type { LogEntry, LogFormatParserRuleConfig } from '../types';

const REMOTE_CATEGORY_PREFIX = '__TRACELENS_LOG_CATEGORY__=';
const REMOTE_SUBSYSTEM_PREFIX = '__TRACELENS_LOG_SUBSYSTEM__=';
const REMOTE_MODULE_PREFIX = '__TRACELENS_LOG_MODULE__=';
const REMOTE_SOURCE_PATH_PREFIX = '__TRACELENS_LOG_SOURCE_PATH__=';

interface RemoteEventContext {
  category?: string;
  subsystem?: string;
  module?: string;
  sourcePath?: string;
}

export type EventRestoreLevel = 'ERROR' | 'EVENT' | 'WARNING' | 'OTHER';

export interface EventCauseNode {
  key: string;
  entry: LogEntry;
  depth: number;
  children: EventCauseNode[];
}

function safeIdPart(value: string): string {
  return value.replace(/[^A-Za-z0-9_-]+/g, '-').slice(0, 80) || 'event-restore';
}

function consumeRemoteMarker(line: string, remote: RemoteEventContext): boolean {
  if (line.startsWith(REMOTE_CATEGORY_PREFIX)) { remote.category = line.slice(REMOTE_CATEGORY_PREFIX.length).trim() || undefined; return true; }
  if (line.startsWith(REMOTE_SUBSYSTEM_PREFIX)) { remote.subsystem = line.slice(REMOTE_SUBSYSTEM_PREFIX.length).trim() || undefined; return true; }
  if (line.startsWith(REMOTE_MODULE_PREFIX)) { remote.module = line.slice(REMOTE_MODULE_PREFIX.length).trim() || undefined; return true; }
  if (line.startsWith(REMOTE_SOURCE_PATH_PREFIX)) { remote.sourcePath = line.slice(REMOTE_SOURCE_PATH_PREFIX.length).trim() || undefined; return true; }
  return false;
}

/**
 * 根因树独立解析入口。远端 event.log 即使没有加入当前日志任务，也可以单独读取，
 * 并继续识别合并流里的真实来源 marker，避免把 event.log 当成普通 debug 日志。
 */
export function parseEventRestoreStream(
  text: string,
  sourceId = 'event-restore',
  formatRules: readonly LogFormatParserRuleConfig[] = [],
): LogEntry[] {
  const entries: LogEntry[] = [];
  const remote: RemoteEventContext = { category: 'run' };
  const lines = text.replace(/^\uFEFF/, '').split(/\r?\n/);
  const idPrefix = safeIdPart(sourceId);

  lines.forEach((rawLine, index) => {
    const line = rawLine.replace(/\r$/, '');
    if (!line.trim() || consumeRemoteMarker(line, remote)) return;
    const sourcePath = remote.sourcePath || 'event.log';
    const sourceName = sourcePath.includes('::')
      ? sourcePath.split('::').at(-1) || 'event.log'
      : sourcePath.split('/').at(-1) || 'event.log';
    const result = parseLogLineByCategories(line, {
      sourceFileId: `${sourceId}-${remote.subsystem || 'run'}-${remote.module || 'event'}-${sourceName}`,
      sourceFileName: sourceName,
      parserSourceFileName: sourceName,
      lineNumber: index + 1,
      idPrefix,
    }, [remote.category || 'run'], formatRules);
    if (!result.entry?.runEvent) return;
    if (remote.subsystem) result.entry.logSubsystem = remote.subsystem;
    if (remote.module) result.entry.logModule = remote.module;
    if (remote.sourcePath) result.entry.remoteSourcePath = remote.sourcePath;
    entries.push(result.entry);
  });

  entries.sort(compareEventEntriesAscending);
  return entries;
}

export function normalizedEventLevel(entry: LogEntry): EventRestoreLevel {
  const level = String(entry.runEvent?.eventLevel || entry.level || '').trim().toUpperCase();
  if (level.includes('ERROR') || level.includes('ALARM') || level.includes('FATAL')) return 'ERROR';
  if (level.includes('EVENT') || level.includes('INFO')) return 'EVENT';
  if (level.includes('WARN')) return 'WARNING';
  return 'OTHER';
}

export function eventComponent(entry: LogEntry): string {
  return String(entry.component || entry.logModule || entry.logSubsystem || '—').trim() || '—';
}

export function eventDisplayCode(entry: LogEntry): string {
  return String(entry.runEvent?.displayCode || '').trim();
}

export function compareEventEntriesAscending(a: LogEntry, b: LogEntry): number {
  if (a.timestampNs !== undefined && b.timestampNs !== undefined) {
    if (a.timestampNs < b.timestampNs) return -1;
    if (a.timestampNs > b.timestampNs) return 1;
  }
  return a.timestamp.localeCompare(b.timestamp) || a.id.localeCompare(b.id);
}

function normalizeLinkToken(value: string): string {
  return String(value || '').trim().toUpperCase();
}

function isMeaningfulLink(value: string): boolean {
  const token = normalizeLinkToken(value);
  if (!token || token === '-' || token === '—' || token === 'NULL' || token === 'NONE') return false;
  const compact = token.replace(/^0X/, '').replace(/[-_:]/g, '');
  return compact.length > 0 && !/^0+$/.test(compact);
}

interface EventRelationIndex {
  byEventCode: Map<string, LogEntry[]>;
  byDisplayCode: Map<string, LogEntry[]>;
  byErrIId: Map<string, LogEntry[]>;
}

function appendIndex(index: Map<string, LogEntry[]>, value: string, entry: LogEntry): void {
  if (!isMeaningfulLink(value)) return;
  const key = normalizeLinkToken(value);
  const current = index.get(key) || [];
  current.push(entry);
  index.set(key, current);
}

function buildRelationIndex(entries: readonly LogEntry[]): EventRelationIndex {
  const result: EventRelationIndex = { byEventCode: new Map(), byDisplayCode: new Map(), byErrIId: new Map() };
  entries.forEach((entry) => {
    if (!entry.runEvent) return;
    appendIndex(result.byEventCode, entry.runEvent.currentEventCode, entry);
    appendIndex(result.byDisplayCode, entry.runEvent.displayCode, entry);
    appendIndex(result.byErrIId, entry.runEvent.currentErrIId, entry);
  });
  for (const index of [result.byEventCode, result.byDisplayCode, result.byErrIId]) {
    index.forEach((list) => list.sort(compareEventEntriesAscending));
  }
  return result;
}

function nearestLinkedEntry(candidates: readonly LogEntry[], parent: LogEntry): LogEntry | undefined {
  const clean = candidates.filter((entry) => entry.id !== parent.id);
  if (!clean.length) return undefined;
  if (parent.timestampNs === undefined) return clean.at(-1);
  const prior = clean.filter((entry) => entry.timestampNs !== undefined && entry.timestampNs <= parent.timestampNs!);
  if (prior.length) return prior.at(-1);
  return clean.find((entry) => entry.timestampNs !== undefined) || clean[0];
}

function linkedEntries(parent: LogEntry, index: EventRelationIndex): LogEntry[] {
  if (!parent.runEvent) return [];
  const result: LogEntry[] = [];
  const seen = new Set<string>();
  const addNearest = (values: readonly string[], source: Map<string, LogEntry[]>) => {
    values.filter(isMeaningfulLink).forEach((value) => {
      const candidate = nearestLinkedEntry(source.get(normalizeLinkToken(value)) || [], parent);
      if (candidate && !seen.has(candidate.id)) {
        seen.add(candidate.id);
        result.push(candidate);
      }
    });
  };
  // DisplayCode 对使用者最直观，优先展示；事件码 / ErrIId 用于补齐底层链路。
  addNearest(parent.runEvent.linkedDisplayCodes, index.byDisplayCode);
  addNearest(parent.runEvent.linkedEventCodes, index.byEventCode);
  addNearest(parent.runEvent.linkedErrIIds, index.byErrIId);
  return result.sort(compareEventEntriesAscending);
}

export interface EventRootCauseResolver {
  hasChildren(entry: LogEntry): boolean;
  children(entry: LogEntry, ancestry?: ReadonlySet<string>, depth?: number): EventCauseNode[];
}

export function createEventRootCauseResolver(entries: readonly LogEntry[], maxDepth = 12): EventRootCauseResolver {
  const index = buildRelationIndex(entries);
  const directCache = new Map<string, LogEntry[]>();
  const direct = (entry: LogEntry) => {
    const cached = directCache.get(entry.id);
    if (cached) return cached;
    const next = linkedEntries(entry, index);
    directCache.set(entry.id, next);
    return next;
  };

  const buildChildren = (entry: LogEntry, ancestry: ReadonlySet<string> = new Set([entry.id]), depth = 1): EventCauseNode[] => {
    if (depth > maxDepth) return [];
    return direct(entry).flatMap((child, position) => {
      if (ancestry.has(child.id)) return [];
      const nextAncestry = new Set(ancestry);
      nextAncestry.add(child.id);
      return [{
        key: `${entry.id}>${child.id}:${depth}:${position}`,
        entry: child,
        depth,
        children: buildChildren(child, nextAncestry, depth + 1),
      }];
    });
  };

  return {
    hasChildren: (entry) => direct(entry).length > 0,
    children: buildChildren,
  };
}
