import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Braces,
  Check,
  EyeOff,
  FileCode2,
  FlaskConical,
  LoaderCircle,
  Pencil,
  Plus,
  Save,
  Search,
  ShieldAlert,
  Database,
  Sparkles,
  Trash2,
  X,
} from 'lucide-react';
import { autoconfigureRule, type SemanticRuleAutoconfigResult } from '../api/resourceApi';
import { AiAutoconfigButton, type AiAutoconfigOutcome } from './AiAutoconfigButton';
import {
  buildPatternTokens,
  createDisplayRuleId,
  extractDisplayRuleMessage,
  markParameterOccurrences,
  matchDisplayRuleToMessage,
  removeParameterMarks,
  renderPatternPreview,
  validateDisplayRule,
  type DisplayRule,
  type DisplayRuleEditorRequest,
  type DisplayRuleKind,
  type DisplayRuleMode,
  type DisplayRuleParameter,
  type ParameterMark,
} from '../rendering/displayRules';
import { createMaskingRuleId, type MaskingRule, type MaskingRuleKind, type MaskingRuleScope } from '../rendering/maskingRules';
import { recognizeSemanticSource } from '../api/resourceApi';
import { createErrorMatchRule, type ErrorMatchRule } from '../parser/logParser';
import type { DataExtractionRule } from '../rendering/dataExtractionRules';
import { createCustomFoldingRule, foldingRuleLogicLabel, type FoldingRule } from '../rendering/foldingRules';
import { DataExtractionRulesPanel } from './DataExtractionRulesPanel';
import { LogFormatRulesPanel } from './LogFormatRulesPanel';

type RuleTab = 'parser' | 'semantic' | 'anomaly' | 'masking' | 'folding' | 'data';
interface Props {
  errorRules: readonly ErrorMatchRule[];
  displayRules: readonly DisplayRule[];
  maskingRules: readonly MaskingRule[];
  foldingRules: readonly FoldingRule[];
  dataExtractionRules: readonly DataExtractionRule[];
  editorRequest?: DisplayRuleEditorRequest;
  sourceSeed?: DisplayRuleEditorRequest;
  onErrorRulesChange: (values: ErrorMatchRule[]) => void;
  onDisplayRulesChange: (rules: DisplayRule[]) => void;
  onMaskingRulesChange: (rules: MaskingRule[]) => void;
  onFoldingRulesChange: (rules: FoldingRule[]) => void;
  onDataExtractionRulesChange: (rules: DataExtractionRule[]) => void;
  onEditorRequestHandled?: () => void;
}

interface RuleEditorState {
  rule: DisplayRule;
  sampleRaw: string;
  sampleMessage: string;
  marks: ParameterMark[];
  testInput: string;
  testState?: { success: boolean; message: string };
}

function semanticKind(rule: DisplayRule) { return rule.kind === 'template' ? '智能参数模板' : '关键字规则'; }
function scopeLabel(scope: DisplayRule['scope']) { return scope === 'function' ? '函数' : scope === 'log' ? '日志' : '函数 + 日志'; }
function displayModeLabel(mode?: DisplayRuleMode) { return (mode ?? 'semantic') === 'label' ? '仅标签' : (mode ?? 'semantic') === 'both' ? '语义 + 标签' : '仅语义'; }
function maskKindLabel(kind: MaskingRuleKind) { return ({ keyword: '关键字', function: '函数名', regex: '正则', template: '智能模板' } as const)[kind]; }

function createEmptyRule(kind: DisplayRuleKind = 'keyword'): DisplayRule {
  return {
    id: createDisplayRuleId(),
    name: '',
    enabled: true,
    kind,
    scope: 'function',
    keyword: '',
    displayTemplate: '',
    displayMode: 'semantic',
    customLabelTemplate: '',
    customLabelColor: '#2563eb',
    showLabelOnTimeline: true,
    liveWatch: false,
    supplementalDescription: '',
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

export function LogRulesSettingsPage({ errorRules, displayRules, maskingRules, foldingRules, dataExtractionRules, editorRequest, sourceSeed, onErrorRulesChange, onDisplayRulesChange, onMaskingRulesChange, onFoldingRulesChange, onDataExtractionRulesChange, onEditorRequestHandled }: Props) {
  const [tab, setTab] = useState<RuleTab>(() => {
    if (typeof window === 'undefined') return 'semantic';
    const requested = window.sessionStorage.getItem('tracelens-ai-rule-tab-v1');
    window.sessionStorage.removeItem('tracelens-ai-rule-tab-v1');
    // parser / folding 已从界面隐藏：历史会话里存过的值要落到 semantic，
    // 否则会出现「一个标签都没高亮、内容却是格式解析」的空态。
    return requested === 'data' || requested === 'anomaly' ? requested : 'semantic';
  });
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [semanticEditor, setSemanticEditor] = useState<RuleEditorState>();
  const [selectedText, setSelectedText] = useState('');
  const [markAllOccurrences, setMarkAllOccurrences] = useState(false);
  const sampleTextareaRef = useRef<HTMLTextAreaElement>(null);
  const templateTextareaRef = useRef<HTMLTextAreaElement>(null);
  const labelTemplateTextareaRef = useRef<HTMLTextAreaElement>(null);
  const [maskDraft, setMaskDraft] = useState<MaskingRule>();
  const [foldDraft, setFoldDraft] = useState<FoldingRule>();
  const [keywordDraft, setKeywordDraft] = useState('');
  const [semanticRecognition, setSemanticRecognition] = useState<{ loading: boolean; message?: string; success?: boolean }>({ loading: false });

  useEffect(() => {
    if (!editorRequest) return;
    const existing = editorRequest.ruleId ? displayRules.find((rule) => rule.id === editorRequest.ruleId) : undefined;
    if (existing) {
      setSemanticEditor(createEditorState(existing));
    } else {
      const seeded: DisplayRule = {
        ...createEmptyRule('keyword'),
        name: editorRequest.suggestedName,
        keyword: editorRequest.suggestedKeyword,
        scope: editorRequest.suggestedScope,
        displayTemplate: editorRequest.suggestedDisplayTemplate ?? '',
        sampleMessage: extractDisplayRuleMessage(editorRequest.sampleRaw),
      };
      const state = createEditorState(seeded);
      state.sampleRaw = editorRequest.sampleRaw;
      state.sampleMessage = extractDisplayRuleMessage(editorRequest.sampleRaw);
      state.testInput = editorRequest.sampleRaw;
      setSemanticEditor(state);
    }
    setSelectedText('');
    setMarkAllOccurrences(false);
    setTab('semantic');
    onEditorRequestHandled?.();
  }, [editorRequest?.requestId]);

  useEffect(() => {
    if (!sourceSeed) return;
    setTab('semantic');
    setQuery('');
    setPage(1);
    setSelected(new Set());
    setKeywordDraft(sourceSeed.suggestedKeyword || sourceSeed.functionName || extractDisplayRuleMessage(sourceSeed.sampleRaw).slice(0, 80));
    setMaskDraft(undefined);
    if (sourceSeed.openSemanticEditor) {
      const sampleMessage = extractDisplayRuleMessage(sourceSeed.sampleRaw);
      const seeded: DisplayRule = {
        ...createEmptyRule('keyword'),
        name: sourceSeed.suggestedName,
        keyword: sourceSeed.suggestedKeyword,
        scope: sourceSeed.suggestedScope,
        displayTemplate: sourceSeed.suggestedDisplayTemplate ?? '',
        sampleMessage,
      };
      const state = createEditorState(seeded);
      state.sampleRaw = sourceSeed.sampleRaw;
      state.sampleMessage = sampleMessage;
      state.testInput = sourceSeed.sampleRaw;
      setSemanticEditor(state);
    } else {
      setSemanticEditor(undefined);
    }
    setSemanticRecognition({ loading: false });
  }, [sourceSeed?.requestId]);


  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const values = tab === 'semantic' ? displayRules.map((rule) => ({ id: rule.id, search: `${rule.name} ${rule.keyword || ''} ${rule.displayTemplate} ${rule.customLabelTemplate || ''} ${rule.supplementalDescription || ''} ${rule.sampleMessage || ''} ${rule.parameters.map((item) => `${item.label} ${item.sampleValue}`).join(' ')} ${semanticKind(rule)}`, value: rule }))
      : tab === 'anomaly' ? errorRules.map((rule) => ({ id: rule.id, search: `${rule.keyword} ${rule.caseSensitive ? '区分大小写' : '不区分大小写'} ${rule.wholeWord ? '全词' : '子串'}`, value: rule }))
      : tab === 'masking' ? maskingRules.map((rule) => ({ id: rule.id, search: `${rule.name} ${rule.pattern} ${maskKindLabel(rule.kind)}`, value: rule }))
      : tab === 'folding' ? foldingRules.map((rule) => ({ id: rule.id, search: `${rule.name} ${rule.description} ${foldingRuleLogicLabel(rule)}`, value: rule }))
      : [];
    return needle ? values.filter((row) => row.search.toLowerCase().includes(needle)) : values;
  }, [tab, query, displayRules, errorRules, maskingRules, foldingRules]);
  const totalPages = Math.max(1, Math.ceil(rows.length / pageSize));
  const pageRows = rows.slice((page - 1) * pageSize, page * pageSize);
  const allPageSelected = pageRows.length > 0 && pageRows.every((row) => selected.has(row.id));
  const previewTokens = useMemo(() => (
    semanticEditor ? renderPatternPreview(semanticEditor.sampleMessage, semanticEditor.marks, semanticEditor.rule.parameters) : []
  ), [semanticEditor]);
  const selectedOccurrenceCount = useMemo(() => {
    if (!semanticEditor || !selectedText) return 0;
    let count = 0;
    let cursor = 0;
    while (cursor <= semanticEditor.sampleMessage.length - selectedText.length) {
      const index = semanticEditor.sampleMessage.indexOf(selectedText, cursor);
      if (index < 0) break;
      count += 1;
      cursor = index + Math.max(1, selectedText.length);
    }
    return count;
  }, [semanticEditor, selectedText]);

  function changeTab(next: RuleTab) {
    setTab(next);
    setSelected(new Set());
    setQuery('');
    setPage(1);
    if (typeof window !== 'undefined') {
      window.sessionStorage.setItem('tracelens-log-rules-active-tab-v1', next);
      window.dispatchEvent(new CustomEvent('tracelens:assistant-context-changed'));
    }
  }
  useEffect(() => {
    if (typeof window === 'undefined') return;
    window.sessionStorage.setItem('tracelens-log-rules-active-tab-v1', tab);
    window.dispatchEvent(new CustomEvent('tracelens:assistant-context-changed'));
  }, [tab, errorRules.length, displayRules.length, dataExtractionRules.length]);
  function toggleAll() { setSelected((current) => { const next = new Set(current); pageRows.forEach((row) => allPageSelected ? next.delete(row.id) : next.add(row.id)); return next; }); }
  function toggle(id: string) { setSelected((current) => { const next = new Set(current); next.has(id) ? next.delete(id) : next.add(id); return next; }); }
  function bulkDelete() {
    if (!selected.size) return;
    if (tab === 'semantic') onDisplayRulesChange(displayRules.filter((rule) => !selected.has(rule.id)));
    else if (tab === 'anomaly') onErrorRulesChange(errorRules.filter((rule) => !selected.has(rule.id)));
    else if (tab === 'masking') onMaskingRulesChange(maskingRules.filter((rule) => !selected.has(rule.id)));
    else if (tab === 'folding') onFoldingRulesChange(foldingRules.filter((rule) => rule.builtIn || !selected.has(rule.id)));
    setSelected(new Set());
  }
  function bulkEnabled(enabled: boolean) {
    if (tab === 'semantic') onDisplayRulesChange(displayRules.map((rule) => selected.has(rule.id) ? { ...rule, enabled } : rule));
    if (tab === 'anomaly') onErrorRulesChange(errorRules.map((rule) => selected.has(rule.id) ? { ...rule, enabled } : rule));
    if (tab === 'masking') onMaskingRulesChange(maskingRules.map((rule) => selected.has(rule.id) ? { ...rule, enabled } : rule));
    if (tab === 'folding') onFoldingRulesChange(foldingRules.map((rule) => selected.has(rule.id) ? { ...rule, enabled } : rule));
  }

  function beginSemantic(kind: DisplayRuleKind = 'keyword') {
    const base = createEmptyRule(kind);
    if (sourceSeed) {
      const sampleMessage = extractDisplayRuleMessage(sourceSeed.sampleRaw);
      const seeded: DisplayRule = {
        ...base,
        name: sourceSeed.suggestedName,
        keyword: kind === 'keyword' ? sourceSeed.suggestedKeyword : undefined,
        scope: sourceSeed.suggestedScope,
        displayTemplate: sourceSeed.suggestedDisplayTemplate ?? '',
        sampleMessage,
        patternTokens: kind === 'template' && sampleMessage ? [{ kind: 'text', value: sampleMessage }] : [],
      };
      const state = createEditorState(seeded);
      state.sampleRaw = sourceSeed.sampleRaw;
      state.sampleMessage = sampleMessage;
      state.testInput = sourceSeed.sampleRaw;
      setSemanticEditor(state);
    } else {
      setSemanticEditor(createEditorState(base));
    }
    setSelectedText('');
    setMarkAllOccurrences(false);
    setSemanticRecognition({ loading: false });
  }
  function editSemantic(rule: DisplayRule) {
    setSemanticEditor(createEditorState(rule));
    setSelectedText('');
    setMarkAllOccurrences(false);
    setSemanticRecognition({ loading: false });
  }
  function closeSemanticEditor() {
    setSemanticEditor(undefined);
    setSelectedText('');
    setMarkAllOccurrences(false);
    setSemanticRecognition({ loading: false });
  }
  function updateRule(patch: Partial<DisplayRule>) {
    setSemanticEditor((current) => current ? { ...current, rule: { ...current.rule, ...patch }, testState: undefined } : current);
  }
  function changeRuleKind(kind: DisplayRuleKind) {
    setSemanticEditor((current) => {
      if (!current || current.rule.kind === kind) return current;
      const empty = createEmptyRule(kind);
      const sampleMessage = current.sampleMessage || extractDisplayRuleMessage(current.sampleRaw);
      return {
        ...current,
        marks: [],
        testState: undefined,
        rule: {
          ...empty,
          id: current.rule.id,
          name: current.rule.name,
          enabled: current.rule.enabled,
          keyword: kind === 'keyword' ? current.rule.keyword : undefined,
          displayTemplate: current.rule.displayTemplate,
          displayMode: current.rule.displayMode ?? 'semantic',
          customLabelTemplate: current.rule.customLabelTemplate ?? '',
          customLabelColor: current.rule.customLabelColor ?? '#2563eb',
          showLabelOnTimeline: current.rule.showLabelOnTimeline !== false,
          liveWatch: current.rule.liveWatch === true,
          supplementalDescription: current.rule.supplementalDescription,
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
  function updateSample(raw: string) {
    const message = extractDisplayRuleMessage(raw);
    setSemanticEditor((current) => current ? {
      ...current,
      sampleRaw: raw,
      sampleMessage: message,
      marks: [],
      testInput: raw,
      testState: undefined,
      rule: { ...current.rule, sampleMessage: message, parameters: [], patternTokens: message ? [{ kind: 'text', value: message }] : [] },
    } : current);
    setSelectedText('');
  }
  function captureSelection() {
    const textarea = sampleTextareaRef.current;
    if (!textarea || !semanticEditor) return;
    const start = Math.min(textarea.selectionStart, textarea.selectionEnd);
    const end = Math.max(textarea.selectionStart, textarea.selectionEnd);
    setSelectedText(semanticEditor.sampleMessage.slice(start, end));
  }
  function addSelectedParameter() {
    const textarea = sampleTextareaRef.current;
    if (!textarea || !semanticEditor) return;
    let parameterIndex = 1;
    const usedLabels = new Set(semanticEditor.rule.parameters.map((parameter) => parameter.label));
    while (usedLabels.has(`参数${parameterIndex}`)) parameterIndex += 1;
    const parameterId = `parameter-${parameterIndex}-${Date.now()}`;
    const result = markParameterOccurrences(
      semanticEditor.sampleMessage,
      textarea.selectionStart,
      textarea.selectionEnd,
      parameterId,
      semanticEditor.marks,
      markAllOccurrences,
    );
    if (!result.value) return;
    if (!result.marks.some((mark) => mark.parameterId === parameterId)) {
      setSemanticEditor({ ...semanticEditor, testState: { success: false, message: '当前选区与已有参数重叠，请重新划选。' } });
      return;
    }
    const parameter: DisplayRuleParameter = { id: parameterId, label: `参数${parameterIndex}`, sampleValue: result.value };
    const parameters = [...semanticEditor.rule.parameters, parameter];
    setSemanticEditor({
      ...semanticEditor,
      marks: result.marks,
      testState: undefined,
      rule: { ...semanticEditor.rule, parameters, patternTokens: buildPatternTokens(semanticEditor.sampleMessage, result.marks), sampleMessage: semanticEditor.sampleMessage },
    });
    setSelectedText('');
    setMarkAllOccurrences(false);
  }
  function removeParameter(parameterId: string) {
    setSemanticEditor((current) => {
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
          displayTemplate: oldPlaceholder ? current.rule.displayTemplate.split(oldPlaceholder).join('') : current.rule.displayTemplate,
          customLabelTemplate: oldPlaceholder ? (current.rule.customLabelTemplate ?? '').split(oldPlaceholder).join('') : current.rule.customLabelTemplate,
        },
      };
    });
  }
  function renameParameter(parameterId: string, label: string) {
    setSemanticEditor((current) => {
      if (!current) return current;
      const previous = current.rule.parameters.find((parameter) => parameter.id === parameterId);
      const parameters = current.rule.parameters.map((parameter) => parameter.id === parameterId ? { ...parameter, label } : parameter);
      const displayTemplate = previous && previous.label !== label
        ? current.rule.displayTemplate.split(`{${previous.label}}`).join(`{${label}}`)
        : current.rule.displayTemplate;
      const customLabelTemplate = previous && previous.label !== label
        ? (current.rule.customLabelTemplate ?? '').split(`{${previous.label}}`).join(`{${label}}`)
        : current.rule.customLabelTemplate;
      return { ...current, testState: undefined, rule: { ...current.rule, parameters, displayTemplate, customLabelTemplate } };
    });
  }
  function insertPlaceholder(parameter: DisplayRuleParameter) {
    const textarea = templateTextareaRef.current;
    if (!textarea || !semanticEditor) return;
    const placeholder = `{${parameter.label}}`;
    const start = textarea.selectionStart ?? semanticEditor.rule.displayTemplate.length;
    const end = textarea.selectionEnd ?? start;
    const next = `${semanticEditor.rule.displayTemplate.slice(0, start)}${placeholder}${semanticEditor.rule.displayTemplate.slice(end)}`;
    updateRule({ displayTemplate: next });
    window.requestAnimationFrame(() => {
      textarea.focus();
      textarea.setSelectionRange(start + placeholder.length, start + placeholder.length);
    });
  }
  function insertLabelPlaceholder(parameter: DisplayRuleParameter) {
    const textarea = labelTemplateTextareaRef.current;
    if (!textarea || !semanticEditor) return;
    const current = semanticEditor.rule.customLabelTemplate ?? '';
    const placeholder = `{${parameter.label}}`;
    const start = textarea.selectionStart ?? current.length;
    const end = textarea.selectionEnd ?? start;
    const next = `${current.slice(0, start)}${placeholder}${current.slice(end)}`;
    updateRule({ customLabelTemplate: next });
    window.requestAnimationFrame(() => {
      textarea.focus();
      textarea.setSelectionRange(start + placeholder.length, start + placeholder.length);
    });
  }
  const canAutoRecognizeSemantic = Boolean(
    sourceSeed?.environmentId
    && sourceSeed.sourceFile?.toLowerCase().endsWith('.py')
    && sourceSeed.sourceTargets?.length,
  );

  async function autoRecognizeSemantic() {
    if (!sourceSeed?.environmentId || !sourceSeed.sourceFile || !sourceSeed.sourceTargets?.length) return;
    setSemanticRecognition({ loading: true });
    try {
      const result = await recognizeSemanticSource(sourceSeed.environmentId, {
        source_file: sourceSeed.sourceFile,
        source_line: sourceSeed.sourceLine,
        function_name: sourceSeed.functionName,
        fm_targets: sourceSeed.sourceTargets,
      });
      if ('found' in result && result.found === false) {
        setSemanticRecognition({ loading: false, success: false, message: result.message || '未找到源码语义' });
        return;
      }
      updateRule({ displayTemplate: result.description });
      setSemanticRecognition({
        loading: false,
        success: true,
        message: `${result.cache_hit ? '缓存命中' : '源码解析'} · ${result.subsystem}/${result.module} · ${result.qualified_name} · ${result.source_path}`,
      });
    } catch (error) {
      setSemanticRecognition({ loading: false, success: false, message: error instanceof Error ? error.message : String(error) });
    }
  }

  /**
   * 把 AI 给出的参数值在正文里定位成 ParameterMark。
   *
   * 参数只用「样例原文逐字子串」表达，所以定位是确定性的 —— 不依赖模型给下标
   * （模型给的下标经常是错的，而错一位整条模板就废了）。同一个值出现多次时只标第一处，
   * 除非用户本来就勾了「同步标记相同文本」。
   */
  function marksForParameters(message: string, parameters: readonly { id: string; sampleValue: string }[]): ParameterMark[] {
    const marks: ParameterMark[] = [];
    // 光标只前进不后退：AI 给出的参数顺序就是它们在日志里的出现顺序，按顺序定位能避免
    // 「重试次数 3」被定位到 PID「1234」里那个 3 上（真实踩过：模板预览变成 [12{重试次数}4]）。
    let cursor = 0;
    const embedded = (at: number, length: number) => {
      const isWord = (ch: string | undefined) => Boolean(ch && /[\w]/.test(ch));
      return isWord(message[at - 1]) || isWord(message[at + length]);
    };
    const findFrom = (value: string, from: number, skipEmbedded: boolean) => {
      let at = message.indexOf(value, Math.max(0, from));
      while (at >= 0) {
        const overlaps = marks.some((mark) => at < mark.end && at + value.length > mark.start);
        // 单个字符/纯数字的参数值最容易撞进别的数字里，优先挑不被字母数字包夹的那一处。
        if (!overlaps && (!skipEmbedded || !embedded(at, value.length))) return at;
        at = message.indexOf(value, at + 1);
      }
      return -1;
    };
    for (const parameter of parameters) {
      const value = parameter.sampleValue;
      if (!value) continue;
      let at = findFrom(value, cursor, true);
      if (at < 0) at = findFrom(value, cursor, false);
      if (at < 0) at = findFrom(value, 0, true);
      if (at < 0) continue;
      marks.push({ parameterId: parameter.id, start: at, end: at + value.length });
      cursor = at + value.length;
    }
    return marks.sort((left, right) => left.start - right.start);
  }

  /** 「AI 一键自动配置」语义规则：识别正文里会变化的部分，填好语义说明、参数和标签。 */
  async function autoConfigureSemantic(focus: 'all' | 'semantic' | 'label' = 'all'): Promise<AiAutoconfigOutcome> {
    const current = semanticEditor;
    if (!current) throw new Error('请先打开新增/编辑语义规则。');
    const raw = current.sampleRaw.trim() || current.sampleMessage.trim();
    if (!raw) throw new Error('请先粘贴样例日志，AI 才能识别这条日志该显示成什么语义。');
    const result = await autoconfigureRule({
      target: 'semantic_rule',
      sample: raw,
      kind: current.rule.kind,
      focus,
      hints: {
        // 浏览器已经知道「第 8 个字段之后才是正文」，把这份确定性结果交给后端，
        // 别让模型去猜日志格式。
        sampleMessage: current.sampleMessage || extractDisplayRuleMessage(raw),
        subsystems: sourceSeed?.sourceTargets?.map((item) => item.subsystem) ?? [],
        modules: sourceSeed?.sourceTargets?.map((item) => item.module) ?? [],
        functionName: sourceSeed?.functionName,
        // 已有参数名交给后端：不然模型会为标签发明一个 {模块名} 之类的占位符，
        // 而这个占位符没有对应参数，保存时会被校验直接拦下来。
        parameterLabels: current.rule.parameters.map((parameter) => parameter.label),
      },
    }) as SemanticRuleAutoconfigResult;

    const message = result.sampleMessage || extractDisplayRuleMessage(raw) || raw;
    const parameters: DisplayRuleParameter[] = result.kind === 'template'
      ? result.parameters.map((parameter) => ({ id: parameter.id, label: parameter.label, sampleValue: parameter.sampleValue }))
      : [];
    const marks = result.kind === 'template' ? marksForParameters(message, parameters) : [];
    const keptIds = new Set(marks.map((mark) => mark.parameterId));
    const keptParameters = parameters.filter((parameter) => keptIds.has(parameter.id));
    const droppedCount = parameters.length - keptParameters.length;

    // 按 focus 只改它负责的那块：语义说明框的按钮不该顺手改标签，反之亦然。
    const labelPatch = {
      customLabelTemplate: result.customLabelTemplate || current.rule.customLabelTemplate,
      customLabelColor: result.customLabelColor || current.rule.customLabelColor,
      displayMode: result.displayMode,
    };
    const semanticPatch = {
      sampleMessage: message,
      parameters: keptParameters,
      patternTokens: result.kind === 'template' ? buildPatternTokens(message, marks) : current.rule.patternTokens,
      displayTemplate: result.displayTemplate || current.rule.displayTemplate,
      supplementalDescription: result.supplementalDescription || current.rule.supplementalDescription,
    };
    setSemanticEditor({
      ...current,
      sampleRaw: raw,
      sampleMessage: message,
      marks,
      testInput: raw,
      testState: undefined,
      rule: {
        ...current.rule,
        ...(focus === 'all' ? {
          name: result.name || current.rule.name,
          keyword: result.kind === 'keyword' ? (result.keyword || current.rule.keyword) : undefined,
          scope: result.scope,
        } : {}),
        ...(focus === 'label' ? labelPatch : semanticPatch),
        ...(focus === 'all' && result.displayMode === 'both' ? labelPatch : {}),
      },
    });

    const warnings = [...(result.warnings ?? [])];
    if (droppedCount > 0) warnings.push(`有 ${droppedCount} 个参数在正文里定位不到，已丢弃（避免保存后显示成字面量）。`);
    const filledLabel = focus !== 'semantic' && Boolean(result.customLabelTemplate);
    return {
      message: focus === 'label'
        ? (filledLabel ? `已填入标签「${result.customLabelTemplate}」与颜色` : '已切换为标签展示模式')
        : [
            '已填入语义说明',
            result.kind === 'template' ? `${keptParameters.length} 个参数` : (result.keyword ? `关键字「${result.keyword}」` : ''),
            filledLabel ? `标签「${result.customLabelTemplate}」` : '',
          ].filter(Boolean).join(' · '),
      warnings,
      confidence: result.confidence,
    };
  }

  function currentEditorRule(): DisplayRule | undefined {
    if (!semanticEditor) return undefined;
    return {
      ...semanticEditor.rule,
      scope: semanticEditor.rule.scope,
      keyword: semanticEditor.rule.keyword?.trim(),
      sampleMessage: semanticEditor.sampleMessage,
      patternTokens: semanticEditor.rule.kind === 'template'
        ? buildPatternTokens(semanticEditor.sampleMessage, semanticEditor.marks)
        : undefined,
    };
  }
  function testSemanticRule() {
    const rule = currentEditorRule();
    if (!semanticEditor || !rule) return;
    const validation = validateDisplayRule(rule);
    if (validation) {
      setSemanticEditor({ ...semanticEditor, testState: { success: false, message: validation } });
      return;
    }
    if (rule.kind === 'keyword' && rule.scope === 'function' && sourceSeed?.functionName) {
      const matched = Boolean(rule.keyword?.trim() && sourceSeed.functionName.includes(rule.keyword.trim()));
      setSemanticEditor({
        ...semanticEditor,
        testState: matched
          ? { success: true, message: `${sourceSeed.functionName} → ${rule.displayTemplate}` }
          : { success: false, message: `函数名 ${sourceSeed.functionName} 未命中关键字 ${rule.keyword || ''}` },
      });
      return;
    }
    const input = semanticEditor.testInput.trim() ? semanticEditor.testInput : semanticEditor.sampleMessage;
    const result = matchDisplayRuleToMessage(rule, input);
    setSemanticEditor({
      ...semanticEditor,
      testState: result
        ? { success: true, message: [((rule.displayMode ?? 'semantic') !== 'label' && result.text) ? `语义：${result.text}` : '', ((rule.displayMode ?? 'semantic') !== 'semantic' && result.customLabelText) ? `标签：[${result.customLabelText}]` : ''].filter(Boolean).join(' · ') || '规则已命中' }
        : { success: false, message: '未命中。请检查固定语句、参数选区和测试日志是否一致。' },
    });
  }
  function saveSemantic() {
    const rule = currentEditorRule();
    if (!semanticEditor || !rule) return;
    const validation = validateDisplayRule(rule);
    if (validation) {
      setSemanticEditor({ ...semanticEditor, testState: { success: false, message: validation } });
      return;
    }
    if (rule.kind === 'template' && !semanticEditor.testState?.success) {
      setSemanticEditor({ ...semanticEditor, testState: { success: false, message: '保存高级模板前，请先点击“测试解析”并确认命中。' } });
      return;
    }
    const exists = displayRules.some((item) => item.id === rule.id);
    onDisplayRulesChange(exists ? displayRules.map((item) => item.id === rule.id ? rule : item) : [...displayRules, rule]);
    closeSemanticEditor();
  }

  const semanticValidation = currentEditorRule() ? validateDisplayRule(currentEditorRule()!) : undefined;

  function newMask() {
    const suggestedPattern = sourceSeed?.functionName || sourceSeed?.suggestedKeyword || (sourceSeed ? extractDisplayRuleMessage(sourceSeed.sampleRaw).slice(0, 120) : '');
    setMaskDraft({
      id: createMaskingRuleId(),
      name: sourceSeed ? `${sourceSeed.functionName || '当前日志'} 屏蔽规则` : '',
      enabled: true,
      kind: sourceSeed?.functionName ? 'function' : 'keyword',
      scope: sourceSeed?.functionName ? 'function' : 'message',
      pattern: suggestedPattern,
      caseSensitive: false,
      createdAt: Date.now(),
    });
  }
  function saveMask() { if (!maskDraft?.name.trim() || !maskDraft.pattern.trim()) return; const exists = maskingRules.some((rule) => rule.id === maskDraft.id); onMaskingRulesChange(exists ? maskingRules.map((rule) => rule.id === maskDraft.id ? maskDraft : rule) : [...maskingRules, maskDraft]); setMaskDraft(undefined); }

  function newFoldRule() {
    setFoldDraft(createCustomFoldingRule({
      name: sourceSeed?.functionName ? `${sourceSeed.functionName} START/END 折叠` : 'START/END 函数折叠',
      startKeyword: '[START]',
      endKeyword: '[END]',
    }));
  }
  function saveFoldRule() {
    if (!foldDraft || foldDraft.builtIn || !foldDraft.name.trim() || !foldDraft.startKeyword?.trim() || !foldDraft.endKeyword?.trim()) return;
    const normalized = {
      ...foldDraft,
      name: foldDraft.name.trim(),
      startKeyword: foldDraft.startKeyword.trim(),
      endKeyword: foldDraft.endKeyword.trim(),
    };
    const exists = foldingRules.some((rule) => rule.id === normalized.id);
    onFoldingRulesChange(exists ? foldingRules.map((rule) => rule.id === normalized.id ? normalized : rule) : [...foldingRules, normalized]);
    setFoldDraft(undefined);
  }
  function toggleFoldRule(ruleId: string) {
    onFoldingRulesChange(foldingRules.map((rule) => rule.id === ruleId ? { ...rule, enabled: !rule.enabled } : rule));
  }
  function addKeywords() {
    const additions = keywordDraft.split(/[,，;；\n]+/).map((v) => v.trim()).filter(Boolean);
    if (!additions.length) return;
    const existing = new Set(errorRules.map((rule) => `${rule.keyword}\u0000${rule.caseSensitive}\u0000${rule.wholeWord}`));
    const created = additions.flatMap((keyword) => {
      const key = `${keyword}\u0000false\u0000false`;
      if (existing.has(key)) return [];
      existing.add(key);
      return [createErrorMatchRule(keyword, { caseSensitive: false, wholeWord: false })];
    });
    if (created.length) onErrorRulesChange([...errorRules, ...created]);
    setKeywordDraft('');
  }
  function updateErrorRule(id: string, patch: Partial<ErrorMatchRule>) {
    onErrorRulesChange(errorRules.map((rule) => rule.id === id ? { ...rule, ...patch } : rule));
  }

  return <section className="rules-settings-page">
    {sourceSeed && <div className="rules-source-context"><div><strong>来自当前日志</strong><span>{sourceSeed.functionName || extractDisplayRuleMessage(sourceSeed.sampleRaw).slice(0, 120)}</span></div><small>语义规则、异常规则、屏蔽规则和数据提取都可以复用这条日志作为默认输入。</small></div>}
    <header className="rules-settings-toolbar"><div className="rules-subtabs"><button className={tab === 'semantic' ? 'active' : ''} onClick={() => changeTab('semantic')}><FileCode2 size={16}/> 语义规则</button><button className={tab === 'data' ? 'active' : ''} onClick={() => changeTab('data')}><Database size={16}/> 数据提取</button><button className={tab === 'anomaly' ? 'active' : ''} onClick={() => changeTab('anomaly')}><ShieldAlert size={16}/> 异常规则</button><button className={tab === 'masking' ? 'active' : ''} onClick={() => changeTab('masking')}><EyeOff size={16}/> 屏蔽规则</button></div>{tab !== 'data' && tab !== 'parser' && <div className="rules-toolbar-actions"><label><Search size={15}/><input value={query} onChange={(e) => { setQuery(e.target.value); setPage(1); }} placeholder="筛选规则…"/></label>{tab === 'semantic' && <>{sourceSeed?.ruleId && displayRules.some((rule) => rule.id === sourceSeed.ruleId) && <button className="button secondary compact" onClick={() => { const rule = displayRules.find((item) => item.id === sourceSeed.ruleId); if (rule) editSemantic(rule); }}><Pencil size={14}/> 编辑当前命中规则</button>}<button className="button primary compact" onClick={() => beginSemantic('keyword')}><Plus size={14}/> {sourceSeed ? '基于当前日志新增' : '新增语义规则'}</button></>}{tab === 'anomaly' && <div className="anomaly-create-controls"><input className="rule-inline-input" value={keywordDraft} onChange={(e) => setKeywordDraft(e.target.value)} placeholder="新增异常关键字"/><button className="button primary compact" onClick={addKeywords}><Plus size={14}/> 添加</button></div>}{tab === 'masking' && <button className="button primary compact" onClick={newMask}><Plus size={14}/> {sourceSeed ? '基于当前日志新增屏蔽规则' : '新增屏蔽规则'}</button>}{tab === 'folding' && <button className="button primary compact" onClick={newFoldRule}><Plus size={14}/> 新增折叠规则</button>}</div>}</header>
    {tab === 'data' ? <DataExtractionRulesPanel rules={dataExtractionRules} sourceSeed={sourceSeed} onChange={onDataExtractionRulesChange}/> : tab === 'parser' ? <LogFormatRulesPanel sampleRaw={sourceSeed?.sampleRaw}/> : <>
    {selected.size > 0 && <div className="rules-batch-bar"><strong>已选 {selected.size}</strong><button onClick={() => bulkEnabled(true)}>批量启用</button><button onClick={() => bulkEnabled(false)}>批量停用</button><button className="danger" onClick={bulkDelete}><Trash2 size={13}/> 批量删除</button><button onClick={() => setSelected(new Set())}>清空</button></div>}
    <div className="rules-table-wrap"><table className={`rules-table ${tab === 'semantic' ? 'semantic-rules-table' : ''}`}><thead><tr><th><input className="row-check" type="checkbox" checked={allPageSelected} onChange={toggleAll}/></th>{tab === 'semantic' && <><th>规则名称</th><th>类型</th><th>范围</th><th className="rule-match-column">匹配条件</th><th>展示方式</th><th>语义说明</th><th>自定义标签</th><th>补充说明</th><th>状态</th><th>实时监听</th><th>操作</th></>}{tab === 'anomaly' && <><th>异常关键字</th><th>状态</th><th>操作</th></>}{tab === 'masking' && <><th>规则名称</th><th>类型</th><th>匹配范围</th><th>匹配表达式</th><th>状态</th><th>操作</th></>}{tab === 'folding' && <><th>规则名称</th><th>类型</th><th>匹配逻辑</th><th>说明</th><th>状态</th><th>操作</th></>}</tr></thead><tbody>
      {tab === 'semantic' && pageRows.map((row) => { const rule = row.value as DisplayRule; return <tr key={rule.id}><td><input className="row-check" type="checkbox" checked={selected.has(rule.id)} onChange={() => toggle(rule.id)}/></td><td><strong>{rule.name}</strong></td><td>{semanticKind(rule)}</td><td>{scopeLabel(rule.scope)}</td><td className="rule-code-cell rule-match-column">{rule.kind === 'keyword' ? rule.keyword : (rule.sampleMessage || '样例参数模板')}</td><td>{displayModeLabel(rule.displayMode)}</td><td>{(rule.displayMode ?? 'semantic') === 'label' ? '—' : rule.displayTemplate}</td><td>{(rule.displayMode ?? 'semantic') === 'semantic' ? '—' : <span className="rule-custom-label-preview" style={{ '--rule-label-color': rule.customLabelColor || '#2563eb' } as React.CSSProperties}>{rule.customLabelTemplate || '—'}</span>}</td><td className="rule-supplement-cell" title={rule.supplementalDescription || ''}>{rule.supplementalDescription || '—'}</td><td><span className={`rule-status ${rule.enabled ? 'enabled' : ''}`}>{rule.enabled ? '启用' : '停用'}</span></td><td><label className={`data-live-capture-toggle ${rule.liveWatch ? 'on' : ''}`} title={rule.liveWatch ? '实时监听会把这条规则的命中推到时间线标签流' : '勾选后，实时监听才会监听这条规则'}><input type="checkbox" checked={rule.liveWatch === true} onChange={(event) => onDisplayRulesChange(displayRules.map((item) => item.id === rule.id ? { ...item, liveWatch: event.target.checked } : item))}/><span>{rule.liveWatch ? '监听中' : '不监听'}</span></label></td><td><div className="rules-row-actions"><button onClick={() => editSemantic(rule)}><Pencil size={13}/> 编辑</button><button onClick={() => onDisplayRulesChange(displayRules.map((item) => item.id === rule.id ? { ...item, enabled: !item.enabled } : item))}>{rule.enabled ? '停用' : '启用'}</button><button className="danger" onClick={() => onDisplayRulesChange(displayRules.filter((item) => item.id !== rule.id))}>删除</button></div></td></tr>; })}
      {tab === 'anomaly' && pageRows.map((row) => { const rule = row.value as ErrorMatchRule; return <tr key={rule.id}><td><input className="row-check" type="checkbox" checked={selected.has(rule.id)} onChange={() => toggle(rule.id)}/></td><td><strong>{rule.keyword}</strong></td><td><span className={`rule-status ${rule.enabled ? 'enabled' : ''}`}>{rule.enabled ? '启用' : '停用'}</span></td><td><div className="rules-row-actions"><button onClick={() => updateErrorRule(rule.id, { enabled: !rule.enabled })}>{rule.enabled ? '停用' : '启用'}</button><button className="danger" onClick={() => onErrorRulesChange(errorRules.filter((item) => item.id !== rule.id))}>删除</button></div></td></tr>; })}
      {tab === 'masking' && pageRows.map((row) => { const rule = row.value as MaskingRule; return <tr key={rule.id}><td><input className="row-check" type="checkbox" checked={selected.has(rule.id)} onChange={() => toggle(rule.id)}/></td><td><strong>{rule.name}</strong></td><td>{maskKindLabel(rule.kind)}</td><td>{rule.scope}</td><td className="rule-code-cell">{rule.pattern}</td><td><span className={`rule-status ${rule.enabled ? 'enabled' : ''}`}>{rule.enabled ? '启用' : '停用'}</span></td><td><div className="rules-row-actions"><button onClick={() => setMaskDraft({ ...rule })}><Pencil size={13}/> 编辑</button><button onClick={() => onMaskingRulesChange(maskingRules.map((item) => item.id === rule.id ? { ...item, enabled: !item.enabled } : item))}>{rule.enabled ? '停用' : '启用'}</button><button className="danger" onClick={() => onMaskingRulesChange(maskingRules.filter((item) => item.id !== rule.id))}>删除</button></div></td></tr>; })}
      {tab === 'folding' && pageRows.map((row) => { const rule = row.value as FoldingRule; return <tr key={rule.id}><td><input className="row-check" type="checkbox" checked={selected.has(rule.id)} onChange={() => toggle(rule.id)}/></td><td><strong>{rule.name}</strong></td><td>{rule.builtIn ? '内置规则' : '自定义边界'}</td><td className="rule-code-cell folding-rule-logic">{foldingRuleLogicLabel(rule)}</td><td className="rule-supplement-cell" title={rule.description}>{rule.description}</td><td><span className={`rule-status ${rule.enabled ? 'enabled' : ''}`}>{rule.enabled ? '启用' : '停用'}</span></td><td><div className="rules-row-actions">{!rule.builtIn && <button onClick={() => setFoldDraft({ ...rule })}><Pencil size={13}/> 编辑</button>}<button onClick={() => toggleFoldRule(rule.id)}>{rule.enabled ? '停用' : '启用'}</button>{!rule.builtIn && <button className="danger" onClick={() => onFoldingRulesChange(foldingRules.filter((item) => item.id !== rule.id))}>删除</button>}</div></td></tr>; })}
      {!pageRows.length && <tr><td colSpan={12}><div className="resource-empty inline-empty">没有匹配规则。</div></td></tr>}
    </tbody></table></div>
    <footer className="rules-pagination"><span>共 {rows.length} 条</span><label>每页 <select value={pageSize} onChange={(e) => { setPageSize(Number(e.target.value)); setPage(1); }}><option>20</option><option>50</option><option>100</option></select></label><button disabled={page <= 1} onClick={() => setPage((v) => v - 1)}>上一页</button><span>{page}/{totalPages}</span><button disabled={page >= totalPages} onClick={() => setPage((v) => v + 1)}>下一页</button></footer>
    </>}

    {semanticEditor && <div className="rule-drawer-backdrop semantic-rule-drawer-backdrop" onMouseDown={closeSemanticEditor}>
      <aside className="rule-drawer semantic-rule-drawer" onMouseDown={(e) => e.stopPropagation()}>
        <header><div><span className="eyebrow">SEMANTIC RULE</span><h2>{displayRules.some((r) => r.id === semanticEditor.rule.id) ? '编辑语义规则' : '新增语义规则'}</h2><p>完整保留智能模板的样例划选、参数捕获、展示模板和测试解析能力。</p></div><div className="rule-drawer-header-actions"><AiAutoconfigButton onRun={() => autoConfigureSemantic('all')} disabled={!semanticEditor.sampleRaw.trim() && !semanticEditor.sampleMessage.trim()} disabledHint="先在第 1 步粘贴样例日志" label="AI 一键识别并配置" title="读取样例日志，自动识别正文里会变化的部分，并填好语义说明、参数和自定义标签"/><button className="icon-button" onClick={closeSemanticEditor}><X size={18}/></button></div></header>
        <div className="rule-drawer-body semantic-rule-drawer-body">
          <div className="rule-editor-toolbar">
            <div className="rule-kind-switch">
              <button type="button" className={semanticEditor.rule.kind === 'keyword' ? 'active' : ''} onClick={() => changeRuleKind('keyword')}>关键字替换</button>
              <button type="button" className={semanticEditor.rule.kind === 'template' ? 'active' : ''} onClick={() => changeRuleKind('template')}>智能参数模板</button>
            </div>
            <label className="rule-enabled-control"><input type="checkbox" checked={semanticEditor.rule.enabled} onChange={(event) => updateRule({ enabled: event.target.checked })}/> 启用</label>
          </div>

          <div className={`rule-form-grid ${semanticEditor.rule.kind === 'keyword' ? 'three-columns' : 'two-columns'}`}>
            <label><span>规则名称</span><input value={semanticEditor.rule.name} onChange={(event) => updateRule({ name: event.target.value })} placeholder="例如：安全初始化依赖组件"/></label>
            {semanticEditor.rule.kind === 'keyword' && <label><span>函数名包含关键字</span><input value={semanticEditor.rule.keyword ?? ''} onChange={(event) => updateRule({ keyword: event.target.value })} placeholder="safe_initialize_default_dependencies()"/></label>}
            <label><span>应用位置</span><select value={semanticEditor.rule.scope} onChange={(event) => updateRule({ scope: event.target.value as DisplayRule['scope'] })}><option value="function">仅折叠函数标题</option><option value="log">仅日志正文</option><option value="both">函数标题 + 日志正文</option></select></label>
            <label><span>展示方式</span><select value={semanticEditor.rule.displayMode ?? 'semantic'} onChange={(event) => { const displayMode = event.target.value as DisplayRuleMode; updateRule({ displayMode, ...(displayMode !== 'semantic' && semanticEditor.rule.scope === 'function' ? { scope: displayMode === 'label' ? 'log' : 'both' } : {}) }); }}><option value="semantic">仅显示语义</option><option value="label">仅显示自定义标签</option><option value="both">语义 + 自定义标签</option></select></label>
          </div>

          {semanticEditor.rule.kind === 'template' && <>
            <section className="rule-editor-step">
              <div className="rule-step-heading rule-step-heading-with-action"><span>1</span><div><strong>粘贴样例日志</strong><small>可以粘贴完整原始日志，系统会自动提取第 8 个字段之后的正文。</small></div><AiAutoconfigButton onRun={() => autoConfigureSemantic('all')} disabled={!semanticEditor.sampleRaw.trim()} disabledHint="先粘贴样例日志" label="AI 识别正文与参数" compact title="从样例日志识别正文和会变化的部分，自动填好参数"/></div>
              <textarea className="rule-sample-input" value={semanticEditor.sampleRaw} onChange={(event) => updateSample(event.target.value)} placeholder="粘贴一条完整日志，例如：[2026-...] ... init_and_release() <DSPWSFT> Measurer DSPWSFT begin init."/>
            </section>
            <section className="rule-editor-step">
              <div className="rule-step-heading"><span>2</span><div><strong>划选变量</strong><small>在正文中拖选变量后点击“设为参数”；可选择同步标记同样文本。</small></div></div>
              <textarea ref={sampleTextareaRef} className="rule-selection-textarea" readOnly value={semanticEditor.sampleMessage} onSelect={captureSelection} onMouseUp={captureSelection} onKeyUp={captureSelection} placeholder="识别后的日志正文会显示在这里"/>
              <div className="rule-selection-actions">
                <span>当前选中：<strong>{selectedText || '请在上方划选文本'}</strong></span>
                <div className="rule-selection-control-group"><label><input type="checkbox" checked={markAllOccurrences} onChange={(event) => setMarkAllOccurrences(event.target.checked)}/> 同步标记相同文本{selectedOccurrenceCount > 1 ? `（${selectedOccurrenceCount} 处）` : ''}</label><button type="button" className="button primary compact-button" disabled={!selectedText} onClick={addSelectedParameter}><Braces size={14}/> 设为参数</button></div>
              </div>
              {semanticEditor.rule.parameters.length > 0 && <div className="rule-parameter-list">{semanticEditor.rule.parameters.map((parameter) => <div key={parameter.id} className="rule-parameter-item"><span className="parameter-sample-value" title={parameter.sampleValue}>{parameter.sampleValue}</span><span>→</span><input value={parameter.label} onChange={(event) => renameParameter(parameter.id, event.target.value)} aria-label="参数名称"/><code>{`{${parameter.label}}`}</code><button type="button" onClick={() => removeParameter(parameter.id)} aria-label={`删除${parameter.label}`}><X size={13}/></button></div>)}</div>}
              {semanticEditor.sampleMessage && <div className="rule-pattern-preview" aria-label="自动生成的匹配模板">{previewTokens.map((token) => token.kind === 'parameter' ? <mark key={token.key} title={token.value}>{`{${token.label}}`}</mark> : <span key={token.key}>{token.value}</span>)}</div>}
            </section>
          </>}

          {(semanticEditor.rule.displayMode ?? 'semantic') !== 'label' && <section className="rule-editor-step">
            <div className="rule-step-heading rule-step-heading-with-action"><span>{semanticEditor.rule.kind === 'template' ? '3' : '1'}</span><div><strong>语义说明</strong><small>显示在函数标题或日志正文左侧，用于解释这条日志代表什么。</small></div><div className="rule-step-heading-actions">{canAutoRecognizeSemantic && <button type="button" className="button secondary compact semantic-auto-button" onClick={() => void autoRecognizeSemantic()} disabled={semanticRecognition.loading} title="读取该函数所在的 Python 源码 docstring 作为语义说明">{semanticRecognition.loading ? <LoaderCircle className="spin" size={14}/> : <Sparkles size={14}/>} 自动识别语义（源码）</button>}<AiAutoconfigButton onRun={() => autoConfigureSemantic('semantic')} disabled={!semanticEditor.sampleRaw.trim() && !semanticEditor.sampleMessage.trim()} disabledHint="先粘贴样例日志" label="AI 重写语义" compact title="按样例日志重写语义说明（不依赖 Python 源码）"/></div></div>
            <textarea ref={templateTextareaRef} className="rule-display-template-input" value={semanticEditor.rule.displayTemplate} onChange={(event) => updateRule({ displayTemplate: event.target.value })} placeholder={semanticEditor.rule.kind === 'template' ? '例如：状态切换 {参数1}' : '例如：状态切换'}/>
            <label className="rule-supplemental-description">
              <span>补充说明 <small>可选 · 可填写异常含义、处置建议或排查策略</small></span>
              <textarea value={semanticEditor.rule.supplementalDescription ?? ''} onChange={(event) => updateRule({ supplementalDescription: event.target.value })} placeholder="可选补充说明"/>
            </label>
            {semanticRecognition.message && <div className={`semantic-auto-result ${semanticRecognition.success ? 'success' : 'error'}`}><Sparkles size={13}/><span>{semanticRecognition.message}</span></div>}
            {semanticEditor.rule.parameters.length > 0 && <div className="rule-placeholder-buttons"><span>插入参数：</span>{semanticEditor.rule.parameters.map((parameter) => <button type="button" key={parameter.id} onClick={() => insertPlaceholder(parameter)}>{`{${parameter.label}}`}</button>)}</div>}
          </section>}

          {(semanticEditor.rule.displayMode ?? 'semantic') !== 'semantic' && <section className="rule-editor-step custom-label-editor-step">
            <div className="rule-step-heading rule-step-heading-with-action"><span>{semanticEditor.rule.kind === 'template' ? ((semanticEditor.rule.displayMode ?? 'semantic') === 'both' ? '4' : '3') : ((semanticEditor.rule.displayMode ?? 'semantic') === 'both' ? '2' : '1')}</span><div><strong>自定义标签</strong><small>显示在日志行右侧；可使用固定文本、提取参数或两者组合。</small></div><AiAutoconfigButton onRun={() => autoConfigureSemantic('label')} disabled={!semanticEditor.sampleRaw.trim() && !semanticEditor.sampleMessage.trim()} disabledHint="先粘贴样例日志" label="AI 生成标签" compact title="按样例日志生成标签文本与颜色"/></div>
            <div className="custom-label-config-grid">
              <label className="custom-label-template-field"><span>标签文本</span><textarea ref={labelTemplateTextareaRef} className="rule-display-template-input" value={semanticEditor.rule.customLabelTemplate ?? ''} onChange={(event) => updateRule({ customLabelTemplate: event.target.value })} placeholder={semanticEditor.rule.kind === 'template' ? '例如：{参数1} 或 状态 {参数1}' : '例如：READY'}/></label>
              <label className="custom-label-color-field"><span>标签颜色</span><div className="custom-label-color-control"><input type="color" value={semanticEditor.rule.customLabelColor ?? '#2563eb'} onChange={(event) => updateRule({ customLabelColor: event.target.value })}/><code>{semanticEditor.rule.customLabelColor ?? '#2563eb'}</code><span className="rule-custom-label-preview" style={{ '--rule-label-color': semanticEditor.rule.customLabelColor || '#2563eb' } as React.CSSProperties}>{semanticEditor.rule.customLabelTemplate || '标签预览'}</span></div></label>
            </div>
            <label className="rule-switch custom-label-timeline-switch"><input type="checkbox" checked={semanticEditor.rule.showLabelOnTimeline !== false} onChange={(event) => updateRule({ showLabelOnTimeline: event.target.checked })}/> 在时间线上显示标签颜色</label><label className="rule-switch custom-label-timeline-switch"><input type="checkbox" checked={semanticEditor.rule.liveWatch === true} onChange={(event) => updateRule({ liveWatch: event.target.checked })}/> 实时监听这条规则（命中推送到时间线标签流）</label>
            {semanticEditor.rule.parameters.length > 0 && <div className="rule-placeholder-buttons"><span>插入标签参数：</span>{semanticEditor.rule.parameters.map((parameter) => <button type="button" key={parameter.id} onClick={() => insertLabelPlaceholder(parameter)}>{`{${parameter.label}}`}</button>)}</div>}
          </section>}

          <section className="rule-editor-step">
            <div className="rule-step-heading"><span>{semanticEditor.rule.kind === 'template' ? ((semanticEditor.rule.displayMode ?? 'semantic') === 'both' ? '5' : '4') : ((semanticEditor.rule.displayMode ?? 'semantic') === 'both' ? '3' : '2')}</span><div><strong>测试解析</strong><small>粘贴同类日志，确认参数提取和最终语义说明正确。</small></div></div>
            <textarea className="rule-test-input" value={semanticEditor.testInput} onChange={(event) => setSemanticEditor({ ...semanticEditor, testInput: event.target.value, testState: undefined })} placeholder="粘贴用于验证的日志；不填时使用样例日志"/>
            <div className="rule-test-row">{semanticEditor.testState ? <div className={`rule-test-result ${semanticEditor.testState.success ? 'success' : 'error'}`}>{semanticEditor.testState.success ? <Check size={14}/> : <X size={14}/>}<span>{semanticEditor.testState.message}</span></div> : <span className="rule-test-hint">填写测试日志后，在下方固定操作栏点击“测试解析”。</span>}</div>
          </section>
        </div>
        <footer className="semantic-rule-drawer-footer"><span className={semanticValidation ? 'validation-error' : 'validation-ready'}>{semanticValidation ?? '规则字段完整，可测试并保存。'}</span><div><button type="button" className="button secondary" onClick={testSemanticRule}><FlaskConical size={14}/> 测试解析</button><button type="button" className="button ghost" onClick={closeSemanticEditor}>取消</button><button type="button" className="button primary" onClick={saveSemantic}><Save size={14}/> 保存规则</button></div></footer>
      </aside>
    </div>}

    {foldDraft && <div className="rule-drawer-backdrop" onMouseDown={() => setFoldDraft(undefined)}><aside className="rule-drawer folding-rule-drawer" onMouseDown={(e) => e.stopPropagation()}><header><div><span className="eyebrow">FOLD RULE</span><h2>{foldingRules.some((rule) => rule.id === foldDraft.id) ? '编辑折叠规则' : '新增折叠规则'}</h2><p>同一函数名的入口与出口关键字配对后形成可折叠调用，并自动计算耗时。</p></div><button className="icon-button" onClick={() => setFoldDraft(undefined)}><X size={18}/></button></header><div className="rule-drawer-body"><label>规则名称<input value={foldDraft.name} onChange={(e) => setFoldDraft({ ...foldDraft, name: e.target.value })} placeholder="例如：START/END 函数折叠"/></label><label>入口关键字<input value={foldDraft.startKeyword ?? ''} onChange={(e) => setFoldDraft({ ...foldDraft, startKeyword: e.target.value })} placeholder="[START]"/></label><label>出口关键字<input value={foldDraft.endKeyword ?? ''} onChange={(e) => setFoldDraft({ ...foldDraft, endKeyword: e.target.value })} placeholder="[END]"/></label><div className="rule-template-note">只有函数名一致时才会配对。例如 <code>Func() ... [START]</code> 与 <code>Func() ... [END]</code> 会折叠为同一个 Func() 调用；原有 <code>&gt; () / &lt; ()</code> 规则继续独立生效。</div><label className="rule-switch"><input type="checkbox" checked={Boolean(foldDraft.caseSensitive)} onChange={(e) => setFoldDraft({ ...foldDraft, caseSensitive: e.target.checked })}/> 关键字区分大小写</label><label className="rule-switch"><input type="checkbox" checked={foldDraft.enabled} onChange={(e) => setFoldDraft({ ...foldDraft, enabled: e.target.checked })}/> 启用规则</label></div><footer><button className="button ghost" onClick={() => setFoldDraft(undefined)}>取消</button><button className="button primary" onClick={saveFoldRule}>保存规则</button></footer></aside></div>}

    {maskDraft && <div className="rule-drawer-backdrop" onMouseDown={() => setMaskDraft(undefined)}><aside className="rule-drawer" onMouseDown={(e) => e.stopPropagation()}><header><div><span className="eyebrow">MASK RULE</span><h2>{maskingRules.some((r) => r.id === maskDraft.id) ? '编辑屏蔽规则' : '新增屏蔽规则'}</h2></div><button className="icon-button" onClick={() => setMaskDraft(undefined)}><X size={18}/></button></header><div className="rule-drawer-body"><label>规则名称<input value={maskDraft.name} onChange={(e) => setMaskDraft({ ...maskDraft, name: e.target.value })}/></label><label>规则类型<select value={maskDraft.kind} onChange={(e) => setMaskDraft({ ...maskDraft, kind: e.target.value as MaskingRuleKind })}><option value="keyword">关键字</option><option value="function">函数名</option><option value="regex">正则表达式</option><option value="template">智能模板</option></select></label><label>匹配范围<select value={maskDraft.scope} onChange={(e) => setMaskDraft({ ...maskDraft, scope: e.target.value as MaskingRuleScope })}><option value="any">正文 + 函数 + 原文</option><option value="message">日志正文</option><option value="function">函数名</option><option value="raw">完整原文</option></select></label><label>匹配表达式<textarea rows={5} value={maskDraft.pattern} onChange={(e) => setMaskDraft({ ...maskDraft, pattern: e.target.value })} placeholder={maskDraft.kind === 'template' ? '例：Heartbeat {*} finished；{*} 表示可变语段' : maskDraft.kind === 'regex' ? '输入正则表达式' : '输入关键字或函数名'}/></label>{maskDraft.kind === 'template' && <div className="rule-template-note">智能模板使用固定语段 + <code>{'{*}'}</code> 通配可变部分，适合屏蔽重复心跳、轮询等日志。</div>}<label className="rule-switch"><input type="checkbox" checked={maskDraft.caseSensitive} onChange={(e) => setMaskDraft({ ...maskDraft, caseSensitive: e.target.checked })}/> 大小写敏感</label><label className="rule-switch"><input type="checkbox" checked={maskDraft.enabled} onChange={(e) => setMaskDraft({ ...maskDraft, enabled: e.target.checked })}/> 启用规则</label></div><footer><button className="button ghost" onClick={() => setMaskDraft(undefined)}>取消</button><button className="button primary" onClick={saveMask}>保存规则</button></footer></aside></div>}
  </section>;
}
