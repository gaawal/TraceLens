import type {
  ContentSeverity,
  FlowMarker,
  LogDocument,
  LogEntry,
  LogFormatParserRuleConfig,
  ParseIssue,
  ParseResult,
  RpcTrace,
  SourceLocation,
} from '../types';

const LOG_LINE_REGEX = /^\[([^\]]+)]\s+\[([^\]]+)]\s+\[([^\]]+)]\s+\[([^\]]+)]\s+\[([^\]]+)]\s+\[([^\]]+)]\s+\[([^\]]+)]\s+\[([^\]]+)]\s*(.*)$/;
const RUN_EVENT_LOG_REGEX = /^\[([^\]]+)]\s+\[([^\]]+)]\s+\[([^\]]+)]\s+\[([^\]]+)]\s+\[([^\]]+)]\s+\[([^\]]+)]\s+\[([^\]]*)]\s+\[([^\]]*)]\s+\[([^\]]*)]\s+\[([^\]]*)]\s+\[([^\]]*)]\s+\[([^\]]*)]\s+\[([^\]]+)]\s*(.*)$/;
const RUN_EVENT_CATEGORIES = new Set(['SYSTEM', 'PROCESS', 'MATERIAL', 'ENVIROMENT', 'ENVIRONMENT', 'USER']);
const RUN_EVENT_TYPES = new Set(['SET', 'EVT', 'DEA', 'CLR']);
const FUNCTION_PREFIX_REGEX = /^\s*([A-Za-z_~][\w:<>~.-]*\(\))/;
const EXECUTOR_BRACKET_FUNCTION_REGEX = /(?:=>|<=)\s*(?:\[(?:100|101)\]\s*)*\[([A-Za-z_~][\w:<>~.\-]*)\]/;
const BRACKET_BOUNDARY_FUNCTION_REGEX = /\[([A-Za-z_~][\w:<>~.\-]*)\]\s*\[(?:START|END)\]/i;
const EXECUTOR_BASE_REGEX = /^\[([^\]]+)]\s+\[([^\]]+)]\s+\[([^\]]+)]\s+\[([^\]]+)]\s+\[([^\]]+)]\s+\[([^\]]+)]\s*(.*)$/;
const EXECUTOR_MODE_VALUES = new Set(['100', '101']);
const START_MARKER_REGEX = />\s*\(\s*\)/;
const END_MARKER_REGEX = /<\s*\(\s*\)/;
export const DEFAULT_ERROR_KEYWORDS = ['ERROR', 'ERRORS', 'errors', 'EXCEPTION', 'exception', 'FAILED', 'failed', 'FATAL'] as const;
export const DEFAULT_WARNING_KEYWORDS = ['WARN', 'WARNING', 'WRAN'] as const;

/**
 * 多行日志（一条记录跨多个物理行）的续行支持。
 *
 * 背景：解析是**按行**做的 —— 一行必须自己带时间戳（或命中某个日志格式）才算一条新日志。
 * 但真实日志里一条记录常常打好几行才出现下一个时间戳：Python 堆栈、JSON/XML dump、
 * 被换行拆开的正文、ASCII 表格……过去这些行解析不出来，就被当成「不符合当前日志格式」
 * 的告警丢掉，于是**日志正文被截断**，只剩第一行。
 *
 * 这里只做加法，不改任何已有解析结果：
 * - 只有「按所有格式都解析不出来」的行才会走到续行判定；
 * - 看起来像新记录（行内出现时间戳样式）的行绝不并进上一条，维持原来的告警行为；
 * - 未知格式的文件（解析成功率很低）不启用「无提示续行」，仍然逐行报格式告警；
 * - 续行有上限，超过就退回原来的告警，不会把整份文件吞进一条日志。
 */
export const LOG_CONTINUATION_REASON = '多行日志续行';
export const LOG_CONTINUATION_MAX_LINES = 200;
export const LOG_CONTINUATION_MAX_CHARS = 64 * 1024;

/** 行内是否出现“像时间戳”的片段：出现就说明这更可能是一条（暂不支持的）新记录。 */
const TIMESTAMP_LIKE_REGEX = /(\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2})|(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3})|(\[\s*\d{4}[-/]\d{1,2}[-/]\d{1,2})|(\d{4}[-/]\d{1,2}[-/]\d{1,2}\s*$)/;
/** 续行的典型开头：缩进、闭合符号、列表符号、堆栈/异常关键字、JSON/XML 片段。 */
const CONTINUATION_PREFIX_REGEX = /^(\s|\)|\]|\}|>|\||\+|#|\*|-|~|\.{3}|at\s+\S|File\s+"|Traceback|Caused by|During handling|Exception|Stack trace|Caused:|\.\.\.|"|\{|'|<)/;
/** 上一条正文的结尾在提示“还没写完”。 */
const OPEN_TAIL_REGEX = /[:\\,{(=\[<]|\.{3}$|\b(and|or|with|from|to|for|of|in|by|at|via|using|expected|actual|because|while|when|if|then|the)\s*$/i;

/** 行内是否出现“像时间戳”的片段：出现就说明这更可能是一条（暂不支持的）新记录。 */
export function isTimestampLikeLogLine(line: string): boolean {
  return TIMESTAMP_LIKE_REGEX.test(String(line || ''));
}

export interface LogContinuationTarget {
  message: string;
  raw: string;
  continuationLines?: number;
}

/**
 * 这一行是不是上一条日志的续行？
 *
 * @param knownFormat 同一批里已经有日志解析成功、且失败行不多时传 true：格式已知，
 *   那么解析不出来的行几乎必然是续行（哪怕没有缩进提示）。
 */
export function looksLikeLogContinuation(
  line: string,
  previous?: LogContinuationTarget,
  options?: { knownFormat?: boolean },
): boolean {
  if (!line.trim()) return false;
  if (isTimestampLikeLogLine(line)) return false;
  const continued = Number(previous?.continuationLines || 0);
  if (continued >= LOG_CONTINUATION_MAX_LINES) return false;
  if (previous && String(previous.raw || previous.message || '').length > LOG_CONTINUATION_MAX_CHARS) return false;
  if (CONTINUATION_PREFIX_REGEX.test(line)) return true;
  if (continued > 0) return true;
  if (previous) {
    const tail = String(previous.message || previous.raw || '').trimEnd();
    if (tail && OPEN_TAIL_REGEX.test(tail)) return true;
  }
  return Boolean(options?.knownFormat);
}

/** 把一行续行并进上一条日志：正文与原文都补全，并记录续行数量。 */
export function appendLogContinuation<T extends LogContinuationTarget>(entry: T, line: string): T {
  const text = line.replace(/\r$/, '');
  entry.message = entry.message ? `${entry.message}\n${text}` : text;
  entry.raw = entry.raw ? `${entry.raw}\n${text}` : text;
  entry.continuationLines = Number(entry.continuationLines || 0) + 1;
  return entry;
}

/** 判断一批解析结果算不算“格式已知”：有成功的、且失败的不占多数。 */
export function logFormatIsKnown(validCount: number, issueCount: number): boolean {
  return validCount > 0 && issueCount <= validCount;
}

export interface ErrorMatchRule {
  id: string;
  keyword: string;
  caseSensitive: boolean;
  wholeWord: boolean;
  enabled: boolean;
}

export function createErrorMatchRule(
  keyword: string,
  options: Partial<Pick<ErrorMatchRule, 'caseSensitive' | 'wholeWord' | 'enabled'>> = {},
): ErrorMatchRule {
  const normalized = keyword.trim();
  return {
    id: `error-rule-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    keyword: normalized,
    caseSensitive: options.caseSensitive ?? false,
    wholeWord: options.wholeWord ?? false,
    enabled: options.enabled ?? true,
  };
}

export const DEFAULT_ERROR_RULES: readonly ErrorMatchRule[] = DEFAULT_ERROR_KEYWORDS.map((keyword, index) => ({
  id: `default-error-rule-${index}`,
  keyword,
  // 保持旧版本默认异常词的精确语义，避免升级后突然扩大误报范围。
  caseSensitive: true,
  wholeWord: true,
  enabled: true,
}));

function isIdentifierCharacter(value: string | undefined): boolean {
  return value !== undefined && /[\p{L}\p{N}_]/u.test(value);
}

export function matchesErrorRule(text: string, rule: Pick<ErrorMatchRule, 'keyword' | 'caseSensitive' | 'wholeWord' | 'enabled'>): boolean {
  if (!rule.enabled) return false;
  const token = rule.keyword.trim();
  if (!token) return false;

  const haystack = rule.caseSensitive ? text : text.toLocaleLowerCase();
  const needle = rule.caseSensitive ? token : token.toLocaleLowerCase();
  let searchFrom = 0;

  while (searchFrom <= haystack.length - needle.length) {
    const index = haystack.indexOf(needle, searchFrom);
    if (index < 0) return false;
    if (!rule.wholeWord) return true;

    const before = index > 0 ? text[index - 1] : undefined;
    const afterIndex = index + token.length;
    const after = afterIndex < text.length ? text[afterIndex] : undefined;
    if (!isIdentifierCharacter(before) && !isIdentifierCharacter(after)) return true;
    searchFrom = index + 1;
  }
  return false;
}

export function matchingErrorRules(
  text: string,
  rules: readonly ErrorMatchRule[],
): ErrorMatchRule[] {
  return rules.filter((rule) => matchesErrorRule(text, rule));
}

function containsKeyword(
  text: string,
  rules: readonly (string | ErrorMatchRule)[],
  defaults: Pick<ErrorMatchRule, 'caseSensitive' | 'wholeWord'> = { caseSensitive: true, wholeWord: true },
): boolean {
  return rules.some((item) => {
    const rule: ErrorMatchRule = typeof item === 'string'
      ? { id: item, keyword: item, caseSensitive: defaults.caseSensitive, wholeWord: defaults.wholeWord, enabled: true }
      : item;
    return matchesErrorRule(text, rule);
  });
}

function parseSource(raw: string): SourceLocation {
  const match = raw.match(/^(.*):(\d+)$/);
  if (!match) return { raw, fileName: raw };

  return {
    raw,
    fileName: match[1],
    lineNumber: Number(match[2]),
  };
}

function parseRpc(raw: string): RpcTrace {
  const [traceId = '0', spanId = '0', fatherSpanId = '0'] = raw.split(':');
  return {
    raw,
    traceId,
    spanId,
    fatherSpanId,
    isRpc: [traceId, spanId, fatherSpanId].some((value) => value !== '0'),
  };
}

function parseMarker(message: string): FlowMarker {
  const startIndex = message.search(START_MARKER_REGEX);
  const endIndex = message.search(END_MARKER_REGEX);

  if (startIndex >= 0 && (endIndex < 0 || startIndex < endIndex)) return 'start';
  if (endIndex >= 0) return 'end';
  return 'none';
}

interface ExecutorEnvelope {
  mode: string;
  rpc: string;
  message: string;
}

function readLeadingBracketFields(text: string, limit = 4): Array<{ value: string; end: number }> {
  const fields: Array<{ value: string; end: number }> = [];
  let offset = 0;
  while (fields.length < limit) {
    const remaining = text.slice(offset);
    const match = remaining.match(/^\s*\[([^\]]+)]/);
    if (!match) break;
    offset += match[0].length;
    fields.push({ value: match[1].trim(), end: offset });
  }
  return fields;
}

function looksLikeRpc(value: string | undefined): boolean {
  if (!value) return false;
  return value.split(':').length === 3;
}

/**
 * 执行器存在两种常见 envelope：
 * - 101 外部日志：... [source] [101] [trace:span:parent] message
 * - 100 内部日志：... [source] [context] [trace:span:parent] [100] message
 *
 * 100/101 的位置并不固定，因此不能继续按“source 后第 1 个字段就是 mode”解析。
 * 这里仅在 source 之后的前 3 个结构字段里识别 mode/rpc，并返回真正的正文起点。
 */
function parseExecutorEnvelope(rawLine: string): ExecutorEnvelope | undefined {
  const base = rawLine.match(EXECUTOR_BASE_REGEX);
  if (!base) return undefined;
  const tail = base[7];
  const fields = readLeadingBracketFields(tail, 4);
  if (fields.length < 2) return undefined;

  // 101（以及兼容的 100 标准布局）：mode 在 rpc 前。
  if (EXECUTOR_MODE_VALUES.has(fields[0].value) && looksLikeRpc(fields[1].value)) {
    return {
      mode: fields[0].value,
      rpc: fields[1].value,
      message: tail.slice(fields[1].end).trimStart(),
    };
  }

  // 100 内部布局：context / rpc / mode。也兼容未来同布局的 101。
  if (fields.length >= 3 && looksLikeRpc(fields[1].value) && EXECUTOR_MODE_VALUES.has(fields[2].value)) {
    return {
      mode: fields[2].value,
      rpc: fields[1].value,
      message: tail.slice(fields[2].end).trimStart(),
    };
  }
  return undefined;
}

function parseFunctionName(message: string, rawLine = message): string | undefined {
  const conventional = message.match(FUNCTION_PREFIX_REGEX)?.[1];
  if (conventional) return conventional;

  // 执行器函数名以边界对为准，而不是依赖 100/101 的字段位置。
  // 优先找紧邻 [START]/[END] 的 [FunctionName]，再兼容只有方向符的旧格式。
  for (const text of message === rawLine ? [message] : [message, rawLine]) {
    const bracketBoundary = text.match(BRACKET_BOUNDARY_FUNCTION_REGEX)?.[1];
    if (bracketBoundary) return `${bracketBoundary}()`;
    const directional = text.match(EXECUTOR_BRACKET_FUNCTION_REGEX)?.[1];
    if (directional) return `${directional}()`;
  }
  return undefined;
}


function parseBoundaryFunctionName(
  message: string,
  marker: FlowMarker,
  contextFunctionName?: string,
): string | undefined {
  if (marker === 'none') return undefined;

  const markerRegex = marker === 'start' ? START_MARKER_REGEX : END_MARKER_REGEX;
  const markerIndex = message.search(markerRegex);
  if (markerIndex < 0) return contextFunctionName;

  const prefix = message.slice(0, markerIndex);
  const bracketMatches = Array.from(prefix.matchAll(/\[([A-Za-z_~][\w:<>~.\-]*)\]/g));
  const bracketName = bracketMatches.at(-1)?.[1];
  return bracketName ? `${bracketName}()` : contextFunctionName;
}

function createSummary(message: string, marker: FlowMarker): string {
  const markerRegex = marker === 'start'
    ? START_MARKER_REGEX
    : marker === 'end'
      ? END_MARKER_REGEX
      : undefined;
  if (!markerRegex) return message.trim();

  const index = message.search(markerRegex);
  return index >= 0 ? message.slice(0, index).trim() : message.trim();
}

export function detectSeverity(
  level: string,
  message: string,
  errorKeywords: readonly (string | ErrorMatchRule)[] = DEFAULT_ERROR_RULES,
  warningKeywords: readonly string[] = DEFAULT_WARNING_KEYWORDS,
): ContentSeverity {
  const text = `${level} ${message}`;
  if (containsKeyword(text, errorKeywords)) return 'error';
  if (containsKeyword(text, warningKeywords)) return 'warning';
  return 'normal';
}

export function normalizeKeywordList(keywords: readonly string[]): string[] {
  const seen = new Set<string>();
  return keywords.flatMap((keyword) => {
    const trimmed = keyword.trim();
    if (!trimmed || seen.has(trimmed)) return [];
    seen.add(trimmed);
    return [trimmed];
  });
}

export function timestampToNs(timestamp: string): bigint | undefined {
  const match = timestamp.match(/^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})(?:\.(\d+))?$/);
  if (!match) return undefined;

  const date = new Date(`${match[1]}T${match[2]}`);
  if (Number.isNaN(date.getTime())) return undefined;

  const epochSeconds = BigInt(Math.trunc(date.getTime() / 1000));
  const nanoseconds = BigInt((match[3] ?? '').padEnd(9, '0').slice(0, 9) || '0');
  return epochSeconds * 1_000_000_000n + nanoseconds;
}

export function timestampToNsWithFormat(timestamp: string, format = 'auto'): bigint | undefined {
  const value = String(timestamp || '').trim();
  if (!value) return undefined;
  if (format === 'unix_ns') {
    try { return BigInt(value); } catch { return undefined; }
  }
  if (format === 'unix_ms') {
    const ms = Number(value);
    return Number.isFinite(ms) ? BigInt(Math.trunc(ms * 1_000_000)) : undefined;
  }
  const direct = timestampToNs(value);
  if (direct !== undefined) return direct;
  if (format === 'auto' || format.includes('/')) {
    const normalized = value.replace(/^(\d{4})\/(\d{2})\/(\d{2})/, '$1-$2-$3');
    return timestampToNs(normalized);
  }
  return undefined;
}

function globMatches(value: string, patternList: string): boolean {
  const patterns = String(patternList || '').split(/[;,]/).map((item) => item.trim()).filter(Boolean);
  if (!patterns.length) return true;
  return patterns.some((pattern) => {
    const source = pattern.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.');
    try { return new RegExp(`^${source}$`, 'i').test(value); } catch { return false; }
  });
}

function configuredRuleCategories(categories: readonly string[]): string[] {
  const normalized = categories.map((item) => String(item || '').trim().toLowerCase()).filter(Boolean);
  return normalized.length ? normalized : ['debug'];
}

function configuredRuleCandidates(
  rules: readonly LogFormatParserRuleConfig[],
  categories: readonly string[],
  sourceFileName: string,
  enabledOnly: boolean,
): LogFormatParserRuleConfig[] {
  const categorySet = new Set(configuredRuleCategories(categories));
  return rules
    .filter((rule) => (categorySet.has(rule.category.toLowerCase()) || rule.category === '*'))
    .filter((rule) => !enabledOnly || rule.enabled)
    .filter((rule) => globMatches(sourceFileName, rule.file_pattern))
    .sort((left, right) => right.priority - left.priority || left.id - right.id);
}

/** 快速提取时间，供尾部扫描规划使用；配置规则存在时优先使用其 timestamp 捕获。 */
export function extractTimestampNsFromLogLine(
  line: string,
  rules: readonly LogFormatParserRuleConfig[] = [],
  categories: readonly string[] = [],
  sourceFileName = '',
): bigint | undefined {
  for (const rule of configuredRuleCandidates(rules, categories, sourceFileName, true)) {
    const timestampGroup = rule.field_map.timestamp;
    if (!timestampGroup || !rule.client_pattern) continue;
    try {
      const match = new RegExp(rule.client_pattern, rule.ignore_case ? 'i' : '').exec(line);
      const value = match?.groups?.[timestampGroup];
      const timestampNs = value ? timestampToNsWithFormat(value, rule.timestamp_format) : undefined;
      if (timestampNs !== undefined) return timestampNs;
    } catch {
      continue;
    }
  }
  const match = line.match(/^\[([^\]]+)]/);
  return match ? timestampToNs(match[1]) : undefined;
}

function safeIdPart(value: string): string {
  return value.replace(/[^A-Za-z0-9_-]+/g, '-').slice(0, 80) || 'log';
}

export interface ParseLogLineOptions {
  sourceFileId: string;
  sourceFileName: string;
  lineNumber: number;
  idPrefix?: string;
  /** 仅用于选择解析规则的真实文件名；展示仍保留 sourceFileName。 */
  parserSourceFileName?: string;
}

export interface ParseLogLineResult {
  entry?: LogEntry;
  issue?: ParseIssue;
}

/**
 * 解析单行日志。该接口供主线程小文本解析和 Web Worker 流式解析共同使用，
 * 避免大文件先拼成完整字符串再 split。
 */
export function parseLogLine(rawLine: string, options: ParseLogLineOptions): ParseLogLineResult {
  if (!rawLine.trim()) return {};

  const match = rawLine.match(LOG_LINE_REGEX);
  if (!match) {
    return {
      issue: {
        sourceFile: options.sourceFileName,
        lineNumber: options.lineNumber,
        raw: rawLine,
        reason: '不符合当前日志格式',
      },
    };
  }

  const [, timestamp, level, component, processId, threadId, sourceRaw, parsedMode, parsedRpcRaw, parsedMessage] = match;
  const executorEnvelope = parseExecutorEnvelope(rawLine);
  const mode = executorEnvelope?.mode ?? parsedMode;
  const rpcRaw = executorEnvelope?.rpc ?? parsedRpcRaw;
  const message = executorEnvelope?.message ?? parsedMessage;
  const marker = parseMarker(message);
  const functionName = parseFunctionName(message, rawLine);
  const boundaryFunctionName = parseBoundaryFunctionName(message, marker, functionName);
  const idPrefix = options.idPrefix ?? safeIdPart(options.sourceFileId || options.sourceFileName);

  return {
    entry: {
      id: `${idPrefix}-line-${options.lineNumber}`,
      sourceFile: options.sourceFileName,
      sourceFileId: options.sourceFileId,
      lineNumber: options.lineNumber,
      timestamp,
      timestampNs: timestampToNs(timestamp),
      level,
      component,
      processId,
      threadId,
      source: parseSource(sourceRaw),
      mode,
      rpc: parseRpc(rpcRaw),
      message,
      functionName,
      boundaryFunctionName,
      marker,
      severity: detectSeverity(level, message),
      summary: createSummary(message, marker),
      raw: rawLine,
    },
  };
}

export function parseLogDocument(document: LogDocument): ParseResult {
  const entries: LogEntry[] = [];
  const issues: ParseIssue[] = [];
  const lines = document.text.replace(/^\uFEFF/, '').split(/\r?\n/);
  const idPrefix = safeIdPart(document.id || document.name);

  lines.forEach((rawLine, index) => {
    const result = parseLogLine(rawLine, {
      sourceFileId: document.id,
      sourceFileName: document.name,
      lineNumber: index + 1,
      idPrefix,
    });
    if (result.entry) {
      entries.push(result.entry);
      return;
    }
    // 解析不出来的行先按「多行日志续行」试着并进上一条（堆栈、JSON dump、换行正文），
    // 并回去就不算格式告警；不满足续行条件时行为与过去完全一致。
    const previous = entries[entries.length - 1];
    if (result.issue && previous && looksLikeLogContinuation(rawLine, previous, {
      knownFormat: logFormatIsKnown(entries.length, issues.length),
    })) {
      appendLogContinuation(previous, rawLine);
      return;
    }
    if (result.issue) issues.push(result.issue);
  });

  return { entries, issues };
}

export function parseLogDocuments(documents: LogDocument[]): ParseResult {
  const results = documents.map(parseLogDocument);
  return {
    entries: results.flatMap((result) => result.entries),
    issues: results.flatMap((result) => result.issues),
  };
}

/** 保留旧调用方式，供解析器测试或外部复用。 */
export function parseLogText(text: string): ParseResult {
  return parseLogDocument({ id: 'inline-log', name: 'inline.log', text });
}



function splitLinkedEventValues(raw: string): string[] {
  const text = raw.trim();
  if (!text) return [];
  return text.split(/[,;|\s]+/).map((item) => item.trim()).filter(Boolean);
}

function runEventSeverity(level: string): ContentSeverity {
  const normalized = level.trim().toUpperCase();
  if (normalized.includes('ERROR') || normalized.includes('ALARM')) return 'error';
  if (normalized.includes('WARNING') || normalized === 'WARN' || normalized.includes('(WARN)')) return 'warning';
  if (normalized.includes('EVENT') || normalized.includes('INFO')) return 'normal';
  return detectSeverity(level, '');
}

function emptyRpcTrace(): RpcTrace {
  return { raw: '0:0:0', traceId: '0', spanId: '0', fatherSpanId: '0', isRpc: false };
}

/**
 * 运行日志 event.log 独立格式：
 * 时间 / 组件 / PID / 源码位置 / 事件类别 / 事件级别 / 当前事件码 / 链接事件码 /
 * 当前 ErrIId / 链接 ErrIId / DisplayCode / 链接 DisplayCode / SET|EVT|DEA|CLR / 描述。
 */
export function parseRunEventLogLine(rawLine: string, options: ParseLogLineOptions): ParseLogLineResult {
  if (!rawLine.trim()) return {};
  const match = rawLine.match(RUN_EVENT_LOG_REGEX);
  if (!match) {
    return {
      issue: {
        sourceFile: options.sourceFileName,
        lineNumber: options.lineNumber,
        raw: rawLine,
        reason: '不符合运行日志 event 格式',
      },
    };
  }

  const [
    , timestamp, component, processId, sourceRaw, eventCategory, eventLevel,
    currentEventCode, linkedEventCodesRaw, currentErrIId, linkedErrIIdsRaw,
    displayCode, linkedDisplayCodesRaw, eventTypeRaw, message,
  ] = match;
  const eventType = eventTypeRaw.trim().toUpperCase();
  const normalizedCategory = eventCategory.trim().toUpperCase();
  if (!RUN_EVENT_CATEGORIES.has(normalizedCategory) || !RUN_EVENT_TYPES.has(eventType)) {
    return {
      issue: {
        sourceFile: options.sourceFileName,
        lineNumber: options.lineNumber,
        raw: rawLine,
        reason: '运行日志事件类别或事件类型无法识别',
      },
    };
  }

  const idPrefix = options.idPrefix ?? safeIdPart(options.sourceFileId || options.sourceFileName);
  return {
    entry: {
      id: `${idPrefix}-line-${options.lineNumber}`,
      sourceFile: options.sourceFileName,
      sourceFileId: options.sourceFileId,
      lineNumber: options.lineNumber,
      timestamp,
      timestampNs: timestampToNs(timestamp),
      level: eventLevel,
      component,
      processId,
      threadId: '—',
      source: parseSource(sourceRaw),
      mode: eventType,
      rpc: emptyRpcTrace(),
      message,
      marker: 'none',
      severity: runEventSeverity(eventLevel),
      summary: message.trim(),
      raw: rawLine,
      logCategory: 'run',
      parserProfile: 'run',
      runEvent: {
        eventCategory,
        eventLevel,
        currentEventCode,
        linkedEventCodes: splitLinkedEventValues(linkedEventCodesRaw),
        currentErrIId,
        linkedErrIIds: splitLinkedEventValues(linkedErrIIdsRaw),
        displayCode,
        linkedDisplayCodes: splitLinkedEventValues(linkedDisplayCodesRaw),
        eventType,
      },
    },
  };
}

export type LogParserProfile = 'debug' | 'executor' | 'run' | 'default';

export function parserProfileForCategory(category?: string): LogParserProfile {
  if (category === 'executor') return 'executor';
  if (category === 'run') return 'run';
  if (category === 'debug') return 'debug';
  return 'default';
}

/**
 * 日志类别解析分发入口。运行日志 event 使用独立结构解析；调试/执行器保留原格式。
 */
export function parseLogLineWithProfile(
  profile: LogParserProfile,
  rawLine: string,
  options: ParseLogLineOptions,
): ParseLogLineResult {
  if (profile === 'run') return parseRunEventLogLine(rawLine, options);
  return parseLogLine(rawLine, options);
}

function mappedConfiguredValue(groups: Record<string, string | undefined>, rule: LogFormatParserRuleConfig, field: string): string {
  const groupName = rule.field_map[field];
  return groupName ? String(groups[groupName] ?? '') : '';
}

function parseConfiguredLogLine(
  rule: LogFormatParserRuleConfig,
  rawLine: string,
  options: ParseLogLineOptions,
): ParseLogLineResult {
  if (!rule.client_pattern) return {};
  let match: RegExpExecArray | null = null;
  try {
    match = new RegExp(rule.client_pattern, rule.ignore_case ? 'i' : '').exec(rawLine);
  } catch {
    return {};
  }
  if (!match) return {};
  const groups = (match.groups ?? {}) as Record<string, string | undefined>;
  const timestamp = mappedConfiguredValue(groups, rule, 'timestamp');
  const level = mappedConfiguredValue(groups, rule, 'level') || mappedConfiguredValue(groups, rule, 'event_level') || '—';
  const component = mappedConfiguredValue(groups, rule, 'component') || '—';
  const processId = mappedConfiguredValue(groups, rule, 'process_id') || '—';
  const threadId = mappedConfiguredValue(groups, rule, 'thread_id') || '—';
  const sourceRaw = mappedConfiguredValue(groups, rule, 'source');
  let mode = mappedConfiguredValue(groups, rule, 'mode') || mappedConfiguredValue(groups, rule, 'event_type');
  let rpcRaw = mappedConfiguredValue(groups, rule, 'rpc') || '0:0:0';
  let message = mappedConfiguredValue(groups, rule, 'message');
  if (rule.category === 'executor') {
    const executorEnvelope = parseExecutorEnvelope(rawLine);
    if (executorEnvelope) {
      mode = executorEnvelope.mode;
      rpcRaw = executorEnvelope.rpc;
      message = executorEnvelope.message;
    }
  }
  const marker = parseMarker(message);
  const functionName = parseFunctionName(message, rawLine);
  const boundaryFunctionName = parseBoundaryFunctionName(message, marker, functionName);
  const idPrefix = options.idPrefix ?? safeIdPart(options.sourceFileId || options.sourceFileName);
  const eventType = mappedConfiguredValue(groups, rule, 'event_type').trim().toUpperCase();
  const eventLevel = mappedConfiguredValue(groups, rule, 'event_level') || level;
  const isRunEvent = Boolean(eventType || mappedConfiguredValue(groups, rule, 'event_category'));
  return {
    entry: {
      id: `${idPrefix}-line-${options.lineNumber}`,
      sourceFile: options.sourceFileName,
      sourceFileId: options.sourceFileId,
      lineNumber: options.lineNumber,
      timestamp,
      timestampNs: timestampToNsWithFormat(timestamp, rule.timestamp_format),
      level,
      component,
      processId,
      threadId,
      source: parseSource(sourceRaw),
      mode,
      rpc: parseRpc(rpcRaw),
      message,
      functionName,
      boundaryFunctionName,
      marker,
      severity: isRunEvent ? runEventSeverity(eventLevel) : detectSeverity(level, message),
      summary: createSummary(message, marker),
      raw: rawLine,
      logCategory: rule.category,
      parserProfile: rule.name,
      ...(isRunEvent ? {
        runEvent: {
          eventCategory: mappedConfiguredValue(groups, rule, 'event_category'),
          eventLevel,
          currentEventCode: mappedConfiguredValue(groups, rule, 'current_event_code'),
          linkedEventCodes: splitLinkedEventValues(mappedConfiguredValue(groups, rule, 'linked_event_codes')),
          currentErrIId: mappedConfiguredValue(groups, rule, 'current_err_iid'),
          linkedErrIIds: splitLinkedEventValues(mappedConfiguredValue(groups, rule, 'linked_err_iids')),
          displayCode: mappedConfiguredValue(groups, rule, 'display_code'),
          linkedDisplayCodes: splitLinkedEventValues(mappedConfiguredValue(groups, rule, 'linked_display_codes')),
          eventType,
        },
      } : {}),
    },
  };
}

export function parseLogLineByCategories(
  rawLine: string,
  options: ParseLogLineOptions,
  categories: readonly string[] = [],
  formatRules: readonly LogFormatParserRuleConfig[] = [],
): ParseLogLineResult {
  if (formatRules.length > 0) {
    const configured = configuredRuleCandidates(formatRules, categories, options.parserSourceFileName ?? options.sourceFileName, false);
    const enabled = configured.filter((rule) => rule.enabled);
    for (const rule of enabled) {
      const result = parseConfiguredLogLine(rule, rawLine, options);
      if (result.entry) return result;
    }
    if (configured.length > 0) {
      return { issue: { sourceFile: options.sourceFileName, lineNumber: options.lineNumber, raw: rawLine, reason: enabled.length ? '未命中已配置的日志格式解析规则' : '当前日志类型的解析规则均已停用' } };
    }
  }
  // 混合检索时，一个下载流里可能同时包含 debug/executor/run。
  // event 行结构是自描述的：只要任务包含 run，就优先尝试运行日志解析；
  // 成功后直接标记 run，失败再交给普通解析器，避免运行日志被当作调试日志拆错字段。
  if (categories.includes('run')) {
    const runResult = parseRunEventLogLine(rawLine, options);
    if (runResult.entry) return runResult;
    if (categories.length === 1) return runResult;
  }

  const nonRunCategories = categories.filter((category) => category !== 'run');
  const category = nonRunCategories.length === 1 ? nonRunCategories[0] : undefined;
  const profile = parserProfileForCategory(category);
  const result = parseLogLineWithProfile(profile, rawLine, options);
  if (result.entry) {
    result.entry.logCategory = category ?? (categories.length > 1 ? 'mixed' : 'imported');
    result.entry.parserProfile = profile === 'default' ? 'debug' : profile;
  }
  return result;
}
