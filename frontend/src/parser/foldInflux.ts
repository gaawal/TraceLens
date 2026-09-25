/**
 * 「函数折叠动画」的数据层：实时监听开着函数折叠时，后面新到的日志会立刻被
 * 收进上方那张入口卡片里 —— 如果只是数字从 8 变成 9，用户根本看不见发生了什么。
 * 这里负责把「哪张卡片刚刚折进去了哪几行」算出来，交给视图播一段折叠动画。
 *
 * 设计要点：
 * - 只做**差值**：每次重新折叠后扫描一遍函数节点直接收纳的日志条数，和上一帧比，
 *   变多的就是刚折进去的。第一帧只当基线，不把已有日志当成「刚折进来」。
 * - 动画挂到祖先链上：内层节点在父级折叠时压根没渲染，只有祖先卡片可见，
 *   所以新增行同时记到该节点和它的所有祖先名下（折叠态才播放，不会重复播）。
 * - 有界：每条卡片最多同时挂 FOLD_INFLUX_MAX_LINES 行，一帧最多影响
 *   FOLD_INFLUX_MAX_NODES 张卡片，行到期就清掉，避免实时高频追加把 DOM 拖垮。
 */
import type { LogEntry, TimelineItem } from '../types';

/** 单个函数节点最近一次扫描的指纹：直接收纳的日志条数 + 末尾几行（用来取新增行）。 */
export type FoldLogFingerprint = { count: number; tail: LogEntry[]; parents: string[] };
export type FoldLogScan = Map<string, FoldLogFingerprint>;

/** 一行待播放的折叠动画（id 唯一，便于 React 在连续追加时正确重放动画）。 */
export type FoldInfluxLine = { id: string; entry: LogEntry; at: number };

export const FOLD_INFLUX_MAX_LINES = 3;
export const FOLD_INFLUX_MAX_NODES = 48;
/** 和 styles.css 里 .function-fold-line 的动画时长对齐；过期即清理。 */
export const FOLD_INFLUX_LIFETIME_MS = 1100;
/** 每个节点只留末尾几行做对比，够算出「这次新增了几行」。 */
const TAIL_KEEP = 4;

/** 扫描折叠树，记录每个函数节点直接收纳的日志。纯函数，方便单测。 */
export function scanFoldLogs(items: readonly TimelineItem[]): FoldLogScan {
  const scan: FoldLogScan = new Map();
  const visit = (list: readonly TimelineItem[], parents: string[]) => {
    for (const item of list) {
      if (item.kind !== 'function') continue;
      const own: LogEntry[] = [];
      for (const child of item.children) {
        if (child.kind === 'log') own.push(child.entry);
      }
      scan.set(item.id, { count: own.length, tail: own.slice(-TAIL_KEEP), parents });
      if (item.children.length) visit(item.children, [...parents, item.id]);
    }
  };
  visit(items, []);
  return scan;
}

/**
 * 比较前后两次扫描：返回刚刚「折进新日志」的卡片 id 链和新增的日志行。
 * 节点新增的日志行数超过保留窗口时只取最后 TAIL_KEEP 行，动画本来就只播几行。
 */
export function diffFoldLogs(prev: FoldLogScan, next: FoldLogScan): Array<{ ids: string[]; entries: LogEntry[] }> {
  const grown: Array<{ ids: string[]; entries: LogEntry[] }> = [];
  next.forEach((current, nodeId) => {
    const before = prev.get(nodeId);
    if (!before || current.count <= before.count) return;
    const added = current.tail.slice(-(current.count - before.count));
    if (!added.length) return;
    grown.push({ ids: [nodeId, ...current.parents], entries: added });
  });
  return grown;
}

/** 把这一帧新增的折叠行动画并进状态，并做上限裁剪（每卡片几行、总共几张卡片）。 */
export function mergeFoldInflux(
  current: Record<string, FoldInfluxLine[]>,
  grown: Array<{ ids: string[]; entries: LogEntry[] }>,
  stamp: number,
): Record<string, FoldInfluxLine[]> {
  if (!grown.length) return current;
  const next: Record<string, FoldInfluxLine[]> = { ...current };
  grown.forEach((item, grownIndex) => {
    item.entries.forEach((entry) => {
      item.ids.forEach((nodeId) => {
        const line: FoldInfluxLine = { id: `${nodeId}:${entry.id}:${stamp}:${grownIndex}`, entry, at: stamp };
        next[nodeId] = [...(next[nodeId] || []), line].slice(-FOLD_INFLUX_MAX_LINES);
      });
    });
  });
  const keys = Object.keys(next);
  if (keys.length > FOLD_INFLUX_MAX_NODES) {
    const keep = new Set(keys
      .map((key) => [key, next[key][next[key].length - 1]?.at ?? 0] as const)
      .sort((left, right) => right[1] - left[1])
      .slice(0, FOLD_INFLUX_MAX_NODES)
      .map(([key]) => key));
    keys.forEach((key) => { if (!keep.has(key)) delete next[key]; });
  }
  return next;
}

/** 清掉动画已经播完的行；没有变化时返回原对象，避免无意义的重渲染。 */
export function pruneFoldInflux(
  current: Record<string, FoldInfluxLine[]>,
  now: number,
  lifetime = FOLD_INFLUX_LIFETIME_MS,
): Record<string, FoldInfluxLine[]> {
  let changed = false;
  const next: Record<string, FoldInfluxLine[]> = {};
  Object.entries(current).forEach(([nodeId, lines]) => {
    const kept = lines.filter((line) => now - line.at < lifetime);
    if (kept.length) next[nodeId] = kept;
    if (kept.length !== lines.length) changed = true;
  });
  return changed ? next : current;
}
