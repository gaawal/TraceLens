import type { FunctionNode, LogEntry, TimelineItem } from '../types';

export type DisplayRuleKind = 'keyword' | 'template';
export type DisplayRuleScope = 'function' | 'log' | 'both';
export type DisplayRuleMode = 'semantic' | 'label' | 'both';

export interface DisplayRuleParameter {
  id: string;
  label: string;
  sampleValue: string;
}

export type DisplayPatternToken =
  | { kind: 'text'; value: string }
  | { kind: 'parameter'; parameterId: string };

export interface DisplayRule {
  id: string;
  name: string;
  enabled: boolean;
  kind: DisplayRuleKind;
  scope: DisplayRuleScope;
  /** 简单规则：日志正文或函数名中需要包含的区分大小写关键字。 */
  keyword?: string;
  /** 命中后展示的语义说明模板，可包含 {参数1} 等占位符。 */
  displayTemplate: string;
  /** 展示模式：仅语义、仅自定义标签、语义 + 自定义标签。旧规则默认 semantic。 */
  displayMode?: DisplayRuleMode;
  /** 日志行右侧自定义标签文本模板，可包含参数占位符。 */
  customLabelTemplate?: string;
  /** 自定义标签颜色，同时用于时间线标签 marker。 */
  customLabelColor?: string;
  /** 是否把自定义标签颜色投射到时间线。 */
  showLabelOnTimeline?: boolean;
  /**
   * 实时监听 — whether 实时监听 keeps a server-side watch for this rule and streams its hits
   * into the timeline ribbon. Explicit per rule: the ribbon needs to be told what to watch,
   * not left hoping something matches.
   */
  liveWatch?: boolean;
  /** 可选的第二层知识说明：异常含义、处置建议、排查策略等。 */
  supplementalDescription?: string;
  /** 高级规则：由可视化选区生成，不要求用户编写正则。 */
  patternTokens?: DisplayPatternToken[];
  parameters: DisplayRuleParameter[];
  sampleMessage?: string;
  createdAt: number;
}

export interface ParameterMark {
  parameterId: string;
  start: number;
  end: number;
}

export interface DisplayRuleEditorRequest {
  requestId: string;
  ruleId?: string;
  source: 'function' | 'log';
  functionName?: string;
  sampleRaw: string;
  suggestedName: string;
  suggestedKeyword: string;
  suggestedScope: DisplayRuleScope;
  /** 自动源码语义只作为编辑器预填值，未确认保存前不进入规则库。 */
  suggestedDisplayTemplate?: string;
  /** 自动源码语义快捷配置时，直接打开完整语义规则编辑器。 */
  openSemanticEditor?: boolean;
  /** 远程 Python 源码语义识别上下文；本地粘贴日志没有这些字段。 */
  environmentId?: number;
  sourceFile?: string;
  sourceLine?: number;
  sourceTargets?: Array<{ subsystem: string; module: string }>;
}

export interface DisplayRuleMatch {
  ruleId: string;
  ruleName: string;
  text: string;
  supplementalText?: string;
  customLabelText?: string;
  customLabelColor?: string;
  showLabelOnTimeline?: boolean;
  parameters: Record<string, string>;
  sourceMessage: string;
  sourceEntryId?: string;
}

export interface DisplayRuleStore {
  load(): DisplayRule[];
  save(rules: readonly DisplayRule[]): void;
}

export const DISPLAY_RULE_STORAGE_KEY = 'tracelens.display-rules.v1';
export const DISPLAY_RULE_BACKUP_STORAGE_KEY = 'tracelens.display-rules.backup.v1';
export const SEMANTIC_LABEL_STORAGE_KEY = 'tracelens.semantic-labels.enabled.v1';

function safeParseRules(value: string | null): DisplayRule[] {
  if (!value) return [];
  try {
    const parsed = JSON.parse(value);
    if (!Array.isArray(parsed)) return [];
    return parsed.flatMap((candidate): DisplayRule[] => {
      if (!candidate || typeof candidate !== 'object') return [];
      if (candidate.kind !== 'keyword' && candidate.kind !== 'template') return [];
      if (typeof candidate.id !== 'string' || typeof candidate.displayTemplate !== 'string') return [];
      const parameters = Array.isArray(candidate.parameters)
        ? candidate.parameters.filter((item: unknown): item is DisplayRuleParameter => Boolean(
          item &&
          typeof item === 'object' &&
          typeof (item as DisplayRuleParameter).id === 'string' &&
          typeof (item as DisplayRuleParameter).label === 'string' &&
          typeof (item as DisplayRuleParameter).sampleValue === 'string',
        ))
        : [];
      const patternTokens = Array.isArray(candidate.patternTokens)
        ? candidate.patternTokens.filter((item: unknown): item is DisplayPatternToken => Boolean(
          item &&
          typeof item === 'object' &&
          (((item as DisplayPatternToken).kind === 'text' && typeof (item as { value?: unknown }).value === 'string') ||
            ((item as DisplayPatternToken).kind === 'parameter' && typeof (item as { parameterId?: unknown }).parameterId === 'string')),
        ))
        : undefined;
      return [{
        id: candidate.id,
        name: typeof candidate.name === 'string' ? candidate.name : '未命名规则',
        enabled: candidate.enabled !== false,
        kind: candidate.kind,
        // 保留用户配置的展示位置：函数标题、日志正文或两处同时展示。
        scope: candidate.scope === 'log' || candidate.scope === 'both' || candidate.scope === 'function'
          ? candidate.scope
          : 'function',
        keyword: typeof candidate.keyword === 'string' ? candidate.keyword : undefined,
        displayTemplate: candidate.displayTemplate,
        displayMode: candidate.displayMode === 'label' || candidate.displayMode === 'both' || candidate.displayMode === 'semantic' ? candidate.displayMode : 'semantic',
        customLabelTemplate: typeof candidate.customLabelTemplate === 'string' ? candidate.customLabelTemplate : '',
        customLabelColor: typeof candidate.customLabelColor === 'string' && /^#[0-9a-fA-F]{6}$/.test(candidate.customLabelColor) ? candidate.customLabelColor : '#2563eb',
        showLabelOnTimeline: candidate.showLabelOnTimeline !== false,
        liveWatch: candidate.liveWatch === true,
        supplementalDescription: typeof candidate.supplementalDescription === 'string' ? candidate.supplementalDescription : undefined,
        patternTokens,
        parameters,
        sampleMessage: typeof candidate.sampleMessage === 'string' ? candidate.sampleMessage : undefined,
        createdAt: typeof candidate.createdAt === 'number' ? candidate.createdAt : Date.now(),
      }];
    });
  } catch {
    return [];
  }
}

export function parseDisplayRulesJson(value: string): DisplayRule[] {
  return safeParseRules(value);
}

export const localDisplayRuleStore: DisplayRuleStore & { loadBackup(): DisplayRule[] } = {
  load(): DisplayRule[] {
    if (typeof window === 'undefined') return [];
    return safeParseRules(window.localStorage.getItem(DISPLAY_RULE_STORAGE_KEY));
  },
  loadBackup(): DisplayRule[] {
    if (typeof window === 'undefined') return [];
    return safeParseRules(window.localStorage.getItem(DISPLAY_RULE_BACKUP_STORAGE_KEY));
  },
  save(rules: readonly DisplayRule[]): void {
    if (typeof window === 'undefined') return;
    const serialized = JSON.stringify(rules);
    // 主 key 反映当前状态；非空规则额外写入“只增不清”的恢复备份。
    // 后台返回 [] 或用户明确删除规则时不会擦除历史备份。
    window.localStorage.setItem(DISPLAY_RULE_STORAGE_KEY, serialized);
    if (rules.length > 0) {
      window.localStorage.setItem(DISPLAY_RULE_BACKUP_STORAGE_KEY, serialized);
    }
  },
};

export function loadSemanticLabelsEnabled(): boolean {
  if (typeof window === 'undefined') return true;
  return window.localStorage.getItem(SEMANTIC_LABEL_STORAGE_KEY) !== 'false';
}

export function saveSemanticLabelsEnabled(enabled: boolean): void {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(SEMANTIC_LABEL_STORAGE_KEY, String(enabled));
}

export function createDisplayRuleId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID();
  return `display-rule-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
}

/**
 * 用户既可以粘贴完整原始日志，也可以只粘贴正文。这里自动剥离前八个 [] 字段。
 */
export function extractDisplayRuleMessage(raw: string): string {
  const firstNonEmpty = raw.split(/\r?\n/).find((line) => line.trim())?.trim() ?? '';
  const match = firstNonEmpty.match(/^(?:\[[^\]]*]\s*){8}(.*)$/);
  return (match?.[1] ?? firstNonEmpty).trim();
}

function selectionOverlaps(left: ParameterMark, right: ParameterMark): boolean {
  return left.start < right.end && right.start < left.end;
}

/**
 * 把当前选中的文本设为一个参数。可选同步标记样例中所有相同文本，
 * 适合 DSPWSFT 在同一固定语句中重复出现的场景；默认只标记当前选区。
 */
export function markParameterOccurrences(
  sample: string,
  selectionStart: number,
  selectionEnd: number,
  parameterId: string,
  existingMarks: readonly ParameterMark[],
  markAllOccurrences = false,
): { value: string; marks: ParameterMark[] } {
  const start = Math.max(0, Math.min(selectionStart, selectionEnd));
  const end = Math.min(sample.length, Math.max(selectionStart, selectionEnd));
  const value = sample.slice(start, end);
  if (!value) return { value: '', marks: [...existingMarks] };

  const marks = existingMarks.filter((mark) => mark.parameterId !== parameterId);
  const selectedMark: ParameterMark = { parameterId, start, end };
  if (!marks.some((mark) => selectionOverlaps(mark, selectedMark))) marks.push(selectedMark);

  if (markAllOccurrences) {
    let cursor = 0;
    while (cursor <= sample.length - value.length) {
      const index = sample.indexOf(value, cursor);
      if (index < 0) break;
      const candidate: ParameterMark = { parameterId, start: index, end: index + value.length };
      if (!marks.some((mark) => selectionOverlaps(mark, candidate))) marks.push(candidate);
      cursor = index + Math.max(1, value.length);
    }
  }

  return {
    value,
    marks: marks.sort((left, right) => left.start - right.start || left.end - right.end),
  };
}

export function removeParameterMarks(marks: readonly ParameterMark[], parameterId: string): ParameterMark[] {
  return marks.filter((mark) => mark.parameterId !== parameterId);
}

export function buildPatternTokens(sample: string, marks: readonly ParameterMark[]): DisplayPatternToken[] {
  const ordered = [...marks]
    .filter((mark) => mark.start >= 0 && mark.end <= sample.length && mark.start < mark.end)
    .sort((left, right) => left.start - right.start || left.end - right.end);

  const tokens: DisplayPatternToken[] = [];
  let cursor = 0;
  ordered.forEach((mark) => {
    if (mark.start < cursor) return;
    if (mark.start > cursor) tokens.push({ kind: 'text', value: sample.slice(cursor, mark.start) });
    tokens.push({ kind: 'parameter', parameterId: mark.parameterId });
    cursor = mark.end;
  });
  if (cursor < sample.length) tokens.push({ kind: 'text', value: sample.slice(cursor) });
  if (tokens.length === 0 && sample) tokens.push({ kind: 'text', value: sample });
  return tokens;
}

export function renderPatternPreview(
  sample: string,
  marks: readonly ParameterMark[],
  parameters: readonly DisplayRuleParameter[],
): Array<{ kind: 'text' | 'parameter'; value: string; label?: string; key: string }> {
  const parameterById = new Map(parameters.map((parameter) => [parameter.id, parameter]));
  return buildPatternTokens(sample, marks).map((token, index) => (
    token.kind === 'text'
      ? { kind: 'text', value: token.value, key: `text-${index}` }
      : {
        kind: 'parameter',
        value: parameterById.get(token.parameterId)?.sampleValue ?? '',
        label: parameterById.get(token.parameterId)?.label ?? token.parameterId,
        key: `parameter-${index}-${token.parameterId}`,
      }
  ));
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

interface CompiledTemplatePattern {
  expression: RegExp;
  captureIndexByParameterId: Map<string, number>;
}

const compiledRuleCache = new WeakMap<DisplayRule, CompiledTemplatePattern | null>();

function compileTemplatePattern(tokens: readonly DisplayPatternToken[]): CompiledTemplatePattern | undefined {
  if (tokens.length === 0) return undefined;
  const captureIndexByParameterId = new Map<string, number>();
  let captureCount = 0;
  // 高级模板描述的是一整条日志正文。必须锚定首尾，否则位于句尾的
  // 非贪婪参数会只捕获第一个字符，例如 0x32f93 被截断为 0。
  let source = '^';

  tokens.forEach((token) => {
    if (token.kind === 'text') {
      source += compileFixedText(token.value);
      return;
    }

    const existingIndex = captureIndexByParameterId.get(token.parameterId);
    if (existingIndex !== undefined) {
      source += `\\${existingIndex}`;
      return;
    }

    captureCount += 1;
    captureIndexByParameterId.set(token.parameterId, captureCount);
    // 使用可跨任意字符的非贪婪捕获；后续固定文本或结尾锚点负责确定参数边界。
    source += '([\\s\\S]+?)';
  });
  source += '$';

  try {
    return { expression: new RegExp(source), captureIndexByParameterId };
  } catch {
    return undefined;
  }
}

function applyDisplayTemplate(
  template: string,
  parameters: readonly DisplayRuleParameter[],
  valuesById: Readonly<Record<string, string>>,
): string {
  const idByLabel = new Map(parameters.map((parameter) => [parameter.label, parameter.id]));
  return template.replace(/\{([^{}]+)}/g, (placeholder, label: string) => {
    const id = idByLabel.get(label.trim());
    return id ? valuesById[id] ?? placeholder : placeholder;
  });
}

/**
 * 函数名比较用的归一化。
 *
 * 解析器把函数名统一成 `MoveAbsolute()` 这种带括号的形式，而日志正文里几乎从来不这么写：
 * 常见的是 `[MoveAbsolute] >() enter …`（名字在方括号里、边界符单独一个 `>()`）
 * 或者 `MoveAbsolute >() enter …`。所以比较前把两边的 `()` 都去掉 ——
 * 「函数名包含关键字」写 `MoveAbsolute()` 和写 `MoveAbsolute` 必须等价。
 */
export function normalizeFunctionName(value: string): string {
  return String(value || '').trim().replace(/\(\s*\)$/, '').trim();
}

/** 函数名是否命中关键字（把 `()` 归一化掉，其余仍区分大小写）。 */
export function functionNameMatchesKeyword(functionName: string, keyword: string): boolean {
  const name = normalizeFunctionName(functionName);
  const needle = normalizeFunctionName(keyword);
  if (!name || !needle) return false;
  return name === needle || name.includes(needle);
}

/**
 * 从一段文本（真实日志行、或者用户粘进「测试日志」的正文）里找出可能的函数名。
 *
 * 覆盖常见写法：
 * - `FuncName()`：正文以函数名开头的老格式；
 * - `[FuncName] [START] / [FuncName] >()`：执行器日志里函数标签 + 边界符；
 * - `FuncName >()`：只有裸名字 + 边界符；
 * - 行首的 `[FuncName]`：执行器日志的函数标签。
 */
export function collectFunctionNameCandidates(text: string): string[] {
  const candidates = new Set<string>();
  const push = (value: string | undefined) => {
    const name = String(value || '').trim();
    if (name) candidates.add(name);
  };
  const source = String(text || '');
  for (const match of source.matchAll(/([A-Za-z_~][\w:<>~.\-]*)\(\s*\)/g)) push(match[1]);
  for (const match of source.matchAll(/\[([A-Za-z_~][\w:<>~.\-]*)\]\s*(?:\[?\s*(?:START|END)|[<>]\s*\(\s*\))/gi)) push(match[1]);
  for (const match of source.matchAll(/([A-Za-z_~][\w:<>~.\-]*)\s*[<>]\s*\(\s*\)/g)) push(match[1]);
  const leading = source.match(/^\s*\[([A-Za-z_~][\w:<>~.\-]*)\]/);
  if (leading) push(leading[1]);
  return [...candidates];
}

export function matchDisplayRuleToMessage(rule: DisplayRule, rawOrMessage: string): DisplayRuleMatch | undefined {
  if (!rule.enabled) return undefined;
  const displayMode: DisplayRuleMode = rule.displayMode ?? 'semantic';
  const message = extractDisplayRuleMessage(rawOrMessage);

  if (rule.kind === 'keyword') {
    const keyword = rule.keyword?.trim();
    if (!keyword) return undefined;
    // 函数名关键字（`MoveAbsolute()`）也要能命中把函数名写成 `[MoveAbsolute]` 的正文：
    // 解析器给函数名补的 `()` 在正文里本来就不存在，用户没必要为此改写关键字。
    const withoutParens = normalizeFunctionName(keyword);
    const matched = message.includes(keyword)
      || (keyword !== withoutParens && withoutParens.length >= 3 && message.includes(withoutParens));
    if (!matched) return undefined;
    return {
      ruleId: rule.id,
      ruleName: rule.name,
      text: rule.displayTemplate,
      customLabelText: displayMode !== 'semantic' && rule.customLabelTemplate?.trim() ? rule.customLabelTemplate.trim() : undefined,
      customLabelColor: displayMode !== 'semantic' ? (rule.customLabelColor || '#2563eb') : undefined,
      showLabelOnTimeline: displayMode !== 'semantic' ? rule.showLabelOnTimeline !== false : undefined,
      supplementalText: rule.supplementalDescription?.trim() || undefined,
      parameters: {},
      sourceMessage: message,
    };
  }

  let compiled = compiledRuleCache.get(rule);
  if (compiled === undefined) {
    compiled = compileTemplatePattern(rule.patternTokens ?? []) ?? null;
    compiledRuleCache.set(rule, compiled);
  }
  if (!compiled) return undefined;
  const match = compiled.expression.exec(message);
  if (!match) return undefined;

  const valuesById: Record<string, string> = {};
  compiled.captureIndexByParameterId.forEach((captureIndex, parameterId) => {
    valuesById[parameterId] = match[captureIndex] ?? '';
  });
  const valuesByLabel: Record<string, string> = {};
  rule.parameters.forEach((parameter) => {
    valuesByLabel[parameter.label] = valuesById[parameter.id] ?? '';
  });

  return {
    ruleId: rule.id,
    ruleName: rule.name,
    text: applyDisplayTemplate(rule.displayTemplate, rule.parameters, valuesById),
    customLabelText: displayMode !== 'semantic' && rule.customLabelTemplate?.trim()
      ? applyDisplayTemplate(rule.customLabelTemplate, rule.parameters, valuesById)
      : undefined,
    customLabelColor: displayMode !== 'semantic' ? (rule.customLabelColor || '#2563eb') : undefined,
    showLabelOnTimeline: displayMode !== 'semantic' ? rule.showLabelOnTimeline !== false : undefined,
    supplementalText: rule.supplementalDescription?.trim()
      ? applyDisplayTemplate(rule.supplementalDescription, rule.parameters, valuesById)
      : undefined,
    parameters: valuesByLabel,
    sourceMessage: message,
  };
}

export function matchDisplayRulesToEntry(
  rules: readonly DisplayRule[],
  entry: LogEntry,
): DisplayRuleMatch | undefined {
  for (const rule of rules) {
    if (rule.scope === 'function' || (rule.displayMode ?? 'semantic') === 'label') continue;
    const match = matchDisplayRuleToMessage(rule, entry.message);
    if (match && match.text.trim()) return { ...match, sourceEntryId: entry.id };
  }
  return undefined;
}


export function matchDisplayLabelRulesToEntry(
  rules: readonly DisplayRule[],
  entry: LogEntry,
): DisplayRuleMatch[] {
  const matches: DisplayRuleMatch[] = [];
  for (const rule of rules) {
    if (rule.scope === 'function' || (rule.displayMode ?? 'semantic') === 'semantic') continue;
    const match = matchDisplayRuleToMessage(rule, entry.message);
    if (match?.customLabelText?.trim()) matches.push({ ...match, sourceEntryId: entry.id });
  }
  return matches;
}

function collectNodeEntries(items: readonly TimelineItem[], result: LogEntry[]): void {
  items.forEach((item) => {
    if (item.kind === 'log') result.push(item.entry);
    else collectNodeEntries(item.children, result);
  });
}

export function functionNodeEntries(node: FunctionNode): LogEntry[] {
  const result: LogEntry[] = [];
  collectNodeEntries(node.children, result);
  if (!result.some((entry) => entry.id === node.startEntry.id)) result.unshift(node.startEntry);
  return result;
}

export function matchDisplayRulesToFunction(
  rules: readonly DisplayRule[],
  node: FunctionNode,
): DisplayRuleMatch | undefined {
  let entries: LogEntry[] | undefined;
  for (const rule of rules) {
    if (!rule.enabled || rule.scope === 'log' || (rule.displayMode ?? 'semantic') === 'label') continue;
    if (rule.kind === 'keyword') {
      const keyword = rule.keyword?.trim();
      if (keyword && functionNameMatchesKeyword(node.name, keyword)) {
        return {
          ruleId: rule.id,
          ruleName: rule.name,
          text: rule.displayTemplate,
          supplementalText: rule.supplementalDescription?.trim() || undefined,
          parameters: {},
          sourceMessage: node.name,
          sourceEntryId: node.startEntry.id,
        };
      }
    }

    entries ??= functionNodeEntries(node);
    for (const entry of entries) {
      const match = matchDisplayRuleToMessage(rule, entry.message);
      if (match) return { ...match, sourceEntryId: entry.id };
    }
  }
  return undefined;
}

export function validateDisplayRule(rule: DisplayRule): string | undefined {
  if (!rule.name.trim()) return '请填写规则名称';
  const displayMode: DisplayRuleMode = rule.displayMode ?? 'semantic';
  if (displayMode !== 'label' && !rule.displayTemplate.trim()) return '请填写语义说明';
  if (displayMode !== 'semantic' && !rule.customLabelTemplate?.trim()) return '请填写自定义标签文本';
  if (displayMode !== 'semantic' && rule.scope === 'function') return '自定义标签显示在日志行，请将应用位置改为“仅日志正文”或“函数标题 + 日志正文”';
  if (displayMode !== 'semantic' && rule.customLabelColor && !/^#[0-9a-fA-F]{6}$/.test(rule.customLabelColor)) return '标签颜色格式无效';
  if (rule.kind === 'keyword' && !rule.keyword?.trim()) return '请填写匹配关键字';
  if (rule.kind === 'template') {
    if (!rule.sampleMessage?.trim()) return '请粘贴并识别一条样例日志';
    if (!rule.patternTokens?.length) return '尚未生成语句匹配模板';
    if (rule.parameters.length === 0) return '请至少划选一个变量参数';
    const labels = rule.parameters.map((parameter) => parameter.label.trim());
    if (labels.some((label) => !label)) return '参数名称不能为空';
    if (labels.some((label) => /[{}]/.test(label))) return '参数名称不能包含大括号';
    if (new Set(labels).size !== labels.length) return '参数名称不能重复';
    const placeholders = Array.from(rule.displayTemplate.matchAll(/\{([^{}]+)}/g), (match) => match[1].trim());
    const unknownPlaceholder = placeholders.find((placeholder) => !labels.includes(placeholder));
    if (unknownPlaceholder) return `语义说明中的 {${unknownPlaceholder}} 没有对应参数`;
    const labelPlaceholders = Array.from((rule.customLabelTemplate ?? '').matchAll(/\{([^{}]+)}/g), (match) => match[1].trim());
    const unknownLabelPlaceholder = labelPlaceholders.find((placeholder) => !labels.includes(placeholder));
    if (unknownLabelPlaceholder) return `自定义标签中的 {${unknownLabelPlaceholder}} 没有对应参数`;
    const supplementalPlaceholders = Array.from((rule.supplementalDescription ?? '').matchAll(/\{([^{}]+)\}/g), (match) => match[1].trim());
    const unknownSupplementalPlaceholder = supplementalPlaceholders.find((placeholder) => !labels.includes(placeholder));
    if (unknownSupplementalPlaceholder) return `补充说明中的 {${unknownSupplementalPlaceholder}} 没有对应参数`;
    // 匹配参数与展示参数是两个独立维度：规则可以提取多个参数用于稳定匹配，
    // 但展示说明只需要引用业务上真正关心的部分参数，甚至可以完全使用固定说明。
    const adjacentParameters = rule.patternTokens.some((token, index, tokens) => (
      token.kind === 'parameter' && tokens[index + 1]?.kind === 'parameter'
    ));
    if (adjacentParameters) return '两个参数之间必须保留至少一个固定字符，才能稳定提取';
  }
  return undefined;
}
