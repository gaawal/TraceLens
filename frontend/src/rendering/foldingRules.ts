import type { FlowMarker, LogEntry } from '../types';

export type FoldingRuleKind =
  | 'builtin-boundary'
  | 'builtin-consecutive'
  | 'builtin-trailing'
  | 'builtin-repeated'
  | 'custom-boundary';

export interface FoldingRule {
  id: string;
  name: string;
  kind: FoldingRuleKind;
  enabled: boolean;
  builtIn: boolean;
  description: string;
  startKeyword?: string;
  endKeyword?: string;
  caseSensitive?: boolean;
  createdAt?: number;
}

export interface FoldingPolicy {
  boundary: boolean;
  consecutive: boolean;
  trailing: boolean;
  repeated: boolean;
  customBoundaries: FoldingRule[];
}

export interface FoldingBoundaryMatch {
  marker: Exclude<FlowMarker, 'none'>;
  functionName: string;
  ruleId: string;
  ruleName: string;
}

const STORAGE_KEY = 'tracelens.folding-rules.v1';

export const DEFAULT_FOLDING_RULES: readonly FoldingRule[] = [
  {
    id: 'builtin-explicit-boundary',
    name: '显式函数入口/出口',
    kind: 'builtin-boundary',
    enabled: true,
    builtIn: true,
    description: '使用 > () 作为入口、< () 作为出口，并按函数名配对折叠。',
    startKeyword: '> ()',
    endKeyword: '< ()',
  },
  {
    id: 'builtin-consecutive-function',
    name: '连续同名函数日志',
    kind: 'builtin-consecutive',
    enabled: true,
    builtIn: true,
    description: '没有显式入口/出口时，将同一执行泳道中连续出现的同名 FunctionName() 日志折叠为一组。',
  },
  {
    id: 'builtin-trailing-function',
    name: '闭合后的同名尾随日志',
    kind: 'builtin-trailing',
    enabled: true,
    builtIn: true,
    description: '完整函数调用闭合后，紧接着出现的同执行泳道、同函数名普通日志继续并入该折叠组。',
  },
  {
    id: 'builtin-repeated-function',
    name: '连续完整调用聚合 ×N',
    kind: 'builtin-repeated',
    enabled: true,
    builtIn: true,
    description: '连续出现且入口/出口规则、函数名和执行泳道一致的完整调用，在展示层聚合为 ×N。',
  },
];

function normalizedCustomRule(value: unknown, index: number): FoldingRule | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const raw = value as Record<string, unknown>;
  if (raw.kind !== 'custom-boundary') return undefined;
  const name = String(raw.name ?? '').trim();
  const startKeyword = String(raw.startKeyword ?? '').trim();
  const endKeyword = String(raw.endKeyword ?? '').trim();
  if (!name || !startKeyword || !endKeyword) return undefined;
  return {
    id: String(raw.id || `custom-fold-rule-${index}`),
    name,
    kind: 'custom-boundary',
    enabled: raw.enabled !== false,
    builtIn: false,
    description: String(raw.description ?? '按同一函数名和自定义入口/出口关键字配对折叠。'),
    startKeyword,
    endKeyword,
    caseSensitive: Boolean(raw.caseSensitive),
    createdAt: Number(raw.createdAt) || Date.now(),
  };
}

function mergeWithDefaults(values: readonly FoldingRule[]): FoldingRule[] {
  const byId = new Map(values.map((rule) => [rule.id, rule]));
  const builtIns = DEFAULT_FOLDING_RULES.map((rule) => {
    const stored = byId.get(rule.id);
    return stored ? { ...rule, enabled: stored.enabled !== false } : { ...rule };
  });
  const custom = values.filter((rule) => !rule.builtIn && rule.kind === 'custom-boundary');
  return [...builtIns, ...custom];
}

export function loadFoldingRules(): FoldingRule[] {
  if (typeof window === 'undefined') return DEFAULT_FOLDING_RULES.map((rule) => ({ ...rule }));
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (!stored) return DEFAULT_FOLDING_RULES.map((rule) => ({ ...rule }));
    const parsed = JSON.parse(stored);
    if (!Array.isArray(parsed)) return DEFAULT_FOLDING_RULES.map((rule) => ({ ...rule }));

    const restored: FoldingRule[] = [];
    parsed.forEach((item, index) => {
      if (!item || typeof item !== 'object') return;
      const raw = item as Record<string, unknown>;
      const builtin = DEFAULT_FOLDING_RULES.find((rule) => rule.id === raw.id);
      if (builtin) {
        restored.push({ ...builtin, enabled: raw.enabled !== false });
        return;
      }
      const custom = normalizedCustomRule(item, index);
      if (custom) restored.push(custom);
    });
    return mergeWithDefaults(restored);
  } catch {
    return DEFAULT_FOLDING_RULES.map((rule) => ({ ...rule }));
  }
}

export function saveFoldingRules(rules: readonly FoldingRule[]): void {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(mergeWithDefaults(rules)));
}

export function createCustomFoldingRule(seed?: Partial<FoldingRule>): FoldingRule {
  return {
    id: seed?.id || `custom-fold-rule-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    name: seed?.name ?? '自定义函数边界',
    kind: 'custom-boundary',
    enabled: seed?.enabled ?? true,
    builtIn: false,
    description: seed?.description ?? '按同一函数名和自定义入口/出口关键字配对折叠。',
    startKeyword: seed?.startKeyword ?? '[START]',
    endKeyword: seed?.endKeyword ?? '[END]',
    caseSensitive: seed?.caseSensitive ?? true,
    createdAt: seed?.createdAt ?? Date.now(),
  };
}

export function foldingPolicy(rules: readonly FoldingRule[]): FoldingPolicy {
  const merged = mergeWithDefaults(rules);
  const enabled = (kind: FoldingRuleKind) => merged.some((rule) => rule.kind === kind && rule.enabled);
  return {
    boundary: enabled('builtin-boundary'),
    consecutive: enabled('builtin-consecutive'),
    trailing: enabled('builtin-trailing'),
    repeated: enabled('builtin-repeated'),
    customBoundaries: merged.filter((rule) => rule.kind === 'custom-boundary' && rule.enabled),
  };
}

function indexOfKeyword(text: string, keyword: string, caseSensitive: boolean): number {
  if (!keyword) return -1;
  return caseSensitive
    ? text.indexOf(keyword)
    : text.toLocaleLowerCase().indexOf(keyword.toLocaleLowerCase());
}

export function resolveFoldingBoundaryWithPolicy(
  entry: LogEntry,
  policy: FoldingPolicy,
): FoldingBoundaryMatch | undefined {
  if (policy.boundary && entry.marker !== 'none') {
    const functionName = entry.boundaryFunctionName ?? entry.functionName;
    if (!functionName) return undefined;
    return {
      marker: entry.marker,
      functionName,
      ruleId: 'builtin-explicit-boundary',
      ruleName: '显式函数入口/出口',
    };
  }

  if (!entry.functionName) return undefined;
  for (const rule of policy.customBoundaries) {
    const startKeyword = rule.startKeyword?.trim() ?? '';
    const endKeyword = rule.endKeyword?.trim() ?? '';
    if (!startKeyword || !endKeyword) continue;
    const caseSensitive = Boolean(rule.caseSensitive);
    // 配置解析器可能只把 START/END 后面的 payload 映射到 message；
    // 因此先检查 message，未命中时再检查完整 raw，避免执行器边界因字段布局变化丢失。
    for (const text of [entry.message, entry.raw]) {
      if (!text) continue;
      const startIndex = indexOfKeyword(text, startKeyword, caseSensitive);
      const endIndex = indexOfKeyword(text, endKeyword, caseSensitive);
      if (startIndex < 0 && endIndex < 0) continue;
      const marker: 'start' | 'end' = startIndex >= 0 && (endIndex < 0 || startIndex < endIndex) ? 'start' : 'end';
      return {
        marker,
        functionName: entry.functionName,
        ruleId: rule.id,
        ruleName: rule.name,
      };
    }
  }
  return undefined;
}

export function resolveFoldingBoundary(
  entry: LogEntry,
  rules: readonly FoldingRule[],
): FoldingBoundaryMatch | undefined {
  return resolveFoldingBoundaryWithPolicy(entry, foldingPolicy(rules));
}

export function foldingRuleLogicLabel(rule: FoldingRule): string {
  switch (rule.kind) {
    case 'builtin-boundary': return '> () 入口 / < () 出口';
    case 'builtin-consecutive': return '连续同执行泳道 + 同函数名';
    case 'builtin-trailing': return '闭合后连续同函数名日志';
    case 'builtin-repeated': return '连续完整调用 + 同边界规则';
    case 'custom-boundary': return `${rule.startKeyword || '—'} 入口 / ${rule.endKeyword || '—'} 出口`;
  }
}
