import { type ReactNode, useEffect, useMemo, useState } from 'react';
import { AlertTriangle, BookOpenCheck, ChevronDown, ChevronRight, FileSearch, LoaderCircle, ShieldCheck, X } from 'lucide-react';
import { listAbnormalCases, type AbnormalCase } from '../api/resourceApi';
import type { LogEntry } from '../types';
import type { ErrorMatchRule } from '../parser/logParser';
import { isAbnormalRuleMatchedEntry, rankAbnormalCases, type AbnormalCaseMatchResult } from '../rendering/abnormalKnowledge';
import { KnowledgeEvidenceLogRow } from './KnowledgeEvidenceLogRow';

interface Props {
  entries: readonly LogEntry[];
  selectedModules?: readonly string[];
  environmentId?: number;
  errorRules: readonly ErrorMatchRule[];
  onClose: () => void;
  onLocateEntry: (entry: LogEntry) => void;
  aiPanel?: ReactNode;
  refreshToken?: string | number;
  /** 由外层窗口（智能分析）承载标题栏与标签页时，这里只渲染内容本体。 */
  embedded?: boolean;
}

function confidenceLabel(result: AbnormalCaseMatchResult): { text: string; cls: string } {
  if (result.confidence === 'high') return { text: '高度相似', cls: 'high' };
  if (result.confidence === 'local') return { text: '局部高度相似', cls: 'local' };
  if (result.confidence === 'suspected') return { text: '疑似相似', cls: 'suspected' };
  if (result.confidence === 'related') return { text: '可能相关', cls: 'related' };
  return { text: '低相似', cls: 'none' };
}


function KnowledgeMatchCard({
  result,
  rank,
  open,
  onToggle,
  errorRules,
  onLocateEntry,
  onClose,
}: {
  result: AbnormalCaseMatchResult;
  rank: number;
  open: boolean;
  onToggle: () => void;
  errorRules: readonly ErrorMatchRule[];
  onLocateEntry: (entry: LogEntry) => void;
  onClose: () => void;
}) {
  const label = confidenceLabel(result);
  const proofItems = result.evidenceMatches.map((match, evidenceIndex) => ({ match, evidenceIndex }));
  const matchedItems = proofItems
    .filter(({ match }) => match.observable && match.score >= 45)
    .sort((left, right) => right.match.score - left.match.score);
  const unmatchedItems = proofItems
    .filter(({ match }) => !(match.observable && match.score >= 45))
    .sort((left, right) => {
      if (left.match.observable !== right.match.observable) return left.match.observable ? -1 : 1;
      return right.match.score - left.match.score;
    });

  const renderEvidence = ({ match, evidenceIndex }: typeof proofItems[number]) => {
    const matched = match.observable && match.score >= 45;
    const sectionClass = !match.observable ? 'unobserved' : matched ? (match.score >= 80 ? 'hit' : 'partial') : 'missed';
    return <section className={sectionClass} key={`${result.case.id}-${evidenceIndex}`}>
      <header><div><strong>证据 {evidenceIndex + 1}</strong><span>{match.evidence.subsystem ? `${match.evidence.subsystem} / ` : ''}{match.evidence.module || match.evidence.component || '未知模块'}</span></div><b>{matched ? `${match.score.toFixed(0)}%` : '未命中'}</b></header>
      <div className="knowledge-proof-template-label">案例证据</div>
      <KnowledgeEvidenceLogRow evidence={match.evidence} messageOverride={match.evidence.template || match.evidence.message || match.evidence.raw}/>
      {!match.observable && <p className="knowledge-proof-note">当前检索日志中没有该证据所属模块/范围的可比较日志，因此判定为未命中。</p>}
      {match.observable && <>
        <div className="knowledge-proof-features">
          {match.matchedFeatures.map((feature, featureIndex) => <span className="hit" key={`hit-${featureIndex}`}>✓ {feature.label} · {feature.detail}</span>)}
          {match.missingFeatures.map((feature, featureIndex) => <span className="miss" key={`miss-${featureIndex}`}>× {feature.label} · {feature.detail}</span>)}
          {match.neutralFeatures.map((feature, featureIndex) => <span className="neutral" key={`neutral-${featureIndex}`}>○ {feature.label} · {feature.detail}</span>)}
        </div>
        {match.matchedEntry && <div className="knowledge-current-log-panel"><div className="knowledge-current-log-title"><FileSearch size={13}/><span>当前命中日志</span><small>点击右侧按钮可定位到原日志。</small></div><KnowledgeEvidenceLogRow entry={match.matchedEntry} errorRules={errorRules} onLocate={(entry) => { onLocateEntry(entry); onClose(); }}/></div>}
      </>}
    </section>;
  };

  return <article className={`knowledge-match-card ${label.cls}`}>
    <button type="button" className="knowledge-match-head" onClick={onToggle}>
      <span className="knowledge-rank">#{rank}</span>
      <span className="knowledge-match-title"><strong>{result.case.name}</strong><small>{result.case.category || '未分类'} · {result.case.evidence_count} 条案例证据{result.matchedFeatureGroupTitle ? ` · 最匹配：${result.matchedFeatureGroupTitle}` : ''}</small></span>
      <span className={`knowledge-confidence ${label.cls}`}>{label.text}</span>
      <span className="knowledge-score"><b>{result.similarity.toFixed(1)}%</b><small>相似度</small></span>
      <span className="knowledge-coverage"><b>{result.coverage.toFixed(0)}%</b><small>证据覆盖</small></span>
      {open ? <ChevronDown size={16}/> : <ChevronRight size={16}/>}</button>
    {open && <div className="knowledge-match-detail">
      {(result.case.root_cause || result.case.solution || result.case.symptom) && <div className="knowledge-case-conclusion">
        {result.case.symptom && <div><span>故障现象</span><p>{result.case.symptom}</p></div>}
        {result.case.root_cause && <div><span>历史根因</span><p>{result.case.root_cause}</p></div>}
        {result.case.solution && <div><span>处理建议</span><p>{result.case.solution}</p></div>}
      </div>}
      <div className="knowledge-proof-list">
        {matchedItems.map(renderEvidence)}
        {unmatchedItems.length > 0 && <details className="knowledge-unmatched-evidence-group">
          <summary><span><ChevronRight size={14}/><strong>未命中证据</strong></span><b>{unmatchedItems.length}</b></summary>
          <div className="knowledge-unmatched-evidence-list">{unmatchedItems.map(renderEvidence)}</div>
        </details>}
      </div>
    </div>}
  </article>;
}

export function AbnormalCaseAnalysisDialog({ entries, selectedModules, environmentId, errorRules, onClose, onLocateEntry, aiPanel, refreshToken, embedded }: Props) {
  const [cases, setCases] = useState<AbnormalCase[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  useEffect(() => {
    let alive = true;
    setLoading(true); setError('');
    void listAbnormalCases({ pageSize: 500, enabled: true, environmentId })
      .then((payload) => { if (alive) setCases(payload.results); })
      .catch((exc) => { if (alive) setError(exc instanceof Error ? exc.message : String(exc)); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [environmentId, refreshToken]);

  const abnormalEntries = useMemo(() => entries.filter((entry) => isAbnormalRuleMatchedEntry(entry, errorRules)), [entries, errorRules]);
  const results = useMemo(() => rankAbnormalCases(cases, abnormalEntries, errorRules, selectedModules), [abnormalEntries, cases, errorRules, selectedModules]);
  const visibleResults = results.slice(0, 20);
  const confirmedResults = visibleResults.filter((result) => result.confidence === 'high' || result.confidence === 'local');
  const guessedResults = visibleResults.filter((result) => result.confidence !== 'high' && result.confidence !== 'local');
  const [guessedOpen, setGuessedOpen] = useState(false);

  useEffect(() => { setExpanded(new Set()); setGuessedOpen(false); }, [environmentId, refreshToken]);

  function toggle(id: number) {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  const body = <>
      <div className="knowledge-analysis-scroll">
        <div className="knowledge-analysis-summary">
          <div><AlertTriangle size={17}/><strong>{abnormalEntries.length}</strong><span>条当前异常日志</span></div>
          <div><BookOpenCheck size={17}/><strong>{cases.length}</strong><span>个启用案例</span></div>
          <div><ShieldCheck size={17}/><strong>{results.filter((item) => item.similarity >= 90).length}</strong><span>个 ≥90% 高相似</span></div>
        </div>
        {aiPanel && <div className="knowledge-analysis-ai-slot">{aiPanel}</div>}
        <div className="knowledge-analysis-body">
        {loading && <div className="knowledge-analysis-empty"><LoaderCircle className="spin" size={22}/><strong>正在读取异常知识库…</strong></div>}
        {error && <div className="resource-alert error">{error}</div>}
        {!loading && !error && !cases.length && <div className="knowledge-analysis-empty"><BookOpenCheck size={28}/><strong>知识库暂无启用的异常案例</strong><span>先从日志定位中选取异常日志录入案例。</span></div>}
        {!loading && cases.length > 0 && !visibleResults.length && <div className="knowledge-analysis-empty"><AlertTriangle size={28}/><strong>没有可比较的案例</strong><span>当前异常日志与案例的模块范围没有有效交集。</span></div>}
        <div className="knowledge-match-list">
          {confirmedResults.map((result, index) => <KnowledgeMatchCard
            key={result.case.id}
            result={result}
            rank={index + 1}
            open={expanded.has(result.case.id)}
            onToggle={() => toggle(result.case.id)}
            errorRules={errorRules}
            onLocateEntry={onLocateEntry}
            onClose={onClose}
          />)}
          {guessedResults.length > 0 && <section className={`knowledge-guess-group ${guessedOpen ? 'open' : 'collapsed'}`}>
            <button type="button" className="knowledge-guess-toggle" aria-expanded={guessedOpen} onClick={() => setGuessedOpen((current) => !current)}>
              <span>{guessedOpen ? <ChevronDown size={16}/> : <ChevronRight size={16}/>}<strong>猜测可能案例</strong><small>{guessedResults.length} 个</small></span>
              <em>默认折叠</em>
            </button>
            {guessedOpen && <div className="knowledge-guess-list">
              {guessedResults.map((result, index) => <KnowledgeMatchCard
                key={result.case.id}
                result={result}
                rank={confirmedResults.length + index + 1}
                open={expanded.has(result.case.id)}
                onToggle={() => toggle(result.case.id)}
                errorRules={errorRules}
                onLocateEntry={onLocateEntry}
                onClose={onClose}
              />)}
            </div>}
          </section>}
        </div>
      </div>
      </div>
      <footer className="knowledge-dialog-footer"><span>相似度仅用于辅助判断，请结合实际日志确认。</span><button className="button secondary" onClick={onClose}>关闭</button></footer>
    </>;

  if (embedded) {
    return <div className="knowledge-embedded-pane" onMouseDown={(event) => event.stopPropagation()}>{body}</div>;
  }
  return <div className="knowledge-dialog-backdrop" onMouseDown={onClose}>
    <section className="knowledge-analysis-dialog" onMouseDown={(event) => event.stopPropagation()}>
      <header className="knowledge-dialog-header">
        <div><h2>智能分析</h2></div>
        <button type="button" className="icon-button" onClick={onClose}><X size={18}/></button>
      </header>
      {body}
    </section>
  </div>;
}
