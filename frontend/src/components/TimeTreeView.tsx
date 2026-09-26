import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { AlertTriangle, Maximize2, ZoomIn, ZoomOut } from 'lucide-react';
import type { ContentSeverity, FunctionNode, TimelineItem, TraceTimeline } from '../types';
import { functionNodeEntries, matchDisplayRulesToFunction, type DisplayRule } from '../rendering/displayRules';
import { durationNs, formatDuration } from '../parser/treeBuilder';
import { timestampToNs } from '../parser/logParser';
import { buildTimeSequence, shortTimeLabel } from '../rendering/timeAxis';

/**
 * 时间轴缩进树 —— 保留折叠树的嵌套结构，同时把「谁先谁后」画清楚。
 *
 * 为什么原来的缩进树看不出先后
 * ----------------------------
 * 原来每个节点的水平位置只由缩进深度决定：同级节点无论相隔 5 秒还是 5 分钟，
 * 横向都在同一列。于是「先做谁、后做谁」只能靠上下顺序猜，而上下顺序又和
 * 分支结构混在一起。
 *
 * 这里把水平方向让给**时间**：
 * - 顶部一条时间轴；
 * - 每一行左侧是保留缩进的树形标签（含缩进引导线），右侧是这条时间轴下的时间条，
 *   横条从节点开始时间画到结束时间；
 * - 行的先后顺序按开始时间排（DFS 顺序本身就是时间顺序）。
 * 这样一来「时间轴下的方法/模块先后」是直接看出来的，而嵌套层级仍然由缩进表达。
 *
 * 标签列宽度由最大缩进深度算出：**每一行的时间条都从同一个 x 开始**，
 * 否则时间轴就没法和下面的条对齐。
 */
// 每行要放下「函数名 / 组件 · 子调用数 / 语义」三行小字，30px 会挤到重叠。
const ROW_HEIGHT = 40;
const INDENT = 16;
const LABEL_BASE = 132;
const TRACK_PAD = 16;
const MIN_BAR = 3;
const MIN_ZOOM = 0.4;
const MAX_ZOOM = 40;

interface TreeRow {
  node: FunctionNode;
  depth: number;
  /** 子节点数量，用于决定是否画展开箭头（这里只是视觉提示，始终展开）。 */
  childCount: number;
  severity: ContentSeverity;
  startNs?: bigint;
  endNs?: bigint;
  /** 日志里的时间字符串（正则抓出来的原文）。解析不出时间戳时，行顺序与时间轴都用它排。 */
  startText?: string;
  endText?: string;
  semantic?: string;
}

const SEVERITY_RANK: Record<ContentSeverity, number> = { normal: 0, warning: 1, error: 2 };

function subtreeSeverity(node: FunctionNode): ContentSeverity {
  return node.children.reduce<ContentSeverity>((highest, child) => {
    const value = child.kind === 'log' ? child.entry.severity : subtreeSeverity(child);
    return SEVERITY_RANK[value] > SEVERITY_RANK[highest] ? value : highest;
  }, 'normal');
}

/**
 * 一条日志的纳秒时间。
 *
 * `timestampNs` 是格式化解析器算出来的；解析器不认这个时间格式时它会是 undefined，
 * 而 `timestamp` 字符串**仍然在**（日志里明明有时间）。之前只看 `timestampNs`，
 * 于是这类日志被判成「没有可用的时间戳，无法按时间铺开」——
 * 时间就在那儿，只是没人去解它。这里补一次兜底解析。
 */
function entryNs(entry: { timestampNs?: bigint; timestamp?: string }): bigint | undefined {
  if (entry.timestampNs !== undefined) return entry.timestampNs;
  const text = String(entry.timestamp || '').trim();
  return text ? timestampToNs(text) : undefined;
}

/** 节点的时间范围：优先用折叠边界，缺失时退回子树内日志行的最早/最晚时间。
 *
 * 同时带出**日志原文里的时间字符串**：解析不出时间戳时（格式没配 / 格式不匹配），
 * 时间字符串仍然能用——按字典序排就是时间先后，直接展示给用户看的就是它。
 */
export function nodeRange(node: FunctionNode): { startNs?: bigint; endNs?: bigint; startText: string; endText: string } {
  const entries = functionNodeEntries(node);
  const textOf = (entry?: { timestamp?: string } | null): string => String(entry?.timestamp || '').trim();
  const texts = entries.map(textOf).filter(Boolean);
  const sorted = [...texts].sort();
  const startText = textOf(node.startEntry) || sorted[0] || '';
  const endText = textOf(node.endEntry ?? node.startEntry) || sorted[sorted.length - 1] || startText;

  const directStart = entryNs(node.startEntry);
  if (directStart !== undefined) {
    return { startNs: directStart, endNs: entryNs(node.endEntry ?? node.startEntry) ?? directStart, startText, endText };
  }
  const times = entries
    .map((entry) => entryNs(entry))
    .filter((value): value is bigint => value !== undefined);
  if (!times.length) return { startText, endText };
  return {
    startNs: times.reduce((min, value) => (value < min ? value : min)),
    endNs: times.reduce((max, value) => (value > max ? value : max)),
    startText,
    endText,
  };
}

function collectRows(items: readonly FunctionNode[], depth: number, rules: readonly DisplayRule[], out: TreeRow[], seen: Set<string>): void {
  for (const node of items) {
    // 折叠树理论上不会成环；这里仍然防一手，避免异常数据把页面挂死。
    if (seen.has(node.id)) continue;
    seen.add(node.id);
    const children = node.children.filter((child): child is FunctionNode => child.kind === 'function');
    const range = nodeRange(node);
    out.push({
      node,
      depth,
      childCount: children.length,
      severity: subtreeSeverity(node),
      startNs: range.startNs,
      endNs: range.endNs,
      startText: range.startText,
      endText: range.endText,
      semantic: matchDisplayRulesToFunction([...rules], node)?.text,
    });
    collectRows(children, depth + 1, rules, out, seen);
  }
}

/** 注意参数是**毫秒**（`span.min` 也是先除到毫秒再传进来），别再除一次。 */
function formatTick(milliseconds: number, showDate: boolean): string {
  const date = new Date(milliseconds);
  const pad = (input: number, size = 2) => String(input).padStart(size, '0');
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${pad(date.getMilliseconds(), 3)}`;
  return showDate ? `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${time}` : time;
}

/** 选一个「整」的刻度间隔，让轴上的数字好读。 */
function pickTickStep(spanMs: number, targetCount: number): number {
  const raw = spanMs / Math.max(1, targetCount);
  const candidates = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10_000, 15_000, 30_000, 60_000,
    120_000, 300_000, 600_000, 900_000, 1_800_000, 3_600_000, 7_200_000, 21_600_000, 43_200_000, 86_400_000];
  return candidates.find((value) => value >= raw) ?? candidates[candidates.length - 1];
}

interface Props {
  traces: TraceTimeline[];
  rules: readonly DisplayRule[];
  onSelectNode: (trace: TraceTimeline, node: FunctionNode) => void;
}

export function TimeTreeView({ traces, rules, onSelectNode }: Props) {
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const [zoom, setZoom] = useState(1);
  const dragRef = useRef<{ pointerId: number; startX: number; origin: number }>();
  const [activeTraceId, setActiveTraceId] = useState<string>();

  const activeTrace = traces.find((trace) => trace.id === activeTraceId) ?? traces[0];

  const rows = useMemo(() => {
    if (!activeTrace) return [];
    const roots = activeTrace.items.filter((item): item is FunctionNode => item.kind === 'function');
    const out: TreeRow[] = [];
    collectRows(roots, 0, rules, out, new Set());
    return out;
  }, [activeTrace, rules]);

  /** 时间范围：整条调用链的起止。没有时间戳时退化为「按顺序等距」绘制。 */
  const span = useMemo(() => {
    const starts = rows.map((row) => row.startNs).filter((value): value is bigint => value !== undefined);
    const ends = rows.map((row) => row.endNs).filter((value): value is bigint => value !== undefined);
    if (!starts.length || !ends.length) return undefined;
    const min = starts.reduce((a, b) => (a < b ? a : b));
    const max = ends.reduce((a, b) => (a > b ? a : b));
    const spanNs = max - min;
    return { min, max, spanNs, spanMs: Number(spanNs) / 1_000_000 };
  }, [rows]);

  /**
   * 时间字符串序列：解析不出时间戳时的**兜底坐标**。
   *
   * 日志里明明有时间（正则已经抓成字符串了），只是没被认成可计算的时间戳。
   * 这种格式（`YYYY-MM-DD HH:MM:SS.fff` 之类）**按字典序排就是时间先后**，
   * 所以直接拿字符串当刻度：序号 → 横坐标，轴上的标签就是日志里的原文。
   */
  const sequence = useMemo(() => buildTimeSequence(activeTrace?.items ?? []), [activeTrace]);

  /** 有时间戳就按真实时间铺开；没有就按日志时间字符串的先后顺序等距铺开。 */
  const axisMode: 'time' | 'sequence' = span ? 'time' : 'sequence';

  const maxDepth = rows.reduce((deepest, row) => Math.max(deepest, row.depth), 0);
  const labelWidth = LABEL_BASE + maxDepth * INDENT;
  const trackWidth = 1400;
  /** 轨道像素宽 = 基准宽 × 缩放；横向靠原生滚动，不再用 transform。 */
  const trackPx = Math.round(trackWidth * zoom);
  const axisWidth = labelWidth + trackPx + TRACK_PAD;
  const showDate = span ? new Date(Number(span.min) / 1_000_000).toDateString() !== new Date(Number(span.max) / 1_000_000).toDateString() : false;

  const timeToX = useCallback((ns?: bigint): number => {
    if (!span || ns === undefined || span.spanNs === 0n) return 0;
    return (Number(ns - span.min) / Number(span.spanNs)) * trackPx;
  }, [span, trackPx]);

  /** 序号 → 横坐标（时间字符串模式下用）。 */
  const indexToX = useCallback((index: number): number => {
    if (sequence.last <= 0) return 0;
    return (Math.max(0, Math.min(index, sequence.last)) / sequence.last) * trackPx;
  }, [sequence.last, trackPx]);

  /** 一行的时间条位置：优先真实时间，否则按时间字符串的序号。 */
  const barGeometry = useCallback((row: TreeRow): { x: number; width: number } => {
    if (axisMode === 'time') {
      const x = timeToX(row.startNs);
      return { x, width: Math.max(MIN_BAR, timeToX(row.endNs) - x) };
    }
    const startIndex = sequence.indexOf.get(row.startText || '') ?? 0;
    const endIndex = sequence.indexOf.get(row.endText || row.startText || '') ?? startIndex;
    const x = indexToX(startIndex);
    return { x, width: Math.max(MIN_BAR, indexToX(Math.max(startIndex, endIndex)) - x) };
  }, [axisMode, sequence, timeToX, indexToX]);

  const ticks = useMemo(() => {
    if (axisMode === 'sequence') {
      if (!sequence.labels.length) return [];
      // 均匀挑几个刻度，标签直接用日志里的时间原文（不做任何时间戳换算）。
      const step = Math.max(1, Math.ceil(sequence.labels.length / 8));
      const result: Array<{ x: number; label: string }> = [];
      for (let index = 0; index < sequence.labels.length && result.length < 12; index += step) {
        result.push({ x: indexToX(index), label: shortTimeLabel(sequence.labels[index]) });
      }
      const lastX = indexToX(sequence.last);
      if (result[result.length - 1]?.x !== lastX) {
        result.push({ x: lastX, label: shortTimeLabel(sequence.labels[sequence.last]) });
      }
      return result;
    }
    if (!span || span.spanMs <= 0) return [];
    const step = pickTickStep(span.spanMs, 7);
    const base = Number(span.min) / 1_000_000;
    const result: Array<{ x: number; label: string }> = [];
    // 用「第几个刻度」而不是浮点累加：`Math.ceil(span / step) * step` 在 span 正好是
    // step 整数倍时会算出比 span 大一点点的数（浮点误差），循环一次都不进 ——
    // 时间轴就整个空了。
    const count = Math.floor(span.spanMs / step);
    for (let index = 1; index <= count && result.length < 14; index += 1) {
      const value = index * step;
      result.push({ x: (value / span.spanMs) * trackPx, label: formatTick(base + value, showDate) });
    }
    // 起止两端始终标出来：跨度小于最小刻度时也要有东西可读。
    result.unshift({ x: 0, label: formatTick(base, showDate) });
    if (span.spanMs > step) result.push({ x: trackPx, label: formatTick(base + span.spanMs, showDate) });
    return result;
  }, [axisMode, sequence, indexToX, span, showDate, trackPx]);

  const resetView = useCallback(() => {
    setZoom(1);
    const viewport = viewportRef.current;
    if (viewport) viewport.scrollLeft = 0;
  }, []);

  function onWheel(event: React.WheelEvent<HTMLDivElement>) {
    if (!event.shiftKey && !event.ctrlKey && Math.abs(event.deltaY) > Math.abs(event.deltaX)) {
      // 不按 Shift/Ctrl 时纵向滚轮留给纵向滚动，缩放只在按住修饰键或横向滚轮时触发。
      return;
    }
    event.preventDefault();
    const viewport = viewportRef.current;
    if (!viewport) return;
    const rect = viewport.getBoundingClientRect();
    const pointerX = event.clientX - rect.left + viewport.scrollLeft - labelWidth;
    const next = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom * (event.deltaY < 0 || event.deltaX < 0 ? 1.15 : 0.87)));
    // 以光标为锚点缩放：改完轨道宽度后把同一个时间点挪回光标下面。
    setZoom(next);
    window.requestAnimationFrame(() => {
      viewport.scrollLeft = Math.max(0, (pointerX / zoom) * next - (event.clientX - rect.left - labelWidth));
    });
  }

  function beginDrag(event: React.PointerEvent<HTMLDivElement>) {
    if ((event.target as HTMLElement).closest('button')) return;
    const viewport = viewportRef.current;
    if (!viewport) return;
    dragRef.current = { pointerId: event.pointerId, startX: event.clientX, origin: viewport.scrollLeft };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function moveDrag(event: React.PointerEvent<HTMLDivElement>) {
    const state = dragRef.current;
    const viewport = viewportRef.current;
    if (!state || !viewport || state.pointerId !== event.pointerId) return;
    // 拖拽 = 反向滚横向滚动条。用滚动而不是 transform，左侧标签列才能靠 sticky 钉住。
    viewport.scrollLeft = state.origin - (event.clientX - state.startX);
  }

  function endDrag() {
    dragRef.current = undefined;
  }

  if (!traces.length) {
    return (
      <div className="call-flow-graph-empty">
        <AlertTriangle size={34} />
        <strong>当前范围没有可折叠的调用</strong>
        <span>缩进树按函数折叠结果绘制；可以先关闭函数折叠，或调整筛选条件。</span>
      </div>
    );
  }

  return (
    <div className="time-tree">
      <header className="time-tree-head">
        <div className="time-tree-summary">
          <strong>时间轴缩进树</strong>
          <span>{rows.length} 个节点</span>
          {span
            ? <span>跨度 {formatDuration(span.spanNs)}</span>
            : sequence.labels.length > 0
              ? <span title="日志里的时间没被解析成时间戳，这里直接用时间字符串排序与展示">按日志时间顺序 · {sequence.labels.length} 个时间点</span>
              : null}
        </div>
        <div className="time-tree-actions">
          {traces.length > 1 && (
            <label className="time-tree-trace-picker">
              <span>调用链</span>
              <select value={activeTrace?.id} onChange={(event) => { setActiveTraceId(event.target.value); const v = viewportRef.current; if (v) v.scrollLeft = 0; }}>
                {traces.map((trace, index) => (
                  <option value={trace.id} key={trace.id}>{`${index + 1}. ${trace.firstFunctionName} · ${trace.components.join(' → ')}`}</option>
                ))}
              </select>
            </label>
          )}
          <button type="button" onClick={() => setZoom((value) => Math.min(MAX_ZOOM, value * 1.25))} title="横向放大（时间轴拉长）"><ZoomIn size={14} /></button>
          <button type="button" onClick={() => setZoom((value) => Math.max(MIN_ZOOM, value * 0.8))} title="横向缩小"><ZoomOut size={14} /></button>
          <button type="button" onClick={resetView} title="恢复默认缩放与位置"><Maximize2 size={14} /></button>
          <span className="time-tree-zoom">{Math.round(zoom * 100)}%</span>
        </div>
      </header>

      <div
        className="time-tree-viewport"
        ref={viewportRef}
        onWheel={onWheel}
        onPointerDown={beginDrag}
        onPointerMove={moveDrag}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
      >
        <div className="time-tree-canvas" style={{ width: axisWidth }}>
          {/* 时间轴：所有时间条的横坐标都以它为准。 */}
          <div className="time-tree-axis" style={{ '--time-tree-label-width': `${labelWidth}px`, height: 30 } as CSSProperties}>
            <div className="time-tree-axis-gutter"><span>时间</span></div>
            <div className="time-tree-axis-track" style={{ width: trackPx }}>
              {ticks.map((tick) => (
                <span className="time-tree-tick" key={tick.x} style={{ left: tick.x }}>
                  <em>{tick.label}</em>
                </span>
              ))}
              {!span && !sequence.labels.length && (
                <span className="time-tree-axis-hint">
                  这批日志里没有读到时间字段（解析结果里既没有时间戳也没有时间字符串），只能按行顺序铺开。
                </span>
              )}
            </div>
          </div>

          <div className="time-tree-rows">
            {rows.map((row) => {
              const { x, width } = barGeometry(row);
              const duration = durationNs(row.node);
              return (
                <div
                  className={`time-tree-row severity-${row.severity}`}
                  key={row.node.id}
                  style={{ '--time-tree-label-width': `${labelWidth}px`, height: ROW_HEIGHT } as CSSProperties}
                >
                  <div className="time-tree-label-cell" style={{ paddingLeft: 8 + row.depth * INDENT }}>
                    {row.depth > 0 && <span className="time-tree-indent" aria-hidden="true" style={{ left: 8 + (row.depth - 1) * INDENT + 6 }} />}
                    <button
                      type="button"
                      className="time-tree-label"
                      onClick={() => onSelectNode(activeTrace, row.node)}
                      title={`${row.node.name}\n${row.startText}${row.endText && row.endText !== row.startText ? ` → ${row.endText}` : ''}\n${row.node.source.raw}\n点击回到日志定位并展开`}
                    >
                      <span className="time-tree-node-name">{row.node.name}</span>
                      {/* 组件 + 子调用数 + 语义放在同一行：三行文字塞进 40px 的行会互相压。 */}
                      <span className="time-tree-node-meta">
                        {/* 时间直接显示日志原文（正则抓到的字符串），不依赖时间戳换算。 */}
                        {row.startText && <span className="time-tree-node-time" title={`${row.startText}${row.endText && row.endText !== row.startText ? ` → ${row.endText}` : ''}`}>{row.startText}</span>}
                        <span>{row.node.component || '—'}{row.childCount > 0 ? ` · ${row.childCount} 子调用` : ''}</span>
                        {row.semantic && <b className="time-tree-node-semantic" title={row.semantic}>{row.semantic}</b>}
                      </span>
                    </button>
                  </div>
                  <div className="time-tree-track" style={{ width: trackPx }}>
                    {ticks.map((tick) => <span className="time-tree-gridline" key={tick.x} style={{ left: tick.x }} />)}
                    <button
                      type="button"
                      className="time-tree-bar"
                      style={{ left: x, width }}
                      onClick={() => onSelectNode(activeTrace, row.node)}
                      title={`${row.node.name}\n${formatDuration(duration)}${row.node.incomplete ? '\n（缺少出口，时长可能不完整）' : ''}\n点击回到日志定位并展开`}
                    >
                      <span className="time-tree-bar-text">{formatDuration(duration)}</span>
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>

      <footer className="time-tree-foot">
        <span>
          {axisMode === 'time'
            ? '行顺序 = 开始时间先后 · 左侧缩进 = 嵌套层级 · 水平拖动平移，Shift+滚轮缩放时间轴'
            : '行顺序 = 日志时间字符串先后（未解析成时间戳） · 左侧缩进 = 嵌套层级 · 水平拖动平移，Shift+滚轮缩放'}
        </span>
      </footer>
    </div>
  );
}
