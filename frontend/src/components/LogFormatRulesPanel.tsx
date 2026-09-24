import { useEffect, useMemo, useState } from 'react';
import { Check, Copy, FlaskConical, LoaderCircle, Pencil, Plus, RefreshCw, RotateCcw, Save, Search, Trash2, X } from 'lucide-react';
import {
  createLogFormatRule,
  deleteLogFormatRule,
  getResourceSettings,
  listLogFormatRules,
  restoreDefaultLogFormatRule,
  testLogFormatRuleDraft,
  updateLogFormatRule,
  type LogFormatRuleDraft,
  type LogFormatRuleTestResult,
} from '../api/resourceApi';
import type { LogFormatParserRuleConfig } from '../types';

const CORE_FIELDS = [
  ['timestamp', '时间'],
  ['level', '级别'],
  ['component', '组件'],
  ['process_id', 'PID'],
  ['thread_id', 'TID'],
  ['source', '源码位置'],
  ['mode', '模式'],
  ['rpc', 'Trace / RPC'],
  ['message', '正文'],
] as const;

const RUN_FIELDS = [
  ['event_category', '事件类别'],
  ['event_level', '事件级别'],
  ['current_event_code', '当前事件码'],
  ['linked_event_codes', '关联事件码'],
  ['current_err_iid', '当前 ErrIId'],
  ['linked_err_iids', '关联 ErrIId'],
  ['display_code', 'DisplayCode'],
  ['linked_display_codes', '关联 DisplayCode'],
  ['event_type', 'SET / EVT / DEA / CLR'],
] as const;

const TIMESTAMP_FORMATS = [
  ['auto', '自动识别'],
  ['%Y-%m-%d %H:%M:%S.%f', 'yyyy-MM-dd HH:mm:ss.SSS…'],
  ['%Y-%m-%d %H:%M:%S', 'yyyy-MM-dd HH:mm:ss'],
  ['%Y/%m/%d %H:%M:%S.%f', 'yyyy/MM/dd HH:mm:ss.SSS…'],
  ['%Y/%m/%d %H:%M:%S', 'yyyy/MM/dd HH:mm:ss'],
  ['unix_ms', 'Unix 毫秒'],
  ['unix_ns', 'Unix 纳秒'],
] as const;

const DEFAULT_PATTERN = String.raw`^\[(?P<timestamp>[^\]]+)]\s+\[(?P<level>[^\]]+)]\s*(?P<message>.*)$`;

interface DraftState extends LogFormatRuleDraft {
  id?: number;
  built_in?: boolean;
}

function emptyDraft(category = 'debug'): DraftState {
  return {
    name: '',
    category,
    enabled: true,
    priority: 100,
    file_pattern: '*.log*',
    pattern: DEFAULT_PATTERN,
    ignore_case: false,
    field_map: { timestamp: 'timestamp', level: 'level', message: 'message' },
    timestamp_format: 'auto',
    description: '',
  };
}

function toDraft(rule: LogFormatParserRuleConfig): DraftState {
  return {
    id: rule.id,
    built_in: rule.built_in,
    name: rule.name,
    category: rule.category,
    enabled: rule.enabled,
    priority: rule.priority,
    file_pattern: rule.file_pattern,
    pattern: rule.pattern,
    ignore_case: rule.ignore_case,
    field_map: { ...rule.field_map },
    timestamp_format: rule.timestamp_format,
    description: rule.description,
  };
}

function categoryLabel(category: string, options: Array<{ category: string; label: string }>): string {
  return options.find((item) => item.category === category)?.label ?? category;
}

function mappedFieldCount(rule: Pick<LogFormatParserRuleConfig, 'field_map'>): number {
  return Object.values(rule.field_map || {}).filter(Boolean).length;
}

function notifyRuntimeChanged() {
  window.dispatchEvent(new CustomEvent('tracelens:log-format-rules-changed'));
}

export function LogFormatRulesPanel({ sampleRaw = '' }: { sampleRaw?: string }) {
  const [rules, setRules] = useState<LogFormatParserRuleConfig[]>([]);
  const [categories, setCategories] = useState<Array<{ category: string; label: string }>>([
    { category: 'debug', label: '调试日志' },
    { category: 'executor', label: '执行器日志' },
    { category: 'run', label: '运行日志' },
  ]);
  const [query, setQuery] = useState('');
  const [busy, setBusy] = useState<'load' | 'save' | 'test' | 'restore' | 'delete' | ''>('load');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [draft, setDraft] = useState<DraftState>();
  const [testText, setTestText] = useState(sampleRaw);
  const [testResult, setTestResult] = useState<LogFormatRuleTestResult>();

  async function load() {
    setBusy('load');
    setError('');
    try {
      const [values, settings] = await Promise.all([
        listLogFormatRules(),
        getResourceSettings().catch(() => undefined),
      ]);
      setRules(values);
      if (settings?.log_paths?.length) {
        const next = settings.log_paths.map((item) => ({ category: item.category, label: item.display_name || item.category }));
        const known = new Set(next.map((item) => item.category));
        values.forEach((rule) => { if (!known.has(rule.category)) next.push({ category: rule.category, label: rule.category }); });
        setCategories(next);
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy('');
    }
  }

  useEffect(() => { void load(); }, []);
  useEffect(() => { if (sampleRaw && !testText) setTestText(sampleRaw); }, [sampleRaw]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return rules;
    return rules.filter((rule) => `${rule.name} ${rule.category} ${rule.file_pattern} ${rule.pattern} ${rule.description}`.toLowerCase().includes(needle));
  }, [rules, query]);

  function beginCreate() {
    setDraft(emptyDraft(categories[0]?.category || 'debug'));
    setTestText(sampleRaw || '');
    setTestResult(undefined);
    setError('');
    setMessage('');
  }

  function beginEdit(rule: LogFormatParserRuleConfig) {
    setDraft(toDraft(rule));
    setTestText(sampleRaw || '');
    setTestResult(undefined);
    setError('');
    setMessage('');
  }

  function beginCopy(rule: LogFormatParserRuleConfig) {
    const copied = toDraft(rule);
    delete copied.id;
    copied.built_in = false;
    copied.name = `${rule.name} 副本`;
    setDraft(copied);
    setTestText(sampleRaw || '');
    setTestResult(undefined);
    setError('');
    setMessage('');
  }

  function patch(patchValue: Partial<DraftState>) {
    setDraft((current) => current ? { ...current, ...patchValue } : current);
    setTestResult(undefined);
  }

  function updateFieldMap(field: string, groupName: string) {
    setDraft((current) => {
      if (!current) return current;
      const field_map = { ...current.field_map };
      if (groupName.trim()) field_map[field] = groupName.trim(); else delete field_map[field];
      return { ...current, field_map };
    });
    setTestResult(undefined);
  }

  function payloadOf(value: DraftState): LogFormatRuleDraft {
    return {
      name: value.name.trim(),
      category: value.category.trim().toLowerCase(),
      enabled: value.enabled,
      priority: Number(value.priority) || 0,
      file_pattern: value.file_pattern.trim(),
      pattern: value.pattern,
      ignore_case: value.ignore_case,
      field_map: Object.fromEntries(Object.entries(value.field_map).filter(([, group]) => String(group).trim()).map(([field, group]) => [field, String(group).trim()])),
      timestamp_format: value.timestamp_format,
      description: value.description.trim(),
    };
  }

  async function runTest() {
    if (!draft) return;
    setBusy('test');
    setError('');
    setMessage('');
    try {
      const result = await testLogFormatRuleDraft(payloadOf(draft), testText);
      setTestResult(result);
    } catch (exc) {
      setTestResult(undefined);
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy('');
    }
  }

  async function save() {
    if (!draft) return;
    setBusy('save');
    setError('');
    setMessage('');
    try {
      const payload = payloadOf(draft);
      const saved = draft.id ? await updateLogFormatRule(draft.id, payload) : await createLogFormatRule(payload);
      setRules((current) => draft.id ? current.map((item) => item.id === saved.id ? saved : item) : [...current, saved].sort((a, b) => a.category.localeCompare(b.category) || b.priority - a.priority));
      setDraft(undefined);
      setTestResult(undefined);
      setMessage(`解析规则“${saved.name}”已保存，后续新日志解析任务将使用最新配置。`);
      notifyRuntimeChanged();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy('');
    }
  }

  async function toggleRule(rule: LogFormatParserRuleConfig) {
    setError('');
    try {
      const saved = await updateLogFormatRule(rule.id, { enabled: !rule.enabled });
      setRules((current) => current.map((item) => item.id === saved.id ? saved : item));
      notifyRuntimeChanged();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }

  async function removeRule(rule: LogFormatParserRuleConfig) {
    if (rule.built_in || !window.confirm(`删除解析规则“${rule.name}”？`)) return;
    setBusy('delete');
    setError('');
    try {
      await deleteLogFormatRule(rule.id);
      setRules((current) => current.filter((item) => item.id !== rule.id));
      notifyRuntimeChanged();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy('');
    }
  }

  async function restore(rule: LogFormatParserRuleConfig) {
    if (!rule.built_in || !window.confirm(`恢复“${rule.name}”的系统默认正则和字段映射？`)) return;
    setBusy('restore');
    setError('');
    try {
      const saved = await restoreDefaultLogFormatRule(rule.id);
      setRules((current) => current.map((item) => item.id === saved.id ? saved : item));
      if (draft?.id === saved.id) setDraft(toDraft(saved));
      setMessage(`“${rule.name}”已恢复默认。`);
      notifyRuntimeChanged();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy('');
    }
  }

  const firstMatched = testResult?.results.find((item) => item.matched);
  const unmatched = testResult?.results.filter((item) => !item.matched) ?? [];

  return <div className="log-format-rules-panel">
    <div className="log-format-rules-toolbar">
      <div className="log-format-rules-intro"><strong>日志格式解析</strong><span>维护每类日志的行级正则和标准字段映射；测试由 Django/Python 正式校验器执行。</span></div>
      <div className="log-format-rules-actions">
        <label className="log-format-search"><Search size={14}/><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索日志类型 / 正则…"/></label>
        <button className="button secondary compact" onClick={() => void load()} disabled={busy === 'load'}>{busy === 'load' ? <LoaderCircle className="spin" size={14}/> : <RefreshCw size={14}/>} 刷新</button>
        <button className="button primary compact" onClick={beginCreate}><Plus size={14}/> 新增解析规则</button>
      </div>
    </div>

    {error && <div className="resource-alert error"><X size={14}/>{error}</div>}
    {message && <div className="resource-alert success"><Check size={14}/>{message}</div>}

    <div className="log-format-rule-table-wrap">
      <table className="rules-table log-format-rule-table">
        <thead><tr><th>日志类型</th><th>规则名称</th><th>优先级</th><th>适用文件</th><th>正则表达式</th><th>字段</th><th>来源</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>
          {filtered.map((rule) => <tr key={rule.id}>
            <td><span className="log-format-category">{categoryLabel(rule.category, categories)}</span><small>{rule.category}</small></td>
            <td><strong>{rule.name}</strong>{rule.description && <small title={rule.description}>{rule.description}</small>}</td>
            <td>{rule.priority}</td>
            <td><code className="compact-code">{rule.file_pattern || '全部'}</code></td>
            <td className="log-format-pattern-cell"><code title={rule.pattern}>{rule.pattern}</code></td>
            <td><span>{mappedFieldCount(rule)} 个</span><small>{Object.keys(rule.field_map).join(' / ')}</small></td>
            <td><span className={`rule-origin-badge ${rule.built_in ? 'builtin' : 'custom'}`}>{rule.built_in ? '系统内置' : '用户自定义'}</span></td>
            <td><button className={`rule-status-button ${rule.enabled ? 'enabled' : ''}`} onClick={() => void toggleRule(rule)}>{rule.enabled ? '启用' : '停用'}</button></td>
            <td><div className="rules-row-actions"><button onClick={() => beginEdit(rule)}><Pencil size={13}/> 编辑</button><button onClick={() => beginCopy(rule)}><Copy size={13}/> 复制</button>{rule.built_in ? <button onClick={() => void restore(rule)}><RotateCcw size={13}/> 恢复默认</button> : <button className="danger" onClick={() => void removeRule(rule)}><Trash2 size={13}/> 删除</button>}</div></td>
          </tr>)}
          {!filtered.length && <tr><td colSpan={9}><div className="resource-empty inline-empty">没有日志格式解析规则。</div></td></tr>}
        </tbody>
      </table>
    </div>

    {draft && <div className="rule-drawer-backdrop log-format-rule-backdrop" onMouseDown={() => setDraft(undefined)}>
      <aside className="rule-drawer log-format-rule-drawer" onMouseDown={(event) => event.stopPropagation()}>
        <header><div><span className="eyebrow">LOG FORMAT PARSER</span><h2>{draft.id ? '编辑日志格式解析规则' : '新增日志格式解析规则'}</h2><p>使用 Python re 命名捕获组，例如 <code>{'(?P<timestamp>...)'}</code>；字段映射决定如何生成统一 LogEntry。</p></div><button className="icon-button" onClick={() => setDraft(undefined)}><X size={18}/></button></header>
        <div className="rule-drawer-body log-format-rule-editor-body">
          <section className="log-format-editor-section">
            <h3>基本信息</h3>
            <div className="rule-form-grid three-columns">
              <label><span>规则名称</span><input value={draft.name} disabled={Boolean(draft.built_in)} onChange={(event) => patch({ name: event.target.value })} placeholder="例如：新版控制器日志"/>{draft.built_in && <small>系统内置规则名称固定；如需改名请复制为自定义规则。</small>}</label>
              <label><span>日志类型</span><select value={draft.category} disabled={Boolean(draft.built_in)} onChange={(event) => patch({ category: event.target.value })}>{categories.map((item) => <option value={item.category} key={item.category}>{item.label} · {item.category}</option>)}{!categories.some((item) => item.category === draft.category) && <option value={draft.category}>{draft.category}</option>}</select></label>
              <label><span>优先级</span><input type="number" value={draft.priority} onChange={(event) => patch({ priority: Number(event.target.value) })}/></label>
              <label><span>适用文件</span><input value={draft.file_pattern} onChange={(event) => patch({ file_pattern: event.target.value })} placeholder="*.log* 或 event.log,*.out"/></label>
              <label><span>时间格式</span><select value={draft.timestamp_format} onChange={(event) => patch({ timestamp_format: event.target.value })}>{TIMESTAMP_FORMATS.map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
              <label className="log-format-inline-switch"><span>匹配选项</span><span><input type="checkbox" checked={draft.ignore_case} onChange={(event) => patch({ ignore_case: event.target.checked })}/> 忽略大小写</span><span><input type="checkbox" checked={draft.enabled} onChange={(event) => patch({ enabled: event.target.checked })}/> 启用</span></label>
            </div>
            <label className="span-2"><span>说明</span><input value={draft.description} onChange={(event) => patch({ description: event.target.value })} placeholder="说明适用版本、来源或兼容条件"/></label>
          </section>

          <section className="log-format-editor-section">
            <div className="log-format-section-heading"><div><h3>正则表达式</h3><p>正式保存前由后端 Python re 编译；命名组会转换为浏览器 Worker 可执行的 portable pattern。</p></div></div>
            <textarea className="log-format-pattern-editor" rows={8} spellCheck={false} value={draft.pattern} onChange={(event) => patch({ pattern: event.target.value })}/>
          </section>

          <section className="log-format-editor-section">
            <div className="log-format-section-heading"><div><h3>标准字段映射</h3><p>右侧填写正则中的命名捕获组名称。正文 message 为必填；时间字段建议配置。</p></div></div>
            <div className="log-format-field-map-grid">
              {CORE_FIELDS.map(([field, label]) => <label key={field}><span><strong>{label}</strong><code>{field}</code></span><input value={draft.field_map[field] ?? ''} onChange={(event) => updateFieldMap(field, event.target.value)} placeholder={`捕获组，例如 ${field}`}/></label>)}
            </div>
            <details className="log-format-run-fields"><summary>运行事件扩展字段</summary><div className="log-format-field-map-grid">{RUN_FIELDS.map(([field, label]) => <label key={field}><span><strong>{label}</strong><code>{field}</code></span><input value={draft.field_map[field] ?? ''} onChange={(event) => updateFieldMap(field, event.target.value)} placeholder={`捕获组，例如 ${field}`}/></label>)}</div></details>
          </section>

          <section className="log-format-editor-section log-format-test-section">
            <div className="log-format-section-heading"><div><h3>测试验证</h3><p>可粘贴单条或批量日志，最多 200 行。测试与后端保存校验使用同一 Python 解析器。</p></div><button className="button secondary" onClick={() => void runTest()} disabled={busy === 'test' || !testText.trim()}>{busy === 'test' ? <LoaderCircle className="spin" size={14}/> : <FlaskConical size={14}/>} 测试解析</button></div>
            <textarea className="log-format-test-input" rows={8} value={testText} onChange={(event) => { setTestText(event.target.value); setTestResult(undefined); }} placeholder="粘贴真实日志；多行会统计命中率并列出未命中行。"/>
            {testResult && <div className="log-format-test-result">
              <div className="log-format-test-stats"><span><strong>{testResult.line_count}</strong> 测试行</span><span className="success"><strong>{testResult.matched_count}</strong> 命中</span><span className={testResult.unmatched_count ? 'warning' : 'success'}><strong>{testResult.unmatched_count}</strong> 未命中</span><span><strong>{testResult.match_rate}%</strong> 命中率</span>{draft.field_map.timestamp && <span><strong>{testResult.timestamp_valid_count}</strong> 有效时间</span>}</div>
              {testResult.group_names.length > 0 && <div className="log-format-capture-groups"><strong>识别捕获组</strong>{testResult.group_names.map((name) => <code key={name}>{name}</code>)}</div>}
              {firstMatched && <div className="log-format-field-preview"><strong>首条命中 · 标准字段</strong><div>{Object.entries(firstMatched.fields).map(([field, value]) => <span key={field}><code>{field}</code><em title={value}>{value || '—'}</em>{field === 'timestamp' && <b className={firstMatched.timestamp_valid ? 'ok' : 'bad'}>{firstMatched.timestamp_valid ? '时间有效' : '时间无效'}</b>}</span>)}</div></div>}
              {unmatched.length > 0 && <details className="log-format-unmatched" open><summary>未命中日志（{unmatched.length}）</summary><div>{unmatched.slice(0, 30).map((item) => <p key={item.line_number}><b>L{item.line_number}</b><code>{item.raw}</code></p>)}</div></details>}
            </div>}
          </section>
        </div>
        <footer><span className="log-format-save-hint">{draft.built_in ? '系统内置规则可修改；需要时可在列表中恢复默认。' : '用户规则保存后立即参与后续新日志任务解析。'}</span><div><button className="button ghost" onClick={() => setDraft(undefined)}>取消</button><button className="button secondary" onClick={() => void runTest()} disabled={busy === 'test' || !testText.trim()}><FlaskConical size={14}/> 测试</button><button className="button primary" onClick={() => void save()} disabled={busy === 'save'}>{busy === 'save' ? <LoaderCircle className="spin" size={14}/> : <Save size={14}/>} 保存规则</button></div></footer>
      </aside>
    </div>}
  </div>;
}
