import { useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowDown,
  ArrowLeft,
  ArrowUp,
  Braces,
  Check,
  ChevronRight,
  FlaskConical,
  Plus,
  Save,
  Search,
  Settings,
  Sparkles,
  Trash2,
  X,
  DatabaseBackup,
} from 'lucide-react';
import type { DisplayRule, DisplayRuleEditorRequest, DisplayRuleKind, DisplayRuleParameter, ParameterMark } from '../rendering/displayRules';
import type { LogEntry } from '../types';
import { RuleExchangePanel } from './RuleExchangePanel';
import { useImeCompositionGuard } from '../utils/imeComposition';
import {
  buildPatternTokens,
  createDisplayRuleId,
  extractDisplayRuleMessage,
  markParameterOccurrences,
  matchDisplayRuleToMessage,
  removeParameterMarks,
  renderPatternPreview,
  validateDisplayRule,
} from '../rendering/displayRules';

interface SettingsCenterProps {
  open: boolean;
  errorKeywords: readonly string[];
  keywordDraft: string;
  displayRules: readonly DisplayRule[];
  editorRequest?: DisplayRuleEditorRequest;
  currentEntries: readonly LogEntry[];
  taskName: string;
  onKeywordDraftChange: (value: string) => void;
  onAddErrorKeywords: () => void;
  onRemoveErrorKeyword: (keyword: string) => void;
  onRestoreDefaultErrorKeywords: () => void;
  onDisplayRulesChange: (rules: DisplayRule[]) => void;
  onClose: () => void;
}

type SettingsTab = 'anomaly' | 'display' | 'exchange';

interface RuleEditorState {
  rule: DisplayRule;
  sampleRaw: string;
  sampleMessage: string;
  marks: ParameterMark[];
  testInput: string;
  testState?: { success: boolean; message: string };
}

function createEmptyRule(kind: DisplayRuleKind = 'keyword'): DisplayRule {
  return {
    id: createDisplayRuleId(),
    name: '',
    enabled: true,
    kind,
    scope: kind === 'keyword' ? 'function' : 'both',
    keyword: '',
    displayTemplate: '',
    patternTokens: [],
    parameters: [],
    sampleMessage: '',
    createdAt: Date.now(),
  };
}

function restoreMarks(rule: DisplayRule): ParameterMark[] {
  if (rule.kind !== 'template' || !rule.sampleMessage || !rule.patternTokens?.length) return [];
  const parameterById = new Map(rule.parameters.map((parameter) => [parameter.id, parameter]));
  const marks: ParameterMark[] = [];
  let cursor = 0;
  rule.patternTokens.forEach((token) => {
    if (token.kind === 'text') {
      cursor += token.value.length;
      return;
    }
    const value = parameterById.get(token.parameterId)?.sampleValue ?? '';
    if (!value) return;
    marks.push({ parameterId: token.parameterId, start: cursor, end: cursor + value.length });
    cursor += value.length;
  });
  return marks;
}

function createEditorState(rule: DisplayRule): RuleEditorState {
  return {
    rule: {
      ...rule,
      parameters: rule.parameters.map((parameter) => ({ ...parameter })),
      patternTokens: rule.patternTokens?.map((token) => ({ ...token })),
    },
    sampleRaw: rule.sampleMessage ?? '',
    sampleMessage: rule.sampleMessage ?? '',
    marks: restoreMarks(rule),
    testInput: rule.sampleMessage ?? '',
    testState: undefined,
  };
}

function ruleKindLabel(kind: DisplayRuleKind): string {
  return kind === 'keyword' ? '关键字替换' : '智能参数模板';
}

export function SettingsCenter({
  open,
  errorKeywords,
  keywordDraft,
  displayRules,
  editorRequest,
  currentEntries,
  taskName,
  onKeywordDraftChange,
  onAddErrorKeywords,
  onRemoveErrorKeyword,
  onRestoreDefaultErrorKeywords,
  onDisplayRulesChange,
  onClose,
}: SettingsCenterProps) {
  const [tab, setTab] = useState<SettingsTab>('display');
  const [editor, setEditor] = useState<RuleEditorState>();
  const [selectedText, setSelectedText] = useState('');
  const [markAllOccurrences, setMarkAllOccurrences] = useState(false);
  const [ruleSearch, setRuleSearch] = useState('');
  const sampleTextareaRef = useRef<HTMLTextAreaElement>(null);
  const templateTextareaRef = useRef<HTMLTextAreaElement>(null);
  const handledEditorRequestRef = useRef<string>();

  // 输入法选词时的回车不能当提交（异常关键字这类中文输入尤其容易踩）。
  const imeGuard = useImeCompositionGuard();
  const filteredDisplayRules = useMemo(() => {
    const keyword = ruleSearch.trim().toLocaleLowerCase();
    if (!keyword) return displayRules.map((rule, index) => ({ rule, index }));
    return displayRules.flatMap((rule, index) => {
      const searchable = [
        rule.name,
        ruleKindLabel(rule.kind),
        rule.keyword ?? '',
        rule.displayTemplate,
        rule.sampleMessage ?? '',
        ...rule.parameters.flatMap((parameter) => [parameter.label, parameter.sampleValue]),
      ].join('\n').toLocaleLowerCase();
      return searchable.includes(keyword) ? [{ rule, index }] : [];
    });
  }, [displayRules, ruleSearch]);

  useEffect(() => {
    if (!open) {
      setEditor(undefined);
      setSelectedText('');
    }
  }, [open]);


  useEffect(() => {
    if (!open || !editorRequest || handledEditorRequestRef.current === editorRequest.requestId) return;
    handledEditorRequestRef.current = editorRequest.requestId;
    setTab('display');
    const existing = editorRequest.ruleId
      ? displayRules.find((rule) => rule.id === editorRequest.ruleId)
      : undefined;
    if (existing) {
      setEditor(createEditorState(existing));
      setSelectedText('');
      return;
    }
    const seeded: DisplayRule = {
      ...createEmptyRule('keyword'),
      name: editorRequest.suggestedName,
      keyword: editorRequest.suggestedKeyword,
      scope: editorRequest.suggestedScope,
      sampleMessage: extractDisplayRuleMessage(editorRequest.sampleRaw),
    };
    const state = createEditorState(seeded);
    state.sampleRaw = editorRequest.sampleRaw;
    state.sampleMessage = extractDisplayRuleMessage(editorRequest.sampleRaw);
    state.testInput = editorRequest.sampleRaw;
    setEditor(state);
    setSelectedText('');
  }, [displayRules, editorRequest, open]);

  const previewTokens = useMemo(() => (
    editor ? renderPatternPreview(editor.sampleMessage, editor.marks, editor.rule.parameters) : []
  ), [editor]);
  const selectedOccurrenceCount = useMemo(() => {
    if (!editor || !selectedText) return 0;
    let count = 0;
    let cursor = 0;
    while (cursor <= editor.sampleMessage.length - selectedText.length) {
      const index = editor.sampleMessage.indexOf(selectedText, cursor);
      if (index < 0) break;
      count += 1;
      cursor = index + Math.max(1, selectedText.length);
    }
    return count;
  }, [editor, selectedText]);

  if (!open) return null;

  function updateRule(patch: Partial<DisplayRule>): void {
    setEditor((current) => current ? {
      ...current,
      rule: { ...current.rule, ...patch },
      testState: undefined,
    } : current);
  }

  function beginNewRule(kind: DisplayRuleKind): void {
    setTab('display');
    setEditor(createEditorState(createEmptyRule(kind)));
    setSelectedText('');
  }

  function editRule(rule: DisplayRule): void {
    setEditor(createEditorState(rule));
    setSelectedText('');
  }

  function changeRuleKind(kind: DisplayRuleKind): void {
    setEditor((current) => {
      if (!current) return current;
      const rule = createEmptyRule(kind);
      const sampleMessage = current.sampleMessage || extractDisplayRuleMessage(current.sampleRaw);
      return {
        ...current,
        marks: [],
        testState: undefined,
        rule: {
          ...rule,
          id: current.rule.id,
          name: current.rule.name,
          enabled: current.rule.enabled,
          keyword: kind === 'keyword' ? current.rule.keyword : undefined,
          displayTemplate: current.rule.displayTemplate,
          scope: current.rule.scope,
          sampleMessage,
          patternTokens: kind === 'template' && sampleMessage ? [{ kind: 'text', value: sampleMessage }] : undefined,
          parameters: [],
          createdAt: current.rule.createdAt,
        },
      };
    });
    setSelectedText('');
  }

  function updateSample(raw: string): void {
    const message = extractDisplayRuleMessage(raw);
    setEditor((current) => current ? {
      ...current,
      sampleRaw: raw,
      sampleMessage: message,
      marks: [],
      testInput: raw,
      testState: undefined,
      rule: {
        ...current.rule,
        sampleMessage: message,
        parameters: [],
        patternTokens: message ? [{ kind: 'text', value: message }] : [],
      },
    } : current);
    setSelectedText('');
  }

  function captureSelection(): void {
    const textarea = sampleTextareaRef.current;
    if (!textarea || !editor) return;
    const start = textarea.selectionStart;
    const end = textarea.selectionEnd;
    setSelectedText(editor.sampleMessage.slice(Math.min(start, end), Math.max(start, end)));
  }

  function addSelectedParameter(): void {
    const textarea = sampleTextareaRef.current;
    if (!textarea || !editor) return;
    let parameterIndex = 1;
    const usedLabels = new Set(editor.rule.parameters.map((parameter) => parameter.label));
    while (usedLabels.has(`参数${parameterIndex}`)) parameterIndex += 1;
    const parameterId = `parameter-${parameterIndex}-${Date.now()}`;
    const result = markParameterOccurrences(
      editor.sampleMessage,
      textarea.selectionStart,
      textarea.selectionEnd,
      parameterId,
      editor.marks,
      markAllOccurrences,
    );
    if (!result.value) return;
    if (!result.marks.some((mark) => mark.parameterId === parameterId)) {
      setEditor({ ...editor, testState: { success: false, message: '当前选区与已有参数重叠，请重新划选。' } });
      return;
    }

    const parameter: DisplayRuleParameter = {
      id: parameterId,
      label: `参数${parameterIndex}`,
      sampleValue: result.value,
    };
    const parameters = [...editor.rule.parameters, parameter];
    const patternTokens = buildPatternTokens(editor.sampleMessage, result.marks);
    setEditor({
      ...editor,
      marks: result.marks,
      testState: undefined,
      rule: { ...editor.rule, parameters, patternTokens, sampleMessage: editor.sampleMessage },
    });
    setSelectedText('');
    setMarkAllOccurrences(false);
  }

  function removeParameter(parameterId: string): void {
    setEditor((current) => {
      if (!current) return current;
      const parameter = current.rule.parameters.find((item) => item.id === parameterId);
      const marks = removeParameterMarks(current.marks, parameterId);
      const parameters = current.rule.parameters.filter((item) => item.id !== parameterId);
      const oldPlaceholder = parameter ? `{${parameter.label}}` : '';
      return {
        ...current,
        marks,
        testState: undefined,
        rule: {
          ...current.rule,
          parameters,
          patternTokens: buildPatternTokens(current.sampleMessage, marks),
          displayTemplate: oldPlaceholder
            ? current.rule.displayTemplate.split(oldPlaceholder).join('')
            : current.rule.displayTemplate,
        },
      };
    });
  }

  function renameParameter(parameterId: string, label: string): void {
    setEditor((current) => {
      if (!current) return current;
      const previous = current.rule.parameters.find((parameter) => parameter.id === parameterId);
      const parameters = current.rule.parameters.map((parameter) => (
        parameter.id === parameterId ? { ...parameter, label } : parameter
      ));
      const displayTemplate = previous && previous.label !== label
        ? current.rule.displayTemplate.split(`{${previous.label}}`).join(`{${label}}`)
        : current.rule.displayTemplate;
      return {
        ...current,
        testState: undefined,
        rule: { ...current.rule, parameters, displayTemplate },
      };
    });
  }

  function insertPlaceholder(parameter: DisplayRuleParameter): void {
    const textarea = templateTextareaRef.current;
    if (!textarea || !editor) return;
    const placeholder = `{${parameter.label}}`;
    const start = textarea.selectionStart ?? editor.rule.displayTemplate.length;
    const end = textarea.selectionEnd ?? start;
    const next = `${editor.rule.displayTemplate.slice(0, start)}${placeholder}${editor.rule.displayTemplate.slice(end)}`;
    updateRule({ displayTemplate: next });
    window.requestAnimationFrame(() => {
      textarea.focus();
      textarea.setSelectionRange(start + placeholder.length, start + placeholder.length);
    });
  }

  function currentEditorRule(): DisplayRule | undefined {
    if (!editor) return undefined;
    return {
      ...editor.rule,
      keyword: editor.rule.keyword?.trim(),
      sampleMessage: editor.sampleMessage,
      patternTokens: editor.rule.kind === 'template'
        ? buildPatternTokens(editor.sampleMessage, editor.marks)
        : undefined,
    };
  }

  function testRule(): void {
    const rule = currentEditorRule();
    if (!editor || !rule) return;
    const validation = validateDisplayRule(rule);
    if (validation) {
      setEditor({ ...editor, testState: { success: false, message: validation } });
      return;
    }
    const input = editor.testInput.trim() ? editor.testInput : editor.sampleMessage;
    const result = matchDisplayRuleToMessage(rule, input);
    setEditor({
      ...editor,
      testState: result
        ? { success: true, message: result.text }
        : { success: false, message: '未命中。请检查固定语句、参数选区和测试日志是否一致。' },
    });
  }

  function saveRule(): void {
    const rule = currentEditorRule();
    if (!editor || !rule) return;
    const validation = validateDisplayRule(rule);
    if (validation) {
      setEditor({ ...editor, testState: { success: false, message: validation } });
      return;
    }
    if (rule.kind === 'template' && !editor.testState?.success) {
      setEditor({ ...editor, testState: { success: false, message: '保存高级模板前，请先点击“测试解析”并确认命中。' } });
      return;
    }

    const exists = displayRules.some((item) => item.id === rule.id);
    onDisplayRulesChange(exists
      ? displayRules.map((item) => item.id === rule.id ? rule : item)
      : [...displayRules, rule]);
    setEditor(undefined);
  }

  function toggleRule(ruleId: string): void {
    onDisplayRulesChange(displayRules.map((rule) => rule.id === ruleId ? { ...rule, enabled: !rule.enabled } : rule));
  }

  function deleteRule(ruleId: string): void {
    onDisplayRulesChange(displayRules.filter((rule) => rule.id !== ruleId));
    if (editor?.rule.id === ruleId) setEditor(undefined);
  }

  function moveRule(ruleId: string, direction: -1 | 1): void {
    const index = displayRules.findIndex((rule) => rule.id === ruleId);
    const target = index + direction;
    if (index < 0 || target < 0 || target >= displayRules.length) return;
    const next = [...displayRules];
    [next[index], next[target]] = [next[target], next[index]];
    onDisplayRulesChange(next);
  }

  const ruleForValidation = currentEditorRule();
  const validationMessage = ruleForValidation ? validateDisplayRule(ruleForValidation) : undefined;

  return (
    <div className="settings-page-shell">
      <section
        className="settings-center-page"
        aria-labelledby="settings-center-title"
      >
        <header className="settings-page-header settings-center-header">
          <div className="settings-page-title">
            <button type="button" className="button secondary settings-back-button" onClick={onClose}><ArrowLeft size={16} /> 返回日志</button>
            <div>
              <div className="eyebrow">TRACELENS SETTINGS</div>
              <h2 id="settings-center-title"><Settings size={18} /> 设置中心</h2>
            </div>
          </div>
          <span className="settings-page-task">当前任务：{taskName}</span>
        </header>

        <div className="settings-center-tabs" role="tablist">
          <button type="button" className={tab === 'display' ? 'active' : ''} onClick={() => setTab('display')}><Sparkles size={15} /> 展示模板</button>
          <button type="button" className={tab === 'anomaly' ? 'active' : ''} onClick={() => setTab('anomaly')}><FlaskConical size={15} /> 异常规则</button>
          <button type="button" className={tab === 'exchange' ? 'active' : ''} onClick={() => setTab('exchange')}><DatabaseBackup size={15} /> 导入导出</button>
        </div>

        {tab === 'anomaly' ? (
          <div className="paste-dialog-body anomaly-settings-body settings-tab-body">
            <div className="settings-section-heading">
              <div><strong>异常关键字</strong><span>区分大小写，只匹配完整标记。</span></div>
            </div>
            <div className="anomaly-keyword-editor">
              <input
                value={keywordDraft}
                onChange={(event) => onKeywordDraftChange(event.target.value)}
                onCompositionStart={imeGuard.onCompositionStart}
                onCompositionEnd={imeGuard.onCompositionEnd}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && !imeGuard.isComposing(event)) {
                    event.preventDefault();
                    onAddErrorKeywords();
                  }
                }}
                placeholder="输入关键字；多个关键字可用逗号或换行分隔"
              />
              <button type="button" className="button primary" onClick={onAddErrorKeywords} disabled={!keywordDraft.trim()}><Plus size={15} /> 添加</button>
            </div>
            <div className="anomaly-keyword-list">
              {errorKeywords.map((keyword) => (
                <span className="anomaly-keyword-chip" key={keyword}>
                  {keyword}
                  <button type="button" onClick={() => onRemoveErrorKeyword(keyword)} aria-label={`删除关键字 ${keyword}`}><X size={12} /></button>
                </span>
              ))}
              {errorKeywords.length === 0 && <span className="anomaly-keyword-empty">当前没有异常关键字。</span>}
            </div>
            <div className="settings-inline-actions">
              <button type="button" className="button secondary" onClick={onRestoreDefaultErrorKeywords}>恢复默认</button>
            </div>
          </div>
        ) : tab === 'exchange' ? (
          <div className="paste-dialog-body settings-tab-body rule-exchange-tab-body">
            <RuleExchangePanel
              displayRules={displayRules}
              currentEntries={currentEntries}
              taskName={taskName}
              onDisplayRulesChange={onDisplayRulesChange}
            />
          </div>
        ) : (
          <div className="settings-display-layout">
            <aside className="display-rule-list-panel">
              <div className="settings-section-heading">
                <div><strong>展示规则</strong><span>从上到下优先匹配。</span></div>
                <div className="display-rule-add-actions">
                  <button type="button" className="button ghost compact-button" onClick={() => beginNewRule('keyword')}><Plus size={14} /> 关键字</button>
                  <button type="button" className="button primary compact-button" onClick={() => beginNewRule('template')}><Sparkles size={14} /> 智能模板</button>
                </div>
              </div>
              <div className="display-rule-search">
                <Search size={14} aria-hidden="true" />
                <input
                  value={ruleSearch}
                  onChange={(event) => setRuleSearch(event.target.value)}
                  placeholder="搜索规则名、关键字、模板或样例日志"
                  aria-label="搜索展示规则"
                />
                {ruleSearch && (
                  <button type="button" onClick={() => setRuleSearch('')} aria-label="清空规则搜索"><X size={13} /></button>
                )}
              </div>
              <div className="display-rule-list-summary">
                <span>共 {displayRules.length} 条</span>
                {ruleSearch.trim() && <span>匹配 {filteredDisplayRules.length} 条</span>}
              </div>
              <div className="display-rule-list">
                {filteredDisplayRules.map(({ rule, index }) => {
                  const matchText = rule.kind === 'keyword'
                    ? (rule.keyword || '未设置关键字')
                    : (rule.sampleMessage || '未设置样例');
                  return (
                    <div key={rule.id} className={`display-rule-list-item ${editor?.rule.id === rule.id ? 'active' : ''} ${!rule.enabled ? 'disabled' : ''}`}>
                      <button type="button" className="display-rule-select" onClick={() => editRule(rule)} title={`${rule.name}\n${matchText}\n→ ${rule.displayTemplate}`}>
                        <span className="rule-state-dot" />
                        <strong>{rule.name || '未命名规则'}</strong>
                        <span className="display-rule-kind">{ruleKindLabel(rule.kind)}</span>
                        <span className="display-rule-match">{matchText}</span>
                        <span className="display-rule-arrow">→</span>
                        <span className="display-rule-result">{rule.displayTemplate || '未填写展示模板'}</span>
                        <ChevronRight size={14} />
                      </button>
                      <div className="display-rule-row-actions">
                        <button type="button" onClick={() => toggleRule(rule.id)} title={rule.enabled ? '停用规则' : '启用规则'}>{rule.enabled ? <Check size={13} /> : <X size={13} />}</button>
                        <button type="button" disabled={index === 0} onClick={() => moveRule(rule.id, -1)} title="提高优先级"><ArrowUp size={13} /></button>
                        <button type="button" disabled={index === displayRules.length - 1} onClick={() => moveRule(rule.id, 1)} title="降低优先级"><ArrowDown size={13} /></button>
                        <button type="button" onClick={() => deleteRule(rule.id)} title="删除规则"><Trash2 size={13} /></button>
                      </div>
                    </div>
                  );
                })}
                {displayRules.length === 0 && (
                  <div className="display-rule-empty"><Sparkles size={25} /><strong>还没有展示规则</strong><span>可先创建“关键字替换”，或用智能模板从样例日志提取参数。</span></div>
                )}
                {displayRules.length > 0 && filteredDisplayRules.length === 0 && (
                  <div className="display-rule-empty compact"><Search size={22} /><strong>没有匹配规则</strong><span>换一个规则名、关键字或展示模板继续查找。</span></div>
                )}
              </div>
            </aside>

            <main className="display-rule-editor-panel">
              {!editor ? (
                <div className="display-rule-editor-empty">
                  <Braces size={36} />
                  <h3>配置函数与流程的语义说明</h3>
                  <p>关键字规则适合固定方法名；智能模板可通过划选样例日志中的变量，自动生成匹配规则，无需手写正则。</p>
                </div>
              ) : (
                <div className="display-rule-editor">
                  <div className="rule-editor-scroll-content">
                  <div className="rule-editor-toolbar">
                    <div className="rule-kind-switch">
                      <button type="button" className={editor.rule.kind === 'keyword' ? 'active' : ''} onClick={() => changeRuleKind('keyword')}>关键字替换</button>
                      <button type="button" className={editor.rule.kind === 'template' ? 'active' : ''} onClick={() => changeRuleKind('template')}>智能参数模板</button>
                    </div>
                    <label className="rule-enabled-control"><input type="checkbox" checked={editor.rule.enabled} onChange={(event) => updateRule({ enabled: event.target.checked })} /> 启用</label>
                  </div>

                  <div className="rule-form-grid three-columns">
                    <label><span>规则名称</span><input value={editor.rule.name} onChange={(event) => updateRule({ name: event.target.value })} placeholder="例如：安全初始化依赖组件" /></label>
                    {editor.rule.kind === 'keyword' && (
                      <label><span>正文包含关键字</span><input value={editor.rule.keyword ?? ''} onChange={(event) => updateRule({ keyword: event.target.value })} placeholder="safe_initialize_default_dependencies()" /></label>
                    )}
                    <label><span>应用位置</span><select value={editor.rule.scope} onChange={(event) => updateRule({ scope: event.target.value as DisplayRule['scope'] })}><option value="function">仅折叠函数标题</option><option value="log">仅日志正文</option><option value="both">函数标题与日志正文</option></select></label>
                  </div>

                  {editor.rule.kind === 'template' && (
                    <>
                      <section className="rule-editor-step">
                        <div className="rule-step-heading"><span>1</span><div><strong>粘贴样例日志</strong><small>可以粘贴完整原始日志，系统会自动提取第 8 个字段之后的正文。</small></div></div>
                        <textarea className="rule-sample-input" value={editor.sampleRaw} onChange={(event) => updateSample(event.target.value)} placeholder="粘贴一条完整日志，例如：[2026-...] ... init_and_release() <DSPWSFT> Measurer DSPWSFT begin init." />
                      </section>

                      <section className="rule-editor-step">
                        <div className="rule-step-heading"><span>2</span><div><strong>划选变量</strong><small>在正文中拖选变量后点击“设为参数”；相同文本会自动标记为同一参数。</small></div></div>
                        <textarea
                          ref={sampleTextareaRef}
                          className="rule-selection-textarea"
                          readOnly
                          value={editor.sampleMessage}
                          onSelect={captureSelection}
                          onMouseUp={captureSelection}
                          onKeyUp={captureSelection}
                          placeholder="识别后的日志正文会显示在这里"
                        />
                        <div className="rule-selection-actions">
                          <span>当前选中：<strong>{selectedText || '请在上方划选文本'}</strong></span>
                          <div className="rule-selection-control-group">
                            <label><input type="checkbox" checked={markAllOccurrences} onChange={(event) => setMarkAllOccurrences(event.target.checked)} /> 同步标记相同文本{selectedOccurrenceCount > 1 ? `（${selectedOccurrenceCount} 处）` : ""}</label>
                            <button type="button" className="button primary compact-button" disabled={!selectedText} onClick={addSelectedParameter}><Braces size={14} /> 设为参数</button>
                          </div>
                        </div>
                        {editor.rule.parameters.length > 0 && (
                          <div className="rule-parameter-list">
                            {editor.rule.parameters.map((parameter) => (
                              <div key={parameter.id} className="rule-parameter-item">
                                <span className="parameter-sample-value">{parameter.sampleValue}</span>
                                <span>→</span>
                                <input value={parameter.label} onChange={(event) => renameParameter(parameter.id, event.target.value)} aria-label="参数名称" />
                                <code>{`{${parameter.label}}`}</code>
                                <button type="button" onClick={() => removeParameter(parameter.id)} aria-label={`删除${parameter.label}`}><X size={13} /></button>
                              </div>
                            ))}
                          </div>
                        )}
                        {editor.sampleMessage && (
                          <div className="rule-pattern-preview" aria-label="自动生成的匹配模板">
                            {previewTokens.map((token) => token.kind === 'parameter'
                              ? <mark key={token.key} title={token.value}>{`{${token.label}}`}</mark>
                              : <span key={token.key}>{token.value}</span>)}
                          </div>
                        )}
                      </section>
                    </>
                  )}

                  <section className="rule-editor-step">
                    <div className="rule-step-heading"><span>{editor.rule.kind === 'template' ? '3' : '1'}</span><div><strong>填写展示说明</strong><small>命中后替代函数名或日志正文；只需插入想展示的参数，其他参数可仅用于匹配。</small></div></div>
                    <textarea
                      ref={templateTextareaRef}
                      className="rule-display-template-input"
                      value={editor.rule.displayTemplate}
                      onChange={(event) => updateRule({ displayTemplate: event.target.value })}
                      placeholder={editor.rule.kind === 'template' ? '例如：测校项 {参数1} 开始执行 {参数2} 阶段' : '例如：安全初始化依赖组件'}
                    />
                    {editor.rule.parameters.length > 0 && (
                      <div className="rule-placeholder-buttons">
                        <span>插入参数：</span>
                        {editor.rule.parameters.map((parameter) => (
                          <button type="button" key={parameter.id} onClick={() => insertPlaceholder(parameter)}>{`{${parameter.label}}`}</button>
                        ))}
                      </div>
                    )}
                  </section>

                  <section className="rule-editor-step">
                    <div className="rule-step-heading"><span>{editor.rule.kind === 'template' ? '4' : '2'}</span><div><strong>测试解析</strong><small>粘贴同类日志，确认参数提取和最终说明正确。</small></div></div>
                    <textarea className="rule-test-input" value={editor.testInput} onChange={(event) => setEditor({ ...editor, testInput: event.target.value, testState: undefined })} placeholder="粘贴用于验证的日志；不填时使用样例日志" />
                    <div className="rule-test-row">
                      {editor.testState
                        ? <div className={`rule-test-result ${editor.testState.success ? 'success' : 'error'}`}>{editor.testState.success ? <Check size={14} /> : <X size={14} />}<span>{editor.testState.message}</span></div>
                        : <span className="rule-test-hint">填写测试日志后，在下方固定操作栏点击“测试解析”。</span>}
                    </div>
                  </section>

                  </div>

                  <div className="rule-editor-footer">
                    <span className={validationMessage ? 'validation-error' : 'validation-ready'}>{validationMessage ?? '规则字段完整，可测试并保存。'}</span>
                    <div>
                      <button type="button" className="button secondary" onClick={testRule}><FlaskConical size={14} /> 测试解析</button>
                      <button type="button" className="button ghost" onClick={() => setEditor(undefined)}>取消</button>
                      <button type="button" className="button primary" onClick={saveRule}><Save size={14} /> 保存规则</button>
                    </div>
                  </div>
                </div>
              )}
            </main>
          </div>
        )}

      </section>
    </div>
  );
}
