import { useEffect, useMemo, useRef, useState } from 'react';
import { LoaderCircle, Minus, Plus, Search, X, Zap } from 'lucide-react';
import { fetchLogWindow, type LogSearchProgress, type LogWindowRequest } from '../api/resourceApi';
import type { LogEntry, LogFormatParserRuleConfig } from '../types';
import {
  createEventRootCauseResolver,
  eventComponent,
  eventDisplayCode,
  normalizedEventLevel,
  parseEventRestoreStream,
  type EventCauseNode,
  type EventRestoreLevel,
} from '../rendering/eventRestore';

interface Props {
  open: boolean;
  environmentId?: number;
  environmentName?: string;
  startTime: string;
  endTime: string;
  formatRules: readonly LogFormatParserRuleConfig[];
  sourceEntries?: readonly LogEntry[];
  onClose: () => void;
}

const DEFAULT_LEVELS = new Set<EventRestoreLevel>(['EVENT', 'ERROR']);
const LEVEL_ORDER: EventRestoreLevel[] = ['ERROR', 'EVENT', 'WARNING', 'OTHER'];
const LEVEL_LABEL: Record<EventRestoreLevel, string> = { ERROR: 'ERROR', EVENT: 'EVENT', WARNING: 'WARNING', OTHER: 'OTHER' };

function localInputValue(value: string): string {
  const text = String(value || '').trim().replace(' ', 'T');
  const match = text.match(/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})(?::(\d{2}))?(?:\.(\d{1,3}))?/);
  if (!match) return text;
  return `${match[1]}:${match[2] || '00'}${match[3] ? `.${match[3].padEnd(3, '0')}` : ''}`;
}

function apiDateValue(value: string): string {
  return String(value || '').trim().replace(' ', 'T');
}

function entryWithinRange(entry: LogEntry, startTime: string, endTime: string): boolean {
  if (!entry.timestamp) return true;
  const entryMs = Date.parse(String(entry.timestamp).trim().replace(' ', 'T'));
  const startMs = Date.parse(apiDateValue(startTime));
  const endMs = Date.parse(apiDateValue(endTime));
  if (!Number.isFinite(entryMs) || !Number.isFinite(startMs) || !Number.isFinite(endMs)) return true;
  return entryMs >= startMs && entryMs <= endMs;
}

function occurrenceTime(entry: LogEntry): string {
  return entry.timestamp || '—';
}

function eventDescription(entry: LogEntry): string {
  return entry.message?.trim() || entry.summary?.trim() || entry.raw;
}

function eventSearchHaystack(entry: LogEntry): string {
  const event = entry.runEvent;
  return [
    entry.timestamp,
    entry.component,
    entry.logSubsystem,
    entry.logModule,
    event?.eventLevel,
    event?.currentEventCode,
    event?.linkedEventCodes.join(' '),
    event?.displayCode,
    event?.linkedDisplayCodes.join(' '),
    event?.currentErrIId,
    event?.linkedErrIIds.join(' '),
    entry.message,
  ].filter(Boolean).join(' ').toLowerCase();
}

function LevelCell({ entry }: { entry: LogEntry }) {
  const normalized = normalizedEventLevel(entry);
  const raw = String(entry.runEvent?.eventLevel || entry.level || normalized).trim().toUpperCase();
  if (normalized === 'ERROR') {
    return <span className="event-restore-error-level"><span className="event-restore-lightning"><Zap size={13} fill="currentColor" /></span><strong>{raw || 'ERROR'}</strong></span>;
  }
  return <span className={`event-restore-level level-${normalized.toLowerCase()}`}>{raw || normalized}</span>;
}

function CauseRows({
  rootId,
  nodes,
  expandedCauseKeys,
  onToggle,
}: {
  rootId: string;
  nodes: EventCauseNode[];
  expandedCauseKeys: Set<string>;
  onToggle: (key: string) => void;
}) {
  return <>{nodes.flatMap((node) => {
    const key = `${rootId}:${node.key}`;
    const expanded = expandedCauseKeys.has(key);
    const hasChildren = node.children.length > 0;
    const level = normalizedEventLevel(node.entry);
    const row = <tr className={`event-restore-cause-row ${expanded && hasChildren ? 'is-expanded' : ''} ${level === 'ERROR' ? 'is-error' : ''} ${!hasChildren ? 'is-leaf' : ''}`} key={key}>
      <td colSpan={6}>
        <div
          className="event-restore-cause-log"
          style={{ marginLeft: `${48 + Math.max(0, node.depth - 1) * 32}px` }}
        >
          <span className="event-restore-cause-branch" aria-hidden="true">└─</span>
          {hasChildren ? <button type="button" className="event-restore-tree-toggle cause" onClick={() => onToggle(key)} aria-label={expanded ? '折叠下层根因' : '展开下层根因'}>{expanded ? <Minus size={13}/> : <Plus size={13}/>}</button> : <span className="event-restore-tree-spacer"/>}
          <span className="event-cause-time">{occurrenceTime(node.entry)}</span>
          <strong className="event-cause-component">{eventComponent(node.entry)}</strong>
          <strong className="event-cause-code">{eventDisplayCode(node.entry) || node.entry.runEvent?.currentEventCode || '—'}</strong>
          <span className="event-cause-message">{eventDescription(node.entry)}</span>
        </div>
      </td>
    </tr>;
    if (!expanded || !hasChildren) return [row];
    return [row, <CauseRows key={`${key}:children`} rootId={rootId} nodes={node.children} expandedCauseKeys={expandedCauseKeys} onToggle={onToggle}/>];
  })}</>;
}


export function EventRootCauseTable({ entries }: { entries: LogEntry[] }) {
  const resolver = useMemo(() => createEventRootCauseResolver(entries), [entries]);
  const [expandedRoots, setExpandedRoots] = useState<Set<string>>(new Set());
  const [expandedCauseKeys, setExpandedCauseKeys] = useState<Set<string>>(new Set());
  const collectCauseKeys = (rootId: string, nodes: EventCauseNode[], target: Set<string>) => {
    nodes.forEach((node) => {
      const key = `${rootId}:${node.key}`;
      target.add(key);
      if (node.children.length) collectCauseKeys(rootId, node.children, target);
    });
  };
  const toggleRoot = (id: string) => {
    const expanded = expandedRoots.has(id);
    setExpandedRoots((current) => {
      const next = new Set(current);
      expanded ? next.delete(id) : next.add(id);
      return next;
    });
    if (!expanded) {
      const root = entries.find((entry) => entry.id === id);
      if (root) {
        const children = resolver.children(root);
        setExpandedCauseKeys((current) => {
          const next = new Set(current);
          collectCauseKeys(id, children, next);
          return next;
        });
      }
    }
  };
  const toggleCause = (key: string) => {
    const expanded = expandedCauseKeys.has(key);
    if (expanded) {
      setExpandedCauseKeys((current) => { const next = new Set(current); next.delete(key); return next; });
      return;
    }
    const separator = key.indexOf(':');
    const rootId = separator >= 0 ? key.slice(0, separator) : key;
    const nodeKey = separator >= 0 ? key.slice(separator + 1) : key;
    const root = entries.find((entry) => entry.id === rootId);
    if (!root) return;
    const findNode = (nodes: EventCauseNode[]): EventCauseNode | undefined => {
      for (const node of nodes) {
        if (node.key === nodeKey) return node;
        const found = findNode(node.children);
        if (found) return found;
      }
      return undefined;
    };
    const node = findNode(resolver.children(root));
    setExpandedCauseKeys((current) => {
      const next = new Set(current);
      next.add(key);
      if (node) collectCauseKeys(rootId, node.children, next);
      return next;
    });
  };
  return <div className="event-restore-table-wrap">
    <table className="event-restore-table">
      <thead><tr><th className="tree-col" aria-label="根因树"></th><th>事件级别</th><th>发生时间</th><th>事件码所属组件</th><th>当前异常 DisplayCode</th><th>事件描述</th></tr></thead>
      <tbody>
        {entries.map((entry) => {
          const hasChildren = resolver.hasChildren(entry);
          const expanded = expandedRoots.has(entry.id);
          const children = expanded ? resolver.children(entry) : [];
          return [
            <tr className={`event-restore-main-row ${expanded ? 'is-expanded' : ''} ${normalizedEventLevel(entry) === 'ERROR' ? 'is-error' : ''}`} key={entry.id}>
              <td className="tree-col">{hasChildren ? <button type="button" className="event-restore-tree-toggle" onClick={() => toggleRoot(entry.id)} aria-label={expanded ? '折叠根因树' : '展开根因树'}>{expanded ? <Minus size={14}/> : <Plus size={14}/>}</button> : null}</td>
              <td><LevelCell entry={entry}/></td>
              <td className="event-time-cell">{occurrenceTime(entry)}</td>
              <td><strong className="event-component-cell">{eventComponent(entry)}</strong></td>
              <td><code className="event-display-code">{eventDisplayCode(entry) || '—'}</code></td>
              <td className="event-description-cell" title={eventDescription(entry)}>{eventDescription(entry)}</td>
            </tr>,
            ...(expanded && children.length ? [<CauseRows key={`${entry.id}:causes`} rootId={entry.id} nodes={children} expandedCauseKeys={expandedCauseKeys} onToggle={toggleCause}/>] : []),
          ];
        })}
        {!entries.length && <tr><td colSpan={6}><div className="event-restore-empty">当前条件下没有可展示的 EVENT / ERROR 事件。</div></td></tr>}
      </tbody>
    </table>
  </div>;
}

export function EventRestoreDialog(props: Props) {
  const [startTime, setStartTime] = useState(localInputValue(props.startTime));
  const [endTime, setEndTime] = useState(localInputValue(props.endTime));
  const [keyword, setKeyword] = useState('');
  const [selectedLevels, setSelectedLevels] = useState<Set<EventRestoreLevel>>(new Set(DEFAULT_LEVELS));
  const [selectedComponents, setSelectedComponents] = useState<Set<string>>(new Set());
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [progress, setProgress] = useState<LogSearchProgress>();
  const [expandedRoots, setExpandedRoots] = useState<Set<string>>(new Set());
  const [expandedCauseKeys, setExpandedCauseKeys] = useState<Set<string>>(new Set());
  const abortRef = useRef<AbortController>();

  const components = useMemo(() => Array.from(new Set(entries.map(eventComponent))).sort((a, b) => a.localeCompare(b)), [entries]);
  const errorComponents = useMemo(() => new Set(entries.filter((entry) => normalizedEventLevel(entry) === 'ERROR').map(eventComponent)), [entries]);
  const resolver = useMemo(() => createEventRootCauseResolver(entries), [entries]);

  const filteredEntries = useMemo(() => {
    const query = keyword.trim().toLowerCase();
    return entries
      .filter((entry) => selectedLevels.has(normalizedEventLevel(entry)))
      .filter((entry) => selectedComponents.size === 0 || selectedComponents.has(eventComponent(entry)))
      .filter((entry) => !query || eventSearchHaystack(entry).includes(query))
      .sort((a, b) => {
        if (a.timestampNs !== undefined && b.timestampNs !== undefined) {
          if (a.timestampNs > b.timestampNs) return -1;
          if (a.timestampNs < b.timestampNs) return 1;
        }
        return b.timestamp.localeCompare(a.timestamp);
      });
  }, [entries, keyword, selectedComponents, selectedLevels]);

  const applyEntries = (parsed: LogEntry[], resetComponentDefault = false) => {
    setEntries(parsed);
    const nextComponents = Array.from(new Set(parsed.map(eventComponent)));
    const nextErrorComponents = new Set(parsed.filter((entry) => normalizedEventLevel(entry) === 'ERROR').map(eventComponent));
    setSelectedComponents((current) => {
      if (resetComponentDefault || current.size === 0) return nextErrorComponents.size ? nextErrorComponents : new Set(nextComponents);
      const kept = new Set(Array.from(current).filter((item) => nextComponents.includes(item)));
      return kept.size ? kept : (nextErrorComponents.size ? nextErrorComponents : new Set(nextComponents));
    });
  };

  const runSearch = async (resetComponentDefault = false, rangeStart = startTime, rangeEnd = endTime) => {
    if (!rangeStart || !rangeEnd) return;
    if (props.sourceEntries) {
      setLoading(false);
      setError('');
      setProgress(undefined);
      setExpandedRoots(new Set());
      setExpandedCauseKeys(new Set());
      applyEntries(props.sourceEntries.filter((entry) => entryWithinRange(entry, rangeStart, rangeEnd)).map((entry) => ({ ...entry })), resetComponentDefault);
      return;
    }
    if (!props.environmentId) return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setLoading(true);
    setError('');
    setProgress(undefined);
    setExpandedRoots(new Set());
    setExpandedCauseKeys(new Set());
    try {
      const request: LogWindowRequest = {
        start_time: apiDateValue(rangeStart),
        end_time: apiDateValue(rangeEnd),
        source_categories: ['run'],
        subsystems: [],
        fms: [],
        fm_targets: [],
        keyword: '',
      };
      const { blob } = await fetchLogWindow(props.environmentId, request, {
        signal: controller.signal,
        onProgress: setProgress,
      });
      const parsed = parseEventRestoreStream(await blob.text(), `event-restore-${props.environmentId}-${Date.now()}`, props.formatRules);
      if (controller.signal.aborted) return;
      applyEntries(parsed, resetComponentDefault);
    } catch (exc) {
      if (!controller.signal.aborted) setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      if (abortRef.current === controller) abortRef.current = undefined;
      if (!controller.signal.aborted) setLoading(false);
    }
  };

  useEffect(() => {
    if (!props.open) {
      abortRef.current?.abort();
      setLoading(false);
      return;
    }
    const initialStart = localInputValue(props.startTime);
    const initialEnd = localInputValue(props.endTime);
    setStartTime(initialStart);
    setEndTime(initialEnd);
    setKeyword('');
    setSelectedLevels(new Set(DEFAULT_LEVELS));
    setEntries([]);
    setSelectedComponents(new Set());
    setExpandedRoots(new Set());
    setExpandedCauseKeys(new Set());
    setError('');
    setProgress(undefined);
    if ((!props.environmentId && !props.sourceEntries) || !initialStart || !initialEnd) return;
    const timer = window.setTimeout(() => {
      // 点击根因树后无需用户再补一次操作：按当前筛选时间范围自动读取 event.log。
      void runSearch(true, initialStart, initialEnd);
    }, 0);
    return () => window.clearTimeout(timer);
    // runSearch intentionally uses the freshly reset local range values.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.open, props.environmentId, props.sourceEntries, props.startTime, props.endTime]);

  useEffect(() => () => abortRef.current?.abort(), []);

  if (!props.open) return null;

  const toggleLevel = (level: EventRestoreLevel) => setSelectedLevels((current) => {
    const next = new Set(current);
    next.has(level) ? next.delete(level) : next.add(level);
    return next;
  });
  const toggleComponent = (component: string) => setSelectedComponents((current) => {
    const next = new Set(current);
    next.has(component) ? next.delete(component) : next.add(component);
    return next;
  });
  const collectCauseKeys = (rootId: string, nodes: EventCauseNode[], target: Set<string>) => {
    nodes.forEach((node) => {
      const key = `${rootId}:${node.key}`;
      target.add(key);
      if (node.children.length) collectCauseKeys(rootId, node.children, target);
    });
  };
  const toggleRoot = (id: string) => {
    const expanded = expandedRoots.has(id);
    setExpandedRoots((current) => {
      const next = new Set(current);
      expanded ? next.delete(id) : next.add(id);
      return next;
    });
    if (!expanded) {
      const root = entries.find((entry) => entry.id === id);
      if (root) {
        const children = resolver.children(root);
        setExpandedCauseKeys((current) => {
          const next = new Set(current);
          collectCauseKeys(id, children, next);
          return next;
        });
      }
    }
  };
  const toggleCause = (key: string) => {
    const expanded = expandedCauseKeys.has(key);
    if (expanded) {
      setExpandedCauseKeys((current) => { const next = new Set(current); next.delete(key); return next; });
      return;
    }
    const separator = key.indexOf(':');
    const rootId = separator >= 0 ? key.slice(0, separator) : key;
    const nodeKey = separator >= 0 ? key.slice(separator + 1) : key;
    const root = entries.find((entry) => entry.id === rootId);
    if (!root) return;
    const findNode = (nodes: EventCauseNode[]): EventCauseNode | undefined => {
      for (const node of nodes) {
        if (node.key === nodeKey) return node;
        const found = findNode(node.children);
        if (found) return found;
      }
      return undefined;
    };
    const node = findNode(resolver.children(root));
    setExpandedCauseKeys((current) => {
      const next = new Set(current);
      next.add(key);
      if (node) collectCauseKeys(rootId, node.children, next);
      return next;
    });
  };

  const selectedComponentLabel = selectedComponents.size === components.length && components.length
    ? `全部组件（${components.length}）`
    : selectedComponents.size
      ? `${selectedComponents.size} 个组件`
      : '全部组件';

  return <div className="event-restore-backdrop" role="presentation" onMouseDown={() => !loading && props.onClose()}>
    <section className="event-restore-dialog" role="dialog" aria-modal="true" aria-label="根因树" onMouseDown={(event) => event.stopPropagation()}>
      <header className="event-restore-header">
        <div><h2>根因树</h2>{props.environmentName && <span className="event-restore-environment-name">{props.environmentName}</span>}</div>
        <button type="button" className="icon-button" onClick={props.onClose} aria-label="关闭根因树"><X size={20}/></button>
      </header>

      <div className="event-restore-filterbar">
        <div className="event-restore-filter-group level-filter">
          <span className="event-restore-filter-label">事件级别</span>
          <div className="event-restore-level-options">
            {LEVEL_ORDER.map((level) => <label key={level} className={`event-level-check ${selectedLevels.has(level) ? 'selected' : ''}`}>
              <input type="checkbox" checked={selectedLevels.has(level)} onChange={() => toggleLevel(level)}/>
              <span>{LEVEL_LABEL[level]}</span>
            </label>)}
          </div>
        </div>

        <div className="event-restore-filter-group component-filter">
          <span className="event-restore-filter-label">所属组件</span>
          <details className="event-component-picker">
            <summary>{selectedComponentLabel}<span>⌄</span></summary>
            <div className="event-component-menu">
              <div className="event-component-menu-actions"><button type="button" onClick={() => setSelectedComponents(new Set(components))}>全选</button><button type="button" onClick={() => setSelectedComponents(new Set(errorComponents))}>仅异常组件</button></div>
              {components.map((component) => <label key={component}><input type="checkbox" checked={selectedComponents.has(component)} onChange={() => toggleComponent(component)}/><span>{component}</span>{errorComponents.has(component) && <em>ERROR</em>}</label>)}
              {!components.length && <div className="event-component-empty">暂无组件</div>}
            </div>
          </details>
        </div>

        <label className="event-restore-filter-group time-filter"><span className="event-restore-filter-label">开始时间</span><input type="datetime-local" step="0.001" value={startTime} onChange={(event) => setStartTime(event.target.value)}/></label>
        <label className="event-restore-filter-group time-filter"><span className="event-restore-filter-label">结束时间</span><input type="datetime-local" step="0.001" value={endTime} onChange={(event) => setEndTime(event.target.value)}/></label>
        <label className="event-restore-filter-group keyword-filter"><span className="event-restore-filter-label">检索关键字</span><span className="event-restore-search-input"><Search size={15}/><input value={keyword} onChange={(event) => setKeyword(event.target.value)} placeholder="DisplayCode / 事件码 / 描述"/></span></label>
        <button type="button" className="button primary event-restore-search-button" onClick={() => void runSearch(false)} disabled={loading || (!props.environmentId && !props.sourceEntries) || !startTime || !endTime}>{loading ? <LoaderCircle className="spin" size={16}/> : <Search size={16}/>} 检索</button>
      </div>

      <div className="event-restore-summary">
        <span>事件 <strong>{filteredEntries.length.toLocaleString()}</strong> / {entries.length.toLocaleString()}</span>
        <span>ERROR <strong>{entries.filter((entry) => normalizedEventLevel(entry) === 'ERROR').length.toLocaleString()}</strong></span>
        <span>异常组件 <strong>{errorComponents.size}</strong></span>
        {loading && <span className="event-restore-loading"><LoaderCircle className="spin" size={14}/> {progress?.message || '正在读取 event.log…'} {progress?.percent ?? 0}%</span>}
      </div>

      {error ? <div className="event-restore-table-wrap"><table className="event-restore-table"><tbody><tr><td colSpan={6}><div className="event-restore-error">事件日志检索失败：{error}</div></td></tr></tbody></table></div> : loading && !filteredEntries.length ? <div className="event-restore-table-wrap"><div className="event-restore-empty"><LoaderCircle className="spin" size={14}/> 正在读取 event.log…</div></div> : <EventRootCauseTable entries={filteredEntries}/>}
    </section>
  </div>;
}
