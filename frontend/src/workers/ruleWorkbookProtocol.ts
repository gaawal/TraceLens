import type { LogEntry } from '../types';
import type { DisplayRule } from '../rendering/displayRules';
import type { RuleImportPreview, RuleWorkbookExportMode } from '../rendering/ruleExchange';

export type RuleWorkbookWorkerRequest =
  | {
      type: 'EXPORT_WORKBOOK_START';
      requestId: string;
      mode: RuleWorkbookExportMode;
      taskName: string;
      rules: DisplayRule[];
      totalEntries: number;
    }
  | {
      type: 'EXPORT_WORKBOOK_CHUNK';
      requestId: string;
      entries: LogEntry[];
      final: boolean;
    }
  | {
      type: 'IMPORT_WORKBOOK';
      requestId: string;
      sourceName: string;
      buffer: ArrayBuffer;
    };

export type RuleWorkbookWorkerResponse =
  | { type: 'WORKBOOK_PROGRESS'; requestId: string; percent: number; message: string }
  | { type: 'WORKBOOK_EXPORTED'; requestId: string; fileName: string; buffer: ArrayBuffer }
  | { type: 'WORKBOOK_IMPORTED'; requestId: string; preview: RuleImportPreview }
  | { type: 'WORKBOOK_ERROR'; requestId: string; message: string };
