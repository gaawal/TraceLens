import { useMemo, useState } from 'react';
import { AlertTriangle, BookOpenCheck, Check, GitMerge, PlusCircle, Save, ShieldCheck, X } from 'lucide-react';
import {
  createAbnormalCase,
  listAbnormalCases,
  updateAbnormalCase,
  type AbnormalCase,
  type AbnormalCaseEvidence,
  type AbnormalCaseFeatureGroup,
  type LogWindowRequest,
} from '../api/resourceApi';
import type { LogEntry } from '../types';
import type { ErrorMatchRule } from '../parser/logParser';
import {
  activeAbnormalCaseEvidences,
  createAbnormalEvidence,
  effectiveAbnormalCaseFeatureGroups,
  isAbnormalRuleMatchedEntry,
  rankDuplicateAbnormalCases,
  type AbnormalCaseDuplicateCandidate,
} from '../rendering/abnormalKnowledge';
import { KnowledgeEvidenceLogRow } from './KnowledgeEvidenceLogRow';

interface Props {
  caseItem?: AbnormalCase;
  initialDraft?: Partial<AbnormalCase>;
  presetEvidences?: AbnormalCaseEvidence[];
  entries?: readonly LogEntry[];
  defaultSelectedEntryId?: string;
  environmentId?: number;
  environmentName?: string;
  sourceOperationId?: string;
  sourceTaskName?: string;
  querySnapshot?: LogWindowRequest;
  errorRules?: readonly ErrorMatchRule[];
  onClose: () => void;
  onSaved: (item: AbnormalCase) => void;
}

function featureGroupId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID();
  return `feature-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function featureGroupTitle(caseName: string): string {
  const now = new Date();
  const time = `${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')} ${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
  return caseName.trim() ? `补充现场 · ${caseName.trim()} · ${time}` : `补充现场 · ${time}`;
}

function buildFeatureGroup(props: Props, evidences: AbnormalCaseEvidence[], caseName: string): AbnormalCaseFeatureGroup {
  return {
    id: featureGroupId(),
    title: featureGroupTitle(caseName),
    enabled: true,
    created_at: new Date().toISOString(),
    source_operation_id: props.sourceOperationId || '',
    source_task_name: props.sourceTaskName || '',
    environment_name: props.environmentName || '',
    note: '由新案例录入流程补充到已有案例。',
    evidences,
  };
}

export function AbnormalCaseEditorDialog(props: Props) {
  const errorRules = props.errorRules || [];
  const abnormalEntries = useMemo(
    () => (props.entries || []).filter((entry) => isAbnormalRuleMatchedEntry(entry, errorRules)),
    [errorRules, props.entries],
  );
  const [name, setName] = useState(props.caseItem?.name || props.initialDraft?.name || '');
  const [category, setCategory] = useState(props.caseItem?.category || props.initialDraft?.category || '');
  const [symptom, setSymptom] = useState(props.caseItem?.symptom || props.initialDraft?.symptom || '');
  const [rootCause, setRootCause] = useState(props.caseItem?.root_cause || props.initialDraft?.root_cause || '');
  const [solution, setSolution] = useState(props.caseItem?.solution || props.initialDraft?.solution || '');
  const [description, setDescription] = useState(props.caseItem?.description || props.initialDraft?.description || '');
  const [tagsText, setTagsText] = useState((props.caseItem?.tags || props.initialDraft?.tags || []).join(', '));
  const [enabled, setEnabled] = useState(props.caseItem?.enabled ?? props.initialDraft?.enabled ?? true);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => {
    if (props.caseItem || props.presetEvidences?.length) return new Set();
    if (props.defaultSelectedEntryId && abnormalEntries.some((entry) => entry.id === props.defaultSelectedEntryId)) return new Set([props.defaultSelectedEntryId]);
    return new Set(abnormalEntries.slice(0, Math.min(3, abnormalEntries.length)).map((entry) => entry.id));
  });
  const presetEvidences = props.caseItem ? [] : (props.presetEvidences || []);
  const [selectedPresetIds, setSelectedPresetIds] = useState<Set<string>>(() => new Set(presetEvidences.map((evidence, index) => evidence.source_entry_id || `preset-${index}`)));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [duplicateCandidates, setDuplicateCandidates] = useState<AbnormalCaseDuplicateCandidate[]>();

  const newEvidences = useMemo(() => {
    const preset = presetEvidences.filter((evidence, index) => selectedPresetIds.has(evidence.source_entry_id || `preset-${index}`));
    const live = abnormalEntries.filter((entry) => selectedIds.has(entry.id)).map((entry) => createAbnormalEvidence(entry, errorRules));
    const seen = new Set<string>();
    return [...preset, ...live].filter((evidence) => {
      const key = `${evidence.source_file}|${evidence.source_line || ''}|${evidence.raw}`;
      if (seen.has(key)) return false;
      seen.add(key); return true;
    });
  }, [abnormalEntries, errorRules, presetEvidences, selectedIds, selectedPresetIds]);
  const previewEvidences = props.caseItem ? activeAbnormalCaseEvidences(props.caseItem) : newEvidences;
  const allEvidenceSelected = abnormalEntries.length > 0 && abnormalEntries.every((entry) => selectedIds.has(entry.id));

  function togglePresetEvidence(id: string) {
    setSelectedPresetIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
    setDuplicateCandidates(undefined);
  }

  function toggleSelectAll() {
    setSelectedIds(allEvidenceSelected ? new Set() : new Set(abnormalEntries.map((entry) => entry.id)));
    setDuplicateCandidates(undefined);
  }

  function toggleEntry(id: string) {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
    setDuplicateCandidates(undefined);
  }

  function commonPayload() {
    return {
      name: name.trim(),
      category: category.trim(),
      symptom: symptom.trim(),
      root_cause: rootCause.trim(),
      solution: solution.trim(),
      description: description.trim(),
      tags: tagsText.split(/[,，;；]/).map((item) => item.trim()).filter(Boolean),
      enabled,
      source_operation_id: props.caseItem?.source_operation_id || props.initialDraft?.source_operation_id || props.sourceOperationId || '',
      source_task_name: props.caseItem?.source_task_name || props.initialDraft?.source_task_name || props.sourceTaskName || '',
      environment: props.caseItem?.environment ?? props.initialDraft?.environment ?? props.environmentId ?? null,
      environment_name: props.caseItem?.environment_name || props.initialDraft?.environment_name || props.environmentName || '',
      query_snapshot: props.caseItem?.query_snapshot || props.initialDraft?.query_snapshot || props.querySnapshot || {},
      fingerprint_version: 4,
    };
  }

  async function createNewCase() {
    const saved = await createAbnormalCase({ ...commonPayload(), evidences: newEvidences });
    props.onSaved(saved);
  }

  async function appendToExisting(candidate: AbnormalCaseDuplicateCandidate) {
    setSaving(true); setError('');
    try {
      const groups = effectiveAbnormalCaseFeatureGroups(candidate.case);
      const nextGroup = buildFeatureGroup(props, newEvidences, name);
      const saved = await updateAbnormalCase(candidate.case.id, {
        feature_groups: [...groups, nextGroup],
        fingerprint_version: 4,
      });
      props.onSaved(saved);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally { setSaving(false); }
  }

  async function save(forceCreate = false) {
    const trimmed = name.trim();
    if (!trimmed) { setError('请填写案例名称。'); return; }
    if (!props.caseItem && !newEvidences.length) { setError('至少选择一条异常日志作为案例举证。'); return; }
    setSaving(true); setError('');
    try {
      if (props.caseItem) {
        // 编辑案例基本信息时不重写现场特征组；特征组在案例详情中独立治理。
        const saved = await updateAbnormalCase(props.caseItem.id, commonPayload());
        props.onSaved(saved);
        return;
      }
      if (!forceCreate) {
        const existing = await listAbnormalCases({ pageSize: 500, enabled: true, environmentId: props.environmentId });
        const candidates = rankDuplicateAbnormalCases(existing.results, newEvidences).filter((item) => item.similarity >= 85).slice(0, 6);
        if (candidates.length) {
          setDuplicateCandidates(candidates);
          return;
        }
      }
      await createNewCase();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally { setSaving(false); }
  }

  return <div className="knowledge-dialog-backdrop" onMouseDown={() => { if (!saving) props.onClose(); }}>
    <section className="knowledge-editor-dialog" onMouseDown={(event) => event.stopPropagation()}>
      <header className="knowledge-dialog-header">
        <div><h2>{props.caseItem ? '编辑异常案例' : '录入异常案例'}</h2></div>
        <button type="button" className="icon-button" disabled={saving} onClick={props.onClose}><X size={18}/></button>
      </header>

      <div className="knowledge-editor-body">
        <section className="knowledge-form-grid">
          <label className="span-2"><span>案例名称 *</span><input value={name} onChange={(event) => { setName(event.target.value); setDuplicateCandidates(undefined); }} placeholder="请输入便于识别的异常案例名称"/></label>
          <label><span>故障分类</span><input value={category} onChange={(event) => setCategory(event.target.value)} placeholder="请输入故障分类"/></label>
          <label><span>标签</span><input value={tagsText} onChange={(event) => setTagsText(event.target.value)} placeholder="多个标签请用逗号分隔"/></label>
          <label className="span-2"><span>故障现象</span><textarea value={symptom} onChange={(event) => setSymptom(event.target.value)} placeholder="用户看到的异常现象"/></label>
          <label><span>根因</span><textarea value={rootCause} onChange={(event) => setRootCause(event.target.value)} placeholder="确认后的故障根因"/></label>
          <label><span>处理建议</span><textarea value={solution} onChange={(event) => setSolution(event.target.value)} placeholder="后续排查 / 处理步骤"/></label>
          <label className="span-2"><span>补充说明</span><textarea value={description} onChange={(event) => setDescription(event.target.value)} placeholder="可选"/></label>
          <label className="knowledge-enable-row span-2"><input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)}/><span>{enabled ? '启用：参与后续异常案例分析' : '停用：保留案例但不参与分析'}</span></label>
        </section>

        {!props.caseItem && presetEvidences.length > 0 && <section className="knowledge-evidence-picker ai-prefill-evidence">
          <header><div><BookOpenCheck size={17}/><strong>AI 已验证举证</strong><span>{selectedPresetIds.size} / {presetEvidences.length}</span></div><div className="knowledge-evidence-picker-actions"><small>包含用例报告证据与运行日志证据，可在保存前取消不需要的证据。</small></div></header>
          <div className="knowledge-evidence-list">
            {presetEvidences.map((evidence, index) => {
              const id = evidence.source_entry_id || `preset-${index}`;
              const selected = selectedPresetIds.has(id);
              return <div role="checkbox" aria-checked={selected} tabIndex={0} key={id} className={`knowledge-evidence-select ${selected ? 'selected' : ''}`} onClick={() => togglePresetEvidence(id)} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); togglePresetEvidence(id); } }}>
                <span className="knowledge-check">{selected ? <Check size={13}/> : null}</span>
                <div className="knowledge-evidence-select-content">
                  <KnowledgeEvidenceLogRow evidence={evidence}/>
                  <div className="knowledge-evidence-select-meta"><span>来源：{evidence.source_category === 'case_report' ? '用例报告' : '运行日志'}</span><span>异常规则：{evidence.anomaly_rules?.map((rule) => rule.keyword).join(', ') || '报告证据'}</span>{evidence.module && <span>组件：{evidence.subsystem ? `${evidence.subsystem} / ` : ''}{evidence.module}</span>}</div>
                </div>
              </div>;
            })}
          </div>
        </section>}

        {!props.caseItem && <section className="knowledge-evidence-picker">
          <header><div><BookOpenCheck size={17}/><strong>选择异常举证</strong><span>{selectedIds.size} / {abnormalEntries.length}</span></div><div className="knowledge-evidence-picker-actions"><small>仅展示当前异常候选日志。</small><button type="button" disabled={!abnormalEntries.length} onClick={toggleSelectAll}>{allEvidenceSelected ? '取消全选' : '全选举证'}</button></div></header>
          <div className="knowledge-evidence-list">
            {abnormalEntries.slice(0, 300).map((entry) => {
              const evidence = createAbnormalEvidence(entry, errorRules);
              const selected = selectedIds.has(entry.id);
              return <div role="checkbox" aria-checked={selected} tabIndex={0} key={entry.id} className={`knowledge-evidence-select ${selected ? 'selected' : ''}`} onClick={() => toggleEntry(entry.id)} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); toggleEntry(entry.id); } }}>
                <span className="knowledge-check">{selected ? <Check size={13}/> : null}</span>
                <div className="knowledge-evidence-select-content">
                  <KnowledgeEvidenceLogRow entry={entry} errorRules={errorRules}/>
                  <div className="knowledge-evidence-select-meta"><span>异常规则：{evidence.anomaly_rules.map((rule) => rule.keyword).join(', ') || '—'}</span>{evidence.function_name && <span>函数：{evidence.function_name}</span>}{evidence.error_codes.length > 0 && <span>错误码：{evidence.error_codes.map((item) => item.value).join(', ')}</span>}</div>
                </div>
              </div>;
            })}
            {!abnormalEntries.length && <div className="knowledge-empty-inline"><AlertTriangle size={18}/>当前筛选范围没有异常日志。</div>}
          </div>
        </section>}

        {!props.caseItem && duplicateCandidates && <section className="knowledge-duplicate-governance">
          <header><div><ShieldCheck size={18}/><div><strong>发现疑似重复案例</strong><span>建议优先确认是否应补充已有案例；相似度仅表示指纹相似，不代表根因一定相同。</span></div></div><button type="button" onClick={() => setDuplicateCandidates(undefined)}>返回修改</button></header>
          <div className="knowledge-duplicate-list">
            {duplicateCandidates.map((candidate) => <article className={candidate.similarity >= 92 ? 'strong' : ''} key={candidate.case.id}>
              <div className="knowledge-duplicate-case-main"><div><strong>{candidate.case.name}</strong><span>{candidate.case.category || '未分类'} · 匹配现场「{candidate.matchedFeatureGroupTitle}」</span></div><div className="knowledge-duplicate-score"><b>{candidate.similarity.toFixed(1)}%</b><small>{candidate.similarity >= 92 ? '高度疑似重复' : '可能相关'}</small></div></div>
              {(candidate.case.symptom || candidate.case.root_cause) && <div className="knowledge-duplicate-case-copy">{candidate.case.symptom && <span><b>现象</b>{candidate.case.symptom}</span>}{candidate.case.root_cause && <span><b>根因</b>{candidate.case.root_cause}</span>}</div>}
              <footer><span>当前覆盖 {candidate.coverage.toFixed(0)}% 的该现场特征</span><button type="button" className="button primary compact" disabled={saving} onClick={() => void appendToExisting(candidate)}><GitMerge size={13}/>补充已有案例</button></footer>
            </article>)}
          </div>
          <div className="knowledge-duplicate-create-new"><div><PlusCircle size={16}/><span>确认这是不同问题时，可保留为独立案例。</span></div><button type="button" className="button secondary" disabled={saving} onClick={() => void save(true)}>仍创建新案例</button></div>
        </section>}

        <section className="knowledge-fingerprint-preview">
          <header><strong>指纹预览</strong><span>{previewEvidences.length} 条证据{props.caseItem ? ` · ${effectiveAbnormalCaseFeatureGroups(props.caseItem).length} 组现场特征` : ''}</span></header>
          <div className="knowledge-fingerprint-list">
            {previewEvidences.map((evidence, index) => <article key={`${evidence.source_entry_id || index}-${index}`}>
              <div className="knowledge-fingerprint-title"><b>证据 {index + 1}</b><span>{evidence.subsystem ? `${evidence.subsystem} / ` : ''}{evidence.module || evidence.component || '未知模块'}</span></div>
              <div className="knowledge-fingerprint-log-label">归一化指纹</div>
              <KnowledgeEvidenceLogRow evidence={evidence} messageOverride={evidence.template}/>
              <div className="knowledge-fingerprint-meta"><span>异常规则：{evidence.anomaly_rules?.length ? evidence.anomaly_rules.map((rule) => rule.keyword).join(', ') : '—'}</span><span>Token：{evidence.tokens?.slice(0, 10).join(', ') || '—'}</span><span>函数：{evidence.function_name || '—'}</span><span>错误码：{evidence.error_codes?.length ? evidence.error_codes.map((item) => item.value).join(', ') : '无显式错误码（不影响录入）'}</span></div>
            </article>)}
          </div>
        </section>
        {error && <div className="resource-alert error">{error}</div>}
      </div>

      <footer className="knowledge-dialog-footer"><button className="button secondary" disabled={saving} onClick={props.onClose}>取消</button><button className="button primary" disabled={saving || (!props.caseItem && !newEvidences.length)} onClick={() => void save()}><Save size={14}/>{saving ? (duplicateCandidates ? '处理中…' : '检查中…') : props.caseItem ? '保存案例' : duplicateCandidates ? '重新检查' : '保存并检查重复'}</button></footer>
    </section>
  </div>;
}
