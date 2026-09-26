import type {
  FunctionNode,
  LogEntry,
  LogLeaf,
  ProcessTimeline,
  RpcTrace,
  ThreadTimeline,
  TimelineItem,
  TraceTimeline,
} from '../types';
import { foldingPolicy, resolveFoldingBoundaryWithPolicy, type FoldingPolicy, type FoldingRule } from '../rendering/foldingRules';

interface MutableTraceGroup {
  rpc: RpcTrace;
  entries: LogEntry[];
}

export function compareEntries(left: LogEntry, right: LogEntry): number {
  if (left.timestampNs !== undefined && right.timestampNs !== undefined) {
    if (left.timestampNs < right.timestampNs) return -1;
    if (left.timestampNs > right.timestampNs) return 1;
  }
  const fileOrder = left.sourceFile.localeCompare(right.sourceFile);
  if (fileOrder !== 0) return fileOrder;
  return left.lineNumber - right.lineNumber;
}

function traceKey(entry: LogEntry): string {
  if (!entry.rpc.isRpc) return 'local';
  if (entry.rpc.traceId !== '0') return `trace-${entry.rpc.traceId}`;
  return `rpc-${entry.rpc.raw}`;
}

function addItem(roots: TimelineItem[], stack: FunctionNode[], item: TimelineItem): void {
  const parent = stack[stack.length - 1];
  if (parent) parent.children.push(item);
  else roots.push(item);
}

function currentChildren(roots: TimelineItem[], stack: FunctionNode[]): TimelineItem[] {
  return stack[stack.length - 1]?.children ?? roots;
}

function leaf(entry: LogEntry): LogLeaf {
  return {
    kind: 'log',
    id: `leaf-${entry.id}`,
    entry,
  };
}

interface FunctionPairing {
  startToEnd: Map<string, string>;
  endToStart: Map<string, string>;
}

function pairFunctionBoundaries(entries: LogEntry[], policy: FoldingPolicy): FunctionPairing {
  const openByRuleAndName = new Map<string, LogEntry[]>();
  const startToEnd = new Map<string, string>();
  const endToStart = new Map<string, string>();

  entries.forEach((entry) => {
    const boundary = resolveFoldingBoundaryWithPolicy(entry, policy);
    const callName = boundary?.functionName;
    if (!boundary || !callName) return;

    const pairKey = `${boundary.ruleId}::${callName}`;
    if (boundary.marker === 'start') {
      const starts = openByRuleAndName.get(pairKey) ?? [];
      starts.push(entry);
      openByRuleAndName.set(pairKey, starts);
      return;
    }

    if (boundary.marker === 'end') {
      const starts = openByRuleAndName.get(pairKey);
      const startEntry = starts?.pop();
      if (!startEntry) return;

      startToEnd.set(startEntry.id, entry.id);
      endToStart.set(entry.id, startEntry.id);
    }
  });

  return { startToEnd, endToStart };
}

function takeTrailingFunctionLogs(children: TimelineItem[], functionName: string): LogLeaf[] {
  const collected: LogLeaf[] = [];

  while (children.length > 0) {
    const item = children[children.length - 1];

    if (item.kind === 'function' && item.origin === 'consecutive' && item.name === functionName) {
      children.pop();
      const groupedLogs = item.children.filter((child): child is LogLeaf => child.kind === 'log');
      collected.unshift(...groupedLogs);
      continue;
    }

    if (
      item.kind !== 'log' ||
      item.entry.marker !== 'none' ||
      item.entry.functionName !== functionName
    ) {
      break;
    }
    collected.unshift(children.pop() as LogLeaf);
  }

  return collected;
}

function sameExecutionLane(node: FunctionNode, entry: LogEntry): boolean {
  return (
    node.component === entry.component &&
    node.processId === entry.processId &&
    node.threadId === entry.threadId &&
    node.rpc.traceId === entry.rpc.traceId &&
    node.rpc.spanId === entry.rpc.spanId
  );
}

function closeInactiveContextFrames(stack: FunctionNode[], entry: LogEntry): void {
  while (stack.length > 0) {
    const top = stack[stack.length - 1];
    if (top.origin !== 'context') return;
    if (entry.functionName && top.name === entry.functionName && sameExecutionLane(top, entry)) return;
    stack.pop();
  }
}

function touchOpenContextFrames(stack: FunctionNode[], entry: LogEntry): void {
  stack.forEach((node) => {
    if (node.origin === 'context') node.endEntry = entry;
  });
}

function ensureContextContainer(
  roots: TimelineItem[],
  stack: FunctionNode[],
  entry: LogEntry,
): FunctionNode | undefined {
  const ownerName = entry.functionName;
  if (!ownerName) return undefined;

  const top = stack[stack.length - 1];
  if (top && top.name === ownerName && sameExecutionLane(top, entry)) return top;

  const siblings = currentChildren(roots, stack);
  const previous = siblings[siblings.length - 1];
  if (
    previous?.kind === 'function' &&
    previous.origin === 'context' &&
    previous.name === ownerName &&
    sameExecutionLane(previous, entry)
  ) {
    previous.endEntry = entry;
    stack.push(previous);
    return previous;
  }

  const contextNode: FunctionNode = {
    kind: 'function',
    origin: 'context',
    id: `context-${entry.id}`,
    name: ownerName,
    component: entry.component,
    processId: entry.processId,
    threadId: entry.threadId,
    rpc: entry.rpc,
    source: entry.source,
    startEntry: entry,
    endEntry: entry,
    children: [],
    incomplete: false,
  };
  addItem(roots, stack, contextNode);
  stack.push(contextNode);
  return contextNode;
}

/**
 * 没有 >()/ <() 边界的普通日志，只要正文以 FunctionName() 开头，
 * 就把同一执行泳道中连续出现的同名日志折叠成一个轻量函数组。
 */
function addConsecutiveFunctionLog(
  roots: TimelineItem[],
  stack: FunctionNode[],
  entry: LogEntry,
  allowConsecutiveGrouping: boolean,
  allowBoundaryContinuation: boolean,
): void {
  if (!entry.functionName) {
    addItem(roots, stack, leaf(entry));
    return;
  }

  const siblings = currentChildren(roots, stack);
  const previous = siblings[siblings.length - 1];
  if (
    previous?.kind === 'function' &&
    previous.name === entry.functionName &&
    sameExecutionLane(previous, entry)
  ) {
    if (allowConsecutiveGrouping && previous.origin === 'consecutive') {
      previous.children.push(leaf(entry));
      previous.endEntry = entry;
      return;
    }

    // 显式入口/出口已经闭合后，如果紧接着仍然是同一执行泳道、同名函数的普通日志，
    // 这些日志仍属于同一段函数流程，继续收进前一个边界函数卡片。
    // endEntry 保留真实 <() 出口，不用尾随普通日志改写函数耗时。
    if (allowBoundaryContinuation && previous.origin === 'boundary' && previous.endEntry && !previous.incomplete) {
      previous.children.push(leaf(entry));
      return;
    }
  }

  if (!allowConsecutiveGrouping) {
    addItem(roots, stack, leaf(entry));
    return;
  }

  const node: FunctionNode = {
    kind: 'function',
    origin: 'consecutive',
    id: `group-${entry.id}`,
    name: entry.functionName,
    component: entry.component,
    processId: entry.processId,
    threadId: entry.threadId,
    rpc: entry.rpc,
    source: entry.source,
    startEntry: entry,
    endEntry: entry,
    children: [leaf(entry)],
    incomplete: false,
  };
  addItem(roots, stack, node);
}

function buildTreeRaw(entries: LogEntry[], foldingRules: readonly FoldingRule[]): TimelineItem[] {
  const roots: TimelineItem[] = [];
  const stack: FunctionNode[] = [];
  const policy = foldingPolicy(foldingRules);
  const pairing = pairFunctionBoundaries(entries, policy);
  let danglingNode: FunctionNode | undefined;

  entries.forEach((entry) => {
    closeInactiveContextFrames(stack, entry);

    const boundary = resolveFoldingBoundaryWithPolicy(entry, policy);
    const callName = boundary?.functionName;
    const delegatedBoundary = Boolean(
      boundary &&
      entry.functionName &&
      callName &&
      callName !== entry.functionName,
    );

    if (boundary?.marker === 'start' && callName) {
      danglingNode = undefined;

      if (delegatedBoundary) ensureContextContainer(roots, stack, entry);
      touchOpenContextFrames(stack, entry);

      const siblings = currentChildren(roots, stack);
      const preambleLogs = policy.trailing ? takeTrailingFunctionLogs(siblings, callName) : [];
      const hasMatchingEnd = pairing.startToEnd.has(entry.id);
      const node: FunctionNode = {
        kind: 'function',
        origin: 'boundary',
        id: `fn-${entry.id}`,
        name: callName,
        component: entry.component,
        processId: entry.processId,
        threadId: entry.threadId,
        rpc: entry.rpc,
        source: entry.source,
        startEntry: entry,
        // 入口行**不再**作为子节点重复一遍：展开时卡片自己就是入口日志的形态
        //（原来展开后第一行总是和卡片一模一样，只是多了详情，白白多一行）。
        children: [...preambleLogs],
        incomplete: !hasMatchingEnd,
        foldRuleId: boundary.ruleId,
        foldRuleName: boundary.ruleName,
      };

      addItem(roots, stack, node);

      if (hasMatchingEnd) stack.push(node);
      else danglingNode = node;
      return;
    }

    if (boundary?.marker === 'end' && callName) {
      danglingNode = undefined;
      touchOpenContextFrames(stack, entry);
      const pairedStartId = pairing.endToStart.get(entry.id);
      let matchIndex = -1;

      if (pairedStartId) {
        const expectedNodeId = `fn-${pairedStartId}`;
        for (let index = stack.length - 1; index >= 0; index -= 1) {
          if (stack[index].id === expectedNodeId) {
            matchIndex = index;
            break;
          }
        }
      }

      if (matchIndex >= 0) {
        const matchedNode = stack[matchIndex];
        matchedNode.children.push(leaf(entry));
        matchedNode.endEntry = entry;
        matchedNode.incomplete = false;

        for (let index = stack.length - 1; index > matchIndex; index -= 1) {
          if (stack[index].origin === 'boundary') stack[index].incomplete = true;
        }
        stack.splice(matchIndex);
      } else {
        addItem(roots, stack, leaf(entry));
      }
      return;
    }

    touchOpenContextFrames(stack, entry);

    if (danglingNode && entry.functionName === danglingNode.name) {
      danglingNode.children.push(leaf(entry));
      return;
    }

    if (danglingNode) danglingNode = undefined;

    if (entry.functionName) {
      for (let index = stack.length - 1; index >= 0; index -= 1) {
        if (stack[index].name === entry.functionName) {
          stack[index].children.push(leaf(entry));
          return;
        }
      }
    }

    addConsecutiveFunctionLog(roots, stack, entry, policy.consecutive, policy.trailing);
  });

  return roots;
}

function repeatableBoundaryNode(item: TimelineItem): item is FunctionNode {
  return item.kind === 'function' && (item.origin === 'boundary' || item.origin === 'repeated') && !item.incomplete && Boolean(item.endEntry);
}

function sameFunctionExecutionLane(left: FunctionNode, right: FunctionNode): boolean {
  // 连续重复调用允许每次生成新的 Trace/Span；真正需要固定的是物理执行通道。
  return left.name === right.name
    && left.component === right.component
    && left.processId === right.processId
    && left.threadId === right.threadId;
}

function sameRepeatedBoundaryPattern(left: FunctionNode, right: FunctionNode): boolean {
  return sameFunctionExecutionLane(left, right)
    && (left.foldRuleId ?? 'builtin-explicit-boundary') === (right.foldRuleId ?? 'builtin-explicit-boundary')
    && left.startEntry.component === right.startEntry.component
    && left.startEntry.mode === right.startEntry.mode
    && left.endEntry?.component === right.endEntry?.component
    && left.endEntry?.mode === right.endEntry?.mode;
}

function repeatedCallCount(node: FunctionNode): number {
  return node.origin === 'repeated' ? (node.repeatCount ?? node.children.length) : 1;
}

function repeatedCallChildren(node: FunctionNode): TimelineItem[] {
  return node.origin === 'repeated' ? node.children : [node];
}

/**
 * 连续出现、物理执行通道相同，且入口/出口边界规则一致的完整函数调用，只在展示层再收纳一层。
 * Trace/Span 可以随每次调用变化；只要中间插入其它函数/日志，或者入口/出口内容发生变化，就立即终止 ×N 聚合。
 * 每一次真实 boundary 节点仍作为 children 保留，因此入口/出口、异常定位与单次调用边界不会丢失。
 */
export function mergeRepeatedFunctionGroups(items: TimelineItem[], foldingRules: readonly FoldingRule[] = []): TimelineItem[] {
  const normalizedRules = foldingRules.length ? foldingRules : undefined;
  const normalized = items.map((item): TimelineItem => (
    item.kind === 'function'
      ? { ...item, children: mergeRepeatedFunctionGroups(item.children, foldingRules) }
      : item
  ));
  if (normalizedRules && !foldingPolicy(normalizedRules).repeated) return normalized;

  const merged: TimelineItem[] = [];
  normalized.forEach((item) => {
    if (!repeatableBoundaryNode(item)) {
      merged.push(item);
      return;
    }

    const previous = merged[merged.length - 1];
    if (!previous || !repeatableBoundaryNode(previous) || !sameRepeatedBoundaryPattern(previous, item)) {
      merged.push(item);
      return;
    }

    if (previous.origin === 'repeated') {
      previous.children.push(...repeatedCallChildren(item));
      previous.endEntry = item.endEntry;
      previous.repeatCount = repeatedCallCount(previous) + repeatedCallCount(item);
      return;
    }

    const group: FunctionNode = {
      kind: 'function',
      origin: 'repeated',
      id: `repeat-${previous.id}-${item.id}`,
      name: previous.name,
      component: previous.component,
      processId: previous.processId,
      threadId: previous.threadId,
      rpc: previous.rpc,
      source: previous.source,
      startEntry: previous.startEntry,
      endEntry: item.endEntry,
      children: [...repeatedCallChildren(previous), ...repeatedCallChildren(item)],
      incomplete: false,
      repeatCount: repeatedCallCount(previous) + repeatedCallCount(item),
      foldRuleId: previous.foldRuleId,
      foldRuleName: previous.foldRuleName,
    };
    merged[merged.length - 1] = group;
  });

  return merged;
}

export function buildTree(entries: LogEntry[], foldingRules: readonly FoldingRule[] = []): TimelineItem[] {
  return mergeRepeatedFunctionGroups(buildTreeRaw(entries, foldingRules), foldingRules);
}

function firstFunctionName(items: TimelineItem[], entries: LogEntry[]): string {
  const ordered = [...items].sort(compareTimelineItems);
  const firstFunction = ordered.find((item): item is FunctionNode => item.kind === 'function');
  if (firstFunction) return firstFunction.name;
  return entries.find((entry) => entry.functionName)?.functionName ?? '无函数入口';
}

function itemStartEntry(item: TimelineItem): LogEntry {
  return item.kind === 'function' ? item.startEntry : item.entry;
}

function compareTimelineItems(left: TimelineItem, right: TimelineItem): number {
  return compareEntries(itemStartEntry(left), itemStartEntry(right));
}

function collectFunctionNodes(items: TimelineItem[], target: FunctionNode[] = []): FunctionNode[] {
  items.forEach((item) => {
    if (item.kind !== 'function') return;
    target.push(item);
    collectFunctionNodes(item.children, target);
  });
  return target;
}

function functionContainsEntry(parent: FunctionNode, child: FunctionNode): boolean {
  const parentStart = parent.startEntry.timestampNs;
  const childStart = child.startEntry.timestampNs;
  if (parentStart !== undefined && childStart !== undefined && parentStart > childStart) return false;

  const parentEnd = parent.endEntry?.timestampNs;
  if (parentEnd !== undefined && childStart !== undefined && parentEnd < childStart) return false;
  return true;
}

function attachCrossComponentSpanChildren(laneItems: TimelineItem[][]): TimelineItem[] {
  const roots = laneItems.flat();
  const allFunctions = collectFunctionNodes(roots);
  const attachedRootIds = new Set<string>();
  const rootFunctions = roots.filter((item): item is FunctionNode => item.kind === 'function');

  rootFunctions
    .slice()
    .sort(compareTimelineItems)
    .forEach((child) => {
      const fatherSpanId = child.rpc.fatherSpanId;
      if (!fatherSpanId || fatherSpanId === '0') return;

      const parent = allFunctions
        .filter((candidate) => (
          candidate.id !== child.id &&
          candidate.rpc.spanId === fatherSpanId &&
          functionContainsEntry(candidate, child)
        ))
        .sort((left, right) => compareEntries(right.startEntry, left.startEntry))[0];

      if (!parent) return;
      parent.children.push(child);
      parent.children.sort(compareTimelineItems);
      attachedRootIds.add(child.id);
    });

  return roots
    .filter((item) => item.kind !== 'function' || !attachedRootIds.has(item.id))
    .sort(compareTimelineItems);
}

function uniqueSorted(values: string[]): string[] {
  return Array.from(new Set(values)).sort((left, right) => left.localeCompare(right));
}

function createTraceTimeline(
  id: string,
  key: string,
  group: MutableTraceGroup,
  items: TimelineItem[],
  crossComponent: boolean,
  participantCount: number,
): TraceTimeline {
  const sortedEntries = [...group.entries].sort(compareEntries);
  return {
    id,
    traceKey: key,
    rpc: group.rpc,
    firstFunctionName: firstFunctionName(items, sortedEntries),
    entries: sortedEntries,
    items,
    components: uniqueSorted(sortedEntries.map((entry) => entry.component)),
    sourceFiles: uniqueSorted(sortedEntries.map((entry) => entry.sourceFile)),
    participantCount,
    crossComponent,
  };
}

export function buildProcessTimelines(entries: LogEntry[], foldingRules: readonly FoldingRule[] = []): ProcessTimeline[] {
  interface MutableProcessGroup {
    component: string;
    processId: string;
    threads: Map<string, Map<string, MutableTraceGroup>>;
  }

  const processMap = new Map<string, MutableProcessGroup>();

  entries.forEach((entry) => {
    // 不同组件可能出现相同的 PID/TID 数值，必须以 component + PID 隔离进程。
    const processKey = `${entry.component}|${entry.processId}`;
    if (!processMap.has(processKey)) {
      processMap.set(processKey, {
        component: entry.component,
        processId: entry.processId,
        threads: new Map(),
      });
    }
    const processGroup = processMap.get(processKey)!;
    if (!processGroup.threads.has(entry.threadId)) processGroup.threads.set(entry.threadId, new Map());
    const traceMap = processGroup.threads.get(entry.threadId)!;
    const key = traceKey(entry);

    if (!traceMap.has(key)) traceMap.set(key, { rpc: entry.rpc, entries: [] });
    traceMap.get(key)!.entries.push(entry);
  });

  return Array.from(processMap.values()).map(({ component, processId, threads: threadMap }) => {
    const threads: ThreadTimeline[] = Array.from(threadMap.entries()).map(([threadId, traceMap]) => {
      const traces: TraceTimeline[] = Array.from(traceMap.entries()).map(([key, group]) => {
        const sortedEntries = [...group.entries].sort(compareEntries);
        return createTraceTimeline(
          `trace-${component}-${processId}-${threadId}-${key}`,
          key,
          group,
          buildTree(sortedEntries, foldingRules),
          false,
          1,
        );
      });

      return {
        id: `thread-${component}-${processId}-${threadId}`,
        component,
        processId,
        threadId,
        traces,
        entryCount: traces.reduce((sum, trace) => sum + trace.entries.length, 0),
      };
    });

    return {
      id: `process-${component}-${processId}`,
      component,
      processId,
      threads,
      entryCount: threads.reduce((sum, thread) => sum + thread.entryCount, 0),
    };
  });
}

/**
 * 将多个日志文件中相同 TraceID 的 RPC 日志合并为一条跨组件时间线。
 * 每个 component + PID + TID 是一条独立执行泳道：泳道内部建立函数树，
 * 各泳道的根节点再按全局时间排序，既能跨组件还原顺序，又不会伪造跨进程调用栈嵌套。
 */
export function buildCrossComponentTraces(entries: LogEntry[], foldingRules: readonly FoldingRule[] = []): TraceTimeline[] {
  const traceGroups = new Map<string, MutableTraceGroup>();

  entries.forEach((entry) => {
    if (!entry.rpc.isRpc || entry.rpc.traceId === '0') return;
    const key = entry.rpc.traceId;
    if (!traceGroups.has(key)) traceGroups.set(key, { rpc: entry.rpc, entries: [] });
    traceGroups.get(key)!.entries.push(entry);
  });

  return Array.from(traceGroups.entries())
    .map(([traceId, group]) => {
      const laneMap = new Map<string, LogEntry[]>();
      group.entries.forEach((entry) => {
        const laneKey = `${entry.component}|${entry.processId}|${entry.threadId}`;
        const laneEntries = laneMap.get(laneKey) ?? [];
        laneEntries.push(entry);
        laneMap.set(laneKey, laneEntries);
      });

      const laneItems = Array.from(laneMap.values())
        .map((laneEntries) => buildTreeRaw([...laneEntries].sort(compareEntries), foldingRules));
      const mergedItems = mergeRepeatedFunctionGroups(attachCrossComponentSpanChildren(laneItems), foldingRules);

      return createTraceTimeline(
        `cross-trace-${traceId}`,
        `trace-${traceId}`,
        group,
        mergedItems,
        true,
        laneMap.size,
      );
    })
    .sort((left, right) => {
      const leftEntry = left.entries[0];
      const rightEntry = right.entries[0];
      return leftEntry && rightEntry ? compareEntries(leftEntry, rightEntry) : 0;
    });
}

/**
 * 跨组件的函数折叠：把「落在别的函数时间范围内」的折叠函数收进去。
 *
 * 背景：不同组件 / 不同线程之间没有能对齐的 traceId，唯一可用的关联是**时间** ——
 * 某个折叠函数的起止时间整体落在外层函数范围内，它就是这段流程的一部分。
 * 按字节去逐个时间窗查找代价很高，这里直接用已有的 start/end 时间戳做区间包含，
 * 一次遍历（输入已按时间排序）即可完成，不做任何额外日志读取。
 *
 * 只收拢**折叠函数**节点（连同它自己的子节点一起搬进去），普通日志行保持原位，
 * 避免把无关行吞进别的组件的卡片里。
 *
 * 返回的是浅拷贝的新树：原节点可能同时被时间线 / Gantt 等视图引用，不能就地改。
 */
export function nestFunctionsByTimeSpan(items: readonly TimelineItem[]): TimelineItem[] {
  const startNsOf = (item: TimelineItem): bigint | undefined => (
    item.kind === 'function' ? item.startEntry.timestampNs : item.entry.timestampNs
  );
  const endNsOf = (node: FunctionNode): bigint | undefined => (
    node.endEntry?.timestampNs ?? node.startEntry.timestampNs
  );
  const roots: TimelineItem[] = [];
  const open: FunctionNode[] = [];
  const copyOf = (node: FunctionNode): FunctionNode => ({ ...node, children: [...node.children] });

  for (const item of items) {
    const start = startNsOf(item);
    // 关掉所有已经结束、且结束时间早于当前项的容器。
    while (open.length) {
      const top = open[open.length - 1];
      const end = endNsOf(top);
      if (end === undefined || start === undefined || end >= start) break;
      open.pop();
    }
    if (item.kind !== 'function') { roots.push(item); continue; }
    const end = endNsOf(item);
    const parent = open[open.length - 1];
    const parentEnd = parent ? endNsOf(parent) : undefined;
    if (parent && end !== undefined && (parentEnd === undefined || end <= parentEnd)) {
      const copy = copyOf(item);
      parent.children = [...parent.children, copy];
      open.push(copy);
      continue;
    }
    const copy = copyOf(item);
    roots.push(copy);
    if (end !== undefined) open.push(copy);
  }
  return roots;
}

export function durationNs(node: FunctionNode): bigint | undefined {
  const start = node.startEntry.timestampNs;
  const end = node.endEntry?.timestampNs;
  if (start === undefined || end === undefined || end < start) return undefined;
  return end - start;
}

export function formatDuration(value?: bigint): string {
  if (value === undefined) return '—';
  if (value < 1_000n) return `${value} ns`;
  if (value < 1_000_000n) return `${Number(value) / 1_000} μs`;
  if (value < 1_000_000_000n) return `${Number(value) / 1_000_000} ms`;
  return `${Number(value) / 1_000_000_000} s`;
}
