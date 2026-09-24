import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import { AlertTriangle, CheckCircle2, Crosshair, Maximize2, Minus, Plus, Tag, ZoomIn, ZoomOut } from 'lucide-react';
import type { ContentSeverity, FunctionNode, LogEntry, LogLeaf, TraceTimeline } from '../types';
import {
  functionNodeEntries,
  matchDisplayRuleToMessage,
  matchDisplayRulesToFunction,
  type DisplayRule,
  type DisplayRuleMatch,
} from '../rendering/displayRules';

/**
 * 流程地图 —— 把「一行一行读日志」换成「一眼看流程」。
 *
 * 为什么是横向树而不是别的形状
 * ----------------------------
 * 调用折叠本身已经是一棵树（外层函数包内层），横向摆放时：
 * - 横轴天然就是**时间**方向，和日志的时间语义一致，读图顺序 = 实际执行顺序；
 * - 缩进深度就是嵌套层级，外层在左、内层在右，不用额外图例解释「谁包含谁」；
 * - 到分支处按子节点数量上下展开，形状接近一棵横过来的二叉树，
 *   所以「层层嵌套」是可以直接看出来的，而不是靠缩进猜。
 * 力导向图（force-directed）在这里是错的：它会把同一层的节点打散，
 * 而调用层级恰恰是这张图唯一必须保住的信息。
 *
 * 为什么节点不是每个函数调用而是「折叠后的函数」
 * ---------------------------------------------
 * 折叠规则（START/END 配对、>()/ <() 边界）已经把重复调用合成一个节点，
 * 直接复用这棵树，图上的一个节点就等于用户在日志里折叠出来的一个函数/子流程。
 * 日志叶子不单独成节点：它们的信息（异常、标签、语义）上卷到所属函数节点上，
 * 否则一条 200 行的日志会画出 200 个节点，图就没法看了。
 *
 * 标签怎么来
 * ----------
 * 语义规则分两类，都要能在图上看见：
 * - 标签型（displayMode = label / both）→ 节点上的彩色小标签，颜色取自规则；
 * - 语义型（displayMode = semantic / both）→ 节点上的一条语义标题；
 *   悬浮节点或标签时弹出日志原文，标题就是这句语义。
 */

/** 节点尺寸固定，布局才能算得准；内容超长靠省略号，靠悬浮看原文。 */
const NODE_WIDTH = 236;
const NODE_HEIGHT = 74;
const GAP_X = 92;
const GAP_Y = 18;
const PADDING = 48;
const MIN_ZOOM = 0.25;
const MAX_ZOOM = 2.4;
/** 低于这个缩放，节点里的文字就读不了了；「适应窗口」不会越过它。 */
const READABLE_ZOOM = 0.55;

interface LaidOutNode {
  node: FunctionNode;
  trace: TraceTimeline;
  depth: number;
  x: number;
  y: number;
  /** 该节点整棵子树的最高严重度：决定分支是否「有异常」。 */
  subtreeSeverity: ContentSeverity;
  /** 这个节点**自己**的日志里的异常数：这才是真正需要闪边框的节点。 */
  ownErrorCount: number;
  ownWarningCount: number;
  labels: DisplayRuleMatch[];
  semantic?: DisplayRuleMatch;
  /** 悬浮弹框展示用的代表日志行。 */
  evidenceEntry: LogEntry;
  /** 折叠节点自身的起止时间，作为横轴上的「这一步」。 */
  timeText: string;
  durationText: string;
}

interface FlowMapProps {
  traces: TraceTimeline[];
  rules: readonly DisplayRule[];
  semanticEnabled: boolean;
  /** 用主日志区的 LogRow 渲染原文：样式必须与日志列表完全一致。 */
  renderLogRow: (entry: LogEntry) => ReactNode;
  onSelectNode: (trace: TraceTimeline, node: FunctionNode) => void;
  onSelectEntry?: (entry: LogEntry) => void;
}

const SEVERITY_RANK: Record<ContentSeverity, number> = { normal: 0, warning: 1, error: 2 };

function maxSeverity(values: readonly ContentSeverity[]): ContentSeverity {
  return values.reduce<ContentSeverity>(
    (highest, current) => (SEVERITY_RANK[current] > SEVERITY_RANK[highest] ? current : highest),
    'normal',
  );
}

function functionChildren(node: FunctionNode): FunctionNode[] {
  return node.children.filter((item): item is FunctionNode => item.kind === 'function');
}

function logLeaves(node: FunctionNode): LogLeaf[] {
  return node.children.filter((item): item is LogLeaf => item.kind === 'log');
}

/** 子树最高严重度。与日志列表里的折叠标题用的是同一套判定。 */
function subtreeSeverityOf(node: FunctionNode): ContentSeverity {
  return maxSeverity(node.children.map((child) => (
    child.kind === 'log' ? child.entry.severity : subtreeSeverityOf(child)
  )));
}

function compactTime(value: string): string {
  const text = String(value || '');
  const match = text.match(/(\d{2}:\d{2}:\d{2})(?:\.(\d{1,3}))?/);
  if (!match) return text.slice(0, 12);
  return match[2] ? `${match[1]}.${match[2]}` : match[1];
}

function formatDuration(startNs?: bigint, endNs?: bigint): string {
  if (startNs === undefined || endNs === undefined) return '';
  const delta = Number(endNs - startNs);
  if (!Number.isFinite(delta) || delta < 0) return '';
  if (delta < 1000) return `${delta} ns`;
  if (delta < 1_000_000) return `${(delta / 1000).toFixed(1)} us`;
  if (delta < 1_000_000_000) return `${(delta / 1_000_000).toFixed(1)} ms`;
  return `${(delta / 1_000_000_000).toFixed(2)} s`;
}

/**
 * 一个节点「自己的」标签和语义。
 *
 * 只看直接日志子节点 + 自己的函数名，**不看整棵子树** ——
 * 否则根节点会挂上所有后代的标签，图上一片花花绿绿，反而什么都看不出。
 * 想看某条分支里有什么标签，顺着连线往里看就行。
 */
function nodeDecorations(
  node: FunctionNode,
  rules: readonly DisplayRule[],
  semanticEnabled: boolean,
): { labels: DisplayRuleMatch[]; semantic?: DisplayRuleMatch } {
  if (!semanticEnabled) return { labels: [] };
  const entries = logLeaves(node).map((leaf) => leaf.entry);
  const labels: DisplayRuleMatch[] = [];
  const seen = new Set<string>();
  const pushLabel = (match: DisplayRuleMatch) => {
    const key = `${match.ruleId}|${match.customLabelText}`;
    if (seen.has(key)) return;
    seen.add(key);
    labels.push(match);
  };

  for (const rule of rules) {
    if (!rule.enabled) continue;
    // 只关心带标签的规则：纯语义规则走节点上的语义标题，不作为标签展示。
    if ((rule.displayMode ?? 'semantic') === 'semantic') continue;

    // 1) 关键字命中**函数名** → 标签挂在这个函数节点自己身上。
    if (rule.scope !== 'log' && rule.kind === 'keyword') {
      const keyword = rule.keyword?.trim();
      if (keyword && node.name.includes(keyword)) {
        if (rule.customLabelTemplate?.trim()) {
          pushLabel({
            ruleId: rule.id,
            ruleName: rule.name,
            text: rule.displayTemplate,
            customLabelText: rule.customLabelTemplate.trim(),
            customLabelColor: rule.customLabelColor || '#2563eb',
            parameters: {},
            sourceMessage: node.name,
            sourceEntryId: node.startEntry.id,
          });
        }
        continue;
      }
      // 注意：**不能**因为是「函数范围」就跳过日志行。
      // 现有规则里 scope=function 的 keyword 往往其实是日志正文里的短语
      // （例如「光源功率异常」规则的 keyword 是 illumination source power），
      // 直接跳过会让这类规则的标签在图上完全不出现。这里跟着日志正文走，
      // 标签就落在真正包含这行日志的那个节点上。
    }

    // 2) 否则在**直接**日志行里找。这里不能因为「函数名没命中」就跳过整条规则 ——
    //    大多数标签规则的区分点是日志正文（keyword 往往不在函数名里），
    //    早先的 `continue` 让这类规则的标签在图上完全不出现。
    for (const entry of entries) {
      const match = matchDisplayRuleToMessage(rule, entry.message);
      if (!match?.customLabelText?.trim()) continue;
      pushLabel({ ...match, sourceEntryId: entry.id });
    }
  }

  const semantic = matchDisplayRulesToFunction([...rules], node);
  return { labels: labels.slice(0, 4), semantic };
}

/**
 * 横向流程布局：**只有一个子节点的链路走同一行**，只有分叉才占用新的行。
 *
 * 这一条决定了图能不能看。全流程日志的调用链绝大多数是「一层套一层」的直线
 * （A 调 B，B 调 C…），如果每个节点都占一行，117 个节点就是 117 行 ——
 * 一屏放不下，缩到能放下时每个节点只有十几像素高，等于什么都没画。
 * 让直线段横向排下去、分叉才上下展开之后，纵向高度只与**并行分支数**有关，
 * 形状也就成了真正的流程图：一条主干，旁边岔出支路。
 */
function subtreeRows(node: FunctionNode): number {
  const children = functionChildren(node);
  if (!children.length) return 1;
  if (children.length === 1) return subtreeRows(children[0]);
  return children.reduce((total, child) => total + subtreeRows(child), 0);
}

/**
 * 子树的横向宽度，单位是「列」。
 *
 * 单子节点只往前走一列，多子节点取最宽的那条分支 —— 因为并行的分支共用同一列。
 * 有了它，同一个 Trace 里的**顶层函数就能按时间顺序首尾相接排成一条流水线**：
 * 上一步整棵子树结束后，下一步才开始，这正是「全流程」的读法。
 */
function subtreeColumns(node: FunctionNode): number {
  const children = functionChildren(node);
  if (!children.length) return 1;
  if (children.length === 1) return 1 + subtreeColumns(children[0]);
  return 1 + Math.max(...children.map((child) => subtreeColumns(child)));
}

function layoutTrace(trace: TraceTimeline, rules: readonly DisplayRule[], semanticEnabled: boolean) {
  const roots = trace.items.filter((item): item is FunctionNode => item.kind === 'function');
  const positioned: LaidOutNode[] = [];
  const rowHeight = NODE_HEIGHT + GAP_Y;
  const columnWidth = NODE_WIDTH + GAP_X;

  const place = (node: FunctionNode, column: number, top: number): number => {
    const children = functionChildren(node);
    let y: number;
    if (!children.length) {
      y = top;
    } else if (children.length === 1) {
      // 直线段继续往前走，不占新行。
      y = place(children[0], column + 1, top);
    } else {
      const childYs: number[] = [];
      let cursor = top;
      for (const child of children) {
        childYs.push(place(child, column + 1, cursor));
        cursor += subtreeRows(child) * rowHeight;
      }
      y = (childYs[0] + childYs[childYs.length - 1]) / 2;
    }
    const leaves = logLeaves(node);
    const decorated = nodeDecorations(node, rules, semanticEnabled);
    positioned.push({
      node,
      trace,
      depth: column,
      x: column * columnWidth,
      y,
      subtreeSeverity: subtreeSeverityOf(node),
      ownErrorCount: leaves.filter((leaf) => leaf.entry.severity === 'error').length,
      ownWarningCount: leaves.filter((leaf) => leaf.entry.severity === 'warning').length,
      labels: decorated.labels,
      semantic: decorated.semantic,
      // 只挑一条代表行：异常优先，其次有语义命中的行，最后是第一条。
      evidenceEntry: leaves.find((leaf) => leaf.entry.severity === 'error')?.entry
        ?? leaves[0]?.entry
        ?? node.startEntry,
      timeText: compactTime(node.startEntry.timestamp),
      durationText: formatDuration(
        node.startEntry.timestampNs ?? undefined,
        node.endEntry?.timestampNs ?? undefined,
      ),
    });
    return y;
  };

  // 顶层函数按时间顺序首尾相接：整棵子树走完，下一步才在右边开始。
  let column = 0;
  let rowTop = 0;
  roots.forEach((root) => {
    place(root, column, rowTop);
    column += subtreeColumns(root);
    // 换一个顶层步骤时回到最上面一行，让「并行的阶段」共用同一片纵向空间；
    // 上下文行数因此只与最宽的那一步有关，而不是所有步骤相加。
    rowTop = 0;
  });
  return { positioned, roots };
}

export function FlowMapView({
  traces,
  rules,
  semanticEnabled,
  renderLogRow,
  onSelectNode,
  onSelectEntry,
}: FlowMapProps) {
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: PADDING, y: PADDING });
  const [activeTraceId, setActiveTraceId] = useState<string>();
  const [activeLabel, setActiveLabel] = useState<string>();
  /**
   * 悬浮状态按**节点**记录，而不是按「被悬停的那个元素」。
   *
   * 早先 key 用的是元素 id，于是鼠标从节点移到节点里的标签时会换一个 key，
   * 而弹框只在 key === 节点 id 时渲染 —— 结果「悬浮标签看原文」这个需求
   * 恰好被自己的实现挡掉了：一碰标签弹框就消失。
   */
  const [hover, setHover] = useState<{
    nodeId: string;
    source: 'node' | 'label';
    entry: LogEntry;
    title: string;
    semantic?: string;
    supplemental?: string;
    ruleName?: string;
  }>();
  const dragRef = useRef<{ pointerId: number; startX: number; startY: number; originX: number; originY: number }>();

  const activeTrace = traces.find((trace) => trace.id === activeTraceId) ?? traces[0];

  const layout = useMemo(
    () => (activeTrace ? layoutTrace(activeTrace, rules, semanticEnabled) : { positioned: [], roots: [] }),
    [activeTrace, rules, semanticEnabled],
  );

  /** 画布尺寸：缩放与「适应窗口」都要知道内容有多大。 */
  const canvas = useMemo(() => {
    const maxX = layout.positioned.reduce((widest, item) => Math.max(widest, item.x), 0);
    const maxY = layout.positioned.reduce((deepest, item) => Math.max(deepest, item.y), 0);
    return {
      width: PADDING * 2 + maxX + NODE_WIDTH,
      height: PADDING * 2 + maxY + NODE_HEIGHT,
    };
  }, [layout.positioned]);

  /** 图上出现过的标签，按出现次数排序 —— 这就是「标签导航」的目录。 */
  const labelIndex = useMemo(() => {
    const counts = new Map<string, { text: string; color: string; ruleName: string; count: number }>();
    for (const item of layout.positioned) {
      for (const label of item.labels) {
        const text = label.customLabelText || '';
        if (!text) continue;
        const key = `${label.ruleId}|${text}`;
        const current = counts.get(key);
        if (current) current.count += 1;
        else counts.set(key, { text, color: label.customLabelColor || '#2563eb', ruleName: label.ruleName, count: 1 });
      }
    }
    return [...counts.entries()]
      .map(([key, value]) => ({ key, ...value }))
      .sort((left, right) => right.count - left.count);
  }, [layout.positioned]);

  /** 第一个真正异常的节点 —— 「一眼抓住重点」要能一键跳过去。 */
  const firstError = useMemo(
    () => layout.positioned.find((item) => item.ownErrorCount > 0 || item.subtreeSeverity === 'error'),
    [layout.positioned],
  );

  /**
   * 缩略条按**列**聚合，一列一个刻度。
   *
   * 之前每个节点一个刻度，同一列上的几十个节点挤在同一条竖线上互相遮挡，
   * 点哪个都不准。按列聚合成一个刻度后，横轴与流程的「第几步」严格对应，
   * 颜色取该列最高严重度 —— 缩略条要回答的就是「第几步出的问题」。
   */
  const rulerColumns = useMemo(() => {
    const byColumn = new Map<number, LaidOutNode[]>();
    for (const item of layout.positioned) {
      byColumn.set(item.x, [...(byColumn.get(item.x) ?? []), item]);
    }
    return [...byColumn.entries()]
      .sort((left, right) => left[0] - right[0])
      .map(([x, items], index) => {
        const severity = items.some((item) => item.ownErrorCount > 0)
          ? 'error'
          : maxSeverity(items.map((item) => item.subtreeSeverity));
        const anchor = items.find((item) => item.ownErrorCount > 0) ?? items[0];
        return {
          x,
          index,
          y: anchor.y,
          severity,
          label: items.map((item) => item.node.name).slice(0, 3).join(' / ') + (items.length > 3 ? ` 等 ${items.length} 个` : ''),
        };
      });
  }, [layout.positioned]);

  const summary = useMemo(() => {
    let errors = 0;
    let warnings = 0;
    let normal = 0;
    for (const item of layout.positioned) {
      if (item.ownErrorCount > 0 || item.subtreeSeverity === 'error') errors += 1;
      else if (item.subtreeSeverity === 'warning') warnings += 1;
      else normal += 1;
    }
    return { total: layout.positioned.length, errors, warnings, normal };
  }, [layout.positioned]);

  /** 把某个内容坐标挪到视野中央。 */
  const centerOn = useCallback((x: number, y: number, nextZoom = zoom) => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const rect = viewport.getBoundingClientRect();
    setZoom(nextZoom);
    setPan({
      x: rect.width / 2 - (x + NODE_WIDTH / 2) * nextZoom,
      y: rect.height / 2 - (y + NODE_HEIGHT / 2) * nextZoom,
    });
  }, [zoom]);

  /**
   * 「适应窗口」故意**不允许**缩到看不清。
   *
   * 一张 117 节点的图硬塞进 700px 高，每个节点只剩十几像素 —— 那不是全览，是噪点。
   * 所以这里把缩放钳在可读下限，宁可让用户平移；真想看整体形状用下面的「缩略条」。
   */
  const fitToViewport = useCallback(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const rect = viewport.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const fitted = Math.min((rect.width - 24) / canvas.width, (rect.height - 24) / canvas.height);
    const nextZoom = Math.min(MAX_ZOOM, Math.max(READABLE_ZOOM, fitted));
    const anchor = firstError ?? layout.positioned[0];
    if (anchor) {
      centerOn(anchor.x, anchor.y, nextZoom);
      return;
    }
    setZoom(nextZoom);
    setPan({ x: 12, y: 12 });
  }, [canvas.height, canvas.width, centerOn, firstError, layout.positioned]);

  /** 缩略条 / 全览：这是唯一允许缩到很小的入口，因为它的用途就是看形状。 */
  const fitOverview = useCallback(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const rect = viewport.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const nextZoom = Math.min(
      MAX_ZOOM,
      Math.max(MIN_ZOOM, Math.min((rect.width - 24) / canvas.width, (rect.height - 24) / canvas.height)),
    );
    setZoom(nextZoom);
    setPan({ x: 12, y: Math.max(12, (rect.height - canvas.height * nextZoom) / 2) });
  }, [canvas.height, canvas.width]);

  // 换一条调用链时回到流程起点（或第一个异常），不要停在上一张图的位置。
  useEffect(() => {
    const timer = window.setTimeout(() => {
      const anchor = layout.positioned[0];
      if (anchor) centerOn(anchor.x, anchor.y, 1);
    }, 30);
    return () => window.clearTimeout(timer);
    // centerOn 依赖 zoom，跟着它会把每次缩放都变成「跳回起点」。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTrace?.id, layout.positioned.length]);

  function onWheel(event: React.WheelEvent<HTMLDivElement>) {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const rect = viewport.getBoundingClientRect();
    const nextZoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom * (event.deltaY < 0 ? 1.12 : 0.89)));
    // 以光标为锚点缩放：缩小时不会把用户正在看的地方甩出屏幕。
    const pointerX = event.clientX - rect.left;
    const pointerY = event.clientY - rect.top;
    setPan({
      x: pointerX - ((pointerX - pan.x) / zoom) * nextZoom,
      y: pointerY - ((pointerY - pan.y) / zoom) * nextZoom,
    });
    setZoom(nextZoom);
  }

  function beginPan(event: React.PointerEvent<HTMLDivElement>) {
    if ((event.target as HTMLElement).closest('button')) return;
    dragRef.current = { pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, originX: pan.x, originY: pan.y };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function movePan(event: React.PointerEvent<HTMLDivElement>) {
    const state = dragRef.current;
    if (!state || state.pointerId !== event.pointerId) return;
    setPan({ x: state.originX + event.clientX - state.startX, y: state.originY + event.clientY - state.startY });
  }

  function endPan() {
    dragRef.current = undefined;
  }

  if (!traces.length) {
    return (
      <div className="call-flow-graph-empty">
        <AlertTriangle size={34} />
        <strong>当前范围没有可折叠的调用</strong>
        <span>流程地图按函数折叠结果绘制；可以先关闭函数折叠，或调整筛选条件。</span>
      </div>
    );
  }

  const dimmedByLabel = (item: LaidOutNode) => Boolean(activeLabel)
    && !item.labels.some((label) => `${label.ruleId}|${label.customLabelText}` === activeLabel);

  return (
    <div className="flow-map">
      <header className="flow-map-head">
        <div className="flow-map-summary">
          <strong>流程地图</strong>
          <span className="flow-map-stat total">{summary.total} 个节点</span>
          <span className="flow-map-stat error"><AlertTriangle size={12} />异常 {summary.errors}</span>
          <span className="flow-map-stat warning">警告 {summary.warnings}</span>
          <span className="flow-map-stat normal"><CheckCircle2 size={12} />正常 {summary.normal}</span>
        </div>
        <div className="flow-map-actions">
          <button
            type="button"
            className="flow-map-jump"
            disabled={!firstError}
            onClick={() => firstError && centerOn(firstError.x, firstError.y, 1)}
            title={firstError ? `跳到第一个异常：${firstError.node.name}` : '当前范围没有异常节点'}
          >
            <Crosshair size={13} /> 异常定位
          </button>
          <button type="button" onClick={() => setZoom((value) => Math.min(MAX_ZOOM, value * 1.15))} title="放大"><ZoomIn size={14} /></button>
          <button type="button" onClick={() => setZoom((value) => Math.max(MIN_ZOOM, value * 0.87))} title="缩小"><ZoomOut size={14} /></button>
          <button type="button" onClick={fitToViewport} title="适应窗口（不会缩到看不清，会定位到第一个异常）"><Maximize2 size={14} /></button>
          <button type="button" onClick={fitOverview} title="全览：缩到能看见整张图的形状"><Minus size={14} /></button>
          <span className="flow-map-zoom">{Math.round(zoom * 100)}%</span>
        </div>
      </header>

      <div className="flow-map-filterbar">
        {traces.length > 1 && (
          <label className="flow-map-trace-picker">
            <span>调用链</span>
            <select value={activeTrace?.id} onChange={(event) => { setActiveTraceId(event.target.value); setActiveLabel(undefined); }}>
              {traces.map((trace, index) => (
                <option value={trace.id} key={trace.id}>
                  {`${index + 1}. ${trace.firstFunctionName} · ${trace.components.join(' → ')}`}
                </option>
              ))}
            </select>
          </label>
        )}
        <div className="flow-map-labels" aria-label="标签导航">
          <Tag size={12} />
          {labelIndex.length === 0 && <span className="flow-map-labels-empty">当前范围没有自定义标签；在「设置 → 日志规则 → 语义规则」里给规则配一个标签就会出现在这里。</span>}
          {labelIndex.map((item) => (
            <button
              type="button"
              key={item.key}
              className={`flow-map-label-chip ${activeLabel === item.key ? 'active' : ''}`}
              style={{ '--label-color': item.color } as CSSProperties}
              onClick={() => {
                if (activeLabel === item.key) {
                  setActiveLabel(undefined);
                  return;
                }
                setActiveLabel(item.key);
                // 标签导航：点标签不只是筛选，还要**带你过去** ——
                // 否则在 24 步的流程里用户还得自己找那个节点在哪。
                const target = layout.positioned.find((candidate) => candidate.labels.some(
                  (label) => `${label.ruleId}|${label.customLabelText}` === item.key,
                ));
                if (target) centerOn(target.x, target.y, 1);
              }}
              title={`${item.ruleName} · ${item.text}\n点击筛选并跳到第一个带这个标签的节点（再点一次取消）`}
            >
              {item.text}<em>{item.count}</em>
            </button>
          ))}
          {activeLabel && <button type="button" className="flow-map-label-clear" onClick={() => setActiveLabel(undefined)}>清除筛选</button>}
        </div>
      </div>

      {/* 缩略条：横轴与图的 X 一一对应，颜色就是节点严重度。
          117 个节点平铺着看不到头时，这里是唯一能「一眼看到异常在哪一段」的地方。 */}
      <div className="flow-map-ruler" aria-label="流程缩略条">
        {rulerColumns.map((column) => (
          <button
            type="button"
            key={`ruler-${column.x}`}
            className={`flow-map-ruler-tick severity-${column.severity}`}
            style={{ left: `${(column.x / Math.max(1, canvas.width)) * 100}%` }}
            onClick={() => centerOn(column.x, column.y, 1)}
            title={`第 ${column.index + 1} 步 · ${column.label}`}
          />
        ))}
      </div>

      <div
        className="flow-map-viewport"
        ref={viewportRef}
        onWheel={onWheel}
        onPointerDown={beginPan}
        onPointerMove={movePan}
        onPointerUp={endPan}
        onPointerCancel={endPan}
        data-panning={dragRef.current ? 'true' : undefined}
      >
        <div
          className="flow-map-canvas"
          style={{
            width: canvas.width,
            height: canvas.height,
            transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
          }}
        >
          <svg className="flow-map-edges" width={canvas.width} height={canvas.height} aria-hidden="true">
            {layout.positioned.flatMap((item) => functionChildren(item.node).map((child) => {
              const target = layout.positioned.find((candidate) => candidate.node.id === child.id);
              if (!target) return [];
              const x1 = item.x + NODE_WIDTH;
              const y1 = item.y + NODE_HEIGHT / 2;
              const x2 = target.x;
              const y2 = target.y + NODE_HEIGHT / 2;
              const midX = x1 + (x2 - x1) / 2;
              // 折线（水平 → 垂直 → 水平）：比贝塞尔更接近流程图的读图习惯，
              // 也更容易看出分叉是从哪一层发出的。
              const path = `M ${x1} ${y1} H ${midX} V ${y2} H ${x2}`;
              const tone = target.subtreeSeverity;
              return [<path key={`${item.node.id}-${child.id}`} d={path} className={`flow-map-edge severity-${tone}`} />];
            }))}
          </svg>

          {layout.positioned.map((item) => {
            const tone = item.ownErrorCount > 0 ? 'error' : item.subtreeSeverity;
            const dimmed = dimmedByLabel(item);
            return (
              <div
                key={item.node.id}
                className={`flow-map-node severity-${tone} ${item.subtreeSeverity === 'error' ? 'has-error-path' : ''} ${dimmed ? 'dimmed' : ''}`}
                style={{ left: item.x, top: item.y, width: NODE_WIDTH, height: NODE_HEIGHT }}
                onPointerEnter={() => setHover((current) => current?.nodeId === item.node.id && current.source === 'label' ? current : {
                  nodeId: item.node.id,
                  source: 'node',
                  entry: item.evidenceEntry,
                  title: item.semantic?.text || item.node.name,
                  semantic: item.semantic?.text,
                  supplemental: item.semantic?.supplementalText,
                  ruleName: item.semantic?.ruleName,
                })}
                onPointerLeave={() => setHover((current) => current?.nodeId === item.node.id ? undefined : current)}
              >
                <button
                  type="button"
                  className="flow-map-node-main"
                  onClick={() => onSelectNode(item.trace, item.node)}
                  title={`${item.node.name}\n${item.node.source.raw}\n点击回到日志定位并展开`}
                >
                  <span className="flow-map-node-top">
                    <span className="flow-map-node-name">{item.node.name}</span>
                    {item.node.incomplete && <span className="flow-map-node-flag warning">缺出口</span>}
                    {item.node.repeatCount && item.node.repeatCount > 1 && <span className="flow-map-node-flag repeat">×{item.node.repeatCount}</span>}
                  </span>
                  <span className="flow-map-node-meta">
                    <span className="flow-map-node-component">{item.node.component || '—'}</span>
                    <span className="flow-map-node-time">{item.timeText}</span>
                    {item.durationText && <span className="flow-map-node-duration">{item.durationText}</span>}
                    {item.ownErrorCount > 0 && <span className="flow-map-node-errors">{item.ownErrorCount} 异常</span>}
                    {item.ownErrorCount === 0 && item.ownWarningCount > 0 && <span className="flow-map-node-warns">{item.ownWarningCount} 警告</span>}
                  </span>
                  {item.semantic && <span className="flow-map-node-semantic" title={item.semantic.text}>{item.semantic.text}</span>}
                </button>

                {item.labels.length > 0 && (
                  <span className="flow-map-node-labels">
                    {item.labels.map((label) => (
                      <span
                        key={`${label.ruleId}-${label.customLabelText}`}
                        className="flow-map-node-label"
                        style={{ '--label-color': label.customLabelColor || '#2563eb' } as CSSProperties}
                        onPointerEnter={(event) => {
                          event.stopPropagation();
                          setHover({
                            nodeId: item.node.id,
                            source: 'label',
                            entry: label.sourceEntryId
                              ? functionNodeEntries(item.node).find((entry) => entry.id === label.sourceEntryId) ?? item.evidenceEntry
                              : item.evidenceEntry,
                            title: label.text || label.customLabelText || item.node.name,
                            semantic: label.text,
                            supplemental: label.supplementalText,
                            ruleName: label.ruleName,
                          });
                        }}
                        onPointerLeave={(event) => {
                          event.stopPropagation();
                          // 离开标签但还在节点上：退回节点自己的语义，而不是把弹框关掉。
                          setHover((current) => current?.nodeId === item.node.id && current.source === 'label' ? {
                            nodeId: item.node.id,
                            source: 'node',
                            entry: item.evidenceEntry,
                            title: item.semantic?.text || item.node.name,
                            semantic: item.semantic?.text,
                            supplemental: item.semantic?.supplementalText,
                            ruleName: item.semantic?.ruleName,
                          } : current);
                        }}
                        title={`${label.ruleName}\n${label.text}${label.supplementalText ? `\n补充说明：${label.supplementalText}` : ''}`}
                      >{label.customLabelText}</span>
                    ))}
                  </span>
                )}

                {hover?.nodeId === item.node.id && (
                  <div className="flow-map-popover">
                    <div className="flow-map-popover-head">
                      <Crosshair size={12} />
                      <strong title={hover.title}>{hover.title}</strong>
                    </div>
                    {hover.ruleName && <div className="flow-map-popover-rule">语义规则 · {hover.ruleName}</div>}
                    {hover.supplemental && <div className="flow-map-popover-supplement">{hover.supplemental}</div>}
                    <div className="flow-map-popover-log">{renderLogRow(hover.entry)}</div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>

      <footer className="flow-map-foot">
        <span>滚轮缩放 · 拖拽平移 · 点击节点回到日志定位</span>
        {activeLabel && <span className="flow-map-foot-filter">已按标签筛选，其余节点变淡</span>}
        {onSelectEntry && <span className="flow-map-foot-hint">悬浮节点或标签查看日志原文</span>}
      </footer>
    </div>
  );
}
