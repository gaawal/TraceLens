import { useMemo, useRef, useState } from 'react';
import { Check, Database, FlaskConical, Pencil, Plus, Save, Search, Trash2, X } from 'lucide-react';
import type { DisplayRuleEditorRequest } from '../rendering/displayRules';
import { extractDisplayRuleMessage } from '../rendering/displayRules';
import {
  createEmptyDataExtractionRule,
  detectStructuredDataCandidates,
  extractDataValues,
  inferDataSourceUnit,
  inferDataValueType,
  isDataUnitConversionEnabled,
  normalizeExtractedNumericValue,
  validateDataExtractionRule,
  type DataExtractionField,
  type DataExtractionRule,
  type DataUnitConversionRule,
  type StructuredDataCandidate,
  type DataValueType,
} from '../rendering/dataExtractionRules';

interface Props {
  rules: readonly DataExtractionRule[];
  sourceSeed?: DisplayRuleEditorRequest;
  onChange: (rules: DataExtractionRule[]) => void;
}

function splitList(value: string): string[] {
  return value.split(/[,，;；\n]+/).map((item) => item.trim()).filter(Boolean);
}

function scopeText(values: string[]) { return values.length ? values.join(', ') : '全部'; }

export function DataExtractionRulesPanel({ rules, sourceSeed, onChange }: Props) {
  const [query, setQuery] = useState('');
  const [draft, setDraft] = useState<DataExtractionRule>();
  const [sample, setSample] = useState('');
  const [selection, setSelection] = useState<{ text: string; start: number; end: number }>();
  const [pendingKey, setPendingKey] = useState<{ text: string; start: number; end: number }>();
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string }>();
  const sampleRef = useRef<HTMLTextAreaElement>(null);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return rules;
    return rules.filter((rule) => `${rule.name} ${rule.description} ${rule.matchKeyword} ${rule.fields.map((field) => `${field.key} ${field.name}`).join(' ')} ${rule.modules.join(' ')}`.toLowerCase().includes(needle));
  }, [rules, query]);

  const structuredCandidates = useMemo(() => detectStructuredDataCandidates(sample), [sample]);

  function matchesStructuredCandidate(field: DataExtractionField, candidate: StructuredDataCandidate) {
    return (field.structuredRootHint || '') === (candidate.rootHint || '')
      && JSON.stringify(field.structuredPath || []) === JSON.stringify(candidate.path);
  }

  function toggleStructuredCandidate(candidate: StructuredDataCandidate) {
    if (!draft) return;
    const existing = draft.fields.find((field) => matchesStructuredCandidate(field, candidate));
    if (existing) {
      update({ fields: draft.fields.filter((field) => field.id !== existing.id) });
      return;
    }
    const field: DataExtractionField = {
      id: `field-${Date.now()}-${Math.random().toString(16).slice(2)}`,
      key: candidate.displayPath,
      name: candidate.displayPath,
      sampleValue: candidate.sampleValue,
      valueType: candidate.valueType,
      sourceUnit: candidate.sourceUnit,
      plotUnit: 'source',
      unitConversionEnabled: false,
      unitConversions: [],
      structuredPath: [...candidate.path],
      structuredRootHint: candidate.rootHint,
    };
    update({ fields: [...draft.fields, field] });
  }

  function selectAllStructuredCandidates() {
    if (!draft || !structuredCandidates.length) return;
    const manualFields = draft.fields.filter((field) => !(field.structuredPath || []).length);
    const existingStructured = draft.fields.filter((field) => (field.structuredPath || []).length);
    const nextStructured = [...existingStructured];
    structuredCandidates.forEach((candidate) => {
      if (nextStructured.some((field) => matchesStructuredCandidate(field, candidate))) return;
      nextStructured.push({
        id: `field-${Date.now()}-${Math.random().toString(16).slice(2)}`,
        key: candidate.displayPath,
        name: candidate.displayPath,
        sampleValue: candidate.sampleValue,
        valueType: candidate.valueType,
        sourceUnit: candidate.sourceUnit,
        plotUnit: 'source',
        unitConversionEnabled: false,
        unitConversions: [],
        structuredPath: [...candidate.path],
        structuredRootHint: candidate.rootHint,
      });
    });
    update({ fields: [...manualFields, ...nextStructured] });
  }

  function beginNew() {
    const message = sourceSeed ? extractDisplayRuleMessage(sourceSeed.sampleRaw) : '';
    const next = createEmptyDataExtractionRule(message);
    if (sourceSeed?.sourceTargets?.length === 1) {
      next.subsystems = [sourceSeed.sourceTargets[0].subsystem];
      next.modules = [sourceSeed.sourceTargets[0].module];
    }
    next.name = sourceSeed ? `${sourceSeed.functionName || '当前日志'} 数据提取` : '';
    setDraft(next);
    setSample(message);
    setSelection(undefined);
    setPendingKey(undefined);
    setTestResult(undefined);
  }

  function edit(rule: DataExtractionRule) {
    setDraft({ ...rule, fields: rule.fields.map((field) => ({ ...field, unitConversions: (field.unitConversions || []).map((item) => ({ ...item })) })), sourceCategories: [...rule.sourceCategories], subsystems: [...rule.subsystems], modules: [...rule.modules] });
    setSample(rule.sampleMessage || '');
    setSelection(undefined);
    setPendingKey(undefined);
    setTestResult(undefined);
  }

  function update(patch: Partial<DataExtractionRule>) {
    setDraft((current) => current ? { ...current, ...patch, updatedAt: Date.now() } : current);
    setTestResult(undefined);
  }

  function captureSelection() {
    if (!draft || !sampleRef.current) return;
    const rawStart = Math.min(sampleRef.current.selectionStart, sampleRef.current.selectionEnd);
    const rawEnd = Math.max(sampleRef.current.selectionStart, sampleRef.current.selectionEnd);
    const raw = sample.slice(rawStart, rawEnd);
    const text = raw.trim();
    if (!text) { setSelection(undefined); return; }
    const leading = raw.length - raw.trimStart().length;
    const trailing = raw.length - raw.trimEnd().length;
    setSelection({ text, start: rawStart + leading, end: rawEnd - trailing });
  }

  function setAsMatchKeyword() {
    if (!selection) return;
    update({ matchKeyword: selection.text });
    setSelection(undefined);
  }

  function setAsKey() {
    if (!selection) return;
    setPendingKey(selection);
    setSelection(undefined);
  }

  function setAsValue() {
    if (!draft || !selection || !pendingKey) return;
    const field: DataExtractionField = {
      id: `field-${Date.now()}-${Math.random().toString(16).slice(2)}`,
      key: pendingKey.text,
      name: pendingKey.text,
      sampleValue: selection.text,
      valueType: inferDataValueType(selection.text),
      sourceUnit: inferDataSourceUnit(selection.text),
      plotUnit: 'source',
      unitConversionEnabled: false,
      unitConversions: [],
      keyStart: pendingKey.start,
      keyEnd: pendingKey.end,
      valueStart: selection.start,
      valueEnd: selection.end,
    };
    update({ fields: [...draft.fields, field] });
    setPendingKey(undefined);
    setSelection(undefined);
  }

  function patchField(id: string, patch: Partial<DataExtractionField>) {
    if (!draft) return;
    update({ fields: draft.fields.map((field) => field.id === id ? { ...field, ...patch } : field) });
  }

  function addUnitConversion(fieldId: string) {
    if (!draft) return;
    update({ fields: draft.fields.map((field) => {
      if (field.id !== fieldId) return field;
      const source = field.sourceUnit && field.sourceUnit !== 'auto' && field.sourceUnit !== 'none' ? field.sourceUnit : inferDataSourceUnit(field.sampleValue);
      const next: DataUnitConversionRule = {
        id: `unit-${Date.now()}-${Math.random().toString(16).slice(2)}`,
        leftValue: 1000,
        leftUnit: source === 'auto' ? '' : source,
        rightValue: 1,
        rightUnit: field.plotUnit && field.plotUnit !== 'source' && field.plotUnit !== 'none' ? field.plotUnit : '',
      };
      return { ...field, unitConversions: [...(field.unitConversions || []), next] };
    }) });
  }

  function patchUnitConversion(fieldId: string, conversionId: string, patch: Partial<DataUnitConversionRule>) {
    if (!draft) return;
    update({ fields: draft.fields.map((field) => field.id !== fieldId ? field : {
      ...field,
      unitConversions: (field.unitConversions || []).map((item) => item.id === conversionId ? { ...item, ...patch } : item),
    }) });
  }

  function removeUnitConversion(fieldId: string, conversionId: string) {
    if (!draft) return;
    update({ fields: draft.fields.map((field) => field.id !== fieldId ? field : {
      ...field,
      unitConversions: (field.unitConversions || []).filter((item) => item.id !== conversionId),
    }) });
  }

  function removeField(id: string) {
    if (!draft) return;
    update({ fields: draft.fields.filter((field) => field.id !== id) });
  }

  function testRule() {
    if (!draft) return;
    const validation = validateDataExtractionRule(draft);
    if (validation) { setTestResult({ ok: false, message: validation }); return; }
    const values = extractDataValues(sample || draft.sampleMessage, draft);
    if (!values) { setTestResult({ ok: false, message: '当前样例未命中，请检查参数名、参数值的划选位置和类型。' }); return; }
    const parts = draft.fields.map((field) => {
      const name = field.name || field.key;
      const raw = values[name];
      if ((field.valueType === 'number' || field.valueType === 'integer') && (typeof raw === 'string' || typeof raw === 'number')) {
        const normalized = normalizeExtractedNumericValue(raw, field);
        const targetUnit = String(field.plotUnit || 'source').trim() || 'source';
        if (normalized && isDataUnitConversionEnabled(field) && targetUnit !== 'source') {
          if (targetUnit !== 'none' && normalized.unit !== targetUnit) return `${name}=${String(raw)}（未找到 ${normalized.unit} → ${targetUnit} 的换算路径）`;
          return `${name}=${String(raw)}（绘图=${normalized.value}${normalized.unit === 'none' ? '' : ` ${normalized.unit}`}）`;
        }
      }
      return `${name}=${String(raw ?? '')}`;
    });
    setTestResult({ ok: true, message: parts.join(' · ') });
  }

  function save() {
    if (!draft) return;
    const next = { ...draft, sampleMessage: sample, updatedAt: Date.now() };
    const validation = validateDataExtractionRule(next);
    if (validation) { setTestResult({ ok: false, message: validation }); return; }
    const exists = rules.some((item) => item.id === next.id);
    onChange(exists ? rules.map((item) => item.id === next.id ? next : item) : [...rules, next]);
    setDraft(undefined);
  }

  return <div className="data-rule-panel">
    <div className="data-rule-toolbar">
      <label><Search size={15}/><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="筛选数据提取器…"/></label>
      <button className="button primary compact" onClick={beginNew}><Plus size={14}/> {sourceSeed ? '基于当前日志新增提取器' : '新增数据提取器'}</button>
    </div>
    <div className="data-rule-note"><Database size={16}/><span>数据提取器保存到<strong>后台数据库并对所有用户共享</strong>。启用的提取器会出现在“提取数据”的采集能力列表中；需要持续采集的提取器再单独勾选<strong>实时采集</strong> —— 只有勾选的提取器会在点击“实时监听”后被采集。</span></div>
    <div className="rules-table-wrap"><table className="rules-table"><thead><tr><th>提取器名称</th><th>Match（可选）</th><th>字段</th><th>作用范围</th><th>输出</th><th>状态</th><th>实时采集</th><th>操作</th></tr></thead><tbody>
      {filtered.map((rule) => <tr key={rule.id}><td><strong>{rule.name}</strong><small className="rule-table-secondary">{rule.description || '—'}</small></td><td className="rule-code-cell">{rule.matchKeyword || '—'}</td><td><div className="data-field-chips">{rule.fields.map((field) => <span key={field.id}>{field.name}{isDataUnitConversionEnabled(field) && field.plotUnit && field.plotUnit !== 'source' && field.plotUnit !== 'none' ? ` → ${field.plotUnit}` : ''}</span>)}</div></td><td><small>子系统：{scopeText(rule.subsystems)}</small><small className="rule-table-secondary">FM：{scopeText(rule.modules)}</small></td><td>{rule.outputFormat === 'text' ? '文本' : '表格'}</td><td><span className={`rule-status ${rule.enabled ? 'enabled' : ''}`}>{rule.enabled ? '启用' : '停用'}</span></td><td><label className={`data-live-capture-toggle ${rule.liveCapture ? 'on' : ''}`} title={rule.liveCapture ? '实时监听时采集这个提取器' : '勾选后，实时监听才会采集这个提取器'}><input type="checkbox" checked={rule.liveCapture === true} onChange={(event) => onChange(rules.map((item) => item.id === rule.id ? { ...item, liveCapture: event.target.checked, updatedAt: Date.now() } : item))}/><span>{rule.liveCapture ? '采集中' : '不采集'}</span></label></td><td><div className="rules-row-actions"><button onClick={() => edit(rule)}><Pencil size={13}/> 编辑</button><button onClick={() => onChange(rules.map((item) => item.id === rule.id ? { ...item, enabled: !item.enabled, updatedAt: Date.now() } : item))}>{rule.enabled ? '停用' : '启用'}</button><button className="danger" onClick={() => onChange(rules.filter((item) => item.id !== rule.id))}><Trash2 size={13}/> 删除</button></div></td></tr>)}
      {!filtered.length && <tr><td colSpan={8}><div className="resource-empty inline-empty">暂无数据提取器。</div></td></tr>}
    </tbody></table></div>

    {draft && <div className="rule-drawer-backdrop semantic-rule-drawer-backdrop" onMouseDown={() => setDraft(undefined)}><aside className="rule-drawer semantic-rule-drawer data-rule-drawer" onMouseDown={(event) => event.stopPropagation()}>
      <header><div><span className="eyebrow">DATA EXTRACTOR</span><h2>{rules.some((rule) => rule.id === draft.id) ? '编辑数据提取器' : '新增数据提取器'}</h2><p>普通日志支持语义划选；JSON / 字典日志会自动识别 key 路径，勾选字段后即可提取。</p></div><button className="icon-button" onClick={() => setDraft(undefined)}><X size={18}/></button></header>
      <div className="rule-drawer-body semantic-rule-drawer-body">
        <div className="rule-editor-toolbar"><div className="rule-kind-switch"><button type="button" className="active">参数语义提取</button></div><label className="rule-enabled-control"><input type="checkbox" checked={draft.enabled} onChange={(event) => update({ enabled: event.target.checked })}/> 启用数据提取</label><label className="rule-enabled-control"><input type="checkbox" checked={draft.liveCapture === true} onChange={(event) => update({ liveCapture: event.target.checked })}/> 实时采集</label></div>
        <div className="rule-form-grid two-columns">
          <label><span>数据集名称</span><input value={draft.name} onChange={(event) => update({ name: event.target.value })} placeholder="例如：参数采集"/></label>
          <label><span>输出形式</span><select value={draft.outputFormat} onChange={(event) => update({ outputFormat: event.target.value as 'table' | 'text' })}><option value="table">表格 / CSV</option><option value="text">文本 / TXT</option></select></label>
          <label className="span-2"><span>说明</span><input value={draft.description} onChange={(event) => update({ description: event.target.value })} placeholder="例如：持续采集日志中的多个参数值"/></label>
        </div>

        <section className="rule-editor-step">
          <div className="rule-step-heading"><span>1</span><div><strong>划选日志内容</strong><small>普通日志可继续划选参数名和对应参数值；如果样例中包含 JSON / 字典，系统会在下方自动识别字段，直接勾选即可。可选 Match 仅用于缩小匹配范围。</small></div></div>
          <textarea ref={sampleRef} className="rule-selection-textarea data-selection-textarea" value={sample} onChange={(event) => { setSample(event.target.value); update({ sampleMessage: event.target.value }); }} onSelect={captureSelection} onMouseUp={captureSelection} onKeyUp={captureSelection} placeholder="粘贴或从当前日志带入样例，例如：参数1  参数2  0.01  2.0"/>
          <div className="rule-selection-actions data-selection-actions"><span>当前选中：<strong>{selection?.text || '请在上方划选文本'}</strong>{pendingKey && <em>待绑定参数名：{pendingKey.text}</em>}</span><div className="rule-selection-control-group"><button type="button" className="button secondary compact-button" disabled={!selection} onClick={setAsMatchKeyword}>设为 Match</button><button type="button" className="button secondary compact-button" disabled={!selection} onClick={setAsKey}>设为参数名</button><button type="button" className="button primary compact-button" disabled={!selection || !pendingKey} onClick={setAsValue}>设为参数值</button></div></div>
          {structuredCandidates.length > 0 && <div className="data-structured-detect">
            <div className="data-structured-detect-head"><div><strong>识别到字典字段 · {structuredCandidates.length}</strong><small>直接勾选需要的数据字段；后续按 key 路径提取，value 变化或字段顺序变化都无需重新配置。</small></div><button type="button" className="button secondary compact-button" onClick={selectAllStructuredCandidates}>全选字段</button></div>
            <div className="data-structured-field-options">{structuredCandidates.map((candidate) => { const checked = draft.fields.some((field) => matchesStructuredCandidate(field, candidate)); return <label key={candidate.id} className={checked ? 'selected' : ''}><input type="checkbox" checked={checked} onChange={() => toggleStructuredCandidate(candidate)}/><span><strong>{candidate.displayPath}</strong><small>{candidate.sampleValue}</small></span><em>{candidate.valueType === 'number' ? '浮点' : candidate.valueType === 'integer' ? '整数' : candidate.valueType === 'boolean' ? '布尔' : '文本'}</em></label>; })}</div>
          </div>}
          {draft.matchKeyword && <div className="data-match-preview"><span>Match：<code>{draft.matchKeyword}</code></span><button onClick={() => update({ matchKeyword: '' })} aria-label="清除 Match"><X size={12}/></button></div>}
        </section>

        <section className="rule-editor-step">
          <div className="rule-step-heading"><span>2</span><div><strong>字段定义</strong><small>参数值类型会根据样例自动判断；数值字段支持千分位和任意单位后缀；单位换算关系由你自行定义，可按多条等价规则链式换算后再绘图。匹配依据仍是参数名、参数值及其相对顺序。</small></div></div>
          <div className="data-field-editor-list">{draft.fields.map((field) => <div className="data-field-editor" key={field.id}>
            <div className="data-field-main-row"><code>{field.key}</code>{(field.structuredPath || []).length > 0 && <b className="data-field-structured-badge">字典</b>}<span>→</span><input value={field.name} onChange={(event) => patchField(field.id, { name: event.target.value })} aria-label="保存字段名"/><select value={field.valueType} onChange={(event) => patchField(field.id, { valueType: event.target.value as DataValueType })}><option value="number">浮点数</option><option value="integer">整数</option><option value="boolean">布尔</option><option value="string">文本</option></select><small>样例 {field.sampleValue}</small><button onClick={() => removeField(field.id)}><X size={13}/></button></div>
            {(field.valueType === 'number' || field.valueType === 'integer') && <div className={`data-field-unit-config ${isDataUnitConversionEnabled(field) ? 'expanded' : 'collapsed'}`}>
              <label className="data-field-unit-toggle"><input type="checkbox" checked={isDataUnitConversionEnabled(field)} onChange={(event) => patchField(field.id, { unitConversionEnabled: event.target.checked })}/><span><strong>单位转换（可选）</strong><small>{isDataUnitConversionEnabled(field) ? '已启用，展开配置自定义换算关系。' : '勾选后配置目标单位与换算规则。'}</small></span></label>
              {isDataUnitConversionEnabled(field) && <div className="data-field-unit-details">
                <div className="data-field-unit-head"><div><small>单位名称和换算比例完全自定义。原始提取值始终保留，绘图/统计只使用换算后的数值。</small></div><button type="button" className="button secondary compact-button" onClick={() => addUnitConversion(field.id)}><Plus size={13}/> 添加规则</button></div>
                <div className="data-field-unit-targets">
                  <label><span>无单位后缀时按</span><input value={(field.sourceUnit || 'auto') === 'auto' ? '' : (field.sourceUnit || '')} onChange={(event) => patchField(field.id, { sourceUnit: event.target.value.trim() || 'auto' })} placeholder="自动识别日志后缀"/></label>
                  <span className="data-field-unit-arrow">→</span>
                  <label><span>绘图目标单位</span><input value={(field.plotUnit || 'source') === 'source' ? '' : (field.plotUnit || '')} onChange={(event) => patchField(field.id, { plotUnit: event.target.value.trim() || 'source' })} placeholder="留空=保持原单位"/></label>
                </div>
                <div className="data-unit-conversion-list">
                  {(field.unitConversions || []).map((conversion, index) => <div className="data-unit-conversion-row" key={conversion.id}><b>规则 {index + 1}</b><input type="number" min="0" step="any" value={conversion.leftValue} onChange={(event) => patchUnitConversion(field.id, conversion.id, { leftValue: Number(event.target.value) })} aria-label={`规则${index + 1}左侧数值`}/><input value={conversion.leftUnit} onChange={(event) => patchUnitConversion(field.id, conversion.id, { leftUnit: event.target.value })} placeholder="单位 A" aria-label={`规则${index + 1}左侧单位`}/><strong>=</strong><input type="number" min="0" step="any" value={conversion.rightValue} onChange={(event) => patchUnitConversion(field.id, conversion.id, { rightValue: Number(event.target.value) })} aria-label={`规则${index + 1}右侧数值`}/><input value={conversion.rightUnit} onChange={(event) => patchUnitConversion(field.id, conversion.id, { rightUnit: event.target.value })} placeholder="单位 B" aria-label={`规则${index + 1}右侧单位`}/><button type="button" className="icon-button danger" onClick={() => removeUnitConversion(field.id, conversion.id)} aria-label={`删除规则${index + 1}`}><Trash2 size={13}/></button></div>)}
                  {!(field.unitConversions || []).length && <small className="data-unit-conversion-empty">例如：1000 us = 1 ms；再添加 1000 ms = 1 s，可自动完成 us → ms → s 的链式换算。</small>}
                </div>
              </div>}
            </div>}
          </div>)}{!draft.fields.length && <div className="resource-empty inline-empty">请在样例日志中划选参数名/参数值；如果日志包含字典，也可以直接勾选自动识别出的字段。</div>}</div>
        </section>

        <section className="rule-editor-step">
          <div className="rule-step-heading"><span>3</span><div><strong>作用范围</strong><small>为空表示全部；建议限定到具体子系统 / FM，减少每行日志需要检查的规则数量。</small></div></div>
          <div className="data-scope-inline-row"><div className="rule-form-grid three-columns data-scope-fields"><label><span>日志类型</span><input value={draft.sourceCategories.join(', ')} onChange={(event) => update({ sourceCategories: splitList(event.target.value) })} placeholder="debug, executor"/></label><label><span>子系统</span><input value={draft.subsystems.join(', ')} onChange={(event) => update({ subsystems: splitList(event.target.value) })} placeholder="例如：子系统A"/></label><label><span>FM / 模块</span><input value={draft.modules.join(', ')} onChange={(event) => update({ modules: splitList(event.target.value) })} placeholder="例如：模块A"/></label></div><label className="data-case-inline-toggle"><input type="checkbox" checked={draft.caseSensitive} onChange={(event) => update({ caseSensitive: event.target.checked })}/> 匹配时区分大小写</label></div>
        </section>

        <section className="rule-editor-step"><div className="rule-step-heading"><span>4</span><div><strong>测试提取</strong><small>直接使用上面的样例检查最终字段值。</small></div></div>{testResult ? <div className={`rule-test-result ${testResult.ok ? 'success' : 'error'}`}>{testResult.ok ? <Check size={14}/> : <X size={14}/>}<span>{testResult.message}</span></div> : <div className="rule-test-hint">保存前建议先测试一次。</div>}</section>
      </div>
      <footer className="semantic-rule-drawer-footer"><span className={validateDataExtractionRule(draft) ? 'validation-error' : 'validation-ready'}>{validateDataExtractionRule(draft) || (draft.enabled ? '规则已启用，会出现在“提取数据”的采集能力列表中。' : '规则当前停用，保存后不会参与数据提取。')}</span><div><button className="button secondary" onClick={testRule}><FlaskConical size={14}/> 测试提取</button><button className="button ghost" onClick={() => setDraft(undefined)}>取消</button><button className="button primary" onClick={save}><Save size={14}/> 保存规则</button></div></footer>
    </aside></div>}
  </div>;
}
