import { useEffect, useMemo, useState } from 'react';
import { Check, LoaderCircle, Pencil, Plus, RefreshCw, Save, Search, Trash2, X } from 'lucide-react';
import {
  createLogQuerySkill,
  deleteLogQuerySkill,
  getGlobalLogCatalogTree,
  listLogQuerySkills,
  updateLogQuerySkill,
  type GlobalLogSubsystem,
  type LogQuerySkill,
  type LogQuerySkillMachineScope,
  type LogQuerySkillSourceType,
  type LogQuerySkillStep,
  type LogQuerySkillWhen,
} from '../api/resourceApi';

const WHEN_OPTIONS: Array<[LogQuerySkillWhen, string]> = [
  ['insufficient_evidence', '证据不足'],
  ['no_match', '标准查询无匹配'],
  ['source_empty', '日志源为空'],
  ['source_not_found', '日志源不存在'],
  ['keyword_match', '命中特征后'],
  ['always', '始终执行'],
];
const SCOPE_OPTIONS: Array<[LogQuerySkillMachineScope, string]> = [
  ['upper', '上位机'], ['lower', '单个下位机'], ['all_lower', '全部下位机'], ['dhh', 'DHH'],
];

function emptyStep(): LogQuerySkillStep {
  return {
    name: '补充日志',
    when: 'insufficient_evidence',
    source_type: 'standard',
    machine_scope: 'upper',
    source_category: 'debug',
    module: '',
    path_template: '',
    file_pattern: '*.log*',
    keywords: [],
    time_before_seconds: 5,
    time_after_seconds: 5,
    note: '',
  };
}

function emptyDraft(subsystemId = 0): LogQuerySkill {
  return {
    id: 0,
    subsystem: subsystemId,
    subsystem_name: '',
    subsystem_display_name: '',
    name: '',
    enabled: true,
    priority: 100,
    trigger_modules: [],
    trigger_keywords: [],
    description: '',
    steps: [emptyStep()],
  };
}

function splitText(value: string): string[] {
  return Array.from(new Set(value.split(/[,，;；\n]+/).map((item) => item.trim()).filter(Boolean)));
}

function syncAssistantSummary(skills: LogQuerySkill[]) {
  if (typeof window === 'undefined') return;
  const summary = skills.slice(0, 80).map((skill) => ({
    id: skill.id,
    name: skill.name,
    subsystem: skill.subsystem_name,
    modules: skill.trigger_modules.slice(0, 8),
    keywords: skill.trigger_keywords.slice(0, 12),
    steps: skill.steps.slice(0, 6).map((step) => ({
      name: step.name,
      when: step.when,
      source_type: step.source_type,
      machine_scope: step.machine_scope,
      source_category: step.source_category,
      module: step.module,
    })),
    enabled: skill.enabled,
  }));
  window.sessionStorage.setItem('tracelens-log-query-skill-summary-v1', JSON.stringify(summary));
  window.dispatchEvent(new CustomEvent('tracelens:assistant-context-changed'));
}

export function LogQuerySkillsPanel() {
  const [subsystems, setSubsystems] = useState<GlobalLogSubsystem[]>([]);
  const [skills, setSkills] = useState<LogQuerySkill[]>([]);
  const [subsystemFilter, setSubsystemFilter] = useState('');
  const [query, setQuery] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [draft, setDraft] = useState<LogQuerySkill>();

  async function load() {
    setBusy(true); setError('');
    try {
      const [tree, rows] = await Promise.all([getGlobalLogCatalogTree(true), listLogQuerySkills()]);
      setSubsystems(tree);
      setSkills(rows);
      syncAssistantSummary(rows);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => { void load(); }, []);

  const selectedSubsystem = useMemo(
    () => subsystems.find((item) => item.id === draft?.subsystem),
    [subsystems, draft?.subsystem],
  );
  const subsystemModules = selectedSubsystem?.fms.filter((item) => item.enabled).map((item) => item.name) ?? [];

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return skills.filter((skill) => {
      if (subsystemFilter && String(skill.subsystem) !== subsystemFilter) return false;
      if (!needle) return true;
      return `${skill.name} ${skill.subsystem_name} ${skill.subsystem_display_name} ${skill.description} ${skill.trigger_modules.join(' ')} ${skill.trigger_keywords.join(' ')}`.toLowerCase().includes(needle);
    });
  }, [skills, subsystemFilter, query]);

  function startCreate() {
    const first = subsystemFilter ? Number(subsystemFilter) : (subsystems.find((item) => item.enabled)?.id ?? subsystems[0]?.id ?? 0);
    setDraft(emptyDraft(first));
    setError(''); setMessage('');
  }

  function patchStep(index: number, patch: Partial<LogQuerySkillStep>) {
    setDraft((current) => current ? ({
      ...current,
      steps: current.steps.map((step, idx) => idx === index ? ({ ...step, ...patch }) : step),
    }) : current);
  }

  async function saveDraft() {
    if (!draft) return;
    if (!draft.subsystem) { setError('请选择子系统。'); return; }
    if (!draft.name.trim()) { setError('请输入 Skill 名称。'); return; }
    if (!draft.steps.length) { setError('至少配置一个补充检索步骤。'); return; }
    setBusy(true); setError(''); setMessage('');
    try {
      const payload = {
        subsystem: draft.subsystem,
        name: draft.name.trim(),
        enabled: draft.enabled,
        priority: Number(draft.priority) || 100,
        trigger_modules: draft.trigger_modules,
        trigger_keywords: draft.trigger_keywords,
        description: draft.description,
        steps: draft.steps,
      };
      if (draft.id) await updateLogQuerySkill(draft.id, payload);
      else await createLogQuerySkill(payload);
      setDraft(undefined);
      setMessage('日志查询 Skill 已保存。');
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  }

  async function removeSkill(skill: LogQuerySkill) {
    if (!window.confirm(`删除 ${skill.subsystem_name}/${skill.name}？`)) return;
    setBusy(true); setError('');
    try {
      await deleteLogQuerySkill(skill.id);
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  }

  async function toggleSkill(skill: LogQuerySkill) {
    setBusy(true); setError('');
    try {
      await updateLogQuerySkill(skill.id, { enabled: !skill.enabled });
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  }

  return <section className="log-query-skills-panel">
    <div className="log-query-skills-toolbar">
      <div className="log-query-skills-filters">
        <select value={subsystemFilter} onChange={(event) => setSubsystemFilter(event.target.value)} aria-label="筛选子系统">
          <option value="">全部子系统</option>
          {subsystems.map((item) => <option key={item.id} value={item.id}>{item.display_name || item.name}</option>)}
        </select>
        <label><Search size={15}/><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="筛选 Skill…"/></label>
      </div>
      <div className="log-query-skills-actions"><button className="button secondary compact" onClick={() => void load()} disabled={busy}>{busy ? <LoaderCircle className="spin" size={14}/> : <RefreshCw size={14}/>} 刷新</button><button className="button primary compact" onClick={startCreate}><Plus size={14}/> 新增日志查询 Skill</button></div>
    </div>
    {error && <div className="resource-alert error"><X size={14}/>{error}</div>}
    {message && <div className="resource-alert success"><Check size={14}/>{message}</div>}
    <div className="rules-table-wrap"><table className="rules-table log-query-skills-table"><thead><tr><th>子系统</th><th>Skill</th><th>触发模块</th><th>触发关键字</th><th>补充步骤</th><th>优先级</th><th>状态</th><th>操作</th></tr></thead><tbody>
      {visible.map((skill) => <tr key={skill.id}><td><strong>{skill.subsystem_display_name || skill.subsystem_name}</strong><small>{skill.subsystem_name}</small></td><td><strong>{skill.name}</strong><small title={skill.description}>{skill.description || '—'}</small></td><td>{skill.trigger_modules.length ? skill.trigger_modules.join('、') : '子系统通用'}</td><td>{skill.trigger_keywords.length ? skill.trigger_keywords.join('、') : '—'}</td><td>{skill.steps.length}</td><td>{skill.priority}</td><td><span className={`rule-status ${skill.enabled ? 'enabled' : ''}`}>{skill.enabled ? '启用' : '停用'}</span></td><td><div className="rules-row-actions"><button onClick={() => setDraft({ ...skill, steps: skill.steps.map((step) => ({ ...step, keywords: [...step.keywords] })) })}><Pencil size={13}/> 编辑</button><button onClick={() => void toggleSkill(skill)}>{skill.enabled ? '停用' : '启用'}</button><button className="danger" onClick={() => void removeSkill(skill)}><Trash2 size={13}/> 删除</button></div></td></tr>)}
      {!visible.length && <tr><td colSpan={8}><div className="resource-empty inline-empty">当前筛选条件下没有日志查询 Skill。</div></td></tr>}
    </tbody></table></div>

    {draft && <div className="rule-drawer-backdrop" onMouseDown={() => setDraft(undefined)}><aside className="rule-drawer log-query-skill-drawer" onMouseDown={(event) => event.stopPropagation()}><header><div><h2>{draft.id ? '编辑日志查询 Skill' : '新增日志查询 Skill'}</h2></div><button type="button" className="icon-button" onClick={() => setDraft(undefined)}><X size={18}/></button></header><div className="rule-drawer-body">
      <div className="log-query-skill-grid">
        <label>子系统<select value={draft.subsystem || ''} onChange={(event) => {
          const subsystem = Number(event.target.value);
          setDraft({
            ...draft,
            subsystem,
            trigger_modules: [],
            steps: draft.steps.map((step) => step.source_type === 'standard' ? { ...step, module: '' } : step),
          });
        }}><option value="">请选择</option>{subsystems.map((item) => <option key={item.id} value={item.id}>{item.display_name || item.name}</option>)}</select></label>
        <label>Skill 名称<input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} placeholder="例如 Encoder 反馈异常下钻"/></label>
        <label>优先级<input type="number" value={draft.priority} onChange={(event) => setDraft({ ...draft, priority: Number(event.target.value) })}/></label>
        <label className="rule-switch log-query-skill-enabled"><input type="checkbox" checked={draft.enabled} onChange={(event) => setDraft({ ...draft, enabled: event.target.checked })}/> 启用</label>
      </div>
      <label>触发模块<input value={draft.trigger_modules.join(', ')} onChange={(event) => setDraft({ ...draft, trigger_modules: splitText(event.target.value) })} placeholder="MotionCtrl, ServoCtrl"/></label>
      <label>触发关键字<input value={draft.trigger_keywords.join(', ')} onChange={(event) => setDraft({ ...draft, trigger_keywords: splitText(event.target.value) })} placeholder="EncoderRead, timeout, feedback lost"/></label>
      <label>规则说明<textarea rows={4} value={draft.description} onChange={(event) => setDraft({ ...draft, description: event.target.value })} placeholder="说明什么场景需要继续看哪些下层日志，以及什么证据才算根因。"/></label>
      <div className="log-query-skill-step-heading"><strong>补充检索步骤</strong><button type="button" className="button secondary compact" disabled={draft.steps.length >= 8} onClick={() => setDraft({ ...draft, steps: [...draft.steps, emptyStep()] })}><Plus size={13}/> 添加步骤</button></div>
      <div className="log-query-skill-steps">{draft.steps.map((step, index) => <section className="log-query-skill-step" key={`${index}-${step.name}`}><div className="log-query-skill-step-title"><strong>{index + 1}. {step.name || '补充日志'}</strong><button type="button" className="text-danger-button" onClick={() => setDraft({ ...draft, steps: draft.steps.filter((_, idx) => idx !== index) })} disabled={draft.steps.length <= 1}>删除</button></div><div className="log-query-skill-step-grid">
        <label>步骤名称<input value={step.name} onChange={(event) => patchStep(index, { name: event.target.value })}/></label>
        <label>执行条件<select value={step.when} onChange={(event) => patchStep(index, { when: event.target.value as LogQuerySkillWhen })}>{WHEN_OPTIONS.map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
        <label>日志来源<select value={step.source_type} onChange={(event) => patchStep(index, { source_type: event.target.value as LogQuerySkillSourceType })}><option value="standard">平台标准日志</option><option value="custom_path">补充远端路径</option></select></label>
        <label>机器范围<select value={step.machine_scope} onChange={(event) => patchStep(index, { machine_scope: event.target.value as LogQuerySkillMachineScope })}>{SCOPE_OPTIONS.map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
        {step.source_type === 'standard' ? <><label>日志类型<input value={step.source_category} onChange={(event) => patchStep(index, { source_category: event.target.value })} placeholder="debug"/></label><label>目标模块<select value={step.module} onChange={(event) => patchStep(index, { module: event.target.value })}><option value="">请选择当前子系统模块</option>{subsystemModules.map((module) => <option key={module} value={module}>{module}</option>)}</select></label></> : <><label className="span-2">远端路径<input value={step.path_template} onChange={(event) => patchStep(index, { path_template: event.target.value })} placeholder="/var/log/ethercat"/></label><label>文件匹配<input value={step.file_pattern} onChange={(event) => patchStep(index, { file_pattern: event.target.value })} placeholder="*.log*"/></label><label>模块参数<input value={step.module} onChange={(event) => patchStep(index, { module: event.target.value })} placeholder="可供 {module} 路径参数使用"/></label></>}
        <label className="span-2">步骤关键字<input value={step.keywords.join(', ')} onChange={(event) => patchStep(index, { keywords: splitText(event.target.value) })} placeholder="可选；多个关键字任一命中"/></label>
        <label>前扩时间（秒）<input type="number" min={0} max={600} value={step.time_before_seconds} onChange={(event) => patchStep(index, { time_before_seconds: Number(event.target.value) })}/></label>
        <label>后扩时间（秒）<input type="number" min={0} max={600} value={step.time_after_seconds} onChange={(event) => patchStep(index, { time_after_seconds: Number(event.target.value) })}/></label>
        <label className="span-2">备注<input value={step.note} onChange={(event) => patchStep(index, { note: event.target.value })} placeholder="例如：如果只有 communication timeout，继续下一步。"/></label>
      </div></section>)}</div>
    </div><footer><button className="button ghost" onClick={() => setDraft(undefined)}>取消</button><button className="button primary" onClick={() => void saveDraft()} disabled={busy}>{busy ? <LoaderCircle className="spin" size={14}/> : <Save size={14}/>} 保存 Skill</button></footer></aside></div>}
  </section>;
}
