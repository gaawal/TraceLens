import { useMemo, useState, type CSSProperties, type MouseEvent, type ReactNode } from 'react';
import { Copy, FileSearch } from 'lucide-react';
import type { AbnormalCaseAnomalyRule, AbnormalCaseEvidence } from '../api/resourceApi';
import type { ErrorMatchRule } from '../parser/logParser';
import type { LogEntry } from '../types';

type HighlightRule = Pick<ErrorMatchRule, 'keyword' | 'caseSensitive' | 'wholeWord'>;

interface Props {
  evidence?: AbnormalCaseEvidence;
  entry?: LogEntry;
  errorRules?: readonly ErrorMatchRule[];
  onLocate?: (entry: LogEntry) => void;
  messageOverride?: string;
}

function componentHue(component: string): number {
  let hash = 0;
  for (let index = 0; index < component.length; index += 1) {
    hash = (hash * 31 + component.charCodeAt(index)) % 360;
  }
  return hash;
}

function componentStyle(component: string): CSSProperties {
  return { '--component-hue': componentHue(component) } as CSSProperties;
}

function sourceCategoryMeta(category?: string): { label: string; className: string } | undefined {
  switch (category) {
    case 'debug': return { label: '调试日志', className: 'debug' };
    case 'executor': return { label: '执行器日志', className: 'executor' };
    case 'run': return { label: '运行日志', className: 'run' };
    case 'helf': return { label: 'HELF日志', className: 'helf' };
    case 'sil': return { label: 'SIL日志', className: 'sil' };
    // 不是日志的举证也要能一眼认出来源，而不是被当成「没有来源的日志」。
    case 'case_report': return { label: '用例报告', className: 'case-report' };
    case 'case_fragment': return { label: '用例片段', className: 'case-fragment' };
    default: return undefined;
  }
}

function severityFrom(level: string, severity?: string): string {
  if (severity) return severity;
  const normalized = level.toLowerCase();
  if (normalized === 'error' || normalized === 'fatal') return 'error';
  if (normalized === 'warn' || normalized === 'warning') return 'warning';
  return 'normal';
}

function basename(value: string): string {
  const normalized = String(value || '').replace(/\\/g, '/');
  const parts = normalized.split('/');
  return parts.at(-1) || normalized;
}

function rulesFromEvidence(rules: readonly AbnormalCaseAnomalyRule[]): HighlightRule[] {
  return rules.map((rule) => ({ keyword: rule.keyword, caseSensitive: rule.case_sensitive, wholeWord: rule.whole_word }));
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function HighlightedEvidenceText({ text, rules }: { text: string; rules: readonly HighlightRule[] }) {
  const marks = useMemo(() => {
    type Mark = { start: number; end: number; tone: 'error' | 'warning' };
    const candidates: Mark[] = [];
    const pushMatches = (keyword: string, caseSensitive: boolean, wholeWord: boolean, tone: Mark['tone']) => {
      if (!keyword) return;
      const escaped = escapeRegExp(keyword);
      const body = wholeWord ? `(?<![\\p{L}\\p{N}_])${escaped}(?![\\p{L}\\p{N}_])` : escaped;
      const regex = new RegExp(body, `${caseSensitive ? '' : 'i'}gu`);
      for (const match of text.matchAll(regex)) {
        const index = match.index ?? -1;
        if (index >= 0 && match[0]) candidates.push({ start: index, end: index + match[0].length, tone });
      }
    };
    rules.forEach((rule) => pushMatches(rule.keyword, rule.caseSensitive, rule.wholeWord, 'error'));
    ['WARN', 'WARNING', 'WRAN'].forEach((keyword) => pushMatches(keyword, true, true, 'warning'));
    candidates.sort((left, right) => left.start - right.start || (right.end - right.start) - (left.end - left.start) || (left.tone === 'error' ? -1 : 1));
    const accepted: Mark[] = [];
    let cursor = -1;
    candidates.forEach((candidate) => {
      if (candidate.start < cursor) return;
      accepted.push(candidate);
      cursor = candidate.end;
    });
    return accepted;
  }, [rules, text]);

  if (!marks.length) return <>{text}</>;
  const parts: ReactNode[] = [];
  let cursor = 0;
  marks.forEach((mark, index) => {
    if (mark.start > cursor) parts.push(text.slice(cursor, mark.start));
    parts.push(<mark className={`keyword keyword-${mark.tone}`} key={`${mark.start}-${index}`}>{text.slice(mark.start, mark.end)}</mark>);
    cursor = mark.end;
  });
  if (cursor < text.length) parts.push(text.slice(cursor));
  return <>{parts}</>;
}

export function KnowledgeEvidenceLogRow({ evidence, entry, errorRules = [], onLocate, messageOverride }: Props) {
  const [copied, setCopied] = useState(false);
  const timestamp = entry?.timestamp || evidence?.timestamp || '—';
  const component = entry?.logModule || entry?.component || evidence?.module || evidence?.component || '未知模块';
  const level = entry?.level || evidence?.level || 'LOG';
  const severity = severityFrom(level, entry?.severity || evidence?.severity);
  const message = messageOverride ?? entry?.message ?? evidence?.message ?? evidence?.raw ?? '';
  // 用例片段/报告证据不是日志行：没有时间、级别、模块，硬套日志行的版式
  // 会显示成「— | 未知模块 | LOG」这种假字段。这里换成纯文本行。
  const isLogEvidence = Boolean(entry) || (evidence?.evidence_kind
    ? evidence.evidence_kind === 'runtime_log'
    : Boolean(evidence?.timestamp || evidence?.level || evidence?.module || evidence?.component));
  const sourceFile = entry?.source.fileName || entry?.sourceFile || evidence?.source_file || '';
  const sourceLine = entry?.source.lineNumber || entry?.lineNumber || evidence?.source_line;
  const sourceText = sourceFile ? `${sourceFile}${sourceLine ? `:${sourceLine}` : ''}` : '';
  const categoryMeta = sourceCategoryMeta(entry?.logCategory || evidence?.source_category);
  const highlightRules: readonly HighlightRule[] = entry
    ? errorRules.filter((rule) => rule.enabled)
    : rulesFromEvidence(evidence?.anomaly_rules || []);

  async function copySource(event: MouseEvent<HTMLButtonElement>) {
    event.stopPropagation();
    if (!sourceText) return;
    try {
      await navigator.clipboard.writeText(sourceText);
    } catch {
      const textarea = document.createElement('textarea');
      textarea.value = sourceText;
      textarea.style.position = 'fixed';
      textarea.style.opacity = '0';
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand('copy');
      textarea.remove();
    }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  }

  return <div
    className={`log-row knowledge-evidence-log-row ${isLogEvidence ? `severity-${severity}` : 'is-non-log-evidence'}`}
    style={componentStyle(component)}
    title={messageOverride ?? entry?.raw ?? evidence?.raw ?? message}
  >
    {isLogEvidence && <>
      <span className="log-time" title={timestamp}>{timestamp}</span>
      <span className="component-badge compact" style={componentStyle(component)} title={`模块：${component}`}>{component}</span>
      <span className={`level-badge level-${String(level).toLowerCase()}`}>{level}</span>
    </>}
    <span className="log-summary" title={message}><HighlightedEvidenceText text={message} rules={highlightRules}/></span>
    <span className="log-row-meta">
      {sourceText && <button type="button" className={`source-location-button ${copied ? 'copied' : ''}`} onClick={copySource} title={`${sourceText}\n点击复制代码文件和行号`}><Copy size={12}/><span>{copied ? '已复制' : basename(sourceText)}</span></button>}
      {categoryMeta && <span className="log-category-slot"><span className={`log-category-badge ${categoryMeta.className}`}>{categoryMeta.label}</span></span>}
    </span>
    {entry && onLocate ? <button type="button" className="knowledge-evidence-locate-button" onClick={() => onLocate(entry)} title="定位到日志"><FileSearch size={14}/></button> : <span className="knowledge-evidence-log-spacer"/>}
  </div>;
}
