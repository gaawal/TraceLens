export type LogLevel = string;
export type ContentSeverity = 'normal' | 'warning' | 'error';

export interface LogDocument {
  id: string;
  name: string;
  text: string;
}

export interface RpcTrace {
  traceId: string;
  spanId: string;
  fatherSpanId: string;
  raw: string;
  isRpc: boolean;
}

export interface SourceLocation {
  raw: string;
  fileName: string;
  lineNumber?: number;
}

export type FlowMarker = 'start' | 'end' | 'none';

export interface RunEventMetadata {
  eventCategory: string;
  eventLevel: string;
  currentEventCode: string;
  linkedEventCodes: string[];
  currentErrIId: string;
  linkedErrIIds: string[];
  displayCode: string;
  linkedDisplayCodes: string[];
  eventType: string;
}


export interface LogFormatParserRuleConfig {
  id: number;
  name: string;
  category: string;
  enabled: boolean;
  priority: number;
  file_pattern: string;
  pattern: string;
  client_pattern: string;
  ignore_case: boolean;
  field_map: Record<string, string>;
  group_names: string[];
  timestamp_format: string;
  built_in: boolean;
  description: string;
  created_at?: string;
  updated_at?: string;
}

export interface LogEntry {
  id: string;
  sourceFile: string;
  sourceFileId: string;
  lineNumber: number;
  timestamp: string;
  timestampNs?: bigint;
  level: LogLevel;
  component: string;
  processId: string;
  threadId: string;
  source: SourceLocation;
  mode: string;
  rpc: RpcTrace;
  message: string;
  functionName?: string;
  /** 入口/出口标记实际指向的被调用函数；未使用方括号调用格式时与 functionName 相同。 */
  boundaryFunctionName?: string;
  marker: FlowMarker;
  severity: ContentSeverity;
  summary: string;
  raw: string;
  /** 日志类别与实际解析器。 */
  logCategory?: string;
  parserProfile?: string;
  /** 远端日志检索时由流内部元数据保留的真实子系统/模块/源文件。 */
  logSubsystem?: string;
  logModule?: string;
  remoteSourcePath?: string;
  /** 仅运行日志 event 行存在的结构化事件字段。 */
  runEvent?: RunEventMetadata;
}


export interface ParseIssue {
  sourceFile: string;
  lineNumber: number;
  raw: string;
  reason: string;
}

export interface ParseResult {
  entries: LogEntry[];
  issues: ParseIssue[];
}

export interface LogLeaf {
  kind: 'log';
  id: string;
  entry: LogEntry;
}

export type FunctionNodeOrigin = 'boundary' | 'consecutive' | 'context' | 'repeated';

export interface FunctionNode {
  kind: 'function';
  origin: FunctionNodeOrigin;
  id: string;
  name: string;
  component: string;
  processId: string;
  threadId: string;
  rpc: RpcTrace;
  source: SourceLocation;
  startEntry: LogEntry;
  endEntry?: LogEntry;
  children: TimelineItem[];
  incomplete: boolean;
  /** 连续同名完整调用的外层聚合节点；仅 repeated 节点使用。 */
  repeatCount?: number;
  /** 创建该折叠节点的边界规则；自定义 START/END 与内置 >()/ <() 可并行存在。 */
  foldRuleId?: string;
  foldRuleName?: string;
}

export type TimelineItem = FunctionNode | LogLeaf;

export interface TraceTimeline {
  id: string;
  traceKey: string;
  rpc: RpcTrace;
  firstFunctionName: string;
  entries: LogEntry[];
  items: TimelineItem[];
  components: string[];
  sourceFiles: string[];
  participantCount: number;
  crossComponent: boolean;
}

export interface ThreadTimeline {
  id: string;
  component: string;
  processId: string;
  threadId: string;
  traces: TraceTimeline[];
  entryCount: number;
}

export interface ProcessTimeline {
  id: string;
  component: string;
  processId: string;
  threads: ThreadTimeline[];
  entryCount: number;
}
