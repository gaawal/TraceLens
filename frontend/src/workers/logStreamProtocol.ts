import type { LogEntry, LogFormatParserRuleConfig, ParseIssue } from '../types';

export type ImportStrategy = 'recent-tail' | 'full';

export interface ImportProgress {
  taskId: string;
  phase: 'planning' | 'reading' | 'finalizing';
  strategy: ImportStrategy;
  currentSource: string;
  sourceIndex: number;
  sourceCount: number;
  bytesRead: number;
  totalBytes: number;
  scannedBytes: number;
  totalFileBytes: number;
  linesRead: number;
  validCount: number;
  issueCount: number;
  skippedSourceCount: number;
  percent: number;
}

export type WorkerImportSource =
  | {
      kind: 'file';
      id: string;
      name: string;
      file: File;
      logCategories?: string[];
      logModule?: string;
      logSubsystem?: string;
      sourcePath?: string;
      parserRules?: LogFormatParserRuleConfig[];
    }
  | {
      kind: 'text';
      id: string;
      name: string;
      text: string;
      logCategories?: string[];
      logModule?: string;
      logSubsystem?: string;
      sourcePath?: string;
      parserRules?: LogFormatParserRuleConfig[];
    };

export type LogStreamWorkerRequest =
  | {
      type: 'IMPORT_TASK';
      taskId: string;
      sources: WorkerImportSource[];
      batchSize?: number;
      strategy?: ImportStrategy;
      /** 最近 N 秒。strategy=recent-tail 且未指定 targetStartNs 时使用。 */
      recentSeconds?: number;
      /** 指定最早需要覆盖的时间，十进制纳秒字符串。 */
      targetStartNs?: string;
      /** 倒序扫描字节块，默认 2 MiB。 */
      reverseBlockBytes?: number;
      /** 每累计多少行检查一次是否已越过目标时间，默认 5000。 */
      rangeCheckLines?: number;
    }
  | {
      type: 'CANCEL_TASK';
      taskId: string;
    };

export type LogStreamWorkerResponse =
  | {
      type: 'IMPORT_BATCH';
      taskId: string;
      entries: LogEntry[];
      issues: ParseIssue[];
    }
  | {
      type: 'IMPORT_PROGRESS';
      progress: ImportProgress;
    }
  | {
      type: 'IMPORT_COMPLETE';
      taskId: string;
      linesRead: number;
      validCount: number;
      issueCount: number;
      strategy: ImportStrategy;
      fullLoaded: boolean;
      requestedStartNs?: string;
      latestTimestampNs?: string;
      earliestImportedNs?: string;
      latestImportedNs?: string;
      selectedBytes: number;
      totalFileBytes: number;
      skippedSourceCount: number;
    }
  | {
      type: 'IMPORT_ERROR';
      taskId: string;
      message: string;
    };
