import { registerPageContextReader } from '../assistant/contextRegistry';
import { useImeCompositionGuard } from '../utils/imeComposition';
import { useEffect, useMemo, useState } from 'react';
import { AlertTriangle, BookOpenCheck, Edit3, Eye, RefreshCw, Search, ShieldCheck, Trash2, X } from 'lucide-react';
import { deleteAbnormalCase, listAbnormalCases, updateAbnormalCase, type AbnormalCase, type AbnormalCaseFeatureGroup } from '../api/resourceApi';
import { effectiveAbnormalCaseFeatureGroups } from '../rendering/abnormalKnowledge';
import { AbnormalCaseEditorDialog } from './AbnormalCaseEditorDialog';
import { KnowledgeEvidenceLogRow } from './KnowledgeEvidenceLogRow';

function formatMoment(value?: string | null): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).replace('T', ' ').slice(0, 19);
  return date.toLocaleString('zh-CN', { hour12: false });
}

function historyAction(action: string): string {
  if (action === 'case_created') return '创建案例';
  if (action === 'feature_added') return '新增现场特征';
  if (action === 'feature_enabled') return '启用现场特征';
  if (action === 'feature_disabled') return '停用现场特征';
  if (action === 'feature_deleted') return '删除现场特征';
  return action || '案例更新';
}

/**
 * 案例与分析 — one page for both halves of the job.
 *
 * Historical retrieval and AI diagnosis used to live on two separate pages even though they
 * answer the same question in sequence ("have I seen this before?" -> "no? then work it
 * out"). Analysis now runs through TracePilot, so this page owns the case library and offers
 * the two entry points into that flow: 检索历史案例 and AI 诊断.
 */
export function KnowledgeBasePage() {
  const imeGuard = useImeCompositionGuard();
  const [cases, setCases] = useState<AbnormalCase[]>([]);
  const [query, setQuery] = useState('');
  const [enabledFilter, setEnabledFilter] = useState<'all' | 'enabled' | 'disabled'>('all');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [detail, setDetail] = useState<AbnormalCase>();
  const [editing, setEditing] = useState<AbnormalCase>();

  async function refresh(searchText = query) {
    setLoading(true); setError('');
    try {
      const payload = await listAbnormalCases({ pageSize: 500, query: searchText.trim() || undefined });
      setCases(payload.results);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally { setLoading(false); }
  }

  useEffect(() => { void refresh(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, []);

  const filtered = useMemo(() => cases.filter((item) => {
    if (enabledFilter === 'enabled' && !item.enabled) return false;
    if (enabledFilter === 'disabled' && item.enabled) return false;
    const needle = query.trim().toLocaleLowerCase();
    if (!needle) return true;
    const evidenceText = item.evidences.map((evidence) => [
      evidence.module || evidence.component,
      evidence.function_name,
      evidence.template,
      ...(evidence.tokens || []),
      ...(evidence.error_codes || []).flatMap((code) => [code.key, code.value]),
      ...(evidence.anomaly_rules || []).map((rule) => rule.keyword),
    ].join(' ')).join(' ');
    return `${item.name} ${item.category} ${item.symptom} ${item.root_cause} ${item.solution} ${item.tags.join(' ')} ${evidenceText}`.toLocaleLowerCase().includes(needle);
  }), [cases, enabledFilter, query]);

  function acceptUpdated(updated: AbnormalCase) {
    setCases((current) => current.map((candidate) => candidate.id === updated.id ? updated : candidate));
    setDetail((current) => current?.id === updated.id ? updated : current);
    setEditing((current) => current?.id === updated.id ? updated : current);
  }

  async function toggle(item: AbnormalCase) {
    try {
      acceptUpdated(await updateAbnormalCase(item.id, { enabled: !item.enabled }));
    } catch (exc) { setError(exc instanceof Error ? exc.message : String(exc)); }
  }

  async function saveFeatureGroups(item: AbnormalCase, groups: AbnormalCaseFeatureGroup[]) {
    try {
      acceptUpdated(await updateAbnormalCase(item.id, { feature_groups: groups, fingerprint_version: 4 }));
    } catch (exc) { setError(exc instanceof Error ? exc.message : String(exc)); }
  }

  async function toggleFeatureGroup(item: AbnormalCase, groupId: string) {
    const groups = effectiveAbnormalCaseFeatureGroups(item);
    const target = groups.find((group) => group.id === groupId);
    if (!target) return;
    if (target.enabled !== false && groups.filter((group) => group.enabled !== false).length <= 1) {
      setError('至少需要保留一组启用的现场特征，避免案例失去可匹配指纹。');
      return;
    }
    await saveFeatureGroups(item, groups.map((group) => group.id === groupId ? { ...group, enabled: group.enabled === false } : group));
  }

  async function removeFeatureGroup(item: AbnormalCase, groupId: string) {
    const groups = effectiveAbnormalCaseFeatureGroups(item);
    if (groups.length <= 1) { setError('案例至少需要保留一组现场特征。'); return; }
    const target = groups.find((group) => group.id === groupId);
    if (!target || !window.confirm(`确认删除现场特征“${target.title}”吗？该操作不会删除整个案例。`)) return;
    await saveFeatureGroups(item, groups.filter((group) => group.id !== groupId));
  }

  async function remove(item: AbnormalCase) {
    if (!window.confirm(`确认删除异常案例“${item.name}”吗？删除后不会再参与案例分析。`)) return;
    try {
      await deleteAbnormalCase(item.id);
      setCases((current) => current.filter((candidate) => candidate.id !== item.id));
      if (detail?.id === item.id) setDetail(undefined);
    } catch (exc) { setError(exc instanceof Error ? exc.message : String(exc)); }
  }

  const categories = useMemo(() => new Set(cases.map((item) => item.category).filter(Boolean)).size, [cases]);
  const enabledCount = useMemo(() => cases.filter((item) => item.enabled).length, [cases]);
  const featureGroupCount = useMemo(() => cases.reduce((sum, item) => sum + effectiveAbnormalCaseFeatureGroups(item).length, 0), [cases]);


  useEffect(() => {
    const handler = (event: Event) => {
      const custom = event as CustomEvent<{ context?: Record<string, unknown> }>;
      const context = custom.detail?.context;
      if (!context) return;
      const activeCase = editing || detail;
      context.page = 'knowledge';
      context.page_label = '案例与分析';
      context.knowledge_page = {
        query,
        enabled_filter: enabledFilter,
        case_count: cases.length,
        visible_case_count: filtered.length,
        enabled_count: enabledCount,
        category_count: categories,
        feature_group_count: featureGroupCount,
        active_case: activeCase ? {
          id: activeCase.id,
          name: activeCase.name,
          category: activeCase.category || '',
          symptom: activeCase.symptom || activeCase.description || '',
          root_cause: activeCase.root_cause || '',
          solution: activeCase.solution || '',
          tags: activeCase.tags.slice(0, 16),
          enabled: activeCase.enabled,
          evidence_count: activeCase.evidence_count,
          feature_groups: effectiveAbnormalCaseFeatureGroups(activeCase).slice(0, 8).map((group) => ({ id: group.id, title: group.title, enabled: group.enabled !== false })),
        } : null,
        visible_cases: filtered.slice(0, 8).map((item) => ({ id: item.id, name: item.name, category: item.category, root_cause: item.root_cause || '', enabled: item.enabled })),
      };
    };
    return registerPageContextReader(handler, 10);
  }, [query, enabledFilter, cases, filtered, enabledCount, categories, featureGroupCount, detail, editing]);

  // 这一页就是案例库：只展示案例。抬头说明、汇总条、以及跳到「用例分析」的入口都去掉
  // —— 那些是页面上的额外叙述，看案例的人不需要先读一段介绍。
  return <main className="knowledge-page">
    <section className="knowledge-page-header">
      <div className="knowledge-analysis-actions">
        <button
          type="button"
          className="button primary compact"
          onClick={() => {
            // Reuse the chat box rather than a bespoke analysis page: the diagnosis is a
            // conversation, and TracePilot already carries the log context into it.
            window.dispatchEvent(new CustomEvent('tracelens:assistant-open', {
              detail: {
                skill_id: 'auto',
                scope_key: 'knowledge:analysis',
                scope_label: '案例库',
                title: `案例诊断 · ${query || '当前日志'}`.slice(0, 60),
                context: { page: 'knowledge', knowledge_query: query, case_id: detail?.id || editing?.id || null },
              },
            }));
          }}
        >
          <ShieldCheck size={14} /> AI 诊断
        </button>
      </div>
    </section>

    <section className="knowledge-query-bar unified-query-shell">
      <div className="knowledge-query-row remote-unified-query-row-v150">
        <div className="knowledge-status-filter" aria-label="案例状态">
          {(['all', 'enabled', 'disabled'] as const).map((value) => <button type="button" className={enabledFilter === value ? 'active' : ''} key={value} onClick={() => setEnabledFilter(value)}>{value === 'all' ? '全部' : value === 'enabled' ? '启用' : '停用'}</button>)}
        </div>
        <div className="remote-unified-text-search knowledge-unified-search">
          <Search size={15}/><input value={query} onChange={(event) => setQuery(event.target.value)} onCompositionStart={imeGuard.onCompositionStart} onCompositionEnd={imeGuard.onCompositionEnd} onKeyDown={(event) => { if (event.key === 'Enter' && !imeGuard.isComposing(event)) void refresh(); }} placeholder="搜索案例 / 模块 / 错误码 / 根因…"/>
          {query && <button type="button" onClick={() => { setQuery(''); void refresh(''); }} aria-label="清空搜索"><X size={13}/></button>}
        </div>
        <button className="button primary remote-search-apply" onClick={() => void refresh()} disabled={loading}><Search size={14}/> 搜索</button>
        <button className="button ghost knowledge-refresh-button" onClick={() => void refresh()} disabled={loading}><RefreshCw className={loading ? 'spin' : ''} size={14}/> 刷新</button>
      </div>
    </section>

    {error && <div className="resource-alert error">{error}</div>}

    <div className="knowledge-list-wrap">
      <table className="knowledge-list-table">
        <thead><tr><th>状态</th><th>案例名称</th><th>分类</th><th>故障现象</th><th>模块 / 特征</th><th>现场特征</th><th>更新时间</th><th>操作</th></tr></thead>
        <tbody>
          {filtered.map((item) => {
            const groups = effectiveAbnormalCaseFeatureGroups(item);
            const activeGroups = groups.filter((group) => group.enabled !== false).length;
            const modules = Array.from(new Set(item.evidences.map((evidence) => evidence.module || evidence.component).filter(Boolean)));
            const codes = Array.from(new Set(item.evidences.flatMap((evidence) => (evidence.error_codes || []).map((code) => code.value))));
            return <tr className={item.enabled ? '' : 'disabled'} key={item.id}>
              <td><label className="knowledge-toggle knowledge-list-toggle"><input type="checkbox" checked={item.enabled} onChange={() => void toggle(item)}/><span>{item.enabled ? '启用' : '停用'}</span></label></td>
              <td className="knowledge-list-name"><strong><AlertTriangle size={13}/>{item.name}</strong><small>{item.root_cause || item.description || '未填写根因/说明'}</small></td>
              <td><span className="knowledge-list-category">{item.category || '未分类'}</span></td>
              <td className="knowledge-list-symptom" title={item.symptom || item.description || ''}>{item.symptom || item.description || '—'}</td>
              <td><div className="knowledge-chip-row knowledge-list-chips">{modules.slice(0, 3).map((module) => <span key={module}>{module}</span>)}{codes.slice(0, 2).map((code) => <span className="code" key={code}>{code}</span>)}{!modules.length && !codes.length && <span>—</span>}</div></td>
              <td><strong>{activeGroups}/{groups.length}</strong><small className="knowledge-evidence-count"> 组启用 · {item.evidence_count} 条证据</small></td>
              <td className="knowledge-list-time">{formatMoment(item.updated_at)}</td>
              <td><div className="knowledge-list-actions"><button onClick={() => setDetail(item)}><Eye size={13}/>特征</button><button onClick={() => setEditing(item)}><Edit3 size={13}/>编辑</button><button className="danger" onClick={() => void remove(item)}><Trash2 size={13}/>删除</button></div></td>
            </tr>;
          })}
          {!loading && !filtered.length && <tr><td colSpan={8}><div className="knowledge-empty-card"><BookOpenCheck size={32}/><strong>暂无异常案例</strong><span>在日志定位里找到已确认的异常日志后，点击“录入案例”即可沉淀到这里。</span></div></td></tr>}
        </tbody>
      </table>
    </div>

    {detail && <div className="knowledge-dialog-backdrop" onMouseDown={() => setDetail(undefined)}><section className="knowledge-detail-dialog" onMouseDown={(event) => event.stopPropagation()}>
      <header className="knowledge-dialog-header"><div><span className="eyebrow">CASE GOVERNANCE</span><h2>{detail.name}</h2><p>{detail.category || '未分类'} · {detail.enabled ? '当前启用' : '当前停用'} · {effectiveAbnormalCaseFeatureGroups(detail).length} 组现场特征</p></div><button className="icon-button" onClick={() => setDetail(undefined)}><X size={18}/></button></header>
      <div className="knowledge-detail-body">
        <div className="knowledge-detail-info">{detail.symptom && <div><span>故障现象</span><p>{detail.symptom}</p></div>}{detail.root_cause && <div><span>根因</span><p>{detail.root_cause}</p></div>}{detail.solution && <div><span>处理建议</span><p>{detail.solution}</p></div>}{detail.description && <div><span>补充说明</span><p>{detail.description}</p></div>}</div>
        <section className="knowledge-feature-groups"><header><div><ShieldCheck size={16}/><strong>现场特征治理</strong></div><span>匹配时分别比较每组现场特征，取最佳匹配；新增特征不会稀释旧指纹。</span></header>
          {effectiveAbnormalCaseFeatureGroups(detail).map((group, groupIndex) => <article className={`knowledge-feature-group ${group.enabled === false ? 'disabled' : ''}`} key={group.id}>
            <div className="knowledge-feature-group-head"><div><span className="knowledge-feature-index">#{groupIndex + 1}</span><div><strong>{group.title || `现场特征 ${groupIndex + 1}`}</strong><small>{formatMoment(group.created_at)} · {group.source_task_name || group.environment_name || '历史现场'} · {group.evidences.length} 条证据</small></div></div><div><label className="knowledge-toggle"><input type="checkbox" checked={group.enabled !== false} onChange={() => void toggleFeatureGroup(detail, group.id)}/><span>{group.enabled !== false ? '参与匹配' : '已停用'}</span></label><button type="button" className="knowledge-feature-delete" onClick={() => void removeFeatureGroup(detail, group.id)}><Trash2 size={13}/>删除特征</button></div></div>
            {group.note && <p className="knowledge-feature-note">{group.note}</p>}
            <div className="knowledge-detail-evidences">{group.evidences.map((evidence, index) => <section className="knowledge-feature-evidence" key={`${group.id}-${evidence.source_entry_id || index}-${index}`}><div className="knowledge-detail-evidence-head"><strong>证据 {index + 1}</strong><span>{evidence.subsystem ? `${evidence.subsystem} / ` : ''}{evidence.module || evidence.component || '未知模块'}</span></div><KnowledgeEvidenceLogRow evidence={evidence}/>{(evidence.process_id || evidence.thread_id || evidence.trace_id || evidence.function_name) && <div className="knowledge-log-runtime-meta">{evidence.process_id && <span>PID <b>{evidence.process_id}</b></span>}{evidence.thread_id && <span>TID <b>{evidence.thread_id}</b></span>}{evidence.trace_id && <span>Trace <b>{evidence.trace_id}</b></span>}{evidence.function_name && <span>函数 <b>{evidence.function_name}</b></span>}</div>}<div className="knowledge-template-row"><span>归一化指纹</span><code>{evidence.template}</code></div><div className="knowledge-chip-row">{(evidence.anomaly_rules || []).map((rule) => <span key={`rule-${rule.id}`}>异常规则:{rule.keyword}</span>)}{(evidence.error_codes || []).map((code) => <span className="code" key={`${code.key}-${code.value}`}>{code.key}={code.value}</span>)}</div></section>)}</div>
          </article>)}
        </section>
        {(detail.governance_history || []).length > 0 && <section className="knowledge-governance-history"><header><strong>治理记录</strong><span>最近 {Math.min(8, detail.governance_history?.length || 0)} 条</span></header>{detail.governance_history?.slice(-8).reverse().map((event, index) => <div key={`${event.created_at}-${event.action}-${index}`}><span>{formatMoment(event.created_at)}</span><strong>{historyAction(event.action)}</strong><small>{event.feature_group_title || ''}{event.evidence_count ? ` · ${event.evidence_count} 条证据` : ''}</small></div>)}</section>}
      </div>
      <footer className="knowledge-dialog-footer"><span>停用/删除现场特征不会删除整个案例。</span><button className="button secondary" onClick={() => setDetail(undefined)}>关闭</button><button className="button primary" onClick={() => { setDetail(undefined); setEditing(detail); }}><Edit3 size={14}/>编辑案例信息</button></footer>
    </section></div>}

    {editing && <AbnormalCaseEditorDialog caseItem={editing} onClose={() => setEditing(undefined)} onSaved={(saved) => { acceptUpdated(saved); setEditing(undefined); }}/>} 
  </main>;
}
