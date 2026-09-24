import { registerPageContextReader } from '../assistant/contextRegistry';
import { useEffect, useMemo, useState } from 'react';
import { CheckCircle2, Database, FileCode2, FileText, KeyRound, LoaderCircle, Network, Plus, RefreshCw, Save, Settings2, Trash2, X } from 'lucide-react';
import { getResourceSettings, updateResourceSettings, type LogPathProfile, type ResourceSettings } from '../api/resourceApi';
import type { DisplayRule, DisplayRuleEditorRequest } from '../rendering/displayRules';
import type { MaskingRule } from '../rendering/maskingRules';
import type { FoldingRule } from '../rendering/foldingRules';
import type { DataExtractionRule } from '../rendering/dataExtractionRules';
import type { ErrorMatchRule } from '../parser/logParser';
import { LogCatalogSettingsPage } from './LogCatalogSettingsPage';
import { LogRulesSettingsPage } from './LogRulesSettingsPage';

type Tab = 'resource' | 'catalog' | 'rules';
interface Props {
  initialTab?: Tab;
  errorRules: readonly ErrorMatchRule[];
  displayRules: readonly DisplayRule[];
  maskingRules: readonly MaskingRule[];
  foldingRules: readonly FoldingRule[];
  dataExtractionRules: readonly DataExtractionRule[];
  editorRequest?: DisplayRuleEditorRequest;
  onErrorRulesChange: (values: ErrorMatchRule[]) => void;
  onDisplayRulesChange: (rules: DisplayRule[]) => void;
  onMaskingRulesChange: (rules: MaskingRule[]) => void;
  onFoldingRulesChange: (rules: FoldingRule[]) => void;
  onDataExtractionRulesChange: (rules: DataExtractionRule[]) => void;
  onEditorRequestHandled?: () => void;
}
const RULE_LABEL: Record<string, string> = { fm: 'FM.log', fm_timestamp: 'FM_时间戳.log', archive: '压缩包 *.tar.gz', executor_tree: '执行器：IP/子系统/任意日志', run_flat: '运行：event.log / 时间归档' };
const SCOPE_OPTIONS = [
  ['upper_only', '上位机'], ['lower_only', '下位机'], ['each_machine', '上位机 + 下位机'], ['upper_for_lower', '上位机按下位机 IP 展开路径'],
] as const;
function slug(value: string): string { return value.trim().toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, ''); }

export function PlatformSettingsPage(props: Props) {
  const [tab, setTab] = useState<Tab>(props.editorRequest ? 'rules' : (props.initialTab ?? 'resource'));
  const [settings, setSettings] = useState<ResourceSettings>();
  const [draft, setDraft] = useState<ResourceSettings>();
  const [busy, setBusy] = useState('load');
  const [error, setError] = useState(''); const [message, setMessage] = useState('');
  const [selectedTypes, setSelectedTypes] = useState<Set<string>>(new Set());
  useEffect(() => { if (props.editorRequest) setTab('rules'); else if (props.initialTab) setTab(props.initialTab); }, [props.editorRequest?.requestId, props.initialTab]);
  async function load() { setBusy('load'); setError(''); try { const value = await getResourceSettings(); setSettings(value); setDraft(value); setSelectedTypes(new Set()); } catch (exc) { setError(exc instanceof Error ? exc.message : String(exc)); } finally { setBusy(''); } }
  useEffect(() => { void load(); }, []);
  function updateLogPath(category: string, patch: Partial<LogPathProfile>) { setDraft((current) => current ? ({ ...current, log_paths: current.log_paths.map((item) => item.category === category ? ({ ...item, ...patch }) : item) }) : current); }
  function addLogType() {
    setDraft((current) => { if (!current) return current; let n = current.log_paths.length + 1; let category = `custom-${n}`; while (current.log_paths.some((item) => item.category === category)) category = `custom-${++n}`; return { ...current, log_paths: [...current.log_paths, { category, category_label: '自定义日志', display_name: '自定义日志', path_template: '', enabled: false, scope: 'upper_only', match_rules: ['fm', 'fm_timestamp', 'archive'], sort_order: n * 10 }] }; });
  }
  function deleteSelectedTypes() { if (!selectedTypes.size) return; setDraft((current) => current ? ({ ...current, log_paths: current.log_paths.filter((item) => !selectedTypes.has(item.category)) }) : current); setSelectedTypes(new Set()); }
  async function save() { if (!draft) return; const categories = new Set<string>(); for (const item of draft.log_paths) { const candidate = slug(item.category || item.display_name); if (!candidate) { setError('日志类型标识不能为空。'); return; } if (categories.has(candidate)) { setError(`日志类型标识重复：${candidate}`); return; } categories.add(candidate); item.category = candidate; } setBusy('save'); setError(''); setMessage(''); try { const saved = await updateResourceSettings(draft as unknown as Record<string, unknown>); setSettings(saved); setDraft(saved); setMessage('平台资源配置已保存。'); } catch (exc) { setError(exc instanceof Error ? exc.message : String(exc)); } finally { setBusy(''); } }
  const allSelected = useMemo(() => Boolean(draft?.log_paths.length) && draft!.log_paths.every((item) => selectedTypes.has(item.category)), [draft, selectedTypes]);
  function toggleType(category: string) { setSelectedTypes((current) => { const next = new Set(current); next.has(category) ? next.delete(category) : next.add(category); return next; }); }
  function toggleAll() { if (!draft) return; setSelectedTypes(allSelected ? new Set() : new Set(draft.log_paths.map((item) => item.category))); }
  function batchEnabled(enabled: boolean) { setDraft((current) => current ? ({ ...current, log_paths: current.log_paths.map((item) => selectedTypes.has(item.category) ? ({ ...item, enabled }) : item) }) : current); }
  function toggleMatch(category: string, name: 'fm'|'fm_timestamp'|'archive'|'executor_tree'|'run_flat') {
    const item = draft?.log_paths.find((row) => row.category === category); if (!item) return;
    const current = new Set(item.match_rules);
    if (name === 'executor_tree') {
      if (!current.has(name)) updateLogPath(category, { match_rules: ['executor_tree'] });
      return;
    }
    if (name === 'run_flat') {
      if (!current.has(name)) updateLogPath(category, { match_rules: ['run_flat'] });
      return;
    }
    current.delete('executor_tree');
    current.delete('run_flat');
    current.has(name) ? current.delete(name) : current.add(name);
    updateLogPath(category, { match_rules: [...current] });
  }


  useEffect(() => {
    const handler = (event: Event) => {
      const custom = event as CustomEvent<{ context?: Record<string, unknown> }>;
      const context = custom.detail?.context;
      if (!context) return;
      context.page = 'platform-settings';
      context.page_label = '平台设置';
      context.settings_page = {
        tab,
        loading: busy === 'load',
        saving: busy === 'save',
        selected_log_types: Array.from(selectedTypes).slice(0, 20),
        resource: draft ? {
          log_type_count: draft.log_paths.length,
          enabled_log_types: draft.log_paths.filter((item) => item.enabled).map((item) => item.category).slice(0, 24),
          station_xml_path: draft.station_xml_path,
          version_file_path: draft.version_file_path,
          source_code_path_template: draft.source_code_path_template || '',
        } : null,
        error: error || '',
        message: message || '',
      };
    };
    return registerPageContextReader(handler, 10);
  }, [tab, busy, selectedTypes, draft, error, message]);

  return <main className="platform-settings-page">
    <header className="platform-settings-header"><div><span className="eyebrow">PLATFORM SETTINGS</span><h1>设置中心</h1><p>平台规则统一维护；环境资源只保留临时资源绑定和运行状态。</p></div>{tab === 'resource' && <button className="button secondary" onClick={() => void load()}>{busy === 'load' ? <LoaderCircle className="spin" size={15}/> : <RefreshCw size={15}/>} 重新读取</button>}</header>
    <nav className="platform-settings-tabs"><button className={tab === 'resource' ? 'active' : ''} onClick={() => setTab('resource')}><Settings2 size={16}/> 资源与拓扑</button><button className={tab === 'catalog' ? 'active' : ''} onClick={() => setTab('catalog')}><Database size={16}/> 子系统配置</button><button className={tab === 'rules' ? 'active' : ''} onClick={() => setTab('rules')}><FileText size={16}/> 日志规则</button></nav>
    {error && <div className="resource-alert error"><X size={15}/>{error}</div>}{message && <div className="resource-alert success"><CheckCircle2 size={15}/>{message}</div>}
    {tab === 'catalog' && <section className="platform-settings-catalog-scroll"><LogCatalogSettingsPage embedded/></section>}
    {tab === 'rules' && <LogRulesSettingsPage {...props}/>} 
    {tab === 'resource' && <section className="platform-settings-content">{busy === 'load' && <div className="list-loading-layer"><div className="list-loading-card"><LoaderCircle className="spin" size={16}/>正在读取平台配置…</div></div>}{draft && <>
      <article className="settings-card"><header><Network size={18}/><div><h2>上下位机拓扑与版本</h2><p>这些路径是平台规则，不绑定某个环境实例。</p></div></header><div className="settings-form-grid"><label><span>stations.xml 路径</span><input value={draft.station_xml_path} onChange={(e) => setDraft((c) => c ? ({ ...c, station_xml_path: e.target.value }) : c)}/></label><label><span>版本文件路径</span><input value={draft.version_file_path} onChange={(e) => setDraft((c) => c ? ({ ...c, version_file_path: e.target.value }) : c)}/></label><label><span>下位机登录用户</span><input value={draft.lower_username} onChange={(e) => setDraft((c) => c ? ({ ...c, lower_username: e.target.value }) : c)}/></label><label><span>SSH 端口</span><input type="number" value={draft.lower_ssh_port} onChange={(e) => setDraft((c) => c ? ({ ...c, lower_ssh_port: Number(e.target.value) }) : c)}/></label><label className="span-2"><span>下位机统一密码</span><input type="password" placeholder={settings?.has_lower_credential ? '已保存；留空表示不修改' : '请输入密码'} onChange={(e) => setDraft((c) => c ? ({ ...c, lower_password: e.target.value, lower_auth_type: 'password' }) : c)}/></label></div><div className="resource-settings-note"><KeyRound size={15}/>凭据由后端加密保存，接口不会返回明文。</div></article>
      <article className="settings-card"><header><FileText size={18}/><div><h2>日志类型与根目录模板</h2><p>可新增日志类型，选择上/下位机范围，并配置文件命名匹配规则。</p></div><button className="button primary compact settings-card-action" onClick={addLogType}><Plus size={14}/> 新增日志类型</button></header>
        <div className="path-parameter-library"><strong>可用路径参数</strong>{draft.path_parameters?.map((item) => <span key={item.token} title={`示例：${item.example}`}><code>{item.token}</code><em>{item.meaning}</em></span>)}</div>
        <div className="settings-list-toolbar"><label><input className="row-check" type="checkbox" checked={allSelected} onChange={toggleAll}/> 全选</label>{selectedTypes.size > 0 && <div className="batch-toolbar"><strong>已选 {selectedTypes.size}</strong><button onClick={() => batchEnabled(true)}>批量启用</button><button onClick={() => batchEnabled(false)}>批量停用</button><button className="danger" onClick={deleteSelectedTypes}><Trash2 size={13}/> 批量删除</button></div>}</div>
        <div className="settings-log-table settings-log-table-advanced"><div className="settings-log-head"><span></span><span>日志类型</span><span>标识</span><span>状态</span><span>机器范围</span><span>根目录模板</span><span>文件匹配规则</span><span>操作</span></div>{draft.log_paths.map((profile) => <div className="settings-log-row" key={profile.category}><input className="row-check" type="checkbox" checked={selectedTypes.has(profile.category)} onChange={() => toggleType(profile.category)}/><input value={profile.display_name} onChange={(e) => updateLogPath(profile.category, { display_name: e.target.value })}/><input value={profile.category} onChange={(e) => updateLogPath(profile.category, { category: slug(e.target.value) })}/><label className="log-path-toggle"><input type="checkbox" checked={profile.enabled} onChange={(e) => updateLogPath(profile.category, { enabled: e.target.checked })}/><span/></label><select value={profile.scope} onChange={(e) => updateLogPath(profile.category, { scope: e.target.value as LogPathProfile['scope'] })}>{SCOPE_OPTIONS.map(([value,label]) => <option value={value} key={value}>{label}</option>)}</select><input value={profile.path_template} onChange={(e) => updateLogPath(profile.category, { path_template: e.target.value })} placeholder="例如 /log/{username}/debug"/><details className="match-rule-dropdown"><summary title={profile.match_rules.map((name) => RULE_LABEL[name] ?? name).join('、')}><span>{profile.match_rules.length ? profile.match_rules.map((name) => RULE_LABEL[name] ?? name).join('、') : '选择规则'}</span></summary><div className="match-rule-dropdown-menu">{(['fm','fm_timestamp','archive','executor_tree','run_flat'] as const).map((name) => <label key={name}><input type="checkbox" checked={profile.match_rules.includes(name)} onChange={() => toggleMatch(profile.category, name)}/><span>{RULE_LABEL[name]}</span></label>)}</div></details><button className="text-danger-button" onClick={() => { setDraft((c) => c ? ({ ...c, log_paths: c.log_paths.filter((item) => item.category !== profile.category) }) : c); setSelectedTypes((c) => { const n = new Set(c); n.delete(profile.category); return n; }); }}>删除</button></div>)}</div>
      </article>
      <article className="settings-card dhh-log-root-card"><header><FileText size={18}/><div><h2>DHH 日志根目录</h2><p>识别到 DHH 环境后优先使用上位机路径规则；调试日志只要上位机在当前时间窗找到候选日志，就不再查询 DHH，只有上位机完全未找到时才回退 DHH；执行器 / 运行日志仍按缺失模块回退，192.* 小网跳过 DHH 回退。</p></div></header>
        <div className="settings-path-stack dhh-log-root-setting">
          <label><span>DHH 调试日志根目录</span><input value={draft.dhh_debug_log_root ?? '/data/sync/log/debug/'} onChange={(e) => setDraft((c) => c ? ({ ...c, dhh_debug_log_root: e.target.value }) : c)} placeholder="/data/sync/log/debug/"/></label>
          <label><span>DHH 执行器日志根目录</span><input value={draft.dhh_executor_log_root ?? '/data/sync/log/debug/elog/'} onChange={(e) => setDraft((c) => c ? ({ ...c, dhh_executor_log_root: e.target.value }) : c)} placeholder="/data/sync/log/debug/elog/"/></label>
          <label><span>DHH 运行日志根目录</span><input value={draft.dhh_run_log_root ?? '/data/sync/log/run/'} onChange={(e) => setDraft((c) => c ? ({ ...c, dhh_run_log_root: e.target.value }) : c)} placeholder="/data/sync/log/run/"/></label>
        </div>
      </article>
      <article className="settings-card source-code-settings-card"><header><FileCode2 size={18}/><div><h2>Python 源码文件</h2><p>用于日志语义规则的“自动识别语义”；根据当前环境、子系统和模块定位 Python 源码并读取函数 docstring。</p></div></header>
        <div className="settings-path-stack source-code-path-layout">
          <label><span>模块源码目录模板</span><input value={draft.source_code_path_template ?? ''} onChange={(e) => setDraft((c) => c ? ({ ...c, source_code_path_template: e.target.value }) : c)} placeholder="/home/{username}/SW/lib/python/{subsystem}/{module}"/></label>
          <label><span>公共源码目录（每行一个）</span><textarea rows={3} value={draft.source_code_public_paths ?? ''} onChange={(e) => setDraft((c) => c ? ({ ...c, source_code_public_paths: e.target.value }) : c)} placeholder={'/home/{username}/SW/lib/python/me/cpfr/\n/home/{username}/SW/lib/python/sw/adf/'}/></label>
        </div>
        <div className="path-parameter-library source-code-parameters"><strong>源码路径参数</strong>{draft.path_parameters?.filter((item) => ['{username}','{subsystem}','{module}'].includes(item.token)).map((item) => <span key={item.token} title={`示例：${item.example}`}><code>{item.token}</code><em>{item.meaning}</em></span>)}</div>
      </article>
    </>}</section>}
    {tab === 'resource' && <footer className="platform-settings-footer"><button className="button primary" onClick={() => void save()} disabled={busy === 'save'}>{busy === 'save' ? <LoaderCircle className="spin" size={15}/> : <Save size={15}/>} 保存设置</button></footer>}
  </main>;
}
