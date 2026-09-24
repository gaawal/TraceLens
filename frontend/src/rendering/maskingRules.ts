import type { LogEntry } from '../types';

export type MaskingRuleKind = 'keyword' | 'function' | 'regex' | 'template';
export type MaskingRuleScope = 'message' | 'function' | 'raw' | 'any';
export interface MaskingRule {
  id: string;
  name: string;
  enabled: boolean;
  kind: MaskingRuleKind;
  scope: MaskingRuleScope;
  pattern: string;
  caseSensitive: boolean;
  createdAt: number;
}

export const MASKING_RULE_STORAGE_KEY = 'tracelens.masking-rules.v1';
export function createMaskingRuleId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID();
  return `mask-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
}
export function loadMaskingRules(): MaskingRule[] {
  if (typeof window === 'undefined') return [];
  try {
    const parsed = JSON.parse(window.localStorage.getItem(MASKING_RULE_STORAGE_KEY) || '[]');
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((item) => item && typeof item.id === 'string' && typeof item.pattern === 'string').map((item) => ({
      id: item.id, name: typeof item.name === 'string' ? item.name : '未命名屏蔽规则', enabled: item.enabled !== false,
      kind: ['keyword', 'function', 'regex', 'template'].includes(item.kind) ? item.kind : 'keyword',
      scope: ['message', 'function', 'raw', 'any'].includes(item.scope) ? item.scope : 'any',
      pattern: item.pattern, caseSensitive: item.caseSensitive === true, createdAt: typeof item.createdAt === 'number' ? item.createdAt : Date.now(),
    }));
  } catch { return []; }
}
export function saveMaskingRules(rules: readonly MaskingRule[]): void {
  if (typeof window !== 'undefined') window.localStorage.setItem(MASKING_RULE_STORAGE_KEY, JSON.stringify(rules));
}
function targetValues(entry: LogEntry, scope: MaskingRuleScope): string[] {
  const fn = entry.functionName ?? entry.boundaryFunctionName ?? '';
  if (scope === 'message') return [entry.message];
  if (scope === 'function') return [fn];
  if (scope === 'raw') return [entry.raw];
  return [entry.message, fn, entry.raw];
}
function templateRegExp(pattern: string, flags: string): RegExp {
  const escaped = pattern.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/\\\{\\\{\\\*\\\}\\\}|\\\{\\\*\\\}/g, '.+?');
  return new RegExp(escaped, flags);
}
export function matchesMaskingRule(entry: LogEntry, rule: MaskingRule): boolean {
  if (!rule.enabled || !rule.pattern) return false;
  const flags = rule.caseSensitive ? '' : 'i';
  return targetValues(entry, rule.scope).some((value) => {
    if (rule.kind === 'function') {
      const actual = rule.caseSensitive ? value : value.toLowerCase(); const expected = rule.caseSensitive ? rule.pattern : rule.pattern.toLowerCase(); return actual === expected || actual.includes(expected);
    }
    if (rule.kind === 'regex') { try { return new RegExp(rule.pattern, flags).test(value); } catch { return false; } }
    if (rule.kind === 'template') { try { return templateRegExp(rule.pattern, flags).test(value); } catch { return false; } }
    return rule.caseSensitive ? value.includes(rule.pattern) : value.toLowerCase().includes(rule.pattern.toLowerCase());
  });
}
export function isEntryMasked(entry: LogEntry, rules: readonly MaskingRule[]): boolean { return rules.some((rule) => matchesMaskingRule(entry, rule)); }
