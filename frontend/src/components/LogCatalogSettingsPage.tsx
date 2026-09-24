import { useEffect, useMemo, useState } from 'react';
import { ArrowLeft, CheckCircle2, LoaderCircle, Pencil, Plus, RefreshCw, Search, Trash2, X } from 'lucide-react';
import { HierarchyModuleSelect, targetKey } from './RemoteLogQueryPanel';
import {
  bulkGlobalLogFms,
  bulkSetGlobalLogFmTargets,
  createGlobalLogFm,
  createGlobalLogSubsystem,
  deleteGlobalLogFm,
  deleteGlobalLogSubsystem,
  getGlobalLogCatalogTree,
  updateGlobalLogFm,
  updateGlobalLogSubsystem,
  type GlobalLogFm,
  type GlobalLogSubsystem,
} from '../api/resourceApi';

interface Props { onBack?: () => void; embedded?: boolean; }
type SubsystemDraft = Pick<GlobalLogSubsystem, 'name' | 'display_name' | 'enabled' | 'sort_order' | 'description'>;
type ModuleDraft = Pick<GlobalLogFm, 'name' | 'kind' | 'display_name' | 'enabled' | 'sort_order' | 'query_priority' | 'description'> & { subsystem: number };

const emptySubsystem: SubsystemDraft = { name: '', display_name: '', enabled: true, sort_order: 0, description: '' };
const emptyModule: ModuleDraft = { subsystem: 0, name: '', kind: 'normal', display_name: '', enabled: true, sort_order: 0, query_priority: 100, description: '' };

export function LogCatalogSettingsPage({ onBack, embedded = false }: Props) {
  const [items, setItems] = useState<GlobalLogSubsystem[]>([]);
  const [query, setQuery] = useState('');
  const [busy, setBusy] = useState('load');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [subsystemEditor, setSubsystemEditor] = useState<{ id?: number; draft: SubsystemDraft }>();
  const [moduleEditor, setModuleEditor] = useState<{ id?: number; draft: ModuleDraft }>();
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [viewMode, setViewMode] = useState<'overview' | 'grouped'>('grouped');
  const [groupSubsystemId, setGroupSubsystemId] = useState<number>();
  const [targetSavingId, setTargetSavingId] = useState<number>();

  async function load() {
    setBusy('load');
    setError('');
    try {
      const value = await getGlobalLogCatalogTree(true);
      const ordered = [...value]
        .sort((a, b) => a.sort_order - b.sort_order || a.name.localeCompare(b.name))
        .map((item) => ({
          ...item,
          fms: [...item.fms].sort((a, b) => a.query_priority - b.query_priority || a.sort_order - b.sort_order || a.name.localeCompare(b.name)),
        }));
      setItems(ordered);
      const valid = new Set(ordered.flatMap((item) => item.fms.map((fm) => fm.id)));
      setSelected((current) => new Set([...current].filter((id) => valid.has(id))));
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy('');
    }
  }

  useEffect(() => { void load(); }, []);
  useEffect(() => {
    if (viewMode !== 'grouped' || !items.length) return;
    if (!groupSubsystemId || !items.some((item) => item.id === groupSubsystemId)) setGroupSubsystemId(items[0].id);
  }, [viewMode, items, groupSubsystemId]);

  const rows = useMemo(
    () => items.flatMap<{ subsystem: GlobalLogSubsystem; module?: GlobalLogFm }>((subsystem) => subsystem.fms.length
      ? subsystem.fms.map((module) => ({ subsystem, module }))
      : [{ subsystem, module: undefined }]),
    [items],
  );
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return rows.filter(({ subsystem, module }) => {
      if (viewMode === 'grouped' && groupSubsystemId && subsystem.id !== groupSubsystemId) return false;
      if (!needle) return true;
      return [
        subsystem.name,
        subsystem.display_name,
        module?.name,
        module?.display_name,
        module?.description,
        ...(module?.event_display_codes || []),
        ...(module?.event_config_files || []),
        ...(module?.target_module_names || []),
      ].some((value) => String(value || '').toLowerCase().includes(needle));
    });
  }, [rows, query, viewMode, groupSubsystemId]);
  const groupRows = useMemo(() => {
    const split = Math.ceil(items.length / 2);
    return [items.slice(0, split), items.slice(split)];
  }, [items]);

  const selectableIds = filtered.flatMap(({ module }) => module ? [module.id] : []);
  const allSelected = selectableIds.length > 0 && selectableIds.every((id) => selected.has(id));
  const moduleById = useMemo(
    () => new Map(items.flatMap((subsystem) => subsystem.fms.map((fm) => [fm.id, { subsystem, fm }] as const))),
    [items],
  );
  const moduleByKey = useMemo(
    () => new Map(items.flatMap((subsystem) => subsystem.fms.map((fm) => [targetKey(subsystem.name, fm.name, fm.kind ?? 'normal'), fm] as const))),
    [items],
  );
  const selectedModules = useMemo(() => Array.from(selected).flatMap((id) => {
    const item = moduleById.get(id);
    return item ? [item.fm] : [];
  }), [selected, moduleById]);
  const batchTargetSelection = useMemo(() => {
    if (!selectedModules.length) return new Set<string>();
    let commonIds = new Set(selectedModules[0].target_module_ids || []);
    selectedModules.slice(1).forEach((module) => {
      const targetIds = new Set(module.target_module_ids || []);
      commonIds = new Set(Array.from(commonIds).filter((id) => targetIds.has(id)));
    });
    return new Set(Array.from(commonIds).flatMap((id) => {
      const target = moduleById.get(id);
      return target ? [targetKey(target.subsystem.name, target.fm.name, target.fm.kind ?? 'normal')] : [];
    }));
  }, [selectedModules, moduleById]);

  function targetSelection(module: GlobalLogFm): Set<string> {
    return new Set((module.target_module_ids || []).flatMap((id) => {
      const target = moduleById.get(id);
      return target ? [targetKey(target.subsystem.name, target.fm.name, target.fm.kind ?? 'normal')] : [];
    }));
  }
  function targetCatalog(moduleId: number): GlobalLogSubsystem[] {
    return items.map((subsystem) => ({ ...subsystem, fms: subsystem.fms.filter((fm) => fm.id !== moduleId) }));
  }
  function targetIdsFromKeys(keys: Set<string>): number[] {
    return Array.from(new Set(Array.from(keys).flatMap((key) => {
      const fm = moduleByKey.get(key);
      return fm ? [fm.id] : [];
    })));
  }
  async function saveTargets(module: GlobalLogFm, keys: Set<string>) {
    const ids = targetIdsFromKeys(keys).filter((id) => id !== module.id);
    setTargetSavingId(module.id);
    setError('');
    try {
      await updateGlobalLogFm(module.id, { target_module_ids: ids });
      setMessage(`模块 ${module.name} 的目标模块已更新。`);
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setTargetSavingId(undefined);
    }
  }
  async function saveBatchTargets(keys: Set<string>) {
    if (!selected.size) return;
    setBusy('bulk-targets');
    setError('');
    try {
      const result = await bulkSetGlobalLogFmTargets([...selected], targetIdsFromKeys(keys));
      setMessage(`已统一设置 ${result.affected}/${result.requested} 个模块的目标模块。`);
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy('');
    }
  }

  function toggleSelected(id: number) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }
  function toggleAll() {
    setSelected((current) => {
      const next = new Set(current);
      if (allSelected) selectableIds.forEach((id) => next.delete(id));
      else selectableIds.forEach((id) => next.add(id));
      return next;
    });
  }
  async function bulkModuleAction(action: 'enable' | 'disable' | 'delete') {
    if (!selected.size) return;
    if (action === 'delete' && !window.confirm(`删除选中的 ${selected.size} 个模块？`)) return;
    setBusy(`bulk-${action}`);
    setError('');
    try {
      const ids = [...selected];
      const result = await bulkGlobalLogFms(ids, action);
      setSelected(new Set());
      setMessage(`批量处理完成：${result.affected}/${result.requested} 个模块${result.skipped_ids?.length ? `，已跳过 ${result.skipped_ids.length} 个失效记录` : ''}。`);
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy('');
    }
  }

  async function saveSubsystem() {
    if (!subsystemEditor?.draft.name.trim()) return setError('子系统名称不能为空。');
    setBusy('save-subsystem'); setError('');
    try {
      if (subsystemEditor.id) await updateGlobalLogSubsystem(subsystemEditor.id, subsystemEditor.draft);
      else await createGlobalLogSubsystem(subsystemEditor.draft);
      setSubsystemEditor(undefined); setMessage('子系统配置已保存。'); await load();
    } catch (exc) { setError(exc instanceof Error ? exc.message : String(exc)); }
    finally { setBusy(''); }
  }
  async function saveModule() {
    if (!moduleEditor?.draft.name.trim() || !moduleEditor.draft.subsystem) return setError('请选择子系统并填写模块名称。');
    setBusy('save-module'); setError('');
    try {
      const { subsystem, ...payload } = moduleEditor.draft;
      if (moduleEditor.id) await updateGlobalLogFm(moduleEditor.id, payload);
      else await createGlobalLogFm({ subsystem, ...payload });
      setModuleEditor(undefined); setMessage('模块配置已保存。'); await load();
    } catch (exc) { setError(exc instanceof Error ? exc.message : String(exc)); }
    finally { setBusy(''); }
  }
  async function removeModule(module: GlobalLogFm) {
    if (!window.confirm(`删除模块“${module.name}”？`)) return;
    await deleteGlobalLogFm(module.id); setMessage('模块已删除。'); await load();
  }
  async function removeSubsystem(subsystem: GlobalLogSubsystem) {
    if (!window.confirm(`删除子系统“${subsystem.name}”及其全部模块？`)) return;
    await deleteGlobalLogSubsystem(subsystem.id); setMessage('子系统已删除。'); await load();
  }

  return <main className={`catalog-admin-page ${embedded ? 'embedded' : ''}`}>
    {!embedded && <header className="catalog-admin-header"><div>{onBack && <button className="button ghost" type="button" onClick={onBack}><ArrowLeft size={15} /> 返回环境资源</button>}<h1>子系统配置</h1></div><div><button className="button secondary" type="button" onClick={() => void load()}>{busy === 'load' ? <LoaderCircle className="spin" size={15} /> : <RefreshCw size={15} />} 刷新</button></div></header>}
    {error && <div className="resource-alert error"><X size={15} />{error}</div>}
    {message && <div className="resource-alert success"><CheckCircle2 size={15} />{message}</div>}

    <section className="catalog-admin-panel">
      {busy === 'load' && <div className="list-loading-layer"><div className="list-loading-card"><LoaderCircle className="spin" size={16} />正在读取子系统/模块配置…</div></div>}
      <div className="catalog-admin-toolbar">
        <div className="catalog-view-switch" role="tablist" aria-label="子系统配置视图">
          <button type="button" className={viewMode === 'overview' ? 'active' : ''} onClick={() => setViewMode('overview')}>全览</button>
          <button type="button" className={viewMode === 'grouped' ? 'active' : ''} onClick={() => setViewMode('grouped')}>按子系统/模块</button>
        </div>
        <label><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索子系统、模块、Event组件、DisplayCode…" /></label>
        {selected.size > 0 && <div className="batch-toolbar"><strong>已选 {selected.size}</strong><HierarchyModuleSelect catalog={items} selected={batchTargetSelection} onChange={() => undefined} onCommit={(next) => void saveBatchTargets(next)} deferCommit disabled={busy === 'bulk-targets'} label="统一设置目标模块" compact /><button className="button ghost compact" type="button" onClick={() => void bulkModuleAction('enable')}>启用</button><button className="button ghost compact" type="button" onClick={() => void bulkModuleAction('disable')}>停用</button><button className="button danger-outline compact" type="button" onClick={() => void bulkModuleAction('delete')}>删除</button></div>}
        <button className="button secondary" type="button" onClick={() => setSubsystemEditor({ draft: { ...emptySubsystem } })}><Plus size={15} /> 新增子系统</button>
        <button className="button primary" type="button" onClick={() => setModuleEditor({ draft: { ...emptyModule, subsystem: items[0]?.id || 0 } })}><Plus size={15} /> 新增模块</button>
      </div>

      {viewMode === 'grouped' && <div className="catalog-group-strip" aria-label="子系统模块统计卡片">
        {groupRows.map((row, rowIndex) => row.length > 0 && <div className="catalog-group-row" key={`catalog-group-row-${rowIndex}`}>
          {row.map((item) => <button type="button" key={item.id} className={groupSubsystemId === item.id ? 'active' : ''} onClick={() => { setGroupSubsystemId(item.id); setSelected(new Set()); }}><strong>{item.display_name || item.name}</strong><span>{item.fms.length} 模块</span></button>)}
        </div>)}
      </div>}

      <div className="catalog-admin-table-wrap"><table className="catalog-admin-table"><thead><tr>
        <th><input className="row-check" type="checkbox" checked={allSelected} onChange={toggleAll} aria-label="全选模块" /></th>
        <th>子系统</th><th>模块 / Event组件</th><th>类型</th><th>DisplayCode</th><th>事件配置</th><th>目标模块</th><th>查询优先级</th><th>模块状态</th><th>最近命中</th><th>操作</th>
      </tr></thead><tbody>
        {filtered.map(({ subsystem, module }) => <tr key={`${subsystem.id}-${module?.id ?? 'empty'}`}>
          <td>{module ? <input className="row-check" type="checkbox" checked={selected.has(module.id)} onChange={() => toggleSelected(module.id)} aria-label={`选择模块 ${module.name}`} /> : null}</td>
          <td><strong>{subsystem.name}</strong>{subsystem.display_name ? <span className="catalog-inline-note">{subsystem.display_name}</span> : null}</td>
          <td>{module ? <><strong>{module.name}</strong>{module.display_name ? <span className="catalog-inline-note">{module.display_name}</span> : null}{module.event_component ? <span className="catalog-inline-note">Event组件</span> : null}</> : <span className="muted">暂无模块</span>}</td>
          <td>{module ? (module.kind === 'executor' ? '执行器' : '普通') : '-'}</td>
          <td title={(module?.event_display_codes || []).join('、')}>{module?.event_component ? <><strong>{module.event_code_count || 0}</strong>{module.event_display_codes?.[0] ? <span className="catalog-inline-note">{module.event_display_codes[0]}</span> : null}</> : '-'}</td>
          <td title={(module?.event_config_files || []).join('、')}>{module?.event_config_files?.slice(0, 2).join('、') || '-'}</td>
          <td className="catalog-dependency-cell">{module ? <HierarchyModuleSelect catalog={targetCatalog(module.id)} selected={targetSelection(module)} onChange={() => undefined} onCommit={(next) => void saveTargets(module, next)} deferCommit disabled={targetSavingId === module.id} label="目标模块" compact moduleNameOnly /> : '-'}</td>
          <td>{module?.query_priority ?? '-'}</td>
          <td>{module ? <button type="button" className={`admin-status-button ${module.enabled ? 'enabled' : 'disabled'}`} onClick={() => void updateGlobalLogFm(module.id, { enabled: !module.enabled }).then(load)}>{module.enabled ? '启用' : '停用'}</button> : '-'}</td>
          <td>{module?.last_matched_at ? new Date(module.last_matched_at).toLocaleString() : '-'}</td>
          <td><div className="catalog-row-actions"><button type="button" onClick={() => setSubsystemEditor({ id: subsystem.id, draft: { name: subsystem.name, display_name: subsystem.display_name, enabled: subsystem.enabled, sort_order: subsystem.sort_order, description: subsystem.description } })}><Pencil size={13} /> 子系统</button>{module && <><button type="button" onClick={() => setModuleEditor({ id: module.id, draft: { subsystem: subsystem.id, name: module.name, kind: module.kind ?? 'normal', display_name: module.display_name, enabled: module.enabled, sort_order: module.sort_order, query_priority: module.query_priority ?? 100, description: module.description } })}><Pencil size={13} /> 模块</button><button type="button" className="danger" onClick={() => void removeModule(module)}><Trash2 size={13} /></button></>}</div></td>
        </tr>)}
        {!filtered.length && <tr><td colSpan={11}><div className="resource-empty inline-empty">暂无匹配配置。</div></td></tr>}
      </tbody></table></div>
    </section>

    {subsystemEditor && <div className="resource-modal-backdrop" onMouseDown={() => setSubsystemEditor(undefined)}><form className="resource-modal" onSubmit={(event) => { event.preventDefault(); void saveSubsystem(); }} onMouseDown={(event) => event.stopPropagation()}><header><div><h2>{subsystemEditor.id ? '编辑子系统' : '新增子系统'}</h2></div><button type="button" className="icon-button" onClick={() => setSubsystemEditor(undefined)}><X size={18} /></button></header><label><span>子系统名</span><input value={subsystemEditor.draft.name} onChange={(event) => setSubsystemEditor((current) => current ? ({ ...current, draft: { ...current.draft, name: event.target.value } }) : current)} /></label><label><span>显示名称</span><input value={subsystemEditor.draft.display_name} onChange={(event) => setSubsystemEditor((current) => current ? ({ ...current, draft: { ...current.draft, display_name: event.target.value } }) : current)} /></label><div className="resource-field-row"><label><span>状态</span><select value={subsystemEditor.draft.enabled ? '1' : '0'} onChange={(event) => setSubsystemEditor((current) => current ? ({ ...current, draft: { ...current.draft, enabled: event.target.value === '1' } }) : current)}><option value="1">启用</option><option value="0">停用</option></select></label><label><span>排序</span><input type="number" value={subsystemEditor.draft.sort_order} onChange={(event) => setSubsystemEditor((current) => current ? ({ ...current, draft: { ...current.draft, sort_order: Number(event.target.value) } }) : current)} /></label></div><label><span>说明</span><textarea value={subsystemEditor.draft.description} onChange={(event) => setSubsystemEditor((current) => current ? ({ ...current, draft: { ...current.draft, description: event.target.value } }) : current)} /></label><footer>{subsystemEditor.id && <button className="button danger-outline" type="button" onClick={() => { const item = items.find((value) => value.id === subsystemEditor.id); if (item) void removeSubsystem(item).then(() => setSubsystemEditor(undefined)); }}><Trash2 size={14} /> 删除</button>}<button className="button primary" type="submit">保存</button></footer></form></div>}

    {moduleEditor && <div className="resource-modal-backdrop" onMouseDown={() => setModuleEditor(undefined)}><form className="resource-modal" onSubmit={(event) => { event.preventDefault(); void saveModule(); }} onMouseDown={(event) => event.stopPropagation()}><header><div><h2>{moduleEditor.id ? '编辑模块' : '新增模块'}</h2></div><button type="button" className="icon-button" onClick={() => setModuleEditor(undefined)}><X size={18} /></button></header><label><span>所属子系统</span><select value={moduleEditor.draft.subsystem} onChange={(event) => setModuleEditor((current) => current ? ({ ...current, draft: { ...current.draft, subsystem: Number(event.target.value) } }) : current)}>{items.map((item) => <option value={item.id} key={item.id}>{item.name}{item.display_name ? ` · ${item.display_name}` : ''}</option>)}</select></label><label><span>模块类型</span><select value={moduleEditor.draft.kind} onChange={(event) => setModuleEditor((current) => current ? ({ ...current, draft: { ...current.draft, kind: event.target.value as GlobalLogFm['kind'] } }) : current)}><option value="normal">普通模块</option><option value="executor">执行器模块</option></select></label><label><span>模块名</span><input value={moduleEditor.draft.name} onChange={(event) => setModuleEditor((current) => current ? ({ ...current, draft: { ...current.draft, name: event.target.value } }) : current)} /></label><label><span>显示名称</span><input value={moduleEditor.draft.display_name} onChange={(event) => setModuleEditor((current) => current ? ({ ...current, draft: { ...current.draft, display_name: event.target.value } }) : current)} /></label><div className="resource-field-row"><label><span>状态</span><select value={moduleEditor.draft.enabled ? '1' : '0'} onChange={(event) => setModuleEditor((current) => current ? ({ ...current, draft: { ...current.draft, enabled: event.target.value === '1' } }) : current)}><option value="1">启用</option><option value="0">停用</option></select></label><label><span>排序</span><input type="number" value={moduleEditor.draft.sort_order} onChange={(event) => setModuleEditor((current) => current ? ({ ...current, draft: { ...current.draft, sort_order: Number(event.target.value) } }) : current)} /></label></div><label><span>查询优先级</span><input type="number" min="0" value={moduleEditor.draft.query_priority} onChange={(event) => setModuleEditor((current) => current ? ({ ...current, draft: { ...current.draft, query_priority: Math.max(0, Number(event.target.value) || 0) } }) : current)} /></label><label><span>说明</span><textarea value={moduleEditor.draft.description} onChange={(event) => setModuleEditor((current) => current ? ({ ...current, draft: { ...current.draft, description: event.target.value } }) : current)} /></label><footer><button className="button primary" type="submit">保存</button></footer></form></div>}
  </main>;
}
