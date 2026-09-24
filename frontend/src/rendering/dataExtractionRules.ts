import type { LogEntry } from '../types';

export type DataValueType = 'number' | 'integer' | 'boolean' | 'string';
export type DataOutputFormat = 'table' | 'text';
export type DataDurationUnit = string;
export type DataSourceUnit = string;
export type DataPlotUnit = string;

export interface DataUnitConversionRule {
  id: string;
  leftValue: number;
  leftUnit: string;
  rightValue: number;
  rightUnit: string;
}

export interface DataExtractionField {
  id: string;
  key: string;
  name: string;
  sampleValue: string;
  valueType: DataValueType;
  /** 数值字段的日志输入单位；auto 表示优先读取数值后的单位后缀，无后缀时按无单位处理。 */
  sourceUnit?: DataSourceUnit;
  /** 绘图/统计的目标单位；source 表示保持输入尺度。单位名称由用户自定义。 */
  plotUnit?: DataPlotUnit;
  /** 是否启用该字段的单位转换；关闭时保留配置但绘图/统计使用原始数值尺度。 */
  unitConversionEnabled?: boolean;
  /** 用户自定义单位等价关系，例如 1000 us = 1 ms。支持多条规则自动链式换算。 */
  unitConversions?: DataUnitConversionRule[];
  /** 结构化字典字段路径。存在时按字典 key 路径提取，不依赖样例 value 的文本位置。 */
  structuredPath?: string[];
  /** 同一行出现多个字典时用于优先定位所属字典的轻量提示，例如 result / pose。 */
  structuredRootHint?: string;
  /** 参数名、参数值在样例日志中的位置。用于构建不依赖具体分隔符的语义模板。 */
  keyStart?: number;
  keyEnd?: number;
  valueStart?: number;
  valueEnd?: number;
}

export interface DataExtractionRule {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  matchKeyword: string;
  caseSensitive: boolean;
  sourceCategories: string[];
  subsystems: string[];
  modules: string[];
  fields: DataExtractionField[];
  outputFormat: DataOutputFormat;
  sampleMessage: string;
  /**
   * 实时采集 — whether this extractor is one of the collectors live monitoring feeds.
   *
   * Explicit opt-in per extractor rather than "capture everything that is enabled": the
   * extractor answers "what does this number mean", which is not the same question as
   * "which numbers do I want streaming right now".
   */
  liveCapture?: boolean;
  createdAt: number;
  updatedAt: number;
}

export interface ExtractedDataRow {
  timestamp: string;
  sourceFile: string;
  lineNumber: number;
  subsystem?: string;
  module?: string;
  processId?: string;
  threadId?: string;
  traceId?: string;
  values: Record<string, string | number | boolean>;
  /** 数值字段用于绘图/统计的标准化值；原始提取文本仍保存在 values。 */
  normalizedValues?: Record<string, number>;
  /** normalizedValues 对应的单位。 */
  normalizedUnits?: Record<string, string>;
}

const DATA_EXTRACTION_RULE_STORAGE_KEY = 'tracelens.data-extraction-rules.v1';

export function createDataExtractionRuleId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return `data-extractor-${crypto.randomUUID()}`;
  return `data-extractor-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function createEmptyDataExtractionRule(sampleMessage = ''): DataExtractionRule {
  const now = Date.now();
  return {
    id: createDataExtractionRuleId(),
    name: '',
    description: '',
    enabled: true,
    matchKeyword: '',
    caseSensitive: false,
    sourceCategories: [],
    subsystems: [],
    modules: [],
    fields: [],
    outputFormat: 'table',
    sampleMessage,
    liveCapture: false,
    createdAt: now,
    updatedAt: now,
  };
}

function optionalPosition(value: unknown): number | undefined {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= 0 ? parsed : undefined;
}

export function loadDataExtractionRules(): DataExtractionRule[] {
  if (typeof window === 'undefined') return [];
  try {
    const value = JSON.parse(window.localStorage.getItem(DATA_EXTRACTION_RULE_STORAGE_KEY) || '[]');
    if (!Array.isArray(value)) return [];
    return value.flatMap((item): DataExtractionRule[] => {
      if (!item || typeof item !== 'object' || !String(item.id || '').trim()) return [];
      return [{
        id: String(item.id),
        name: String(item.name || ''),
        description: String(item.description || ''),
        enabled: item.enabled !== false,
        matchKeyword: String(item.matchKeyword || ''),
        caseSensitive: Boolean(item.caseSensitive),
        sourceCategories: Array.isArray(item.sourceCategories) ? item.sourceCategories.map(String).filter(Boolean) : [],
        subsystems: Array.isArray(item.subsystems) ? item.subsystems.map(String).filter(Boolean) : [],
        modules: Array.isArray(item.modules) ? item.modules.map(String).filter(Boolean) : [],
        fields: Array.isArray(item.fields) ? item.fields.flatMap((field: unknown): DataExtractionField[] => {
          if (!field || typeof field !== 'object') return [];
          const source = field as Partial<DataExtractionField>;
          const key = String(source.key || '').trim();
          if (!key) return [];
          const inferredValueType = inferDataValueType(String(source.sampleValue || ''));
          const storedValueType = ['number', 'integer', 'boolean', 'string'].includes(String(source.valueType))
            ? source.valueType as DataValueType
            : inferredValueType;
          // 兼容旧版：带千分位/时间单位的值曾被误判为 string，加载时自动升级为数值类型。
          const valueType = storedValueType === 'string' && inferredValueType !== 'string' ? inferredValueType : storedValueType;
          return [{
            id: String(source.id || `field-${Date.now()}-${Math.random().toString(16).slice(2)}`),
            key,
            name: String(source.name || key),
            sampleValue: String(source.sampleValue || ''),
            valueType,
            sourceUnit: String(source.sourceUnit || 'auto').trim() || 'auto',
            plotUnit: String(source.plotUnit || 'source').trim() || 'source',
            // 兼容 v274 及更早版本：只要已配置目标单位/换算规则，就视为原先已启用。
            unitConversionEnabled: typeof source.unitConversionEnabled === 'boolean'
              ? source.unitConversionEnabled
              : (String(source.plotUnit || 'source').trim() !== 'source' || (Array.isArray(source.unitConversions) && source.unitConversions.length > 0)),
            unitConversions: Array.isArray(source.unitConversions) ? source.unitConversions.flatMap((item: unknown): DataUnitConversionRule[] => {
              if (!item || typeof item !== 'object') return [];
              const conversion = item as Partial<DataUnitConversionRule>;
              const leftValue = Number(conversion.leftValue);
              const rightValue = Number(conversion.rightValue);
              const leftUnit = String(conversion.leftUnit || '').trim();
              const rightUnit = String(conversion.rightUnit || '').trim();
              if (!(leftValue > 0) || !(rightValue > 0) || !leftUnit || !rightUnit) return [];
              return [{ id: String(conversion.id || `unit-${Date.now()}-${Math.random().toString(16).slice(2)}`), leftValue, leftUnit, rightValue, rightUnit }];
            }) : [],
            structuredPath: Array.isArray(source.structuredPath) ? source.structuredPath.map(String).map((item) => item.trim()).filter(Boolean) : undefined,
            structuredRootHint: String(source.structuredRootHint || '').trim() || undefined,
            keyStart: optionalPosition(source.keyStart),
            keyEnd: optionalPosition(source.keyEnd),
            valueStart: optionalPosition(source.valueStart),
            valueEnd: optionalPosition(source.valueEnd),
          }];
        }) : [],
        outputFormat: item.outputFormat === 'text' ? 'text' : 'table',
        sampleMessage: String(item.sampleMessage || ''),
        liveCapture: item.liveCapture === true,
        createdAt: Number(item.createdAt || Date.now()),
        updatedAt: Number(item.updatedAt || item.createdAt || Date.now()),
      }];
    });
  } catch {
    return [];
  }
}

export function saveDataExtractionRules(rules: readonly DataExtractionRule[]): void {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(DATA_EXTRACTION_RULE_STORAGE_KEY, JSON.stringify(rules));
}

const NUMERIC_CORE_PATTERN = '[-+]?(?:(?:\\d{1,3}(?:,\\d{3})+|\\d+)(?:\\.\\d*)?|\\.\\d+)(?:[eE][-+]?\\d+)?';
// 单位后缀不再写死为时间单位；允许英文、中文及常见工程单位符号。
const GENERIC_UNIT_PATTERN = '[A-Za-z\\u4e00-\\u9fffµμ°%℃℉Ω][A-Za-z0-9\\u4e00-\\u9fffµμ°%℃℉Ω/_^.\\-]*';

interface ParsedNumericToken {
  raw: string;
  number: number;
  unit?: string;
  decorated: boolean;
}

function stripQuotedValue(text: string): string {
  let value = text.trim();
  if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) value = value.slice(1, -1).trim();
  return value;
}

function normalizeUnitName(value: string | undefined): string | undefined {
  const unit = String(value || '').trim();
  if (!unit) return undefined;
  if (unit === 'µs' || unit === 'μs') return 'us';
  return unit;
}

export function parseNumericToken(text: string): ParsedNumericToken | undefined {
  const value = stripQuotedValue(text);
  const match = value.match(new RegExp(`^(${NUMERIC_CORE_PATTERN})\\s*(${GENERIC_UNIT_PATTERN})?$`, 'i'));
  if (!match) return undefined;
  const parsed = Number(match[1].replace(/,/g, ''));
  if (!Number.isFinite(parsed)) return undefined;
  return {
    raw: value,
    number: parsed,
    unit: normalizeUnitName(match[2]),
    decorated: Boolean(match[2] || match[1].includes(',')),
  };
}

export function inferDataSourceUnit(value: string): DataSourceUnit {
  return parseNumericToken(value)?.unit || 'auto';
}

export function inferDataPlotUnit(_value: string): DataPlotUnit {
  // v274: 单位转换必须由用户明确配置，默认保持日志原始尺度。
  return 'source';
}



export interface StructuredDataCandidate {
  id: string;
  path: string[];
  displayPath: string;
  rootHint?: string;
  sampleValue: string;
  valueType: DataValueType;
  sourceUnit: DataSourceUnit;
}

interface StructuredObjectBlock {
  rootHint?: string;
  value: unknown;
}

function structuredPathKey(path: readonly string[]): string {
  return path.map((item) => item.replace(/\\/g, '\\\\').replace(/\./g, '\\.')).join('.');
}

function findBalancedObjectBlocks(message: string): Array<{ start: number; end: number; text: string }> {
  const blocks: Array<{ start: number; end: number; text: string }> = [];
  let depth = 0;
  let start = -1;
  let quote = '';
  let escaped = false;
  for (let index = 0; index < message.length; index += 1) {
    const char = message[index];
    if (quote) {
      if (escaped) { escaped = false; continue; }
      if (char === '\\') { escaped = true; continue; }
      if (char === quote) quote = '';
      continue;
    }
    if (char === '"' || char === "'") { quote = char; continue; }
    if (char === '{') {
      if (depth === 0) start = index;
      depth += 1;
      continue;
    }
    if (char === '}' && depth > 0) {
      depth -= 1;
      if (depth === 0 && start >= 0) {
        blocks.push({ start, end: index + 1, text: message.slice(start, index + 1) });
        start = -1;
      }
    }
  }
  return blocks;
}

class PythonLikeLiteralParser {
  private index = 0;
  private readonly source: string;
  constructor(source: string) { this.source = source; }

  parse(): unknown {
    const value = this.parseValue();
    this.skipWhitespace();
    if (this.index < this.source.length) throw new Error('trailing structured literal');
    return value;
  }

  private skipWhitespace() { while (/\s/.test(this.source[this.index] || '')) this.index += 1; }
  private peek() { this.skipWhitespace(); return this.source[this.index] || ''; }
  private consume(expected: string) { this.skipWhitespace(); if (this.source[this.index] !== expected) throw new Error(`expected ${expected}`); this.index += 1; }

  private parseValue(): unknown {
    this.skipWhitespace();
    const char = this.source[this.index];
    if (char === '{') return this.parseObject();
    if (char === '[') return this.parseArray(']');
    if (char === '(') return this.parseArray(')');
    if (char === '"' || char === "'") return this.parseString();
    if ((char === 'u' || char === 'r' || char === 'b') && (this.source[this.index + 1] === '"' || this.source[this.index + 1] === "'")) {
      this.index += 1;
      return this.parseString();
    }
    const rest = this.source.slice(this.index);
    const numberMatch = rest.match(/^[-+]?(?:(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)/);
    if (numberMatch) { this.index += numberMatch[0].length; return Number(numberMatch[0]); }
    return this.parseBareToken();
  }

  private parseString(): string {
    const quote = this.source[this.index++];
    let result = '';
    let escaped = false;
    while (this.index < this.source.length) {
      const char = this.source[this.index++];
      if (escaped) {
        const replacements: Record<string, string> = { n: '\n', r: '\r', t: '\t' };
        result += replacements[char] ?? char;
        escaped = false;
        continue;
      }
      if (char === '\\') { escaped = true; continue; }
      if (char === quote) return result;
      result += char;
    }
    throw new Error('unterminated string');
  }

  private parseBareToken(): unknown {
    this.skipWhitespace();
    const start = this.index;
    while (this.index < this.source.length && !/[,:}\]\)\s]/.test(this.source[this.index])) this.index += 1;
    const token = this.source.slice(start, this.index).trim();
    if (!token) throw new Error('empty token');
    if (/^(?:true|True)$/i.test(token)) return true;
    if (/^(?:false|False)$/i.test(token)) return false;
    if (/^(?:none|null)$/i.test(token)) return null;
    return token;
  }

  private parseObject(): Record<string, unknown> {
    const result: Record<string, unknown> = {};
    this.consume('{');
    if (this.peek() === '}') { this.index += 1; return result; }
    while (this.index < this.source.length) {
      this.skipWhitespace();
      let key: string;
      const char = this.source[this.index];
      if (char === '"' || char === "'") key = this.parseString();
      else {
        const start = this.index;
        while (this.index < this.source.length && this.source[this.index] !== ':') this.index += 1;
        key = this.source.slice(start, this.index).trim();
      }
      if (!key) throw new Error('empty object key');
      this.consume(':');
      result[key] = this.parseValue();
      this.skipWhitespace();
      const next = this.source[this.index];
      if (next === ',') { this.index += 1; if (this.peek() === '}') { this.index += 1; break; } continue; }
      if (next === '}') { this.index += 1; break; }
      throw new Error('invalid object separator');
    }
    return result;
  }

  private parseArray(close: ']' | ')'): unknown[] {
    const open = close === ']' ? '[' : '(';
    const result: unknown[] = [];
    this.consume(open);
    if (this.peek() === close) { this.index += 1; return result; }
    while (this.index < this.source.length) {
      result.push(this.parseValue());
      this.skipWhitespace();
      const next = this.source[this.index];
      if (next === ',') { this.index += 1; if (this.peek() === close) { this.index += 1; break; } continue; }
      if (next === close) { this.index += 1; break; }
      throw new Error('invalid array separator');
    }
    return result;
  }
}

function parseStructuredLiteral(text: string): unknown {
  try { return JSON.parse(text); } catch {
    try { return new PythonLikeLiteralParser(text).parse(); } catch { return undefined; }
  }
}

function inferStructuredRootHint(message: string, start: number): string | undefined {
  const prefix = message.slice(Math.max(0, start - 96), start);
  const match = prefix.match(/([A-Za-z_][A-Za-z0-9_.\-]{0,63})\s*(?:=|:|->|=>)\s*$/);
  return match?.[1]?.trim() || undefined;
}

function parseStructuredObjects(message: string): StructuredObjectBlock[] {
  return findBalancedObjectBlocks(message).flatMap((block): StructuredObjectBlock[] => {
    const parsed = parseStructuredLiteral(block.text);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return [];
    return [{ rootHint: inferStructuredRootHint(message, block.start), value: parsed }];
  });
}

function primitiveSampleValue(value: unknown): string | undefined {
  if (value === null) return 'null';
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (Array.isArray(value)) return JSON.stringify(value);
  return undefined;
}

function flattenStructuredObject(value: unknown, prefix: string[] = []): Array<{ path: string[]; value: unknown }> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return prefix.length ? [{ path: prefix, value }] : [];
  const result: Array<{ path: string[]; value: unknown }> = [];
  Object.entries(value as Record<string, unknown>).forEach(([key, child]) => {
    const path = [...prefix, key];
    if (child && typeof child === 'object' && !Array.isArray(child)) result.push(...flattenStructuredObject(child, path));
    else result.push({ path, value: child });
  });
  return result;
}

export function detectStructuredDataCandidates(message: string): StructuredDataCandidate[] {
  const blocks = parseStructuredObjects(message);
  const candidates: StructuredDataCandidate[] = [];
  const seen = new Set<string>();
  blocks.forEach((block, blockIndex) => {
    flattenStructuredObject(block.value).forEach(({ path, value }) => {
      const sampleValue = primitiveSampleValue(value);
      if (sampleValue === undefined) return;
      const pathKey = structuredPathKey(path);
      const identity = `${block.rootHint || `#${blockIndex}`}::${pathKey}`;
      if (seen.has(identity)) return;
      seen.add(identity);
      candidates.push({
        id: identity,
        path,
        displayPath: block.rootHint ? `${block.rootHint}.${path.join('.')}` : path.join('.'),
        rootHint: block.rootHint,
        sampleValue,
        valueType: inferDataValueType(sampleValue),
        sourceUnit: inferDataSourceUnit(sampleValue),
      });
    });
  });
  return candidates;
}

function structuredValueAtPath(value: unknown, path: readonly string[]): unknown {
  let current = value;
  for (const segment of path) {
    if (!current || typeof current !== 'object' || Array.isArray(current) || !(segment in current)) return undefined;
    current = (current as Record<string, unknown>)[segment];
  }
  return current;
}

function extractStructuredField(message: string, field: DataExtractionField): string | number | boolean | undefined {
  const path = field.structuredPath || [];
  if (!path.length) return undefined;
  const blocks = parseStructuredObjects(message);
  const preferred = field.structuredRootHint ? blocks.filter((block) => block.rootHint === field.structuredRootHint) : blocks;
  const ordered = preferred.length ? [...preferred, ...blocks.filter((block) => !preferred.includes(block))] : blocks;
  for (const block of ordered) {
    const raw = structuredValueAtPath(block.value, path);
    if (raw === undefined) continue;
    if (raw === null) return 'null';
    if (typeof raw === 'string') return parseValue(raw, field.valueType) ?? raw;
    if (typeof raw === 'number') return field.valueType === 'string' ? String(raw) : raw;
    if (typeof raw === 'boolean') return field.valueType === 'string' ? String(raw) : raw;
    if (Array.isArray(raw)) return JSON.stringify(raw);
  }
  return undefined;
}

export function inferDataValueType(value: string): DataValueType {
  const text = value.trim();
  const numeric = parseNumericToken(text);
  if (numeric) {
    if (!numeric.unit && !/[.eE]/.test(numeric.raw)) return 'integer';
    return 'number';
  }
  if (/^(?:true|false)$/i.test(text)) return 'boolean';
  return 'string';
}

export function isDataUnitConversionEnabled(field: DataExtractionField): boolean {
  if (typeof field.unitConversionEnabled === 'boolean') return field.unitConversionEnabled;
  // 兼容 v274：旧规则没有显式开关，只要配置过目标单位或换算关系就视为已启用。
  return String(field.plotUnit || 'source').trim() !== 'source' || (field.unitConversions || []).length > 0;
}

function conversionFactor(sourceUnit: string, targetUnit: string, rules: readonly DataUnitConversionRule[]): number | undefined {
  const source = normalizeUnitName(sourceUnit);
  const target = normalizeUnitName(targetUnit);
  if (!source || !target) return undefined;
  if (source === target) return 1;
  const graph = new Map<string, Array<{ unit: string; factor: number }>>();
  const addEdge = (from: string, to: string, factor: number) => {
    const current = graph.get(from) || [];
    current.push({ unit: to, factor });
    graph.set(from, current);
  };
  for (const rule of rules) {
    const leftUnit = normalizeUnitName(rule.leftUnit);
    const rightUnit = normalizeUnitName(rule.rightUnit);
    const leftValue = Number(rule.leftValue);
    const rightValue = Number(rule.rightValue);
    if (!leftUnit || !rightUnit || !(leftValue > 0) || !(rightValue > 0)) continue;
    addEdge(leftUnit, rightUnit, rightValue / leftValue);
    addEdge(rightUnit, leftUnit, leftValue / rightValue);
  }
  const queue: Array<{ unit: string; factor: number }> = [{ unit: source, factor: 1 }];
  const visited = new Set<string>([source]);
  while (queue.length) {
    const current = queue.shift()!;
    for (const edge of graph.get(current.unit) || []) {
      if (visited.has(edge.unit)) continue;
      const factor = current.factor * edge.factor;
      if (edge.unit === target) return factor;
      visited.add(edge.unit);
      queue.push({ unit: edge.unit, factor });
    }
  }
  return undefined;
}

export function normalizeExtractedNumericValue(text: string | number, field: DataExtractionField): { value: number; unit: string; converted: boolean } | undefined {
  const parsed = typeof text === 'number' ? { raw: String(text), number: text, unit: undefined, decorated: false } : parseNumericToken(String(text));
  if (!parsed || !Number.isFinite(parsed.number)) return undefined;
  const configuredSource = String(field.sourceUnit || 'auto').trim() || 'auto';
  const detectedSource = parsed.unit;
  const source = normalizeUnitName(detectedSource || (configuredSource === 'auto' ? 'none' : configuredSource)) || 'none';
  // 单位转换关闭时只保留原始数值尺度；已配置的转换规则不会被删除。
  if (!isDataUnitConversionEnabled(field)) return { value: parsed.number, unit: source, converted: false };
  const target = normalizeUnitName(String(field.plotUnit || 'source').trim()) || 'source';
  if (target === 'source' || target === source) return { value: parsed.number, unit: source, converted: false };
  if (target === 'none') return { value: parsed.number, unit: 'none', converted: source !== 'none' };
  if (source === 'none') return { value: parsed.number, unit: source, converted: false };
  const factor = conversionFactor(source, target, field.unitConversions || []);
  if (factor === undefined) return { value: parsed.number, unit: source, converted: false };
  return { value: parsed.number * factor, unit: target, converted: true };
}

function parseValue(text: string, type: DataValueType): string | number | boolean | undefined {
  const value = stripQuotedValue(text);
  if (type === 'boolean') {
    if (/^true$/i.test(value)) return true;
    if (/^false$/i.test(value)) return false;
    return undefined;
  }
  if (type === 'integer' || type === 'number') {
    const numeric = parseNumericToken(value);
    if (!numeric) return undefined;
    if (type === 'integer' && !Number.isInteger(numeric.number)) return undefined;
    // 带千分位或单位时保留原始可读文本；绘图使用 normalizedValues。
    return numeric.decorated ? numeric.raw : numeric.number;
  }
  return value;
}

function valuePattern(type: DataValueType): string {
  if (type === 'number' || type === 'integer') return `${NUMERIC_CORE_PATTERN}(?:\\s*${GENERIC_UNIT_PATTERN})?`;
  if (type === 'boolean') return '(?:true|false)';
  return "(?:\"[^\"\\r\\n]*\"|'[^'\\r\\n]*'|[^,;，；\\s\\]\\[\\)\\(]+)";
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function keyPattern(value: string): string {
  const text = value.trim();
  const escaped = escapeRegExp(text);
  if (!escaped) return '';
  if (/^[A-Za-z0-9_]+$/.test(text)) return `(?<![A-Za-z0-9_])${escaped}(?![A-Za-z0-9_])`;
  return escaped;
}

interface SemanticMarker {
  start: number;
  end: number;
  kind: 'key' | 'value';
  field: DataExtractionField;
  text: string;
}

function validSpan(sample: string, start?: number, end?: number, expected?: string): boolean {
  return start !== undefined && end !== undefined && start >= 0 && end > start && end <= sample.length
    && (!expected || sample.slice(start, end).trim() === expected.trim());
}

function semanticGapPattern(sampleGap: string): string {
  // 未划选文本只是“相对位置上下文”，不是固定分隔符。允许符号、空格、短描述发生变化。
  const compactLength = sampleGap.replace(/\s+/g, ' ').trim().length;
  const maxGap = Math.max(12, Math.min(120, compactLength * 4 + 20));
  return `[^\\r\\n]{0,${maxGap}}?`;
}

function buildSemanticTemplate(rule: DataExtractionRule): { regex: RegExp; captureFields: DataExtractionField[] } | undefined {
  const sample = String(rule.sampleMessage || '');
  if (!sample || !rule.fields.length) return undefined;
  const markers: SemanticMarker[] = [];
  for (const field of rule.fields) {
    if (!validSpan(sample, field.keyStart, field.keyEnd, field.key)
      || !validSpan(sample, field.valueStart, field.valueEnd, field.sampleValue)) return undefined;
    markers.push({ start: field.keyStart!, end: field.keyEnd!, kind: 'key', field, text: field.key });
    markers.push({ start: field.valueStart!, end: field.valueEnd!, kind: 'value', field, text: field.sampleValue });
  }
  markers.sort((a, b) => a.start - b.start || a.end - b.end);
  for (let index = 1; index < markers.length; index += 1) {
    if (markers[index].start < markers[index - 1].end) return undefined;
  }

  let pattern = '';
  let cursor = markers[0].start;
  const captureFields: DataExtractionField[] = [];
  markers.forEach((marker, index) => {
    if (index > 0) pattern += semanticGapPattern(sample.slice(cursor, marker.start));
    if (marker.kind === 'key') {
      pattern += keyPattern(marker.text);
    } else {
      pattern += `(${valuePattern(marker.field.valueType)})`;
      captureFields.push(marker.field);
    }
    cursor = marker.end;
  });
  if (!pattern || captureFields.length !== rule.fields.length) return undefined;
  try {
    return { regex: new RegExp(pattern, rule.caseSensitive ? '' : 'i'), captureFields };
  } catch {
    return undefined;
  }
}

function extractBySemanticTemplate(message: string, rule: DataExtractionRule): Record<string, string | number | boolean> | undefined {
  const template = buildSemanticTemplate(rule);
  if (!template) return undefined;
  const match = template.regex.exec(message);
  if (!match) return undefined;
  const values: Record<string, string | number | boolean> = {};
  for (let index = 0; index < template.captureFields.length; index += 1) {
    const field = template.captureFields[index];
    const parsed = parseValue(match[index + 1] || '', field.valueType);
    if (parsed === undefined) return undefined;
    values[field.name || field.key] = parsed;
  }
  return values;
}

function extractField(message: string, field: DataExtractionField, caseSensitive: boolean): string | number | boolean | undefined {
  const key = keyPattern(field.key);
  if (!key) return undefined;
  // 兼容旧规则：只要求参数名在参数值附近，不依赖 =、: 等具体分隔符。
  const pattern = `${key}[^\\r\\n]{0,64}?(${valuePattern(field.valueType)})`;
  const match = message.match(new RegExp(pattern, caseSensitive ? '' : 'i'));
  if (!match) return undefined;
  return parseValue(match[1], field.valueType);
}

function inScope(entry: LogEntry, rule: DataExtractionRule): boolean {
  if (rule.sourceCategories.length && !rule.sourceCategories.includes(String(entry.logCategory || ''))) return false;
  if (rule.subsystems.length && !rule.subsystems.includes(String(entry.logSubsystem || ''))) return false;
  if (rule.modules.length && !rule.modules.includes(String(entry.logModule || ''))) return false;
  if (rule.matchKeyword.trim()) {
    const haystack = rule.caseSensitive ? entry.message : entry.message.toLowerCase();
    const needle = rule.caseSensitive ? rule.matchKeyword.trim() : rule.matchKeyword.trim().toLowerCase();
    if (!haystack.includes(needle)) return false;
  }
  return true;
}

export function extractDataValues(message: string, rule: DataExtractionRule): Record<string, string | number | boolean> | undefined {
  if (rule.fields.length === 0) return undefined;
  if (rule.matchKeyword.trim()) {
    const haystack = rule.caseSensitive ? message : message.toLowerCase();
    const needle = rule.caseSensitive ? rule.matchKeyword.trim() : rule.matchKeyword.trim().toLowerCase();
    if (!haystack.includes(needle)) return undefined;
  }

  const values: Record<string, string | number | boolean> = {};
  const structuredFields = rule.fields.filter((field) => (field.structuredPath || []).length > 0);
  for (const field of structuredFields) {
    const value = extractStructuredField(message, field);
    if (value === undefined) return undefined;
    values[field.name || field.key] = value;
  }

  const manualFields = rule.fields.filter((field) => !(field.structuredPath || []).length);
  if (!manualFields.length) return values;
  const manualRule = manualFields.length === rule.fields.length ? rule : { ...rule, fields: manualFields };
  const semanticValues = extractBySemanticTemplate(message, manualRule);
  if (semanticValues) return { ...values, ...semanticValues };

  for (const field of manualFields) {
    const value = extractField(message, field, rule.caseSensitive);
    if (value === undefined) return undefined;
    values[field.name || field.key] = value;
  }
  return values;
}

export function extractDataRow(entry: LogEntry, rule: DataExtractionRule): ExtractedDataRow | undefined {
  if (!rule.enabled || rule.fields.length === 0 || !inScope(entry, rule)) return undefined;
  const values = extractDataValues(entry.message, rule);
  if (!values) return undefined;
  const normalizedValues: Record<string, number> = {};
  const normalizedUnits: Record<string, string> = {};
  for (const field of rule.fields) {
    if (field.valueType !== 'number' && field.valueType !== 'integer') continue;
    const name = field.name || field.key;
    const raw = values[name];
    if (typeof raw !== 'string' && typeof raw !== 'number') continue;
    const normalized = normalizeExtractedNumericValue(raw, field);
    if (!normalized) continue;
    normalizedValues[name] = normalized.value;
    normalizedUnits[name] = normalized.unit;
  }
  return {
    timestamp: entry.timestamp,
    sourceFile: entry.remoteSourcePath || entry.sourceFile || entry.source.fileName,
    lineNumber: entry.lineNumber,
    subsystem: entry.logSubsystem,
    module: entry.logModule,
    processId: entry.processId,
    threadId: entry.threadId,
    traceId: entry.rpc?.traceId,
    values,
    normalizedValues: Object.keys(normalizedValues).length ? normalizedValues : undefined,
    normalizedUnits: Object.keys(normalizedUnits).length ? normalizedUnits : undefined,
  };
}

export function validateDataExtractionRule(rule: DataExtractionRule): string | undefined {
  if (!rule.name.trim()) return '请填写数据提取器名称。';
  if (!rule.fields.length) return '请至少配置一个参数名 / 参数值字段。';
  const names = new Set<string>();
  for (const field of rule.fields) {
    if (!field.key.trim() || !field.name.trim()) return '参数名和保存字段名不能为空。';
    if (names.has(field.name.trim())) return `保存字段名重复：${field.name.trim()}`;
    names.add(field.name.trim());
    if (isDataUnitConversionEnabled(field)) {
      for (const [index, conversion] of (field.unitConversions || []).entries()) {
        if (!(Number(conversion.leftValue) > 0) || !(Number(conversion.rightValue) > 0) || !String(conversion.leftUnit || '').trim() || !String(conversion.rightUnit || '').trim()) {
          return `${field.name || field.key} 的单位转换规则 ${index + 1} 不完整。`;
        }
      }
    }
  }
  return undefined;
}
