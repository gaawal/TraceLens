import type { FunctionNode, LogEntry, TimelineItem } from '../types';

/**
 * 时间轴的**字符串兜底**。
 *
 * 日志里明明有时间（日志格式规则的正则已经把它抓成字符串了），但没被认成可计算的时间戳
 * （例如只抓到 `11:32:59.959`、或者 `26-09-2026 11:32:59` 这种格式）。
 * 这种时候没必要报"格式解析没配时间字段"：这类字符串**按字典序排就是时间先后**，
 * 直接拿它当刻度用，轴上显示的也是日志原文。
 */

/** 一批日志里出现过的所有时间字符串（已排序 = 时间先后）。 */
export function collectTimeLabels(items: readonly TimelineItem[]): string[] {
  const texts = new Set<string>();
  const add = (entry?: LogEntry | null) => {
    const text = String(entry?.timestamp || '').trim();
    if (text) texts.add(text);
  };
  const walkNode = (node: FunctionNode) => {
    add(node.startEntry);
    add(node.endEntry);
    node.children.forEach((child) => {
      if (child.kind === 'function') walkNode(child);
      else add(child.entry);
    });
  };
  items.forEach((item) => {
    if (item.kind === 'function') walkNode(item);
    else add(item.entry);
  });
  return [...texts].sort();
}

export interface TimeSequence {
  /** 升序排列的时间字符串（字典序即时间先后）。 */
  labels: string[];
  indexOf: Map<string, number>;
  /** 最后一个下标（0 表示只有一个时间点）。 */
  last: number;
}

export function buildTimeSequence(items: readonly TimelineItem[]): TimeSequence {
  const labels = collectTimeLabels(items);
  const indexOf = new Map(labels.map((label, index) => [label, index]));
  return { labels, indexOf, last: Math.max(0, labels.length - 1) };
}

/** 轴上的刻度标签：有日期就去掉年份，只有时间就原样（不做任何时间戳换算）。 */
export function shortTimeLabel(text: string): string {
  const value = String(text || '').trim();
  const match = value.match(/^(\d{4})[-/](\d{2})[-/](\d{2})[ T](.+)$/);
  if (!match) return value;
  return `${match[2]}-${match[3]} ${match[4]}`;
}
