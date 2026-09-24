import { useMemo, useState } from 'react';
import { AlertTriangle, BookOpenCheck, BookPlus, Check, GitMerge, PlusCircle, Save, ShieldCheck, X } from 'lucide-react';
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
  /** 由外层窗口（智能分析）承载标题栏与标签页时，这里只渲染表单本体。 */
  embedded?: boolean;
  /** 嵌入时把标题改成标签页语义，避免出现第二层「录入异常案例」。 */
  embeddedTitle?: string;
}

/** 举证类型的中文标签。后端会带 evidence_kind_label，这里只是老数据的兜底。 */
function evidenceKindLabel(evidence: AbnormalCaseEvidence): string {
  if (evidence.evidence_kind_label) return evidence.evidence_kind_label;
  if (evidence.evidence_kind === 'case_report' || evidence.source_category === 'case_report') return '用例报告';
  if (evidence.evidence_kind === 'case_fragment') return '用例片段';
  return '运行日志';
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
  // 用例片段举证：有些用例场景的案例根本不是日志报错，而是用例报告执行过程本身的报错。
  // 这类案例没有日志行可选，必须允许用户直接贴一段报告原文/用例描述作为举证。
  const [fragmentDraft, setFragmentDraft] = useState('');
  const [fragmentEvidences, setFragmentEvidences] = useState<AbnormalCaseEvidence[]>([]);
  const [duplicateCandidates, setDuplicateCandidates] = useState<AbnormalCaseDuplicateCandidate[]>();

  const newEvidences = useMemo(() => {
    const preset = presetEvidences.filter((evidence, index) => selectedPresetIds.has(evidence.source_entry_id || `preset-${index}`));
    const live = abnormalEntries.filter((entry) => selectedIds.has(entry.id)).map((entry) => createAbnormalEvidence(entry, errorRules));
    const seen = new Set<string>();
    return [...preset, ...fragmentEvidences, ...live].filter((evidence) => {
      const key = `${evidence.source_file}|${evidence.source_line || ''}|${evidence.raw}`;
      if (seen.has(key)) return false;
      seen.add(key); return true;
    });
  }, [abnormalEntries, errorRules, presetEvidences, fragmentEvidences, selectedIds, selectedPresetIds]);

  function addFragmentEvidence() {
    const text = fragmentDraft.trim();
    if (!text) return;
    const evidence: AbnormalCaseEvidence = {
      source_entry_id: `fragment-${Date.now()}-${fragmentEvidences.length}`,
      // 不写时间戳/级别：用例片段不是日志行，编一个假时间只会让它看起来像日志。
      timestamp: '',
      raw: text,
      message: text,
      level: '',
      severity: 'warning',
      subsystem: '',
      module: '',
      component: '',
      function_name: '',
      source_file: '',
      source_line: undefined,
      source_category: 'case_fragment',
      anomaly_rules: [],
      // 指纹字段留空：后端会按原文推导。这里不自己造模板，避免两套归一化各说各话。
      template: '',
      tokens: [],
      error_codes: [],
      evidence_kind: 'case_fragment',
      evidence_kind_label: '用例片段',
    };
    setFragmentEvidences((current) => [...current, evidence]);
    setFragmentDraft('');
    setDuplicateCandidates(undefined);
  }

  function removeFragmentEvidence(id: string) {
    setFragmentEvidences((current) => current.filter((item) => item.source_entry_id !== id));
    setDuplicateCandidates(undefined);
  }
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
    if (!props.caseItem && !newEvidences.length) { setError('至少选择一条举证：运行日志、用例报告或用例片段都可以。'); return; }
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

  const body = <>
      {props.embedded && props.embeddedTitle && (
        <div className="knowledge-embedded-title"><strong>{props.embeddedTitle}</strong></div>
      )}
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
          <header><div><BookOpenCheck size={17}/><strong>AI 已验证举证</strong><span>{selectedPresetIds.size} / {presetEvidences.length}</span></div><div className="knowledge-evidence-picker-actions"><small>可包含运行日志、用例报告和用例片段举证；可在保存前取消不需要的证据。</small></div></header>
          <div className="knowledge-evidence-list">
            {presetEvidences.map((evidence, index) => {
              const id = evidence.source_entry_id || `preset-${index}`;
              const selected = selectedPresetIds.has(id);
              return <div role="checkbox" aria-checked={selected} tabIndex={0} key={id} className={`knowledge-evidence-select ${selected ? 'selected' : ''}`} onClick={() => togglePresetEvidence(id)} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); togglePresetEvidence(id); } }}>
                <span className="knowledge-check">{selected ? <Check size={13}/> : null}</span>
                <div className="knowledge-evidence-select-content">
                  <KnowledgeEvidenceLogRow evidence={evidence}/>
                  <div className="knowledge-evidence-select-meta"><span>类型：{evidenceKindLabel(evidence)}</span><span>{evidence.anomaly_rules?.length ? `异常规则：${evidence.anomaly_rules.map((rule) => rule.keyword).join(', ')}` : '不含异常规则（不参与指纹比对）'}</span>{evidence.module && <span>组件：{evidence.subsystem ? `${evidence.subsystem} / ` : ''}${evidence.module}</span>}</div>
                </div>
              </div>;
            })}
          </div>
        </section>}

        {!props.caseItem && <section className="knowledge-evidence-picker knowledge-fragment-picker">
          <header><div><BookPlus size={17}/><strong>用例片段举证</strong><span>{fragmentEvidences.length} 条</span></div><div className="knowledge-evidence-picker-actions"><small>用例报告执行过程本身的报错（断言失败、超时、pytest/xytest 报错）不是日志行，直接贴原文即可入库。</small></div></header>
          <div className="knowledge-fragment-input">
            <textarea
              value={fragmentDraft}
              onChange={(event) => setFragmentDraft(event.target.value)}
              placeholder="例如：AssertionError: expected dose 3.0 got 5.2（粘贴用例报告/断言/执行过程的原文）"
              rows={3}
            />
            <button type="button" className="button secondary compact" disabled={!fragmentDraft.trim()} onClick={addFragmentEvidence}><PlusCircle size={13}/> 加为举证</button>
          </div>
          {fragmentEvidences.length > 0 && <div className="knowledge-evidence-list">
            {fragmentEvidences.map((evidence) => <div className="knowledge-evidence-select selected knowledge-fragment-row" key={evidence.source_entry_id}>
              <div className="knowledge-evidence-select-content">
                <KnowledgeEvidenceLogRow evidence={evidence} messageOverride={evidence.raw}/>
                <div className="knowledge-evidence-select-meta"><span>类型：用例片段</span><span>不含异常规则（不参与指纹比对，仍会入库并可被检索）</span></div>
              </div>
              <button type="button" className="knowledge-fragment-remove" aria-label="移除这条用例片段举证" onClick={() => removeFragmentEvidence(String(evidence.source_entry_id))}><X size={13}/></button>
            </div>)}
          </div>}
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
            {!abnormalEntries.length && <div className="knowledge-empty-inline"><AlertTriangle size={18}/>当前筛选范围没有异常日志；可以用上面的「用例片段举证」直接贴报告原文。</div>}
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
              <div className="knowledge-fingerprint-title"><b>证据 {index + 1} · {evidenceKindLabel(evidence)}</b><span>{evidence.subsystem ? `${evidence.subsystem} / ` : ''}{evidence.module || evidence.component || (evidence.evidence_kind === 'runtime_log' ? '未知模块' : '不绑定模块')}</span></div>
              <div className="knowledge-fingerprint-log-label">归一化指纹</div>
              <KnowledgeEvidenceLogRow evidence={evidence} messageOverride={evidence.template}/>
              <div className="knowledge-fingerprint-meta"><span>异常规则：{evidence.anomaly_rules?.length ? evidence.anomaly_rules.map((rule) => rule.keyword).join(', ') : '—'}</span><span>Token：{evidence.tokens?.slice(0, 10).join(', ') || '—'}</span><span>函数：{evidence.function_name || '—'}</span><span>错误码：{evidence.error_codes?.length ? evidence.error_codes.map((item) => item.value).join(', ') : '无显式错误码（不影响录入）'}</span>{evidence.matchable === false && <span>不参与指纹比对，仍会入库并可被检索</span>}</div>
            </article>)}
          </div>
        </section>
        {error && <div className="resource-alert error">{error}</div>}
      </div>

      <footer className="knowledge-dialog-footer"><button className="button secondary" disabled={saving} onClick={props.onClose}>取消</button><button className="button primary" disabled={saving || (!props.caseItem && !newEvidences.length)} onClick={() => void save()}><Save size={14}/>{saving ? (duplicateCandidates ? '处理中…' : '检查中…') : props.caseItem ? '保存案例' : duplicateCandidates ? '重新检查' : '保存并检查重复'}</button></footer>
    </>;

  if (props.embedded) {
    return <div className="knowledge-embedded-pane" onMouseDown={(event) => event.stopPropagation()}>{body}</div>;
  }
  return <div className="knowledge-dialog-backdrop" onMouseDown={() => { if (!saving) props.onClose(); }}>
    <section className="knowledge-editor-dialog" onMouseDown={(event) => event.stopPropagation()}>
      <header className="knowledge-dialog-header">
        <div><h2>{props.caseItem ? '编辑异常案例' : '录入异常案例'}</h2></div>
        <button type="button" className="icon-button" disabled={saving} onClick={props.onClose}><X size={18}/></button>
      </header>
      {body}
    </section>
  </div>;
}
