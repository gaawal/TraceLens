import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { AlertTriangle, Maximize2, ZoomIn, ZoomOut } from 'lucide-react';
import type { ContentSeverity, FunctionNode, TraceTimeline } from '../types';
import { functionNodeEntries, matchDisplayRulesToFunction, type DisplayRule } from '../rendering/displayRules';
import { durationNs, formatDuration } from '../parser/treeBuilder';

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
  semantic?: string;
}

const SEVERITY_RANK: Record<ContentSeverity, number> = { normal: 0, warning: 1, error: 2 };

function subtreeSeverity(node: FunctionNode): ContentSeverity {
  return node.children.reduce<ContentSeverity>((highest, child) => {
    const value = child.kind === 'log' ? child.entry.severity : subtreeSeverity(child);
    return SEVERITY_RANK[value] > SEVERITY_RANK[highest] ? value : highest;
  }, 'normal');
}

/** 节点的时间范围：优先用折叠边界，缺失时退回子树内日志行的最早/最晚时间。 */
function nodeRange(node: FunctionNode): { startNs?: bigint; endNs?: bigint } {
  if (node.startEntry.timestampNs !== undefined) {
    return { startNs: node.startEntry.timestampNs, endNs: node.endEntry?.timestampNs ?? node.startEntry.timestampNs };
  }
  const times = functionNodeEntries(node)
    .map((entry) => entry.timestampNs)
    .filter((value): value is bigint => value !== undefined);
  if (!times.length) return {};
  return {
    startNs: times.reduce((min, value) => (value < min ? value : min)),
    endNs: times.reduce((max, value) => (value > max ? value : max)),
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
  const [offset, setOffset] = useState(0);
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

  const maxDepth = rows.reduce((deepest, row) => Math.max(deepest, row.depth), 0);
  const labelWidth = LABEL_BASE + maxDepth * INDENT;
  const trackWidth = 1400;
  const axisWidth = labelWidth + trackWidth + TRACK_PAD;
  const showDate = span ? new Date(Number(span.min) / 1_000_000).toDateString() !== new Date(Number(span.max) / 1_000_000).toDateString() : false;

  const timeToX = useCallback((ns?: bigint): number => {
    if (!span || ns === undefined || span.spanNs === 0n) return 0;
    return (Number(ns - span.min) / Number(span.spanNs)) * trackWidth;
  }, [span, trackWidth]);

  const ticks = useMemo(() => {
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
      result.push({ x: (value / span.spanMs) * trackWidth, label: formatTick(base + value, showDate) });
    }
    // 起止两端始终标出来：跨度小于最小刻度时也要有东西可读。
    result.unshift({ x: 0, label: formatTick(base, showDate) });
    if (span.spanMs > step) result.push({ x: trackWidth, label: formatTick(base + span.spanMs, showDate) });
    return result;
  }, [span, showDate, trackWidth]);

  const clampOffset = useCallback((value: number) => {
    const viewport = viewportRef.current;
    if (!viewport) return value;
    const visible = viewport.clientWidth;
    const scaled = axisWidth * zoom;
    // 内容比视口窄时靠左放；否则限制在 0..(内容宽 - 视口宽)，避免拖出空白。
    const min = Math.min(0, visible - scaled);
    return Math.min(0, Math.max(min, value));
  }, [axisWidth, zoom]);

  const resetView = useCallback(() => {
    setZoom(1);
    setOffset(0);
  }, []);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport || typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(() => setOffset((current) => clampOffset(current)));
    observer.observe(viewport);
    return () => observer.disconnect();
  }, [clampOffset]);

  function onWheel(event: React.WheelEvent<HTMLDivElement>) {
    if (!event.shiftKey && Math.abs(event.deltaY) > Math.abs(event.deltaX)) {
      // 不按 Shift 时留给纵向滚动，横向缩放只在按住 Shift 或横向滚轮时触发。
      if (!event.ctrlKey) return;
    }
    event.preventDefault();
    const viewport = viewportRef.current;
    if (!viewport) return;
    const rect = viewport.getBoundingClientRect();
    const pointerX = event.clientX - rect.left;
    const next = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom * (event.deltaY < 0 || event.deltaX < 0 ? 1.15 : 0.87)));
    setOffset(clampOffset(pointerX - ((pointerX - offset) / zoom) * next));
    setZoom(next);
  }

  function beginDrag(event: React.PointerEvent<HTMLDivElement>) {
    if ((event.target as HTMLElement).closest('button')) return;
    dragRef.current = { pointerId: event.pointerId, startX: event.clientX, origin: offset };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function moveDrag(event: React.PointerEvent<HTMLDivElement>) {
    const state = dragRef.current;
    if (!state || state.pointerId !== event.pointerId) return;
    setOffset(clampOffset(state.origin + event.clientX - state.startX));
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
          {span && <span>跨度 {formatDuration(span.spanNs)}</span>}
        </div>
        <div className="time-tree-actions">
          {traces.length > 1 && (
            <label className="time-tree-trace-picker">
              <span>调用链</span>
              <select value={activeTrace?.id} onChange={(event) => { setActiveTraceId(event.target.value); setOffset(0); }}>
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
        <div className="time-tree-canvas" style={{ width: axisWidth, transform: `translateX(${offset}px) scale(${zoom})` }}>
          {/* 时间轴：所有时间条的横坐标都以它为准。 */}
          <div className="time-tree-axis" style={{ '--time-tree-label-width': `${labelWidth}px`, height: 30 } as CSSProperties}>
            <div className="time-tree-axis-gutter"><span>时间</span></div>
            <div className="time-tree-axis-track" style={{ width: trackWidth }}>
              {ticks.map((tick) => (
                <span className="time-tree-tick" key={tick.x} style={{ left: tick.x }}>
                  <em>{tick.label}</em>
                </span>
              ))}
              {!span && <span className="time-tree-axis-hint">这批日志没有可用的时间戳，无法按时间铺开</span>}
            </div>
          </div>

          <div className="time-tree-rows">
            {rows.map((row) => {
              const x = timeToX(row.startNs);
              const width = Math.max(MIN_BAR, timeToX(row.endNs) - x);
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
                      title={`${row.node.name}\n${row.node.source.raw}\n点击回到日志定位并展开`}
                    >
                      <span className="time-tree-node-name">{row.node.name}</span>
                      {/* 组件 + 子调用数 + 语义放在同一行：三行文字塞进 40px 的行会互相压。 */}
                      <span className="time-tree-node-meta">
                        <span>{row.node.component || '—'}{row.childCount > 0 ? ` · ${row.childCount} 子调用` : ''}</span>
                        {row.semantic && <b className="time-tree-node-semantic" title={row.semantic}>{row.semantic}</b>}
                      </span>
                    </button>
                  </div>
                  <div className="time-tree-track" style={{ width: trackWidth }}>
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
        <span>行顺序 = 开始时间先后 · 左侧缩进 = 嵌套层级 · 水平拖动平移，Shift+滚轮缩放时间轴</span>
      </footer>
    </div>
  );
}
