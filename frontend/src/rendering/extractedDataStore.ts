import type { DataExtractionRule, ExtractedDataRow } from './dataExtractionRules';

export interface TemporaryRuleData {
  rule: DataExtractionRule;
  rows: ExtractedDataRow[];
}

export interface MergedTemporaryDataRow extends ExtractedDataRow {
  ruleId: string;
  ruleName: string;
}

export interface MergedTemporaryData {
  fields: string[];
  rows: MergedTemporaryDataRow[];
}

export interface TemporaryExtractionSession {
  key: string;
  recordId?: number;
  taskName: string;
  environmentName?: string;
  startTime?: string;
  endTime?: string;
  createdAt: number;
  results: TemporaryRuleData[];
}

const sessions = new Map<string, TemporaryExtractionSession>();

export function temporarySessionKey(recordId: number | string): string {
  return `data-extraction-${String(recordId)}`;
}

export function saveTemporaryExtractionSession(session: TemporaryExtractionSession): void {
  sessions.set(session.key, session);
}

export function getTemporaryExtractionSession(key: string): TemporaryExtractionSession | undefined {
  return sessions.get(key);
}

export function deleteTemporaryExtractionSession(key: string): void {
  sessions.delete(key);
}

export function listTemporaryExtractionSessions(): TemporaryExtractionSession[] {
  return [...sessions.values()].sort((a, b) => b.createdAt - a.createdAt);
}

function csvEscape(value: unknown): string {
  const text = value === undefined || value === null ? '' : String(value);
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

export function downloadTemporaryRuleData(input: {
  rule: DataExtractionRule;
  rows: ExtractedDataRow[];
  namePrefix?: string;
}): void {
  const { rule, rows } = input;
  const fields = rule.fields.map((field) => field.name || field.key);
  let text = '';
  let extension = 'csv';
  let mime = 'text/csv;charset=utf-8';
  if (rule.outputFormat === 'text') {
    extension = 'txt';
    mime = 'text/plain;charset=utf-8';
    text = rows.map((row) => {
      const values = fields.map((field) => `${field}=${String(row.values[field] ?? '')}`).join(' ');
      return `${row.timestamp}\t${values}`;
    }).join('\n');
  } else {
    const headers = ['timestamp', 'subsystem', 'module', 'sourceFile', 'lineNumber', ...fields];
    text = [
      headers.join(','),
      ...rows.map((row) => [
        row.timestamp,
        row.subsystem || '',
        row.module || '',
        row.sourceFile,
        row.lineNumber,
        ...fields.map((field) => row.values[field] ?? ''),
      ].map(csvEscape).join(',')),
    ].join('\r\n');
    text = `\uFEFF${text}`;
  }
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  const safe = `${input.namePrefix ? `${input.namePrefix}-` : ''}${rule.name || '提取数据'}`.replace(/[\\/:*?"<>|]+/g, '-');
  anchor.download = `${safe}-${new Date().toISOString().replace(/[:.]/g, '-')}.${extension}`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}


function timestampSortValue(value: string): number {
  const parsed = new Date(String(value || '').trim().replace(' ', 'T')).getTime();
  return Number.isFinite(parsed) ? parsed : Number.POSITIVE_INFINITY;
}

/**
 * 合并多个采集规则的临时结果。页面预览与 CSV 下载必须共用这里，
 * 保证字段并集、时间排序和原日志顺序完全一致。
 */
export function mergeTemporaryRuleData(results: TemporaryRuleData[]): MergedTemporaryData {
  const selected = results.filter((item) => item.rows.length > 0);
  const fields: string[] = [];
  const fieldSet = new Set<string>();
  selected.forEach(({ rule }) => {
    rule.fields.forEach((field) => {
      const name = field.name || field.key;
      if (!fieldSet.has(name)) {
        fieldSet.add(name);
        fields.push(name);
      }
    });
  });

  const rows = selected.flatMap((item, ruleOrder) => item.rows.map((row, rowOrder) => ({
    ...row,
    ruleId: item.rule.id,
    ruleName: item.rule.name,
    __ruleOrder: ruleOrder,
    __rowOrder: rowOrder,
  })));
  rows.sort((a, b) => {
    const leftTime = timestampSortValue(a.timestamp);
    const rightTime = timestampSortValue(b.timestamp);
    if (leftTime !== rightTime && Number.isFinite(leftTime) && Number.isFinite(rightTime)) return leftTime - rightTime;
    if (Number.isFinite(leftTime) !== Number.isFinite(rightTime)) return Number.isFinite(leftTime) ? -1 : 1;
    const textTimeDiff = String(a.timestamp).localeCompare(String(b.timestamp));
    if (textTimeDiff) return textTimeDiff;
    const sourceDiff = String(a.sourceFile || '').localeCompare(String(b.sourceFile || ''));
    if (sourceDiff) return sourceDiff;
    const lineDiff = Number(a.lineNumber || 0) - Number(b.lineNumber || 0);
    if (lineDiff) return lineDiff;
    if (a.__ruleOrder !== b.__ruleOrder) return a.__ruleOrder - b.__ruleOrder;
    return a.__rowOrder - b.__rowOrder;
  });

  return {
    fields,
    rows: rows.map(({ __ruleOrder: _ruleOrder, __rowOrder: _rowOrder, ...row }) => row),
  };
}

/**
 * Excel/表格软件会主动把 2026-08-14 15:43:12.123 一类字符串转成日期，
 * 从而丢失原日志格式甚至毫秒。标准日志时间在 CSV 中按文本公式写入，
 * 打开后显示仍是原始时间字符串；非标准文本则保持原样。
 */
function csvTimestamp(value: string): string {
  const text = String(value ?? '');
  if (/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(text)) {
    return `="${text.replace(/"/g, '""')}"`;
  }
  return text;
}

export function downloadMergedTemporaryRuleData(input: {
  results: TemporaryRuleData[];
  namePrefix?: string;
}): void {
  const merged = mergeTemporaryRuleData(input.results);
  if (!merged.rows.length) return;

  const headers = ['timestamp', '采集规则', 'subsystem', 'module', 'PID', 'TID', 'TraceID', 'sourceFile', 'lineNumber', ...merged.fields];
  const text = `\uFEFF${[
    headers.map(csvEscape).join(','),
    ...merged.rows.map((row) => [
      csvTimestamp(row.timestamp),
      row.ruleName,
      row.subsystem || '',
      row.module || '',
      row.processId || '',
      row.threadId || '',
      row.traceId || '',
      row.sourceFile,
      row.lineNumber,
      ...merged.fields.map((field) => row.values[field] ?? ''),
    ].map(csvEscape).join(',')),
  ].join('\r\n')}`;

  const blob = new Blob([text], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  const safe = `${input.namePrefix ? `${input.namePrefix}-` : ''}合并数据`.replace(/[\\/:*?"<>|]+/g, '-');
  anchor.download = `${safe}-${new Date().toISOString().replace(/[:.]/g, '-')}.csv`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
