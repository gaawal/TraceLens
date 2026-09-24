/// <reference lib="webworker" />
import ExcelJS from 'exceljs';
import type { CellValue, Worksheet, Workbook } from 'exceljs';
import {
  ENABLED_CN,
  FILTERED_LOG_EXPORT_LIMIT,
  GENERATE_RULE_CN,
  RULE_SAMPLE_EXPORT_LIMIT,
  RULE_KIND_CN,
  RULE_SCOPE_CN,
  displayPatternToReadableTemplate,
  importedRowToCandidate,
  logEntryToChineseRecord,
  ruleKindToChinese,
  ruleScopeToChinese,
  uniqueLogSamples,
} from '../rendering/ruleExchange';
import { matchDisplayRuleToMessage, matchDisplayRulesToEntry } from '../rendering/displayRules';
import type { DisplayRule } from '../rendering/displayRules';
import type { LogEntry } from '../types';
import type { RuleWorkbookWorkerRequest, RuleWorkbookWorkerResponse } from './ruleWorkbookProtocol';

const context = self as unknown as DedicatedWorkerGlobalScope;

interface WorkbookExportState {
  requestId: string;
  mode: import('../rendering/ruleExchange').RuleWorkbookExportMode;
  taskName: string;
  rules: DisplayRule[];
  totalEntries: number;
  receivedEntries: number;
  entries: LogEntry[];
  sampleKeys: Set<string>;
}

const exportStates = new Map<string, WorkbookExportState>();

function post(message: RuleWorkbookWorkerResponse, transfer?: Transferable[]): void {
  context.postMessage(message, transfer ?? []);
}

function textValue(value: CellValue | undefined): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (value instanceof Date) return value.toISOString();
  if ('text' in value && typeof value.text === 'string') return value.text;
  if ('result' in value) return textValue(value.result as CellValue);
  if ('richText' in value && Array.isArray(value.richText)) return value.richText.map((item) => item.text).join('');
  return String(value);
}

function safeSheetName(value: string): string {
  return value.replace(/[\\/*?:[\]]/g, '_').slice(0, 31) || 'TraceLens';
}

function styleSheet(sheet: Worksheet, decorateRows = true): void {
  sheet.views = [{ state: 'frozen', ySplit: 1 }];
  sheet.autoFilter = { from: { row: 1, column: 1 }, to: { row: 1, column: sheet.columnCount } };
  const header = sheet.getRow(1);
  header.height = 24;
  header.font = { bold: true, color: { argb: 'FFFFFFFF' } };
  header.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FF334155' } };
  header.alignment = { vertical: 'middle', horizontal: 'center' };
  if (!decorateRows) return;
  sheet.eachRow((row: { alignment: unknown; fill: unknown }, index: number) => {
    if (index === 1) return;
    row.alignment = { vertical: 'top', wrapText: false };
    if (index % 2 === 0) row.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FFF8FAFC' } };
  });
}

function setColumns(sheet: Worksheet, headers: Array<[string, number]>): void {
  sheet.columns = headers.map(([header, width]) => ({ header, key: header, width }));
}


function excelSafeValue(value: string | number): string | number {
  return typeof value === 'string' && value.length > 32760 ? `${value.slice(0, 32740)}…[已截断]` : value;
}

function excelSafeRecord(record: Record<string, string | number>): Record<string, string | number> {
  return Object.fromEntries(Object.entries(record).map(([key, value]) => [key, excelSafeValue(value)]));
}

function addValidation(sheet: Worksheet, columnName: string, formula: string, rowCount: number): void {
  const column = sheet.getColumn(columnName);
  const endRow = Math.max(2, rowCount + 1);
  (sheet as Worksheet & { dataValidations: { add: (range: string, rule: unknown) => void } }).dataValidations.add(`${column.letter}2:${column.letter}${endRow}`, {
    type: 'list',
    allowBlank: true,
    formulae: [formula],
    showErrorMessage: true,
    errorTitle: '无效选项',
    error: '请从下拉列表选择中文枚举值。',
  });
}

function addEnumSheet(workbook: Workbook): void {
  const sheet = workbook.addWorksheet('枚举选项');
  setColumns(sheet, [['启用状态', 18], ['规则类型', 22], ['应用范围', 28], ['是否生成规则', 18]]);
  const max = Math.max(ENABLED_CN.length, RULE_KIND_CN.length, RULE_SCOPE_CN.length, GENERATE_RULE_CN.length);
  for (let index = 0; index < max; index += 1) {
    sheet.addRow({
      启用状态: ENABLED_CN[index] ?? '',
      规则类型: RULE_KIND_CN[index] ?? '',
      应用范围: RULE_SCOPE_CN[index] ?? '',
      是否生成规则: GENERATE_RULE_CN[index] ?? '',
    });
  }
  styleSheet(sheet);
  sheet.state = 'veryHidden';
}

function addRulesSheet(workbook: Workbook, rules: readonly DisplayRule[]): void {
  const sheet = workbook.addWorksheet('规则配置');
  setColumns(sheet, [
    ['规则ID', 38], ['启用状态', 14], ['规则名称', 28], ['规则类型', 18], ['应用范围', 22],
    ['优先级', 10], ['匹配关键字', 34], ['匹配模板', 72], ['展示模板', 46], ['补充说明', 56], ['样例日志正文', 90],
  ]);
  rules.forEach((rule, index) => {
    sheet.addRow({
      规则ID: rule.id,
      启用状态: rule.enabled ? '启用' : '停用',
      规则名称: rule.name,
      规则类型: ruleKindToChinese(rule.kind),
      应用范围: ruleScopeToChinese(rule.scope),
      优先级: index + 1,
      匹配关键字: rule.keyword ?? '',
      匹配模板: displayPatternToReadableTemplate(rule),
      展示模板: rule.displayTemplate,
      补充说明: rule.supplementalDescription ?? '',
      样例日志正文: rule.sampleMessage ?? '',
    });
  });
  if (rules.length === 0) sheet.addRow({ 启用状态: '启用', 规则类型: '关键字替换', 应用范围: '仅函数标题', 优先级: 1 });
  addValidation(sheet, '启用状态', "'枚举选项'!$A$2:$A$3", Math.max(200, rules.length + 20));
  addValidation(sheet, '规则类型', "'枚举选项'!$B$2:$B$3", Math.max(200, rules.length + 20));
  addValidation(sheet, '应用范围', "'枚举选项'!$C$2:$C$4", Math.max(200, rules.length + 20));
  styleSheet(sheet);
}

function entrySemanticForExport(entry: LogEntry, rules: readonly DisplayRule[]): { text: string; supplementalText: string; ruleId: string } {
  const logMatch = matchDisplayRulesToEntry(rules, entry);
  if (logMatch) return { text: logMatch.text, supplementalText: logMatch.supplementalText ?? '', ruleId: logMatch.ruleId };
  for (const rule of rules) {
    if (!rule.enabled || rule.scope === 'log') continue;
    if (rule.kind === 'keyword') {
      const keyword = rule.keyword?.trim();
      if (keyword && entry.functionName?.includes(keyword)) return { text: rule.displayTemplate, supplementalText: rule.supplementalDescription?.trim() ?? '', ruleId: rule.id };
    } else {
      const match = matchDisplayRuleToMessage(rule, entry.message);
      if (match) return { text: match.text, supplementalText: match.supplementalText ?? '', ruleId: rule.id };
    }
  }
  return { text: '', supplementalText: '', ruleId: '' };
}

const SAMPLE_HEADERS: Array<[string, number]> = [
  ['样本ID', 22], ['时间', 28], ['级别', 12], ['组件', 18], ['进程ID', 14], ['线程ID', 14], ['源文件', 28], ['源文件行号', 14],
  ['TraceID', 26], ['SpanID', 20], ['父SpanID', 20], ['上下文函数', 36], ['子调用函数', 36], ['日志正文', 90], ['原始日志', 110],
  ['当前语义说明', 40], ['命中规则ID', 38], ['是否生成规则', 16], ['规则名称', 28], ['规则类型', 18], ['应用范围', 22],
  ['匹配关键字', 34], ['匹配模板', 72], ['展示模板', 46], ['补充说明', 56], ['启用状态', 14], ['优先级', 10],
];

function addSamplesSheet(workbook: Workbook, entries: readonly LogEntry[], rules: readonly DisplayRule[]): void {
  const sheet = workbook.addWorksheet('日志样本');
  setColumns(sheet, SAMPLE_HEADERS);
  uniqueLogSamples(entries, RULE_SAMPLE_EXPORT_LIMIT).forEach((entry, index) => {
    const semanticMatch = entrySemanticForExport(entry, rules);
    sheet.addRow({
      样本ID: `样本-${index + 1}`,
      ...excelSafeRecord(logEntryToChineseRecord(entry)),
      当前语义说明: semanticMatch.text,
      命中规则ID: semanticMatch.ruleId,
      是否生成规则: '否',
      规则名称: entry.functionName ? `${entry.functionName} 语义说明` : '',
      规则类型: '关键字替换',
      应用范围: entry.functionName ? '仅函数标题' : '仅日志正文',
      匹配关键字: entry.functionName ?? '',
      匹配模板: '',
      展示模板: '',
      补充说明: semanticMatch.supplementalText,
      启用状态: '启用',
      优先级: '',
    });
  });
  addValidation(sheet, '是否生成规则', "'枚举选项'!$D$2:$D$3", Math.max(120, sheet.rowCount + 20));
  addValidation(sheet, '启用状态', "'枚举选项'!$A$2:$A$3", Math.max(120, sheet.rowCount + 20));
  addValidation(sheet, '规则类型', "'枚举选项'!$B$2:$B$3", Math.max(120, sheet.rowCount + 20));
  addValidation(sheet, '应用范围', "'枚举选项'!$C$2:$C$4", Math.max(120, sheet.rowCount + 20));
  styleSheet(sheet);
}

const DETAIL_HEADERS: Array<[string, number]> = [
  ['序号', 12], ['时间', 28], ['级别', 12], ['组件', 18], ['进程ID', 14], ['线程ID', 14], ['源文件', 28], ['源文件行号', 14],
  ['TraceID', 26], ['SpanID', 20], ['父SpanID', 20], ['上下文函数', 36], ['子调用函数', 36], ['日志正文', 90], ['原始日志', 110],
  ['当前语义说明', 40], ['命中规则ID', 38], ['是否生成规则', 16], ['规则名称', 28], ['规则类型', 18], ['应用范围', 22],
  ['匹配关键字', 34], ['匹配模板', 72], ['展示模板', 46], ['补充说明', 56], ['启用状态', 14], ['优先级', 10],
];

function addDetailSheet(workbook: Workbook, entries: readonly LogEntry[], rules: readonly DisplayRule[], requestId: string): void {
  const limitedEntries = entries.slice(0, FILTERED_LOG_EXPORT_LIMIT);
  const sheet = workbook.addWorksheet('日志明细');
  setColumns(sheet, DETAIL_HEADERS);
  limitedEntries.forEach((entry, index) => {
    const semanticMatch = entrySemanticForExport(entry, rules);
    sheet.addRow({
      序号: index + 1,
      ...excelSafeRecord(logEntryToChineseRecord(entry)),
      当前语义说明: semanticMatch.text,
      命中规则ID: semanticMatch.ruleId,
      是否生成规则: '否',
      规则名称: entry.functionName ? `${entry.functionName} 语义说明` : '',
      规则类型: '关键字替换',
      应用范围: entry.functionName ? '仅函数标题' : '仅日志正文',
      匹配关键字: entry.functionName ?? '',
      匹配模板: '',
      展示模板: '',
      补充说明: semanticMatch.supplementalText,
      启用状态: '启用',
      优先级: '',
    });
  });
  post({
    type: 'WORKBOOK_PROGRESS',
    requestId,
    percent: 88,
    message: `已写入 ${limitedEntries.length} 条筛选日志`,
  });
  addValidation(sheet, '是否生成规则', "'枚举选项'!$D$2:$D$3", Math.max(520, sheet.rowCount + 20));
  addValidation(sheet, '启用状态', "'枚举选项'!$A$2:$A$3", Math.max(520, sheet.rowCount + 20));
  addValidation(sheet, '规则类型', "'枚举选项'!$B$2:$B$3", Math.max(520, sheet.rowCount + 20));
  addValidation(sheet, '应用范围', "'枚举选项'!$C$2:$C$4", Math.max(520, sheet.rowCount + 20));
  styleSheet(sheet, false);
}

async function exportWorkbook(state: WorkbookExportState): Promise<void> {
  post({ type: 'WORKBOOK_PROGRESS', requestId: state.requestId, percent: 78, message: '正在创建中文配置工作簿' });
  const workbook = new ExcelJS.Workbook();
  workbook.creator = 'TraceLens';
  workbook.created = new Date();
  workbook.modified = new Date();
  addRulesSheet(workbook, state.rules);
  addSamplesSheet(workbook, state.entries, state.rules);
  if (state.mode === 'filtered') addDetailSheet(workbook, state.entries, state.rules, state.requestId);
  addEnumSheet(workbook);
  post({ type: 'WORKBOOK_PROGRESS', requestId: state.requestId, percent: 90, message: '正在生成 XLSX 文件' });
  const result = await workbook.xlsx.writeBuffer() as ArrayBuffer | Uint8Array;
  const buffer = result instanceof ArrayBuffer
    ? result
    : result.buffer.slice(result.byteOffset, result.byteOffset + result.byteLength) as ArrayBuffer;
  const suffix = state.mode === 'rules' ? '语义规则模板' : '当前筛选日志';
  const fileName = `${safeSheetName(state.taskName)}-${suffix}.xlsx`;
  post({ type: 'WORKBOOK_EXPORTED', requestId: state.requestId, fileName, buffer }, [buffer]);
}

function appendExportChunk(state: WorkbookExportState, entries: readonly LogEntry[]): void {
  state.receivedEntries += entries.length;
  if (state.mode === 'filtered') {
    const remaining = Math.max(0, FILTERED_LOG_EXPORT_LIMIT - state.entries.length);
    if (remaining > 0) state.entries.push(...entries.slice(0, remaining));
    return;
  }
  for (const entry of entries) {
    if (state.entries.length >= RULE_SAMPLE_EXPORT_LIMIT) break;
    const key = [entry.component, entry.functionName ?? '', entry.boundaryFunctionName ?? '', entry.message].join('\u0001');
    if (state.sampleKeys.has(key)) continue;
    state.sampleKeys.add(key);
    state.entries.push(entry);
  }
}

function headerMap(sheet: Worksheet): Map<string, number> {
  const map = new Map<string, number>();
  sheet.getRow(1).eachCell((cell, column) => map.set(textValue(cell.value).trim(), column));
  return map;
}

function cellByHeader(sheet: Worksheet, rowNumber: number, headers: Map<string, number>, name: string): string {
  const column = headers.get(name);
  return column ? textValue(sheet.getRow(rowNumber).getCell(column).value).trim() : '';
}

function parseRuleSheet(sheet: Worksheet): ReturnType<typeof importedRowToCandidate>[] {
  const headers = headerMap(sheet);
  const result: ReturnType<typeof importedRowToCandidate>[] = [];
  for (let row = 2; row <= sheet.rowCount; row += 1) {
    const name = cellByHeader(sheet, row, headers, '规则名称');
    const template = cellByHeader(sheet, row, headers, '展示模板');
    const keyword = cellByHeader(sheet, row, headers, '匹配关键字');
    const matchTemplate = cellByHeader(sheet, row, headers, '匹配模板');
    if (![name, template, keyword, matchTemplate].some(Boolean)) continue;
    result.push(importedRowToCandidate({
      sourceSheet: sheet.name,
      rowNumber: row,
      priority: cellByHeader(sheet, row, headers, '优先级'),
      ruleId: cellByHeader(sheet, row, headers, '规则ID'),
      enabled: cellByHeader(sheet, row, headers, '启用状态'),
      name,
      kind: cellByHeader(sheet, row, headers, '规则类型'),
      scope: cellByHeader(sheet, row, headers, '应用范围'),
      keyword,
      matchTemplate,
      displayTemplate: template,
      supplementalDescription: cellByHeader(sheet, row, headers, '补充说明') || cellByHeader(sheet, row, headers, '备注'),
      sampleMessage: cellByHeader(sheet, row, headers, '样例日志正文'),
    }));
  }
  return result;
}

function parseSampleSheet(sheet: Worksheet): ReturnType<typeof importedRowToCandidate>[] {
  const headers = headerMap(sheet);
  const result: ReturnType<typeof importedRowToCandidate>[] = [];
  for (let row = 2; row <= sheet.rowCount; row += 1) {
    if (cellByHeader(sheet, row, headers, '是否生成规则') !== '是') continue;
    result.push(importedRowToCandidate({
      sourceSheet: sheet.name,
      rowNumber: row,
      priority: cellByHeader(sheet, row, headers, '优先级'),
      ruleId: cellByHeader(sheet, row, headers, '命中规则ID'),
      enabled: cellByHeader(sheet, row, headers, '启用状态'),
      name: cellByHeader(sheet, row, headers, '规则名称'),
      kind: cellByHeader(sheet, row, headers, '规则类型'),
      scope: cellByHeader(sheet, row, headers, '应用范围'),
      keyword: cellByHeader(sheet, row, headers, '匹配关键字'),
      matchTemplate: cellByHeader(sheet, row, headers, '匹配模板'),
      displayTemplate: cellByHeader(sheet, row, headers, '展示模板'),
      supplementalDescription: cellByHeader(sheet, row, headers, '补充说明') || cellByHeader(sheet, row, headers, '备注'),
      sampleMessage: cellByHeader(sheet, row, headers, '日志正文'),
    }));
  }
  return result;
}

async function importWorkbook(message: Extract<RuleWorkbookWorkerRequest, { type: 'IMPORT_WORKBOOK' }>): Promise<void> {
  post({ type: 'WORKBOOK_PROGRESS', requestId: message.requestId, percent: 10, message: '正在读取 Excel 工作簿' });
  const workbook = new ExcelJS.Workbook();
  await workbook.xlsx.load(message.buffer as never);
  const detailCandidates = workbook.worksheets
    .filter((sheet) => sheet.name === '日志明细' || sheet.name.startsWith('日志明细_'))
    .flatMap((sheet) => parseSampleSheet(sheet));
  const candidates = [
    ...(workbook.getWorksheet('规则配置') ? parseRuleSheet(workbook.getWorksheet('规则配置')!) : []),
    ...(workbook.getWorksheet('日志样本') ? parseSampleSheet(workbook.getWorksheet('日志样本')!) : []),
    ...detailCandidates,
  ];
  if (candidates.length === 0) {
    throw new Error('没有找到可导入规则。请检查“规则配置”工作表，或将“日志样本”中的“是否生成规则”设置为“是”。');
  }
  post({
    type: 'WORKBOOK_IMPORTED',
    requestId: message.requestId,
    preview: { candidates, sourceName: message.sourceName, generatedAt: Date.now() },
  });
}

context.onmessage = (event: MessageEvent<RuleWorkbookWorkerRequest>) => {
  const message = event.data;
  if (message.type === 'EXPORT_WORKBOOK_START') {
    exportStates.set(message.requestId, {
      requestId: message.requestId,
      mode: message.mode,
      taskName: message.taskName,
      rules: message.rules,
      totalEntries: message.totalEntries,
      receivedEntries: 0,
      entries: [],
      sampleKeys: new Set<string>(),
    });
    post({ type: 'WORKBOOK_PROGRESS', requestId: message.requestId, percent: 2, message: '正在分批接收日志数据' });
    return;
  }

  if (message.type === 'EXPORT_WORKBOOK_CHUNK') {
    const state = exportStates.get(message.requestId);
    if (!state) return;
    appendExportChunk(state, message.entries);
    post({
      type: 'WORKBOOK_PROGRESS',
      requestId: message.requestId,
      percent: Math.min(75, Math.round((state.receivedEntries / Math.max(1, state.totalEntries)) * 75)),
      message: `正在接收日志 ${state.receivedEntries}/${state.totalEntries}`,
    });
    if (message.final) {
      exportStates.delete(message.requestId);
      exportWorkbook(state).catch((error: unknown) => {
        post({ type: 'WORKBOOK_ERROR', requestId: message.requestId, message: error instanceof Error ? error.message : String(error) });
      });
    }
    return;
  }

  importWorkbook(message).catch((error: unknown) => {
    post({
      type: 'WORKBOOK_ERROR',
      requestId: message.requestId,
      message: error instanceof Error ? error.message : String(error),
    });
  });
};
