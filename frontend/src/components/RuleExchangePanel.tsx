import { useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  Check,
  Download,
  FileJson,
  FileSpreadsheet,
  History,
  LoaderCircle,
  RotateCcw,
  Upload,
  X,
} from 'lucide-react';
import type { DisplayRule } from '../rendering/displayRules';
import { parseDisplayRulesJson, validateDisplayRule } from '../rendering/displayRules';
import {
  RULE_IMPORT_SNAPSHOT_KEY,
  analyzeRuleImport,
  selectWorkbookExportEntries,
} from '../rendering/ruleExchange';
import type {
  RuleImportCandidate,
  RuleImportPreview,
  RuleWorkbookExportMode,
} from '../rendering/ruleExchange';
import type { LogEntry } from '../types';
import type { RuleWorkbookWorkerResponse } from '../workers/ruleWorkbookProtocol';

interface RuleExchangePanelProps {
  displayRules: readonly DisplayRule[];
  currentEntries: readonly LogEntry[];
  taskName: string;
  onDisplayRulesChange: (rules: DisplayRule[]) => void;
}

interface TransferState {
  percent: number;
  message: string;
}

function createRequestId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID();
  return `rule-transfer-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function safeFileName(value: string): string {
  return value.replace(/[\\/:*?"<>|]/g, '_').trim() || 'TraceLens';
}

function downloadBlob(blob: Blob, fileName: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = fileName;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function jsonPreview(fileName: string, rules: unknown[]): RuleImportPreview {
  const candidates: RuleImportCandidate[] = rules.map((candidate, index) => {
    const errors: string[] = [];
    if (!candidate || typeof candidate !== 'object') {
      return { sourceSheet: 'JSON', rowNumber: index + 1, priority: index + 1, errors: ['不是有效的规则对象'] };
    }
    const normalized = parseDisplayRulesJson(JSON.stringify([candidate]));
    const rule = normalized[0];
    if (!rule) errors.push('规则字段结构无效');
    const validation = rule ? validateDisplayRule(rule) : undefined;
    if (validation) errors.push(validation);
    return {
      sourceSheet: 'JSON',
      rowNumber: index + 1,
      priority: index + 1,
      rule,
      errors,
    };
  });
  return { candidates, sourceName: fileName, generatedAt: Date.now() };
}

export function RuleExchangePanel({
  displayRules,
  currentEntries,
  taskName,
  onDisplayRulesChange,
}: RuleExchangePanelProps) {
  const excelInputRef = useRef<HTMLInputElement>(null);
  const jsonInputRef = useRef<HTMLInputElement>(null);
  const [transfer, setTransfer] = useState<TransferState>();
  const [error, setError] = useState('');
  const [preview, setPreview] = useState<RuleImportPreview>();
  const [lastAppliedAt, setLastAppliedAt] = useState<number>();
  const analysis = useMemo(() => preview ? analyzeRuleImport(displayRules, preview) : undefined, [displayRules, preview]);
  const snapshotAvailable = typeof window !== 'undefined' && Boolean(window.localStorage.getItem(RULE_IMPORT_SNAPSHOT_KEY));

  function runWorkbookWorker(
    setup: (worker: Worker, requestId: string) => void,
  ): void {
    setError('');
    const requestId = createRequestId();
    const worker = new Worker(new URL('../workers/ruleWorkbook.worker.ts', import.meta.url), { type: 'module' });
    const cleanup = () => worker.terminate();
    worker.onerror = (event) => {
      setTransfer(undefined);
      setError(event.message || 'Excel Worker 执行失败');
      cleanup();
    };
    worker.onmessage = (event: MessageEvent<RuleWorkbookWorkerResponse>) => {
      const message = event.data;
      if (message.requestId !== requestId) return;
      if (message.type === 'WORKBOOK_PROGRESS') {
        setTransfer({ percent: message.percent, message: message.message });
        return;
      }
      setTransfer(undefined);
      if (message.type === 'WORKBOOK_EXPORTED') {
        downloadBlob(
          new Blob([message.buffer], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' }),
          message.fileName,
        );
      } else if (message.type === 'WORKBOOK_IMPORTED') {
        setPreview(message.preview);
      } else if (message.type === 'WORKBOOK_ERROR') {
        setError(message.message);
      }
      cleanup();
    };
    setup(worker, requestId);
  }

  function exportExcel(mode: RuleWorkbookExportMode): void {
    const entries = selectWorkbookExportEntries(mode, currentEntries);
    runWorkbookWorker((worker, requestId) => {
      setTransfer({ percent: 1, message: '正在分批准备 Excel 导出数据' });
      worker.postMessage({
        type: 'EXPORT_WORKBOOK_START',
        requestId,
        mode,
        taskName,
        rules: [...displayRules],
        totalEntries: entries.length,
      });
      const chunkSize = 500;
      let offset = 0;
      const sendNext = () => {
        const end = Math.min(entries.length, offset + chunkSize);
        const chunk = entries.slice(offset, end);
        offset = end;
        worker.postMessage({
          type: 'EXPORT_WORKBOOK_CHUNK',
          requestId,
          entries: chunk,
          final: offset >= entries.length,
        });
        if (offset < entries.length) window.setTimeout(sendNext, 0);
      };
      if (entries.length === 0) {
        worker.postMessage({ type: 'EXPORT_WORKBOOK_CHUNK', requestId, entries: [], final: true });
      } else {
        sendNext();
      }
    });
  }

  function importExcel(file: File): void {
    file.arrayBuffer().then((buffer) => {
      runWorkbookWorker((worker, requestId) => {
        setTransfer({ percent: 1, message: '正在读取 Excel 文件' });
        worker.postMessage({ type: 'IMPORT_WORKBOOK', requestId, sourceName: file.name, buffer }, [buffer]);
      });
    }).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : String(reason)));
  }

  function exportJson(): void {
    const content = JSON.stringify({
      format: 'TraceLensDisplayRules',
      version: 1,
      exportedAt: new Date().toISOString(),
      rules: displayRules,
    }, null, 2);
    downloadBlob(new Blob([content], { type: 'application/json;charset=utf-8' }), `${safeFileName(taskName)}-语义规则备份.json`);
  }

  function importJson(file: File): void {
    file.text().then((text) => {
      const parsed = JSON.parse(text) as { rules?: unknown } | unknown[];
      const rules = Array.isArray(parsed) ? parsed : Array.isArray(parsed.rules) ? parsed.rules : undefined;
      if (!rules) throw new Error('JSON 中没有找到 rules 数组');
      setPreview(jsonPreview(file.name, rules));
      setError('');
    }).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : String(reason)));
  }

  function applyImport(): void {
    if (!analysis || analysis.applicableRules.length === 0) return;
    window.localStorage.setItem(RULE_IMPORT_SNAPSHOT_KEY, JSON.stringify({
      savedAt: Date.now(),
      sourceName: preview?.sourceName ?? '',
      rules: displayRules,
    }));
    onDisplayRulesChange(analysis.applicableRules);
    setLastAppliedAt(Date.now());
    setPreview(undefined);
  }

  function undoImport(): void {
    const raw = window.localStorage.getItem(RULE_IMPORT_SNAPSHOT_KEY);
    if (!raw) return;
    try {
      const snapshot = JSON.parse(raw) as { rules?: DisplayRule[] };
      if (!Array.isArray(snapshot.rules)) throw new Error('快照内容无效');
      onDisplayRulesChange(snapshot.rules);
      window.localStorage.removeItem(RULE_IMPORT_SNAPSHOT_KEY);
      setLastAppliedAt(undefined);
      setError('');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  return (
    <div className="rule-exchange-panel">
      <section className="exchange-section">
        <div className="settings-section-heading">
          <div><strong>Excel 批量配置</strong><span>中文列名、中文枚举下拉；生成和解析在 Worker 中执行。</span></div>
        </div>
        <div className="exchange-card-grid">
          <article className="exchange-card">
            <FileSpreadsheet size={24} />
            <div><strong>语义规则模板</strong><span>导出现有规则和最多 100 条去重日志样本，适合批量填写。</span></div>
            <button type="button" className="button primary" onClick={() => exportExcel('rules')} disabled={Boolean(transfer)}><Download size={15} /> 导出模板</button>
          </article>
          <article className="exchange-card">
            <FileSpreadsheet size={24} />
            <div><strong>当前筛选日志</strong><span>当前命中 {currentEntries.length.toLocaleString()} 条，最多导出前 500 条。</span></div>
            <button type="button" className="button secondary" onClick={() => exportExcel('filtered')} disabled={Boolean(transfer) || currentEntries.length === 0}><Download size={15} /> 导出筛选结果</button>
          </article>
        </div>
        <div className="exchange-inline-actions">
          <button type="button" className="button primary" onClick={() => excelInputRef.current?.click()} disabled={Boolean(transfer)}><Upload size={15} /> 导入 Excel</button>
          <input ref={excelInputRef} type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" hidden onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) importExcel(file);
            event.currentTarget.value = '';
          }} />
          <span>导入前会预检，不会立即覆盖当前规则。</span>
        </div>
      </section>

      <section className="exchange-section">
        <div className="settings-section-heading">
          <div><strong>精确备份与恢复</strong><span>JSON 完整保留规则 ID、参数位置和优先级。</span></div>
        </div>
        <div className="exchange-inline-actions">
          <button type="button" className="button secondary" onClick={exportJson}><FileJson size={15} /> 导出 JSON</button>
          <button type="button" className="button secondary" onClick={() => jsonInputRef.current?.click()}><Upload size={15} /> 导入 JSON</button>
          <input ref={jsonInputRef} type="file" accept=".json,application/json" hidden onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) importJson(file);
            event.currentTarget.value = '';
          }} />
          <button type="button" className="button ghost" disabled={!snapshotAvailable} onClick={undoImport}><RotateCcw size={15} /> 撤销上次导入</button>
          {lastAppliedAt && <span className="exchange-success"><Check size={14} /> 已应用导入，可撤销</span>}
        </div>
      </section>

      {transfer && (
        <div className="exchange-progress">
          <LoaderCircle className="spin" size={18} />
          <div><strong>{transfer.message}</strong><span>{transfer.percent}%</span></div>
          <progress max={100} value={transfer.percent} />
        </div>
      )}

      {error && <div className="exchange-error"><AlertTriangle size={16} /><span>{error}</span><button type="button" onClick={() => setError('')}><X size={14} /></button></div>}

      {preview && analysis && (
        <section className="import-preview">
          <header>
            <div><History size={18} /><span><strong>导入预检：{preview.sourceName}</strong><small>确认后才会更新浏览器中的规则。</small></span></div>
            <button type="button" className="icon-button" onClick={() => setPreview(undefined)} aria-label="关闭导入预检"><X size={16} /></button>
          </header>
          <div className="import-preview-metrics">
            <span className="success">新增 <strong>{analysis.additions.length}</strong></span>
            <span>更新 <strong>{analysis.updates.length}</strong></span>
            <span>重复跳过 <strong>{analysis.duplicates.length}</strong></span>
            <span className={analysis.conflicts.length ? 'warning' : ''}>优先级冲突 <strong>{analysis.conflicts.length}</strong></span>
            <span className={analysis.invalid.length ? 'error' : ''}>无效 <strong>{analysis.invalid.length}</strong></span>
          </div>
          {(analysis.invalid.length > 0 || analysis.conflicts.length > 0) && (
            <div className="import-preview-issues">
              {[...analysis.invalid, ...analysis.conflicts].slice(0, 30).map((candidate) => (
                <div key={`${candidate.sourceSheet}-${candidate.rowNumber}`}>
                  <strong>{candidate.sourceSheet}!第 {candidate.rowNumber} 行</strong>
                  <span>{candidate.errors.length ? candidate.errors.join('；') : '同一优先级存在不同匹配规则，将按表格行顺序追加。'}</span>
                </div>
              ))}
              {analysis.invalid.length + analysis.conflicts.length > 30 && <span>仅展示前 30 项问题。</span>}
            </div>
          )}
          <footer>
            <span>无效项不会导入；重复项自动跳过。应用前会自动保存当前规则快照。</span>
            <div>
              <button type="button" className="button ghost" onClick={() => setPreview(undefined)}>取消</button>
              <button type="button" className="button primary" disabled={analysis.additions.length + analysis.updates.length + analysis.conflicts.length === 0} onClick={applyImport}><Check size={15} /> 应用导入</button>
            </div>
          </footer>
        </section>
      )}
    </div>
  );
}
