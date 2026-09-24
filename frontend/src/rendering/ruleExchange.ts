import type { LogEntry } from '../types';
import {
  createDisplayRuleId,
  extractDisplayRuleMessage,
  validateDisplayRule,
} from './displayRules';
import type {
  DisplayPatternToken,
  DisplayRule,
  DisplayRuleKind,
  DisplayRuleParameter,
  DisplayRuleScope,
} from './displayRules';

export type RuleWorkbookExportMode = 'rules' | 'filtered';

export const RULE_SAMPLE_EXPORT_LIMIT = 100;
export const FILTERED_LOG_EXPORT_LIMIT = 500;
export const RULE_SAMPLE_SCAN_LIMIT = 2000;

export interface RuleImportCandidate {
  sourceSheet: string;
  rowNumber: number;
  priority: number;
  rule?: DisplayRule;
  errors: string[];
}

export interface RuleImportPreview {
  candidates: RuleImportCandidate[];
  sourceName: string;
  generatedAt: number;
}

export interface RuleImportAnalysis {
  additions: RuleImportCandidate[];
  updates: RuleImportCandidate[];
  duplicates: RuleImportCandidate[];
  invalid: RuleImportCandidate[];
  conflicts: RuleImportCandidate[];
  applicableRules: DisplayRule[];
}

export const RULE_IMPORT_SNAPSHOT_KEY = 'tracelens.display-rules.import-snapshot.v1';

export const ENABLED_CN = ['启用', '停用'] as const;
export const RULE_KIND_CN = ['关键字替换', '智能参数模板'] as const;
export const RULE_SCOPE_CN = ['仅函数标题', '仅日志正文', '函数标题和日志正文'] as const;
export const GENERATE_RULE_CN = ['是', '否'] as const;

export function ruleKindToChinese(kind: DisplayRuleKind): string {
  return kind === 'keyword' ? '关键字替换' : '智能参数模板';
}

export function chineseToRuleKind(value: string): DisplayRuleKind | undefined {
  if (value === '关键字替换') return 'keyword';
  if (value === '智能参数模板') return 'template';
  return undefined;
}

export function ruleScopeToChinese(scope: DisplayRuleScope): string {
  if (scope === 'function') return '仅函数标题';
  if (scope === 'log') return '仅日志正文';
  return '函数标题和日志正文';
}

export function chineseToRuleScope(value: string): DisplayRuleScope | undefined {
  if (value === '仅函数标题') return 'function';
  if (value === '仅日志正文') return 'log';
  if (value === '函数标题和日志正文') return 'both';
  return undefined;
}

export function displayPatternToReadableTemplate(rule: DisplayRule): string {
  if (rule.kind !== 'template') return '';
  const parameterById = new Map(rule.parameters.map((parameter) => [parameter.id, parameter.label]));
  return (rule.patternTokens ?? []).map((token) => (
    token.kind === 'text' ? token.value : `{${parameterById.get(token.parameterId) ?? token.parameterId}}`
  )).join('');
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function compileFixedText(value: string): string {
  return value
    .split(/(\s+)/)
    .map((part) => /\s+/.test(part) ? '\\s+' : escapeRegExp(part))
    .join('');
}

export function readableTemplateToPattern(
  readableTemplate: string,
  sampleMessage: string,
): { tokens: DisplayPatternToken[]; parameters: DisplayRuleParameter[]; errors: string[] } {
  const errors: string[] = [];
  const parameters: DisplayRuleParameter[] = [];
  const parameterIdByLabel = new Map<string, string>();
  const tokens: DisplayPatternToken[] = [];
  const regex = /\{([^{}]+)\}/g;
  let cursor = 0;
  let match: RegExpExecArray | null;

  while ((match = regex.exec(readableTemplate)) !== null) {
    if (match.index > cursor) tokens.push({ kind: 'text', value: readableTemplate.slice(cursor, match.index) });
    const label = match[1].trim();
    if (!label) {
      errors.push('匹配模板包含空参数名');
      cursor = regex.lastIndex;
      continue;
    }
    let parameterId = parameterIdByLabel.get(label);
    if (!parameterId) {
      parameterId = `parameter-${parameters.length + 1}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
      parameterIdByLabel.set(label, parameterId);
      parameters.push({ id: parameterId, label, sampleValue: '' });
    }
    tokens.push({ kind: 'parameter', parameterId });
    cursor = regex.lastIndex;
  }
  if (cursor < readableTemplate.length) tokens.push({ kind: 'text', value: readableTemplate.slice(cursor) });

  if (parameters.length === 0) errors.push('智能参数模板至少需要一个 {参数} 占位符');
  if (!sampleMessage.trim()) errors.push('智能参数模板必须填写样例日志正文');

  const firstCaptureById = new Map<string, number>();
  let captureCount = 0;
  // Excel 中的可读匹配模板同样按完整日志正文解析。首尾锚定可保证
  // 句尾参数完整提取，而不是让 (.+?) 在首字符处提前结束。
  let source = '^';
  tokens.forEach((token) => {
    if (token.kind === 'text') {
      source += compileFixedText(token.value);
      return;
    }
    const existing = firstCaptureById.get(token.parameterId);
    if (existing) source += `\\${existing}`;
    else {
      captureCount += 1;
      firstCaptureById.set(token.parameterId, captureCount);
      source += '([\\s\\S]+?)';
    }
  });
  source += '$';

  try {
    const expression = new RegExp(source);
    const sampleMatch = expression.exec(extractDisplayRuleMessage(sampleMessage));
    if (!sampleMatch) errors.push('样例日志正文无法匹配填写的匹配模板');
    else {
      firstCaptureById.forEach((captureIndex, parameterId) => {
        const parameter = parameters.find((item) => item.id === parameterId);
        if (parameter) parameter.sampleValue = sampleMatch[captureIndex] ?? '';
      });
    }
  } catch {
    errors.push('匹配模板无法生成有效的自动提取规则');
  }

  return { tokens, parameters, errors };
}

export interface ImportedRuleRow {
  sourceSheet: string;
  rowNumber: number;
  priority?: unknown;
  ruleId?: unknown;
  enabled?: unknown;
  name?: unknown;
  kind?: unknown;
  scope?: unknown;
  keyword?: unknown;
  matchTemplate?: unknown;
  displayTemplate?: unknown;
  supplementalDescription?: unknown;
  sampleMessage?: unknown;
}

function stringValue(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'object' && value && 'text' in value) return String((value as { text?: unknown }).text ?? '');
  return String(value).trim();
}

export function importedRowToCandidate(row: ImportedRuleRow): RuleImportCandidate {
  const errors: string[] = [];
  const kindText = stringValue(row.kind);
  const scopeText = stringValue(row.scope);
  const enabledText = stringValue(row.enabled) || '启用';
  const kind = chineseToRuleKind(kindText);
  const scope = chineseToRuleScope(scopeText);
  const priorityValue = Number(row.priority);
  const priority = Number.isFinite(priorityValue) && priorityValue > 0 ? Math.floor(priorityValue) : row.rowNumber;

  if (!kind) errors.push(`规则类型“${kindText || '空'}”无效`);
  if (!scope) errors.push(`应用范围“${scopeText || '空'}”无效`);
  if (!ENABLED_CN.includes(enabledText as (typeof ENABLED_CN)[number])) errors.push(`启用状态“${enabledText}”无效`);

  const sampleMessage = extractDisplayRuleMessage(stringValue(row.sampleMessage));
  const rule: DisplayRule = {
    id: stringValue(row.ruleId) || createDisplayRuleId(),
    name: stringValue(row.name),
    enabled: enabledText !== '停用',
    kind: kind ?? 'keyword',
    scope: scope ?? 'function',
    keyword: stringValue(row.keyword),
    displayTemplate: stringValue(row.displayTemplate),
    supplementalDescription: stringValue(row.supplementalDescription),
    patternTokens: undefined,
    parameters: [],
    sampleMessage,
    createdAt: Date.now(),
  };

  if (kind === 'template') {
    const parsed = readableTemplateToPattern(stringValue(row.matchTemplate), sampleMessage);
    rule.patternTokens = parsed.tokens;
    rule.parameters = parsed.parameters;
    errors.push(...parsed.errors);
  }

  const validation = validateDisplayRule(rule);
  if (validation) errors.push(validation);
  return { sourceSheet: row.sourceSheet, rowNumber: row.rowNumber, priority, rule, errors: Array.from(new Set(errors)) };
}

export function displayRuleSignature(rule: DisplayRule): string {
  return JSON.stringify({
    kind: rule.kind,
    scope: rule.scope,
    keyword: rule.keyword?.trim() ?? '',
    pattern: displayPatternToReadableTemplate(rule),
    template: rule.displayTemplate.trim(),
    displayMode: rule.displayMode ?? 'semantic',
    customLabelTemplate: rule.customLabelTemplate?.trim() ?? '',
    customLabelColor: rule.customLabelColor ?? '#2563eb',
    showLabelOnTimeline: rule.showLabelOnTimeline !== false,
    supplemental: rule.supplementalDescription?.trim() ?? '',
  });
}

export function analyzeRuleImport(existingRules: readonly DisplayRule[], preview: RuleImportPreview): RuleImportAnalysis {
  const existingById = new Map(existingRules.map((rule) => [rule.id, rule]));
  const existingBySignature = new Map(existingRules.map((rule) => [displayRuleSignature(rule), rule]));
  const valid = preview.candidates.filter((candidate) => candidate.rule && candidate.errors.length === 0);
  const invalid = preview.candidates.filter((candidate) => !candidate.rule || candidate.errors.length > 0);
  const additions: RuleImportCandidate[] = [];
  const updates: RuleImportCandidate[] = [];
  const duplicates: RuleImportCandidate[] = [];
  const conflicts: RuleImportCandidate[] = [];
  const acceptedSignatures = new Set<string>();
  const prioritySignatures = new Map<number, Set<string>>();

  valid.sort((left, right) => left.priority - right.priority || left.rowNumber - right.rowNumber).forEach((candidate) => {
    const rule = candidate.rule!;
    const signature = displayRuleSignature(rule);
    const sameId = existingById.get(rule.id);
    const sameSignature = existingBySignature.get(signature);
    if (sameId) {
      updates.push(candidate);
      acceptedSignatures.add(signature);
      return;
    }
    if (sameSignature || acceptedSignatures.has(signature)) {
      duplicates.push(candidate);
      return;
    }
    const atPriority = prioritySignatures.get(candidate.priority) ?? new Set<string>();
    if (atPriority.size > 0 && !atPriority.has(signature)) conflicts.push(candidate);
    else additions.push(candidate);
    atPriority.add(signature);
    prioritySignatures.set(candidate.priority, atPriority);
    acceptedSignatures.add(signature);
  });

  const orderedCandidates = [...updates, ...additions, ...conflicts]
    .filter((candidate) => candidate.rule)
    .sort((left, right) => left.priority - right.priority || left.rowNumber - right.rowNumber);
  const importedIds = new Set(orderedCandidates.map((candidate) => candidate.rule!.id));
  const importedRules = orderedCandidates.map((candidate) => candidate.rule!);
  const untouchedRules = existingRules.filter((rule) => !importedIds.has(rule.id));
  const applicableRules = [...importedRules, ...untouchedRules];

  return { additions, updates, duplicates, invalid, conflicts, applicableRules };
}

export function logEntryToChineseRecord(entry: LogEntry): Record<string, string | number> {
  return {
    时间: entry.timestamp,
    级别: entry.level,
    组件: entry.component,
    进程ID: entry.processId,
    线程ID: entry.threadId,
    源文件: entry.source.fileName,
    源文件行号: entry.source.lineNumber ?? '',
    TraceID: entry.rpc.traceId,
    SpanID: entry.rpc.spanId,
    父SpanID: entry.rpc.fatherSpanId,
    上下文函数: entry.functionName ?? '',
    子调用函数: entry.boundaryFunctionName ?? '',
    日志正文: entry.message,
    原始日志: entry.raw,
  };
}

export function selectWorkbookExportEntries(
  mode: RuleWorkbookExportMode,
  entries: readonly LogEntry[],
): LogEntry[] {
  const limit = mode === 'filtered' ? FILTERED_LOG_EXPORT_LIMIT : RULE_SAMPLE_SCAN_LIMIT;
  return entries.slice(0, limit);
}

export function uniqueLogSamples(entries: readonly LogEntry[], limit = RULE_SAMPLE_EXPORT_LIMIT): LogEntry[] {
  const result: LogEntry[] = [];
  const seen = new Set<string>();
  for (const entry of entries) {
    const key = [entry.component, entry.functionName ?? '', entry.boundaryFunctionName ?? '', entry.message].join('\u0001');
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(entry);
    if (result.length >= limit) break;
  }
  return result;
}
