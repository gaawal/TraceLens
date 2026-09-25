import { captureContextSnapshot, collectPageContext } from '../assistant/contextRegistry';
import { Fragment, type CSSProperties, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  Activity,
  MessageSquare,
  ListTree,
  MousePointerClick,
  Search,
  Brain,
  Wrench,
  AppWindow,
  ArrowRight,
  ArrowUp,
  Check,
  ChevronDown,
  ChevronUp,
  Code2,
  FileText,
  GripVertical,
  History,
  LoaderCircle,
  Mic,
  Paperclip,
  PanelRight,
  Pencil,
  Smartphone,
  Plus,
  Send,
  ShieldCheck,
  Trash2,
  X,
} from 'lucide-react';
import {
  acknowledgeAssistantUi,
  cancelTraceLensAssistant,
  confirmTraceLensAssistant,
  draftCaseFromConversation,
  getTraceLensVoiceCapabilities,
  guideTraceLensAssistant,
  listTraceLensAiModels,
  listTraceLensTools,
  streamTraceLensAssistant,
  transcribeTraceLensVoice,
  type TraceLensAiModelOption,
  type TraceLensAssistantConfirmation,
  type TraceLensAssistantResultCard,
  type TraceLensAssistantTrace,
  type TraceLensCaseDraftResponse,
} from '../api/resourceApi';
import { executeUiAction } from '../assistant/workstation';
import {
  DEFAULT_EVIDENCE_MAX_CHARS,
  EVIDENCE_MAX_CHARS_PRESETS,
  formatEvidenceMaxChars,
  loadEvidenceMaxChars,
  saveEvidenceMaxChars,
} from '../assistant/logEvidence';
import { listAgentTasks, type AgentTaskSnapshot, removeAgentTask, saveAgentTask, updateAgentTask } from '../assistant/taskStore';
import { selectNextQueuedTask } from '../assistant/taskQueue';

type AssistantRole = 'user' | 'assistant';
type VoiceState = 'idle' | 'recording' | 'transcribing';
type SkillId = 'auto' | 'logs' | 'deployment' | 'environment' | 'atlog' | 'data';
/**
 * TracePilot panel layout — one explicit three-state switch.
 *
 * - `floating`: large draggable/resizable overlay; never touches the workspace width.
 * - `docked`:   flush against the right edge and squeezes `.app-shell` so logs and the
 *               agent stay visible side by side (the shared-screen workstation).
 * - `phone`:    phone-ratio floating panel (narrow, single column).
 *
 * Previously this was two independent flags (`docked: boolean` + `layoutMode:
 * 'desktop'|'mobile'`), which produced 4 combinations where 2 were meaningless and the
 * dock toggle was undiscoverable. Keep this a single source of truth.
 */
type AssistantLayoutMode = 'floating' | 'docked' | 'phone';

interface AssistantTaskState {
  status: 'running' | 'done' | 'confirm' | 'failed' | string;
  phase: string;
  title: string;
  detail?: string;
  progress?: number;
  currentStep?: number;
  totalSteps?: number;
  traces: TraceLensAssistantTrace[];
}

interface AssistantTokenUsage {
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  estimated: boolean;
}

interface AssistantMessage {
  id: string;
  role: AssistantRole;
  content: string;
  /** Interjections accepted by the backend but not yet consumed by a plan step. */
  interject?: 'pending' | 'applied' | 'missed';
  tokenUsage?: AssistantTokenUsage;
  task?: AssistantTaskState;
  confirmations?: TraceLensAssistantConfirmation[];
  resultCards?: TraceLensAssistantResultCard[];
  suggestions?: string[];
  /** 整理成案例 — the extracted conclusion attached to the message it came from. */
  caseDraft?: AssistantCaseDraftState;
}

interface AssistantCaseDraftState {
  status: 'loading' | 'ready' | 'failed';
  /** User's own additions, kept so 「继续补充」 can show what it is re-running with. */
  notes?: string;
  error?: string;
  result?: TraceLensCaseDraftResponse;
}

interface AssistantConversation {
  id: string;
  sessionId?: string;
  title: string;
  skillId: SkillId;
  memory?: string;
  scopeKey?: string;
  scopeLabel?: string;
  scopeContext?: Record<string, unknown>;
  createdAt: number;
  updatedAt: number;
  messages: AssistantMessage[];
}

interface CockpitLayout {
  left: number;
  top: number;
  width: number;
  height: number;
}

interface FabPosition {
  left: number;
  top: number;
}

interface TracePilotLaunchRequest {
  skillId: SkillId;
  scopeKey: string;
  scopeLabel: string;
  title: string;
  context: Record<string, unknown>;
  prompt?: string;
  autoSend?: boolean;
}

interface TracePilotRunRef {
  runId: string;
  conversationId: string;
  assistantId: string;
  controller: AbortController;
}

interface QueuedAssistantTask {
  id: string;
  conversationId: string;
  text: string;
  requestText?: string;
  createdAt: number;
}

interface ComposerReference {
  id: string;
  kind: 'log' | 'page';
  label: string;
  fullText: string;
}

/**
 * UI actions that open a full dialog. The assistant panel deliberately floats above the modal
 * layer so it is reachable from every page (环境部署 included), which means it has to stand
 * down for these — otherwise it would sit on top of the window it just opened.
 */
const TAKES_OVER_SCREEN_ACTIONS = new Set(['open_case_editor']);

/**
 * 托管动效的节奏。助手替用户操作页面时不应该“瞬间完成”：先把面板收成手机客户端
 * 形态靠右停靠、整页亮起呼吸绿边，让用户看清“页面正在被托管”，再逐步执行操作，
 * 每个动作之间留出让页面渲染 + 人眼跟上的间隔，最后停留一拍再还原原样式。
 */
const TAKEOVER_LEAD_IN_MS = 620;
const TAKEOVER_STEP_MS = 300;
const TAKEOVER_HOLD_MS = 520;
const TAKEOVER_ANIM_MS = 700;
/** 复原（手机形态 → 原窗口）的过渡时长；比入场慢，且末端带回弹 = 缓慢伸缩展开。 */
const TAKEOVER_RESTORE_MS = 1100;

const wait = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms));

const CONVERSATIONS_KEY = 'tracelens-ai-cockpit-conversations-v1';
const ACTIVE_CONVERSATION_KEY = 'tracelens-ai-cockpit-active-v1';
const LAYOUT_KEY = 'tracelens-ai-cockpit-layout-v2';
const LAYOUT_MODE_KEY = 'tracelens-ai-cockpit-layout-mode-v3';
// Superseded by LAYOUT_MODE_KEY; kept only so an existing browser keeps its choice.
const LEGACY_LAYOUT_MODE_KEY = 'tracelens-ai-cockpit-layout-mode-v2';
const LEGACY_DOCKED_KEY = 'tracelens-workstation-docked';
const MOBILE_LAYOUT_KEY = 'tracelens-ai-cockpit-mobile-layout-v1';
const FAB_POSITION_KEY = 'tracelens-ai-assistant-fab-position-v1';
const LEGACY_MESSAGES_KEY = 'tracelens-ai-assistant-session-v2';
const MAX_CONVERSATIONS = 24;
const MAX_MESSAGES_PER_CONVERSATION = 36;
const AI_EXECUTION_POLICY_KEY = 'tracelens-ai-execution-policy-v1';
const AI_MODEL_CHOICE_KEY = 'tracelens-ai-model-choice-v1';

/** Effort labels shown in the picker; keys are the gateway's `reasoning_effort` values. */
const EFFORT_LABELS: Record<string, string> = {
  none: '关闭思考',
  minimal: '极低',
  low: '低',
  medium: '中',
  high: '高',
  xhigh: '极高',
  ultra: '超高',
  max: '最高',
};

interface AiModelChoice {
  model: string;
  effort: string;
}

function loadModelChoice(): AiModelChoice {
  if (typeof window === 'undefined') return { model: '', effort: '' };
  try {
    const value = JSON.parse(window.localStorage.getItem(AI_MODEL_CHOICE_KEY) || '{}');
    return { model: String(value.model || '').slice(0, 120), effort: String(value.effort || '').slice(0, 32) };
  } catch {
    return { model: '', effort: '' };
  }
}

const SKILLS: Array<{ id: SkillId; name: string; description: string }> = [
  { id: 'auto', name: '自动识别', description: '根据当前问题自动选择最小工具集' },
  { id: 'logs', name: '日志分析', description: '组件定位、日志检索、异常取证' },
  { id: 'deployment', name: '环境部署', description: '参数准备、部署预览、确认执行' },
  { id: 'environment', name: '环境状态', description: '拓扑、在线状态、版本与资源检查' },
  { id: 'atlog', name: '自动化用例', description: 'ATLog 用例、断言与日志分析' },
  { id: 'data', name: '数据提取', description: '理解当前日志并自动生成、复用数据采集能力' },
];

function AiAgentIcon({ className = '' }: { className?: string }) {
  return <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/></svg>;
}

function createId(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function createSessionId(): string {
  const suffix = (typeof crypto !== 'undefined' && crypto.randomUUID)
    ? crypto.randomUUID().replace(/-/g, '').slice(0, 20)
    : `${Date.now()}${Math.random().toString(36).slice(2, 8)}`;
  return `ses_${suffix}`;
}

function normalizeSkillId(value: unknown): SkillId {
  const text = String(value || 'auto');
  return SKILLS.some((skill) => skill.id === text) ? text as SkillId : 'auto';
}

function createConversation(
  skillId: SkillId = 'auto',
  options?: { title?: string; scopeKey?: string; scopeLabel?: string; scopeContext?: Record<string, unknown> },
): AssistantConversation {
  const now = Date.now();
  return {
    id: createId('conversation'),
    sessionId: createSessionId(),
    title: options?.title || '新对话',
    skillId,
    memory: '',
    scopeKey: options?.scopeKey,
    scopeLabel: options?.scopeLabel,
    scopeContext: captureContextSnapshot(options?.scopeContext),
    createdAt: now,
    updatedAt: now,
    messages: [],
  };
}

function sanitizeStoredMessage(message: AssistantMessage): AssistantMessage {
  const rawUsage = message.tokenUsage as AssistantTokenUsage | undefined;
  const tokenUsage = rawUsage ? {
    inputTokens: Math.max(0, Number(rawUsage.inputTokens || 0)),
    outputTokens: Math.max(0, Number(rawUsage.outputTokens || 0)),
    totalTokens: Math.max(0, Number(rawUsage.totalTokens || Number(rawUsage.inputTokens || 0) + Number(rawUsage.outputTokens || 0))),
    estimated: Boolean(rawUsage.estimated),
  } : undefined;
  return {
    ...message,
    tokenUsage,
    content: String(message.content || '').slice(0, 12000),
    resultCards: Array.isArray(message.resultCards) ? message.resultCards.slice(0, 6).map((card) => ({
      ...card,
      title: String(card.title || '').slice(0, 120),
      summary: card.summary ? String(card.summary).slice(0, 320) : undefined,
      facts: Array.isArray(card.facts) ? card.facts.slice(0, 8).map((fact) => ({ label: String(fact.label || '').slice(0, 40), value: String(fact.value || '').slice(0, 280) })) : [],
      actions: Array.isArray(card.actions) ? card.actions.slice(0, 6) : [],
    })) : undefined,
    suggestions: Array.isArray(message.suggestions) ? message.suggestions.map(String).map((item) => item.trim()).filter(Boolean).slice(0, 3) : undefined,
    caseDraft: message.caseDraft ? {
      status: message.caseDraft.status,
      notes: message.caseDraft.notes ? String(message.caseDraft.notes).slice(0, 2000) : undefined,
      error: message.caseDraft.error ? String(message.caseDraft.error).slice(0, 400) : undefined,
      result: message.caseDraft.result ? {
        ...message.caseDraft.result,
        markdown: String(message.caseDraft.result.markdown || '').slice(0, 8000),
        case_draft: {
          ...message.caseDraft.result.case_draft,
          description: String(message.caseDraft.result.case_draft?.description || '').slice(0, 8000),
        },
        evidences: Array.isArray(message.caseDraft.result.evidences)
          ? message.caseDraft.result.evidences.slice(0, 40).map((evidence) => ({ ...evidence, raw: String(evidence.raw || '').slice(0, 1000) }))
          : [],
      } : undefined,
    } : undefined,
    task: message.task ? {
      ...message.task,
      traces: Array.isArray(message.task.traces) ? message.task.traces.slice(-20).map((trace) => ({
        id: String(trace.id || createId('trace')),
        stage: String(trace.stage || 'tool'),
        title: String(trace.title || '任务步骤').slice(0, 120),
        status: String(trace.status || 'success'),
        detail: trace.detail ? String(trace.detail).slice(0, 500) : undefined,
        input: trace.input,
        output: trace.output,
      })) : [],
    } : undefined,
  };
}

function loadConversations(): AssistantConversation[] {
  if (typeof window === 'undefined') return [createConversation()];
  try {
    const stored = JSON.parse(window.localStorage.getItem(CONVERSATIONS_KEY) || '[]');
    if (Array.isArray(stored) && stored.length) {
      return stored.slice(0, MAX_CONVERSATIONS).map((item) => ({
        id: String(item.id || createId('conversation')),
        title: String(item.title || '新对话').slice(0, 40),
        skillId: normalizeSkillId(item.skillId),
        memory: typeof item.memory === 'string' ? item.memory.slice(0, 3200) : '',
        scopeKey: typeof item.scopeKey === 'string' ? item.scopeKey.slice(0, 500) : undefined,
        scopeLabel: typeof item.scopeLabel === 'string' ? item.scopeLabel.slice(0, 120) : undefined,
        scopeContext: item.scopeContext && typeof item.scopeContext === 'object' ? item.scopeContext as Record<string, unknown> : undefined,
        createdAt: Number(item.createdAt || Date.now()),
        updatedAt: Number(item.updatedAt || Date.now()),
        messages: Array.isArray(item.messages) ? item.messages.slice(-MAX_MESSAGES_PER_CONVERSATION).map(sanitizeStoredMessage) : [],
      }));
    }
  } catch {
    // Fall through to legacy migration.
  }
  try {
    const legacy = JSON.parse(window.sessionStorage.getItem(LEGACY_MESSAGES_KEY) || '[]');
    if (Array.isArray(legacy) && legacy.length) {
      const conversation = createConversation('auto');
      conversation.messages = legacy.slice(-MAX_MESSAGES_PER_CONVERSATION).map(sanitizeStoredMessage);
      const firstUser = conversation.messages.find((message) => message.role === 'user')?.content.trim();
      conversation.title = firstUser ? firstUser.slice(0, 24) : '历史对话';
      return [conversation];
    }
  } catch {
    // Ignore invalid legacy storage.
  }
  return [createConversation()];
}

function defaultDesktopLayout(): CockpitLayout {
  const viewportWidth = typeof window === 'undefined' ? 1440 : window.innerWidth;
  const viewportHeight = typeof window === 'undefined' ? 900 : window.innerHeight;
  const width = Math.min(1500, Math.max(980, Math.round(viewportWidth * 0.84)));
  const height = mobileLayoutBox().height;
  return {
    left: Math.max(12, Math.round((viewportWidth - width) / 2)),
    top: Math.max(12, Math.round((viewportHeight - height) / 2)),
    width,
    height,
  };
}

function loadLayout(): CockpitLayout {
  const fallback = defaultDesktopLayout();
  if (typeof window === 'undefined') return fallback;
  try {
    const value = JSON.parse(window.localStorage.getItem(LAYOUT_KEY) || '{}');
    const next = {
      left: Number(value.left),
      top: Number(value.top),
      width: Number(value.width),
      // Keep desktop and phone-ratio modes the same height. Existing persisted
      // desktop heights are normalized instead of carrying an older shorter/taller value.
      height: fallback.height,
    };
    if ([next.left, next.top, next.width, next.height].every(Number.isFinite)) return next;
  } catch {
    // Ignore invalid persisted layout.
  }
  return fallback;
}

const ASSISTANT_LAYOUT_MODES: AssistantLayoutMode[] = ['floating', 'docked', 'phone'];

/** Below this viewport width the workspace is too narrow to give up 400px+ to the panel. */
const DOCK_MIN_VIEWPORT_WIDTH = 1100;

export function assistantDockIsAvailable(viewportWidth = typeof window === 'undefined' ? 1440 : window.innerWidth): boolean {
  return viewportWidth >= DOCK_MIN_VIEWPORT_WIDTH;
}

function loadLayoutMode(): AssistantLayoutMode {
  if (typeof window === 'undefined') return 'docked';
  const stored = window.localStorage.getItem(LAYOUT_MODE_KEY);
  if (ASSISTANT_LAYOUT_MODES.includes(stored as AssistantLayoutMode)) return stored as AssistantLayoutMode;
  // Migrate the legacy two-flag model: docked flag first, then the desktop/mobile shape.
  const legacyShape = window.localStorage.getItem(LEGACY_LAYOUT_MODE_KEY);
  const legacyDocked = window.localStorage.getItem(LEGACY_DOCKED_KEY);
  if (legacyDocked === '0') return legacyShape === 'mobile' ? 'phone' : 'floating';
  return legacyShape === 'mobile' ? 'phone' : 'docked';
}

function mobileLayoutBox(): CockpitLayout {
  const viewportWidth = typeof window === 'undefined' ? 1440 : window.innerWidth;
  const viewportHeight = typeof window === 'undefined' ? 900 : window.innerHeight;
  if (viewportWidth <= 720) {
    return { left: 2, top: 2, width: Math.max(320, viewportWidth - 4), height: Math.max(480, viewportHeight - 4) };
  }
  const width = Math.min(560, viewportWidth - 24);
  const height = Math.min(820, viewportHeight - 24);
  return {
    left: Math.max(12, Math.round((viewportWidth - width) / 2)),
    top: Math.max(12, Math.round((viewportHeight - height) / 2)),
    width,
    height,
  };
}

function clampMobileLayout(layout: CockpitLayout): CockpitLayout {
  const fallback = mobileLayoutBox();
  const viewportWidth = typeof window === 'undefined' ? 1440 : window.innerWidth;
  const viewportHeight = typeof window === 'undefined' ? 900 : window.innerHeight;
  const width = fallback.width;
  const height = fallback.height;
  if (viewportWidth <= 720) return fallback;
  const visibleX = Math.min(96, Math.max(72, viewportWidth * 0.08));
  const visibleY = 58;
  const minLeft = Math.min(6, visibleX - width);
  const maxLeft = Math.max(6, viewportWidth - visibleX);
  const minTop = 6;
  const maxTop = Math.max(6, viewportHeight - visibleY);
  return {
    left: Math.min(Math.max(minLeft, Number.isFinite(layout.left) ? layout.left : fallback.left), maxLeft),
    top: Math.min(Math.max(minTop, Number.isFinite(layout.top) ? layout.top : fallback.top), maxTop),
    width,
    height,
  };
}

function loadMobileLayout(): CockpitLayout {
  const fallback = mobileLayoutBox();
  if (typeof window === 'undefined') return fallback;
  try {
    const value = JSON.parse(window.localStorage.getItem(MOBILE_LAYOUT_KEY) || '{}');
    const next = { left: Number(value.left), top: Number(value.top), width: fallback.width, height: fallback.height };
    if ([next.left, next.top].every(Number.isFinite)) return clampMobileLayout(next);
  } catch {
    // Ignore invalid persisted mobile position.
  }
  return fallback;
}

function clampFabPosition(position: FabPosition): FabPosition {
  const width = typeof window === 'undefined' ? 1440 : window.innerWidth;
  const height = typeof window === 'undefined' ? 900 : window.innerHeight;
  const size = 58;
  const margin = 10;
  return {
    left: Math.min(Math.max(margin, position.left), Math.max(margin, width - size - margin)),
    top: Math.min(Math.max(margin, position.top), Math.max(margin, height - size - margin)),
  };
}

function loadFabPosition(): FabPosition {
  const fallback = clampFabPosition({
    left: (typeof window === 'undefined' ? 1440 : window.innerWidth) - 82,
    top: (typeof window === 'undefined' ? 900 : window.innerHeight) - 82,
  });
  if (typeof window === 'undefined') return fallback;
  try {
    const value = JSON.parse(window.localStorage.getItem(FAB_POSITION_KEY) || '{}');
    const next = { left: Number(value.left), top: Number(value.top) };
    if ([next.left, next.top].every(Number.isFinite)) return clampFabPosition(next);
  } catch {
    // Ignore invalid persisted position.
  }
  return fallback;
}

function currentContext(): Record<string, unknown> { return collectPageContext(); }


function confirmationText(item: TraceLensAssistantConfirmation): string {
  const summary = item.summary || {};
  const parts: string[] = [];
  if (summary.environment) parts.push(`环境 ${String(summary.environment)}`);
  if (summary.deployment_id) parts.push(`任务 ${summary.deployment_id}`);
  if (summary.step_key) parts.push(`步骤 ${summary.step_key}`);
  if (summary.target_version) parts.push(`版本 ${summary.target_version}`);
  if (summary.simulation_mode) parts.push(`模式 ${summary.simulation_mode}`);
  if (summary.scheduled_at) parts.push(`预约 ${String(summary.scheduled_at).replace('T', ' ').slice(0, 16)}`);
  if (summary.parameter_source === 'last_success') parts.push('沿用最近一次成功参数');
  const parameters = summary.parameters;
  if (parameters && typeof parameters === 'object') {
    const version = (parameters as Record<string, unknown>).target_version;
    const mode = (parameters as Record<string, unknown>).simulation_mode;
    const scheduledAt = (parameters as Record<string, unknown>).scheduled_at;
    if (version && !summary.target_version) parts.push(`版本 ${String(version)}`);
    if (mode && !summary.simulation_mode) parts.push(`模式 ${String(mode)}`);
    if (scheduledAt && !summary.scheduled_at) parts.push(`预约 ${String(scheduledAt).replace('T', ' ').slice(0, 16)}`);
  }
  return parts.join(' · ') || '请确认本次操作参数';
}

function renderInlineMarkdown(text: string, keyPrefix: string) {
  const token = /(\[\[(danger|warning|success|info|neutral):([^\]\n]+)\]\]|`[^`\n]+`|\*\*[^*\n]+\*\*|\*[^*\n]+\*|\[[^\]\n]+\]\([^\)\n]+\)|\b(?:ERROR|FAILED|FAIL|FATAL|WARNING|WARN|SUCCESS|PASSED|PASS|OK)\b)/gi;
  const nodes = [];
  let cursor = 0;
  let match: RegExpExecArray | null;
  let index = 0;
  while ((match = token.exec(text)) !== null) {
    if (match.index > cursor) nodes.push(<Fragment key={`${keyPrefix}-t-${index++}`}>{text.slice(cursor, match.index)}</Fragment>);
    const raw = match[0];
    if (raw.startsWith('[[')) {
      const tone = String(match[2] || 'neutral').toLowerCase();
      nodes.push(<span key={`${keyPrefix}-tag-${index++}`} className={`ai-md-tag ${tone}`}>{match[3]}</span>);
    } else if (raw.startsWith('`')) {
      nodes.push(<code key={`${keyPrefix}-c-${index++}`}>{raw.slice(1, -1)}</code>);
    } else if (raw.startsWith('**')) {
      nodes.push(<strong key={`${keyPrefix}-b-${index++}`}>{raw.slice(2, -2)}</strong>);
    } else if (raw.startsWith('*')) {
      nodes.push(<em key={`${keyPrefix}-e-${index++}`}>{raw.slice(1, -1)}</em>);
    } else if (raw.startsWith('[')) {
      const link = raw.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      if (link && /^(https?:|mailto:|\/|#)/i.test(link[2])) {
        nodes.push(<a key={`${keyPrefix}-a-${index++}`} href={link[2]} target="_blank" rel="noreferrer">{link[1]}</a>);
      } else {
        nodes.push(<Fragment key={`${keyPrefix}-r-${index++}`}>{raw}</Fragment>);
      }
    } else {
      const upper = raw.toUpperCase();
      const tone = /ERROR|FAILED|FAIL|FATAL/.test(upper) ? 'danger'
        : /WARNING|WARN/.test(upper) ? 'warning'
          : /SUCCESS|PASSED|PASS|OK/.test(upper) ? 'success'
            : 'neutral';
      nodes.push(<span key={`${keyPrefix}-status-${index++}`} className={`ai-md-tag ${tone}`}>{raw}</span>);
    }
    cursor = match.index + raw.length;
  }
  if (cursor < text.length) nodes.push(<Fragment key={`${keyPrefix}-tail`}>{text.slice(cursor)}</Fragment>);
  return nodes;
}

function FlowDiagram({ source }: { source: string }) {
  const lines = source.split('\n').map((item) => item.trim()).filter(Boolean).slice(0, 24);
  const steps: Array<{ text: string; edge?: string }> = [];
  for (const line of lines) {
    const parts = line.split(/\s*(?:-->|->|→)\s*/).map((item) => item.trim()).filter(Boolean);
    if (parts.length > 1) {
      parts.forEach((part, index) => {
        if (!steps.length || steps[steps.length - 1].text !== part) steps.push({ text: part, edge: index > 0 ? 'next' : undefined });
      });
    } else if (line) {
      steps.push({ text: line });
    }
  }
  if (!steps.length) return null;
  return (
    <div className="ai-md-flow" role="img" aria-label="流程图">
      {steps.slice(0, 18).map((step, index) => (
        <Fragment key={`${step.text}-${index}`}>
          {index > 0 && <span className="ai-md-flow-arrow" aria-hidden="true">→</span>}
          <div className="ai-md-flow-node">{renderInlineMarkdown(step.text, `flow-${index}`)}</div>
        </Fragment>
      ))}
    </div>
  );
}

function parseMarkdownTable(lines: string[], startIndex: number) {
  if (startIndex + 1 >= lines.length) return null;
  const header = lines[startIndex];
  const separator = lines[startIndex + 1];
  if (!header.includes('|') || !/^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(separator)) return null;
  const splitRow = (row: string) => row.trim().replace(/^\||\|$/g, '').split('|').map((cell) => cell.trim());
  const headers = splitRow(header);
  const rows: string[][] = [];
  let index = startIndex + 2;
  while (index < lines.length && lines[index].includes('|') && lines[index].trim()) {
    rows.push(splitRow(lines[index]));
    index += 1;
  }
  return { headers, rows, nextIndex: index };
}

function MarkdownContent({ content }: { content: string }) {
  const lines = String(content || '').replace(/\r\n/g, '\n').split('\n');
  const blocks = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }
    const fence = line.match(/^```\s*([^\s]*)\s*$/);
    if (fence) {
      const language = fence[1];
      const code: string[] = [];
      index += 1;
      while (index < lines.length && !/^```\s*$/.test(lines[index])) code.push(lines[index++]);
      if (index < lines.length) index += 1;
      if (/^(flow|flowchart)$/i.test(language)) {
        blocks.push(<FlowDiagram key={`flow-${index}`} source={code.join('\n')} />);
      } else {
        blocks.push(
          <div className="ai-md-code" key={`code-${index}`}>
            {language && <div className="ai-md-code-head"><Code2 size={14} /><span>{language}</span></div>}
            <pre><code>{code.join('\n')}</code></pre>
          </div>,
        );
      }
      continue;
    }
    const table = parseMarkdownTable(lines, index);
    if (table) {
      blocks.push(
        <div className="ai-md-table-wrap" key={`table-${index}`}>
          <table className="ai-md-table">
            <thead><tr>{table.headers.map((cell, cellIndex) => <th key={cellIndex}>{renderInlineMarkdown(cell, `th-${index}-${cellIndex}`)}</th>)}</tr></thead>
            <tbody>{table.rows.map((row, rowIndex) => <tr key={rowIndex}>{table.headers.map((_, cellIndex) => <td key={cellIndex}>{renderInlineMarkdown(row[cellIndex] || '', `td-${index}-${rowIndex}-${cellIndex}`)}</td>)}</tr>)}</tbody>
          </table>
        </div>,
      );
      index = table.nextIndex;
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      const level = heading[1].length;
      const Tag = `h${Math.min(level + 1, 5)}` as keyof JSX.IntrinsicElements;
      blocks.push(<Tag key={`h-${index}`}>{renderInlineMarkdown(heading[2], `h-${index}`)}</Tag>);
      index += 1;
      continue;
    }
    if (/^>\s?/.test(line)) {
      const quote: string[] = [];
      while (index < lines.length && /^>\s?/.test(lines[index])) quote.push(lines[index++].replace(/^>\s?/, ''));
      blocks.push(<blockquote key={`q-${index}`}>{quote.map((item, qIndex) => <Fragment key={qIndex}>{renderInlineMarkdown(item, `q-${index}-${qIndex}`)}{qIndex < quote.length - 1 && <br />}</Fragment>)}</blockquote>);
      continue;
    }
    if (/^[-*+]\s+/.test(line)) {
      const items: string[] = [];
      while (index < lines.length && /^[-*+]\s+/.test(lines[index])) items.push(lines[index++].replace(/^[-*+]\s+/, ''));
      blocks.push(<ul key={`ul-${index}`}>{items.map((item, itemIndex) => <li key={itemIndex}>{renderInlineMarkdown(item, `ul-${index}-${itemIndex}`)}</li>)}</ul>);
      continue;
    }
    if (/^\d+\.\s+/.test(line)) {
      const items: string[] = [];
      while (index < lines.length && /^\d+\.\s+/.test(lines[index])) items.push(lines[index++].replace(/^\d+\.\s+/, ''));
      blocks.push(<ol key={`ol-${index}`}>{items.map((item, itemIndex) => <li key={itemIndex}>{renderInlineMarkdown(item, `ol-${index}-${itemIndex}`)}</li>)}</ol>);
      continue;
    }
    if (/^---+$/.test(line.trim())) {
      blocks.push(<hr key={`hr-${index}`} />);
      index += 1;
      continue;
    }
    const paragraph: string[] = [line];
    index += 1;
    while (
      index < lines.length
      && lines[index].trim()
      && !/^```/.test(lines[index])
      && !/^#{1,4}\s+/.test(lines[index])
      && !/^>\s?/.test(lines[index])
      && !/^[-*+]\s+/.test(lines[index])
      && !/^\d+\.\s+/.test(lines[index])
      && !parseMarkdownTable(lines, index)
    ) paragraph.push(lines[index++]);
    blocks.push(
      <p key={`p-${index}`}>
        {paragraph.map((item, pIndex) => <Fragment key={pIndex}>{renderInlineMarkdown(item, `p-${index}-${pIndex}`)}{pIndex < paragraph.length - 1 && <br />}</Fragment>)}
      </p>,
    );
  }
  return <div className="ai-markdown">{blocks}</div>;
}

function confirmationToolProgress(item: TraceLensAssistantConfirmation, instruction = ''): { title: string; detail: string } {
  const toolId = String(item.tool_id || '');
  const text = String(instruction || '').trim();
  if (toolId === 'start_environment_deployment') {
    const scheduled = /预约|定时|(?:\d{1,2})[:：点]/.test(text) && !/立即|现在|马上/.test(text);
    return scheduled
      ? { title: '正在创建预约部署', detail: '正在提交已确认的部署参数和预约时间' }
      : { title: '正在创建部署任务', detail: '正在提交已确认的部署参数' };
  }
  if (toolId === 'stop_environment_deployment') return { title: '正在停止部署', detail: '正在向当前部署任务发送停止请求' };
  if (toolId === 'retry_environment_deployment_step') return { title: '正在重试部署步骤', detail: '正在从已确认的失败步骤重新提交执行' };
  if (toolId === 'ensure_deployment_ssh_trust') return { title: '正在修复部署 SSH 互信', detail: '正在连接目标设备并更新 SSH 授权配置' };
  if (toolId === 'sync_deployment_time') return { title: '正在同步部署目标时间', detail: '正在将目标设备时间同步到上位机时间基准' };
  return { title: `正在运行：${item.tool_name}`, detail: `正在执行 ${item.tool_name}` };
}


function sanitizeToolDetail(value: string): string {
  return String(value || '')
    .replace(/<think>[\s\S]*?<\/think>/gi, '')
    .replace(/<\/?think>/gi, '')
    .replace(/\s*(计划|行动|证据)[:：][^\n]*/g, '')
    .trim();
}

function traceStatusLabel(status: string): string {
  if (status === 'running') return '运行中';
  if (status === 'success') return '已完成';
  if (status === 'confirm') return '待确认';
  if (status === 'failed') return '需处理';
  if (status === 'stopped') return '已终止';
  if (status === 'interrupted') return '已干预';
  return status || '等待中';
}

/**
 * Per-step identity: a kind icon + label, so a long run reads as a sequence of typed
 * actions ("调用工具 get_environment_info") instead of an undifferentiated list.
 *
 * Raw model reasoning is deliberately NOT surfaced here — the backend keeps prompts in its
 * own logs, and showing them would trade auditability for noise.
 */
const TRACE_STAGE_META: Record<string, { label: string; Icon: typeof Wrench }> = {
  tool: { label: '调用工具', Icon: Wrench },
  evidence: { label: '读取证据', Icon: Search },
  thinking: { label: '分析判断', Icon: Brain },
  ui: { label: '操作页面', Icon: MousePointerClick },
  planning: { label: '规划', Icon: ListTree },
  answer: { label: '整理结论', Icon: MessageSquare },
};

function traceStageMeta(stage: string) {
  return TRACE_STAGE_META[stage] || { label: '执行', Icon: Activity };
}

/** Readable labels for the context keys the pages actually publish. */
const CONTEXT_LABELS: Record<string, string> = {
  'page': '页面',   'page_label': '页面名称',   'url': '地址',
  'environment_id': '环境 ID',   'environment_name': '环境',   'client_local_time': '本地时间',
  'client_timezone': '时区',   'captured_at': '采集时间',   'snapshot_id': '快照 ID',
  'log_locator': '日志定位',   'atlog_case': '用例',   'environment_page': '环境资源',
  'knowledge_page': '案例与分析',   'log_rules': '日志规则',   'log_rule_settings': '日志规则配置',
  'cpd_reports': 'CPD 报告',   'data_extraction': '数据提取',   'selected_entry': '选中日志',
  'selected_atlog_case': '选中用例',   'data_page': '数据提取页',   'audit_page': '审计页',
  'tools_page': '工具中心',   'result_count': '日志条数',   'error_count': '异常条数',
  'live_monitoring': '实时监听',   'query time range': '查询时间范围',   'view time range': '展示时间范围',
  'source categories': '日志类型',   'anomaly rules': '异常规则',   'data extraction rules': '数据提取规则',
  'semantic display': '语义规则',   'query skill': '查询 Skill',   'targets': '目标模块',
  'component_name': '组件',   'subsystem_name': '子系统',   'keyword': '关键字',
  'errors_only': '只看报错',   'level_filters': '级别筛选',   'component_filters': '组件筛选',
  'mode_filters': '模式筛选',   'sort_order': '排序',   'live_status': '实时状态',
  'loaded_entry_count': '已加载条数',   'source_files': '来源文件',   'displayed_page_number': '当前页',
  'displayed_page_size': '每页条数',   'function_fold_summary': '函数折叠摘要',   'displayed_evidence': '已展示证据',
  'auto_semantics': '自动语义',   'semantic_auto_status': '语义状态',   'resolved_count': '已解析',
  'rule_count': '规则数',   'parameter_count': '参数数',   'masking_count': '屏蔽规则数',
  'folding_count': '折叠规则数',   'data_extraction_rule_count': '提取规则数',   'source_seed': '来源样例',
  'anomaly_rule_count': '异常规则数',   'subsystem_count': '子系统数',   'fm_count': '模块数',
  'query_time_range': '查询时间范围',   'view_time_range': '展示时间范围',   'source_categories': '日志类型',
  'anomaly_rules': '异常规则',   'data_extraction_rules': '数据提取规则',   'semantic_display': '语义规则',
  'query_skill': '查询 Skill',
};

function contextLabel(key: string): string {
  return CONTEXT_LABELS[key] || key.replace(/_/g, ' ');
}

function contextScalarText(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (typeof value === 'string') return value;
  if (typeof value === 'number') return String(value);
  return String(value);
}

/** Render a value as labelled rows instead of dumping JSON at the user. */
function ContextValue({ value, label, depth = 0 }: { value: unknown; label?: string; depth?: number }) {
  if (value === null || value === undefined || value === '') {
    return label ? <div className="ctx-row" style={{ '--ctx-depth': depth } as React.CSSProperties}><span>{contextLabel(label)}</span><em>—</em></div> : null;
  }
  if (Array.isArray(value)) {
    if (!value.length) return label ? <div className="ctx-row" style={{ '--ctx-depth': depth } as React.CSSProperties}><span>{contextLabel(label)}</span><em>空</em></div> : null;
    return (
      <div className="ctx-group" style={{ '--ctx-depth': depth } as React.CSSProperties}>
        {label && <div className="ctx-group-title">{contextLabel(label)}<i>{value.length}</i></div>}
        {value.slice(0, 12).map((item, index) => (
          <ContextValue value={item} label={depth > 0 ? '' : String(index + 1)} depth={depth + 1} key={index} />
        ))}
        {value.length > 12 && <div className="ctx-more">… 共 {value.length} 项</div>}
      </div>
    );
  }
  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>);
    if (!entries.length) return null;
    return (
      <div className="ctx-group" style={{ '--ctx-depth': depth } as React.CSSProperties}>
        {label && <div className="ctx-group-title">{contextLabel(label)}<i>{entries.length}</i></div>}
        {entries.map(([key, item]) => <ContextValue value={item} label={key} depth={depth + 1} key={key} />)}
      </div>
    );
  }
  const text = contextScalarText(value);
  if (!text) return null;
  return (
    <div className="ctx-row" style={{ '--ctx-depth': depth } as React.CSSProperties}>
      <span>{label ? contextLabel(label) : ''}</span>
      <strong title={text.length > 120 ? text : undefined}>{text.length > 200 ? `${text.slice(0, 200)}…` : text}</strong>
    </div>
  );
}

function TaskTrace({ message, open, onToggle }: { message: AssistantMessage; open: boolean; onToggle: () => void }) {
  const task = message.task;
  if (!task) return null;
  const running = task.status === 'running';
  // Show auditable SSE progress from domain workflows (tool/evidence/thinking),
  // while keeping raw LLM/planning internals hidden.
  const visibleTraces = task.traces.filter((trace) => ['tool', 'evidence', 'thinking', 'ui'].includes(trace.stage));
  // A one-shot deployment preparation has no user-relevant intermediate tools.
  // The confirmation card itself is the useful UI; avoid rendering an empty
  // "0/0 complete" progress box above it.
  if (task.status === 'confirm' && visibleTraces.length === 0) return null;
  const completedCount = visibleTraces.filter((trace) => trace.status === 'success').length;
  const runningTrace = [...visibleTraces].reverse().find((trace) => trace.status === 'running');
  const latestTrace = visibleTraces[visibleTraces.length - 1];
  const summaryTitle = runningTrace?.title
    || (running ? '正在分析' : task.status === 'done' ? '分析完成' : task.title);
  const summaryDetail = runningTrace?.detail
    || (!running ? latestTrace?.detail || task.detail : '正在读取并核对当前证据');
  return (
    <div className={`ai-task-trace ${task.status}`} aria-live={running ? 'polite' : undefined}>
      <button type="button" className={`ai-task-summary ${running ? 'is-running' : ''}`} onClick={onToggle}>
        <span className="ai-task-summary-main">
          <span className={`ai-task-pulse ${running ? 'running' : ''}`} aria-hidden="true">
            {running ? <><i /><i /><i /></> : task.status === 'done' ? <Check size={16} /> : task.status === 'confirm' ? <ShieldCheck size={16} /> : <AlertTriangle size={16} />}
          </span>
          <span className="ai-task-summary-copy">
            <strong>{summaryTitle}</strong>
            {runningTrace?.tool_id && <code className="ai-task-summary-tool">{runningTrace.tool_id}</code>}
            <small>{summaryDetail}</small>
            {running && typeof task.progress === 'number' ? (
              <span className="ai-task-progress" aria-label={`分析进度 ${Math.round(task.progress)}%`}>
                <i style={{ width: `${Math.max(2, Math.min(100, task.progress))}%` }} />
              </span>
            ) : running ? <span className="ai-task-summary-shimmer" aria-hidden="true" /> : null}
          </span>
        </span>
        <span className="ai-task-summary-side">
          <span>{running
            ? (task.totalSteps && task.currentStep ? `${task.currentStep}/${task.totalSteps}` : runningTrace ? '运行中' : '执行中')
            : `${completedCount}/${visibleTraces.length} 完成`}</span>
          {open ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
        </span>
      </button>
      {open && (
        <div className="ai-task-detail">
          <div className="ai-task-detail-head">
            <strong>分析进展</strong>
            <span>{running ? '实时更新' : task.status === 'confirm' ? '等待确认' : task.status === 'failed' ? '执行中断' : task.status === 'stopped' ? '用户已终止' : task.status === 'interrupted' ? '用户已干预' : '已结束'}</span>
          </div>
          {visibleTraces.length === 0 && running && (
            <div className="ai-tool-selecting">
              <LoaderCircle className="spin" size={15} />
              <span>正在分析当前上下文</span>
            </div>
          )}
          <div className="ai-task-stage-list">
            {visibleTraces.map((trace, index) => (
              <div className={`ai-task-stage ${trace.status}`} key={trace.id}>
                <div className="ai-task-stage-rail" aria-hidden="true">
                  <span className="ai-task-stage-index">{trace.status === 'success' ? <Check size={12} /> : trace.status === 'running' ? <LoaderCircle className="spin" size={12} /> : index + 1}</span>
                  {index < visibleTraces.length - 1 && <i />}
                </div>
                <div className="ai-task-stage-body">
                  <div className="ai-task-stage-title">
                    <span className="ai-task-stage-kind">
                      {(() => { const meta = traceStageMeta(trace.stage); return <><meta.Icon size={12} aria-hidden="true" />{meta.label}</>; })()}
                    </span>
                    <span className="ai-task-stage-name"><strong>{trace.title}</strong></span>
                    <span className="ai-task-stage-status">{traceStatusLabel(trace.status)}</span>
                  </div>
                  {trace.tool_id && (
                    <div className="ai-task-stage-tool" title="本步骤实际调用的原子工具">
                      <code>{trace.tool_id}</code>
                    </div>
                  )}
                  {sanitizeToolDetail(trace.detail || '') && <p>{sanitizeToolDetail(trace.detail || '')}</p>}
                  {(trace.input !== undefined || trace.output !== undefined) && <details className="ai-action-audit">
                    <summary>查看执行参数与返回证据</summary>
                    {trace.input !== undefined && <><small>实际调用参数</small><pre>{JSON.stringify(trace.input, null, 2)}</pre></>}
                    {trace.output !== undefined && <><small>实际返回片段（有长度限制，敏感字段已隐藏）</small><pre>{JSON.stringify(trace.output, null, 2)}</pre></>}
                  </details>}
                  {trace.status === 'running' && (
                    <div className="ai-task-stage-shimmer" aria-hidden="true">
                      <i /><i />
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function clampLayout(layout: CockpitLayout): CockpitLayout {
  const viewportWidth = window.innerWidth;
  const viewportHeight = window.innerHeight;
  const minWidth = Math.min(780, Math.max(360, viewportWidth - 12));
  const width = Math.min(Math.max(layout.width, minWidth), Math.max(minWidth, viewportWidth - 12));
  // Height intentionally follows the phone-ratio cockpit so switching layouts
  // does not make the planning/work area jump vertically. Desktop resizing is width-only.
  const height = Math.min(mobileLayoutBox().height, Math.max(480, viewportHeight - 12));
  // Keep a usable part of the cockpit visible even when the user deliberately drags it
  // toward an edge. 72px leaves the avatar/title/restore affordance reachable.
  const visibleX = Math.min(96, Math.max(72, viewportWidth * 0.08));
  const visibleY = 58;
  const minLeft = Math.min(6, visibleX - width);
  const maxLeft = Math.max(6, viewportWidth - visibleX);
  const minTop = 6;
  const maxTop = Math.max(6, viewportHeight - visibleY);
  const left = Math.min(Math.max(minLeft, layout.left), maxLeft);
  const top = Math.min(Math.max(minTop, layout.top), maxTop);
  return { left, top, width, height };
}

/**
 * Resize-time refit.
 *
 * `clampLayout` is deliberately lenient about the right edge so the user can park a
 * floating cockpit half off-screen. That leniency must not survive a viewport change:
 * a box authored on a 1680px screen would otherwise hang off a 1000px window with its
 * header controls unreachable. This recenters only as far as needed to fit.
 */
function fitLayoutToViewport(layout: CockpitLayout, mode: AssistantLayoutMode): CockpitLayout {
  const viewportWidth = typeof window === 'undefined' ? 1440 : window.innerWidth;
  if (mode === 'phone') {
    const box = mobileLayoutBox();
    const maxLeft = Math.max(6, viewportWidth - 6 - box.width);
    return { ...box, left: Math.min(Math.max(box.left, Math.min(maxLeft, layout.left)), maxLeft) };
  }
  const clamped = clampLayout(layout);
  const maxLeft = Math.max(6, viewportWidth - 6 - clamped.width);
  return { ...clamped, left: Math.min(Math.max(6, clamped.left), maxLeft) };
}

function splitAssistantAnswer(content: string): { body: string; next: string[] } {
  const next: string[] = [];
  const body = String(content || '').replace(/\[\[next:([^\]\n]{2,240})\]\]/gi, (_match, action: string) => {
    const value = String(action || '').trim();
    if (value && !next.includes(value) && next.length < 3) next.push(value);
    return '';
  }).replace(/\n{3,}/g, '\n\n').trim();
  return { body, next };
}

function contextChips(context: Record<string, unknown>): string[] {
  const chips: string[] = [];
  const pageLabel = String(context.page_label || context.page || '').trim();
  if (pageLabel) chips.push(pageLabel);
  const environmentName = String(context.environment_name || '').trim();
  if (environmentName) chips.push(environmentName);

  const locator = context.log_locator && typeof context.log_locator === 'object'
    ? context.log_locator as Record<string, unknown>
    : null;
  if (locator) {
    const range = locator.query_time_range && typeof locator.query_time_range === 'object'
      ? locator.query_time_range as Record<string, unknown>
      : null;
    if (range?.start && range?.end) {
      const shortTime = (value: unknown) => String(value || '').replace('T', ' ').split(' ').pop()?.slice(0, 8) || String(value || '');
      chips.push(`${shortTime(range.start)}~${shortTime(range.end)}`);
    }
    const targets = Array.isArray(locator.targets) ? locator.targets.map(String).filter(Boolean) : [];
    if (targets.length) chips.push(targets.slice(0, 2).join(' / '));
    const count = Number(locator.result_count);
    const errors = Number(locator.error_count);
    if (Number.isFinite(count) && count >= 0) {
      chips.push(Number.isFinite(errors) && errors > 0 ? `${count} 条 · ${errors} ERROR` : `${count} 条`);
    }
    if (locator.live_monitoring) chips.push('实时监听');
  }
  return chips.filter(Boolean).slice(0, 5);
}

function useProgressiveText(content: string) {
  const [visible, setVisible] = useState('');
  const targetRef = useRef(content);
  targetRef.current = content;
  useEffect(() => {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) { setVisible(content); return; }
    const tick = () => setVisible((previous) => {
      const target = targetRef.current;
      if (previous === target) return previous;
      const prefix = target.startsWith(previous) ? previous : '';
      const rest = Array.from(target.slice(prefix.length));
      return prefix + rest.slice(0, Math.max(2, Math.ceil(rest.length / 24))).join('');
    });
    tick();
    const timer = window.setInterval(tick, 24);
    return () => window.clearInterval(timer);
  }, [content]);
  return visible;
}

function AssistantRichContent({
  content,
  onNext,
  disabled,
  extraNext = [],
}: {
  content: string;
  onNext: (action: string) => void;
  disabled?: boolean;
  extraNext?: string[];
}) {
  const visibleContent = useProgressiveText(content);
  const parsed = splitAssistantAnswer(visibleContent);
  const next = [...parsed.next];
  extraNext.map(String).map((item) => item.trim()).filter(Boolean).forEach((item) => {
    if (!next.includes(item) && next.length < 3) next.push(item);
  });
  return (
    <>
      <MarkdownContent content={parsed.body} />
      {next.length > 0 && (
        <div className="ai-answer-next" aria-label="继续操作">
          <span>建议下一步</span>
          <div>
            {next.map((action) => (
              <button key={action} type="button" disabled={disabled} onClick={() => onNext(action)}>
                <span>{action}</span><ArrowRight size={13} />
              </button>
            ))}
          </div>
        </div>
      )}
    </>
  );
}


function AssistantResultCards({
  cards,
  onUiAction,
  onPrompt,
  disabled,
}: {
  cards: TraceLensAssistantResultCard[];
  onUiAction: (action: Record<string, unknown>) => void;
  onPrompt: (prompt: string) => void;
  disabled?: boolean;
}) {
  if (!cards.length) return null;
  return (
    <div className="ai-result-cards" aria-label="AI 结果卡片">
      {cards.slice(0, 6).map((card, index) => (
        <article className={`ai-result-card ${card.status || 'neutral'} kind-${card.kind || 'generic'}`} key={card.id || `${card.kind}-${index}`}>
          <div className="ai-result-card-head">
            <span>{card.kind === 'logs' ? '日志' : card.kind === 'environment' ? '环境' : card.kind === 'component' ? '组件' : card.kind === 'data' ? '数据' : '结果'}</span>
            <strong>{card.title}</strong>
          </div>
          {card.summary && <p>{card.summary}</p>}
          {Array.isArray(card.facts) && card.facts.length > 0 && (
            <dl>
              {card.facts.slice(0, 8).map((fact, factIndex) => (
                <div key={`${fact.label}-${factIndex}`}><dt>{fact.label}</dt><dd>{fact.value}</dd></div>
              ))}
            </dl>
          )}
          {Array.isArray(card.actions) && card.actions.length > 0 && (
            <div className="ai-result-card-actions">
              {card.actions.slice(0, 6).map((action, actionIndex) => (
                <button
                  key={`${action.label}-${actionIndex}`}
                  type="button"
                  disabled={disabled}
                  onClick={() => {
                    if (action.kind === 'ui' && action.action) onUiAction(action.action);
                    else if (action.kind === 'prompt' && action.prompt) onPrompt(action.prompt);
                  }}
                >
                  {action.label}<ArrowRight size={13} />
                </button>
              ))}
            </div>
          )}
        </article>
      ))}
    </div>
  );
}

/**
 * Fallback labels for drafts persisted before the tool started returning `missing_labels`.
 * The backend owns this mapping; this is only for already-saved conversations.
 */
const MISSING_FIELD_LABELS: Record<string, string> = {
  root_cause: '根因尚未确认',
  evidence: '缺少真实日志证据',
  open_questions: '仍有待确认的问题',
};

/**
 * 案例草稿卡片 — the review step between "the analysis is done" and "a case is stored".
 *
 * Deliberately not auto-saving: the user sees exactly the fields the editor will receive,
 * can add their own evidence, and only then confirms. Saving goes through the normal
 * `open_case_editor` UI action, so there is still one write path into the case library.
 */
function AssistantCaseDraftCard({
  state,
  disabled,
  onRegenerate,
  onSave,
  onDismiss,
}: {
  state: AssistantCaseDraftState;
  disabled?: boolean;
  onRegenerate: (notes: string) => void;
  onSave: (result: TraceLensCaseDraftResponse) => void;
  onDismiss: () => void;
}) {
  const [notes, setNotes] = useState(state.notes || '');
  const [notesOpen, setNotesOpen] = useState(false);
  const [showMarkdown, setShowMarkdown] = useState(false);
  const result = state.result;
  const draft = result?.case_draft;
  const evidences = Array.isArray(result?.evidences) ? result!.evidences : [];
  // `missing_labels` is the server's own rendering of `missing`; the map is only a fallback
  // for drafts restored from an older localStorage shape.
  const missingLabels = Array.isArray(result?.missing_labels) && result!.missing_labels.length
    ? result!.missing_labels
    : (Array.isArray(result?.missing) ? result!.missing : []).map((item) => MISSING_FIELD_LABELS[item] || item);
  const openQuestions = Array.isArray(result?.open_questions) ? result!.open_questions.filter(Boolean) : [];
  const missing = missingLabels.filter(Boolean);
  const components = Array.isArray(result?.components) ? result!.components.filter(Boolean) : [];
  const confidenceLabel = result?.confidence === 'high' ? '高' : result?.confidence === 'low' ? '低' : '中';

  if (state.status === 'loading') {
    return (
      <article className="ai-case-draft is-loading" aria-live="polite">
        <div className="ai-case-draft-head"><FileText size={14} /><strong>正在整理诊断结论</strong><LoaderCircle className="spin" size={13} /></div>
        <p className="ai-case-draft-hint">正在从本轮对话中抽取现象、根因、建议和证据，不会写入案例库。</p>
      </article>
    );
  }

  if (state.status === 'failed' || !result || !draft) {
    return (
      <article className="ai-case-draft is-failed">
        <div className="ai-case-draft-head"><AlertTriangle size={14} /><strong>整理失败</strong></div>
        <p className="ai-case-draft-hint">{state.error || '模型没有返回可用的结构化结论。'}</p>
        <div className="ai-case-draft-actions">
          <button type="button" className="button ghost compact" disabled={disabled} onClick={onDismiss}>关闭</button>
          <button type="button" className="button primary compact" disabled={disabled} onClick={() => onRegenerate(notes)}>重新整理</button>
        </div>
      </article>
    );
  }

  return (
    <article className="ai-case-draft">
      <div className="ai-case-draft-head">
        <FileText size={14} />
        <strong>案例草稿</strong>
        <span className={`ai-case-draft-confidence is-${result.confidence || 'medium'}`}>置信度 {confidenceLabel}</span>
        <button type="button" className="ai-case-draft-close" aria-label="关闭案例草稿" onClick={onDismiss}><X size={13} /></button>
      </div>
      <p className="ai-case-draft-hint">核对下面这些字段，确认后会用案例编辑器打开并预填，保存前仍可修改。</p>
      <dl className="ai-case-draft-fields">
        <div><dt>案例名</dt><dd>{draft.name || <em>未提取</em>}</dd></div>
        <div><dt>分类</dt><dd>{draft.category || <em>未提取</em>}</dd></div>
        {components.length > 0 && <div><dt>涉及模块</dt><dd>{components.join('、')}</dd></div>}
        <div><dt>故障现象</dt><dd>{draft.symptom || <em>未提取</em>}</dd></div>
        <div><dt>根因</dt><dd>{draft.root_cause || <em>未确认，需补充证据</em>}</dd></div>
        <div><dt>处理建议</dt><dd>{draft.solution || <em>未提取</em>}</dd></div>
        {Array.isArray(draft.tags) && draft.tags.length > 0 && <div><dt>标签</dt><dd>{draft.tags.join('、')}</dd></div>}
      </dl>
      <div className="ai-case-draft-evidence">
        <span>关键证据 · {evidences.length} 条</span>
        {evidences.length > 0
          ? <ul>{evidences.slice(0, 5).map((item, index) => <li key={`${item.raw}-${index}`}><code>{item.raw}</code></li>)}</ul>
          : <p className="ai-case-draft-hint">没有提取到日志原文；保存前建议补一条真实证据。</p>}
      </div>
      {(missing.length > 0 || openQuestions.length > 0) && (
        <div className="ai-case-draft-missing">
          <span>待确认</span>
          <ul>
            {missing.map((item, index) => <li key={`missing-${index}`}>{item}</li>)}
            {openQuestions.slice(0, 4).map((item, index) => <li key={`question-${index}`}>{item}</li>)}
          </ul>
        </div>
      )}
      {showMarkdown && <pre className="ai-case-draft-markdown">{result.markdown}</pre>}
      {notesOpen && (
        <div className="ai-case-draft-notes">
          <textarea
            value={notes}
            rows={3}
            placeholder="补充说明或额外证据（例如：实际是标定文件过期，附上标定记录的日志行）"
            onChange={(event) => setNotes(event.target.value)}
          />
          <div className="ai-case-draft-actions">
            <button type="button" className="button ghost compact" onClick={() => setNotesOpen(false)}>取消</button>
            <button type="button" className="button primary compact" disabled={disabled || !notes.trim()} onClick={() => { setNotesOpen(false); onRegenerate(notes); }}>用补充内容重新整理</button>
          </div>
        </div>
      )}
      <div className="ai-case-draft-actions">
        <button type="button" className="button ghost compact" disabled={disabled} onClick={() => setShowMarkdown((current) => !current)}>{showMarkdown ? '收起预览' : '查看完整预览'}</button>
        <button type="button" className="button ghost compact" disabled={disabled} onClick={() => setNotesOpen((current) => !current)}>{notesOpen ? '收起补充' : '继续补充'}</button>
        <button type="button" className="button ghost compact" disabled={disabled} onClick={() => onRegenerate(notes)}>重新整理</button>
        <button type="button" className="button primary compact" disabled={disabled} onClick={() => onSave(result)}>
          <Check size={13} /> 确认保存
        </button>
      </div>
    </article>
  );
}

function suggestedActions(skillId: SkillId, context: Record<string, unknown>): string[] {
  const page = String(context.page || '');
  const environmentName = String(context.environment_name || '').trim();
  const actions: string[] = [];
  const push = (value: string) => {
    const text = value.trim();
    if (text && !actions.includes(text) && actions.length < 5) actions.push(text);
  };

  const locator = context.log_locator && typeof context.log_locator === 'object'
    ? context.log_locator as Record<string, unknown>
    : undefined;
  const environmentPage = context.environment_page && typeof context.environment_page === 'object'
    ? context.environment_page as Record<string, unknown>
    : undefined;
  const atlogPage = context.atlog_page && typeof context.atlog_page === 'object'
    ? context.atlog_page as Record<string, unknown>
    : undefined;

  if (page === 'logs' || locator) {
    const targets = Array.isArray(locator?.targets) ? locator!.targets.map(String).filter(Boolean) : [];
    const target = targets[0] || '';
    const resultCount = Number(locator?.result_count || 0);
    const errorCount = Number(locator?.error_count || 0);
    const foldSummary = Array.isArray(locator?.function_fold_summary) ? locator!.function_fold_summary : [];
    if (resultCount > 0) push(`分析当前${target ? ` ${target}` : ''} 已展示的 ${resultCount} 条日志`);
    if (foldSummary.length > 0) push(`统计当前${target ? ` ${target}` : ''}各接口耗时并找出慢调用`);
    if (errorCount > 0) push(`定位当前 ${errorCount} 条 ERROR 的根因`);
    else if (resultCount > 0) push(`梳理当前${target ? ` ${target}` : ''}调用流程和关键状态`);
    if (resultCount > 0) push('从当前日志创建可复用的数据提取规则');
    if (resultCount === 0) {
      // Nothing is loaded yet: recommend the step that produces something to analyse,
      // instead of a generic "give me your next action".
      const label = environmentName ? ` ${environmentName}` : '当前环境';
      push(`查询${label}最近 3 小时的 ERROR 日志并定位根因`);
      push(`先检查${label}上下位机状态，再决定查哪个模块的日志`);
      push('导入一份日志文件并直接分析');
    }
  } else if (page === 'resources' || environmentPage) {
    const name = String(environmentPage?.environment_name || environmentName || '').trim();
    const label = name ? ` ${name}` : '当前环境';
    push(`检查${label}上下位机运行状态`);
    push(`核对${label}上下位机版本是否一致`);
    push(`查看${label}最近一次部署结果和失败步骤`);
    if (skillId === 'deployment') push(`预检查并准备部署${label}`);
  } else if (page === 'atlog' || atlogPage) {
    const expandedCase = atlogPage?.expanded_case && typeof atlogPage.expanded_case === 'object'
      ? atlogPage.expanded_case as Record<string, unknown> : undefined;
    const caseName = String(expandedCase?.case_name || expandedCase?.case_id || '').trim();
    push(`分析${caseName ? `用例 ${caseName}` : '当前用例'}失败原因`);
    push(`根据当前失败断言定位根因组件`);
    push(`补充当前用例的关键日志证据`);
  } else if (page === 'reports') {
    push('分析当前 CPD 报告中的异常项');
    push('对比当前报告的版本和环境信息');
    push('从报告时间窗跳转并定位对应日志');
  } else if (page === 'data') {
    push('汇总当前提取结果的关键指标');
    push('找出当前数据中的异常点和极值');
    push('返回日志定位继续补充数据样本');
  } else if (page === 'knowledge') {
    const caseCount = Number((context.knowledge_page as Record<string, unknown> | undefined)?.case_count || 0);
    const selectedCase = String((context.selected_atlog_case as Record<string, unknown> | undefined)?.case_name || '').trim();
    if (selectedCase) push(`分析案例「${selectedCase}」的根因和适用条件`);
    push('根据当前日志证据检索相似的历史案例');
    if (caseCount > 0) push(`梳理案例库中 ${caseCount} 个案例的共性根因`);
    push('把当前诊断结论整理成可入库的案例');
  } else if (page === 'audit') {
    push('分析最近失败的日志检索有什么共同点');
    push('找出被缓存命中最多的检索参数');
  } else if (page === 'tools') {
    push('说明当前可用的原子工具分别适合什么场景');
  } else if (page === 'platform-settings') {
    push('检查当前日志规则配置是否有冲突');
    push('根据当前日志样例新增语义规则');
    push('根据当前日志样例新增数据提取规则');
  }

  // Skill only fills gaps; it no longer replaces live page-specific suggestions.
  if (actions.length < 3 && skillId === 'deployment') push(`检查${environmentName ? ` ${environmentName}` : '当前环境'}是否可部署`);
  if (actions.length < 3 && skillId === 'environment') push(`查询${environmentName ? ` ${environmentName}` : '当前环境'}版本与资源状态`);
  if (actions.length < 3 && skillId === 'data') push('从当前可见日志中识别可提取字段');
  if (actions.length < 3) push('根据当前页面真实状态给出下一步可执行操作');
  return actions.slice(0, 3);
}

function latestPendingConfirmation(conversation?: AssistantConversation): { messageId: string; item: TraceLensAssistantConfirmation } | undefined {
  if (!conversation) return undefined;
  for (let index = conversation.messages.length - 1; index >= 0; index -= 1) {
    const message = conversation.messages[index];
    const confirmations = message.confirmations || [];
    if (confirmations.length) return { messageId: message.id, item: confirmations[confirmations.length - 1] };
  }
  return undefined;
}

function looksLikeConfirmationReply(value: string): boolean {
  const text = value.trim();
  if (!text) return false;
  return /^(?:(?:确认|确定)(?=$|\s|[，,。.!！?？;；:]|但是|但)|(?:好|好的|好了|可以|执行|开始|同意|没问题|就按|按这个|按上面)(?=$|\s|[，,。.!！?？;；:]|这个|上面)|(?:ok|yes|y)\b)/i.test(text);
}

function compactTokenNumber(value: number): string {
  const amount = Math.max(0, Math.round(Number(value || 0)));
  if (amount < 1000) return String(amount);
  if (amount < 10000) return `${(amount / 1000).toFixed(1).replace(/\.0$/, '')}k`;
  return `${Math.round(amount / 1000)}k`;
}

export function AiAssistant() {
  const initialConversationsRef = useRef<AssistantConversation[] | null>(null);
  if (!initialConversationsRef.current) initialConversationsRef.current = loadConversations();
  const [open, setOpen] = useState(false);
  const [conversations, setConversations] = useState<AssistantConversation[]>(initialConversationsRef.current);
  const [activeConversationId, setActiveConversationId] = useState(() => {
    const stored = window.localStorage.getItem(ACTIVE_CONVERSATION_KEY);
    return initialConversationsRef.current?.some((item) => item.id === stored) ? String(stored) : String(initialConversationsRef.current?.[0]?.id || '');
  });
  const [layout, setLayout] = useState<CockpitLayout>(loadLayout);
  const [mobileLayout, setMobileLayout] = useState<CockpitLayout>(loadMobileLayout);
  const [fabPosition, setFabPosition] = useState<FabPosition>(loadFabPosition);
  const [closing, setClosing] = useState(false);
  const [skillCatalog, setSkillCatalog] = useState(SKILLS);
  const [input, setInput] = useState('');
  const [sendingConversationId, setSendingConversationId] = useState('');
  const [confirming, setConfirming] = useState('');
  const [executionPolicy, setExecutionPolicy] = useState<'confirm' | 'auto'>(() => (window.localStorage.getItem(AI_EXECUTION_POLICY_KEY) as 'confirm' | 'auto') || 'confirm');
  const [executionPolicyMenuOpen, setExecutionPolicyMenuOpen] = useState(false);
  const [modelChoice, setModelChoice] = useState<AiModelChoice>(loadModelChoice);
  // 日志证据（异常锚点 ±100 行）原文的长度上限：没超过就直送模型，超过才压缩。
  const [evidenceMaxChars, setEvidenceMaxChars] = useState<number>(loadEvidenceMaxChars);
  const [modelCatalog, setModelCatalog] = useState<TraceLensAiModelOption[]>([]);
  const [modelMenuOpen, setModelMenuOpen] = useState(false);
  const [attachMenuOpen, setAttachMenuOpen] = useState(false);
  /** Selected log row captured when the attach menu opens (never read during render). */
  const [attachSelection, setAttachSelection] = useState<Record<string, unknown> | null>(null);
  const [traceOpen, setTraceOpen] = useState<Record<string, boolean>>({});
  const [takeover, setTakeover] = useState('');
  // 托管动画类要比重启类早一拍加、晚一拍撤，两个方向都能平滑过渡。
  const [takeoverAnim, setTakeoverAnim] = useState(false);
  // 复原方向单独用一套更慢、带轻微回弹的过渡：控制结束后面板是「慢慢伸展开」回原样，
  // 而不是啪一下弹回去。
  const [takeoverRestoring, setTakeoverRestoring] = useState(false);
  const [layoutMode, setLayoutMode] = useState<AssistantLayoutMode>(loadLayoutMode);
  const [dockFits, setDockFits] = useState<boolean>(() => assistantDockIsAvailable());
  const [mobileHistoryOpen, setMobileHistoryOpen] = useState(false);
  const [focusTarget, setFocusTarget] = useState('');
  const [pendingLaunch, setPendingLaunch] = useState<{ conversationId: string; prompt: string }>();
  const [contextRevision, setContextRevision] = useState(0);
  const [suggestionContext, setSuggestionContext] = useState<Record<string, unknown>>({});
  const [composerMenuOpen, setComposerMenuOpen] = useState(false);
  const [composerReferences, setComposerReferences] = useState<ComposerReference[]>([]);
  const [taskQueue, setTaskQueue] = useState<QueuedAssistantTask[]>([]);
  const [interjectingTaskId, setInterjectingTaskId] = useState('');
  /** Message ids of interjections awaiting the backend's `phase: guidance` acknowledgement. */
  const pendingInterjectionsRef = useRef<string[]>([]);
  const [voiceState, setVoiceState] = useState<VoiceState>('idle');
  const [voiceSeconds, setVoiceSeconds] = useState(0);
  const [voiceError, setVoiceError] = useState('');
  const [voiceAvailable, setVoiceAvailable] = useState(true);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const mediaRecorderRef = useRef<MediaRecorder>();
  const voiceStreamRef = useRef<MediaStream>();
  const voiceChunksRef = useRef<Blob[]>([]);
  const voiceTimerRef = useRef<number>();
  const voiceCancelledRef = useRef(false);
  const abortRef = useRef<AbortController>();
  const activeRunRef = useRef<TracePilotRunRef>();
  const dragRef = useRef<{ mode: 'drag' | 'resize'; layoutMode: AssistantLayoutMode; x: number; y: number; layout: CockpitLayout } | null>(null);
  const fabDragRef = useRef<{ pointerId: number; x: number; y: number; left: number; top: number } | null>(null);
  const fabDraggedRef = useRef(false);
  const closeTimerRef = useRef<number>();

  const activeConversation = conversations.find((item) => item.id === activeConversationId) || conversations[0];
  /** Live page context for the panel. `contextRevision` ticks each second while the panel
   *  is open, so this refreshes itself — the manual "更新为当前页面快照" button is gone. */
  const liveContext = useMemo(
    () => (open ? currentContext() : (activeConversation?.scopeContext || {})),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [open, contextRevision, activeConversationId, activeConversation?.scopeContext],
  );
  // Docked means "flush right and squeeze the workspace". On a viewport too narrow to
  // give up the dock width, the panel stays flush right but floats over the workspace
  // instead of pushing it around, and the header explains why.
  const docked = layoutMode === 'docked';
  const dockSqueezes = docked && dockFits;
  useEffect(() => {
    const root = document.documentElement;
    root.classList.toggle('workstation-docked', dockSqueezes && open);
    window.localStorage.setItem(LAYOUT_MODE_KEY, layoutMode);
    window.localStorage.setItem(LEGACY_DOCKED_KEY, docked ? '1' : '0');
    return () => root.classList.remove('workstation-docked');
  }, [dockSqueezes, docked, layoutMode, open]);
  const messages = activeConversation?.messages || [];
  const conversationTokenUsage = messages.reduce<AssistantTokenUsage>((total, message) => {
    const usage = message.tokenUsage;
    if (!usage) return total;
    total.inputTokens += usage.inputTokens;
    total.outputTokens += usage.outputTokens;
    total.totalTokens += usage.totalTokens;
    total.estimated = total.estimated || usage.estimated;
    return total;
  }, { inputTokens: 0, outputTokens: 0, totalTokens: 0, estimated: false });
  const selectedSkill = activeConversation?.skillId || 'auto';
  const sending = sendingConversationId === activeConversation?.id;
  const activeQueuedTasks = taskQueue.filter((item) => item.conversationId === activeConversation?.id);
  /** Only the conversation that owns the live run can accept an interjection. */
  const canInterject = Boolean(sending && activeRunRef.current && activeRunRef.current.conversationId === activeConversation?.id);
  const lastAssistantSuggestions = [...messages].reverse().find((message) => message.role === 'assistant' && Array.isArray(message.suggestions) && message.suggestions.length)?.suggestions || [];
  const composerSuggestions = lastAssistantSuggestions.length ? lastAssistantSuggestions : suggestedActions(selectedSkill, suggestionContext);
  const activeModel = modelCatalog.find((item) => item.id === modelChoice.model);
  const activeModelName = activeModel?.name || modelChoice.model || '默认模型';
  const activeEffortLabel = EFFORT_LABELS[modelChoice.effort || activeModel?.default_effort || ''] || '';
  void contextRevision;

  useEffect(() => {
    if (layoutMode === 'floating') setMobileHistoryOpen(false);
  }, [layoutMode]);

  useEffect(() => {
    if (!open) return;
    const refresh = () => setContextRevision((current) => current + 1);
    const timer = window.setInterval(refresh, 1000);
    window.addEventListener('tracelens:assistant-context-changed', refresh);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener('tracelens:assistant-context-changed', refresh);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    // Suggestions follow the page the user is on *right now*.
    //
    // This used to prefer `activeConversation.scopeContext`, which is a frozen snapshot taken
    // when the conversation started. Once it existed the live page was never consulted again,
    // so switching from 环境资源 to 日志定位 left the assistant recommending the old page's
    // questions forever. The snapshot is still what a *request* carries (so a turn keeps the
    // evidence it started with); only the recommendations follow the live page.
    setSuggestionContext(currentContext());
    // No snapshot here on purpose: merely opening the panel must not freeze a page context.
    // The snapshot is taken when a turn is sent (see executeTask) and refreshed on every
    // turn, so it always describes where the user was when they last asked something.
  }, [open, contextRevision, activeConversationId, selectedSkill]);

  useEffect(() => {
    let alive = true;
    listTraceLensTools()
      .then((payload) => {
        if (!alive || !Array.isArray(payload.skills)) return;
        const supported = payload.skills
          .filter((item) => SKILLS.some((known) => known.id === item.id))
          .map((item) => ({ id: item.id as SkillId, name: item.name, description: item.description }));
        if (supported.length) setSkillCatalog(supported);
      })
      .catch(() => { /* fallback to embedded catalog */ });
    return () => { alive = false; };
  }, []);

  useEffect(() => {
    let alive = true;
    getTraceLensVoiceCapabilities()
      .then((payload) => { if (alive) setVoiceAvailable(Boolean(payload.stt)); })
      .catch(() => { if (alive) setVoiceAvailable(true); });
    return () => { alive = false; };
  }, []);

  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent<Record<string, unknown>>).detail || {};
      const skillId = normalizeSkillId(detail.skill_id || detail.skillId || 'auto');
      const scopeKey = String(detail.scope_key || detail.scopeKey || '').trim();
      const scopeLabel = String(detail.scope_label || detail.scopeLabel || '').trim();
      const title = String(detail.title || scopeLabel || 'TracePilot').trim().slice(0, 40);
      const context = detail.context && typeof detail.context === 'object' ? detail.context as Record<string, unknown> : {};
      const prompt = String(detail.prompt || '').trim();
      const autoSend = Boolean(detail.auto_send ?? detail.autoSend);
      const existing = scopeKey ? conversations.find((conversation) => conversation.scopeKey === scopeKey) : undefined;
      let targetId = existing?.id || '';
      let shouldAutoSend = false;

      if (existing) {
        shouldAutoSend = autoSend && existing.messages.length === 0 && Boolean(prompt);
        setConversations((current) => current.map((conversation) => conversation.id === existing.id
          ? {
              ...conversation,
              skillId,
              scopeLabel: scopeLabel || conversation.scopeLabel,
              scopeContext: { ...(conversation.scopeContext || {}), ...context },
              updatedAt: Date.now(),
            }
          : conversation));
      } else {
        const conversation = createConversation(skillId, {
          title,
          scopeKey: scopeKey || undefined,
          scopeLabel: scopeLabel || undefined,
          scopeContext: context,
        });
        targetId = conversation.id;
        shouldAutoSend = autoSend && Boolean(prompt);
        setConversations((current) => [conversation, ...current].slice(0, MAX_CONVERSATIONS));
      }

      if (!targetId) return;
      setActiveConversationId(targetId);
      setOpen(true);
      if (shouldAutoSend) setPendingLaunch({ conversationId: targetId, prompt });
    };
    window.addEventListener('tracelens:assistant-open', handler as EventListener);
    return () => window.removeEventListener('tracelens:assistant-open', handler as EventListener);
  }, [conversations]);

  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent<Record<string, unknown>>).detail || {};
      const raw = String(detail.raw || detail.message || '').trim();
      if (!raw) return;
      const timestamp = String(detail.timestamp || '').trim();
      const component = String(detail.component || '').trim();
      const level = String(detail.level || '').trim();
      const source = String(detail.source || '').trim();
      const header = [timestamp, component, level].filter(Boolean).join(' · ');
      const fullText = `【引用日志${header ? ` · ${header}` : ''}】\n${raw}${source ? `\n来源：${source}` : ''}`;
      const compactSource = source ? source.split(/[\\/]/).filter(Boolean).pop() || source : '';
      const preview = raw.replace(/\s+/g, ' ').trim();
      const label = ['日志', component || compactSource || level, preview].filter(Boolean).join(' · ');
      setComposerReferences((current) => [
        ...current.filter((item) => item.fullText !== fullText),
        { id: createId('composer-ref-log'), kind: 'log' as const, label, fullText },
      ].slice(-4));
      setOpen(true);
      setClosing(false);
      window.setTimeout(() => inputRef.current?.focus(), 80);
    };
    window.addEventListener('tracelens:assistant-quote-log', handler as EventListener);
    return () => window.removeEventListener('tracelens:assistant-quote-log', handler as EventListener);
  }, []);

  useEffect(() => {
    if (!pendingLaunch || activeConversation?.id !== pendingLaunch.conversationId || sendingConversationId) return;
    const next = pendingLaunch;
    setPendingLaunch(undefined);
    void send(next.prompt);
  }, [pendingLaunch, activeConversation?.id, sendingConversationId]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    // The assistant runs one task at a time, so anything queued anywhere must wait for
    // the live run. What must NOT happen is a backlog in one conversation making the
    // message you are looking at wait behind it — see selectNextQueuedTask.
    if (sendingConversationId || activeRunRef.current || taskQueue.length === 0) return;
    const known = new Set(conversations.map((conversation) => conversation.id));
    const next = selectNextQueuedTask(taskQueue, known, activeConversationId);
    // Never dequeue something we are not about to run; that is how messages used to
    // disappear without a trace when their conversation was gone.
    if (!next) return;
    const conversation = conversations.find((item) => item.id === next.conversationId);
    if (!conversation) return;
    setTaskQueue((current) => current.filter((item) => item.id !== next.id));
    void executeTask(next.text, conversation, next.requestText || next.text);
  }, [sendingConversationId, taskQueue, conversations, activeConversationId]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const root = document.documentElement;
    if (focusTarget) root.dataset.aiFocus = focusTarget;
    else delete root.dataset.aiFocus;
    return () => { delete root.dataset.aiFocus; };
  }, [focusTarget]);

  useEffect(() => {
    // Streaming can update the same assistant message many times per second.
    // Persisting the complete conversation synchronously on every token/trace
    // blocks the main thread and makes backend progress look delayed. Debounce
    // storage so painting always wins while still keeping recent state durable.
    const timer = window.setTimeout(() => {
      const safe = conversations.slice(0, MAX_CONVERSATIONS).map((conversation) => ({
        ...conversation,
        messages: conversation.messages.slice(-MAX_MESSAGES_PER_CONVERSATION).map(sanitizeStoredMessage),
      }));
      window.localStorage.setItem(CONVERSATIONS_KEY, JSON.stringify(safe));
    }, 250);
    return () => window.clearTimeout(timer);
  }, [conversations]);

  useEffect(() => {
    if (activeConversationId) window.localStorage.setItem(ACTIVE_CONVERSATION_KEY, activeConversationId);
  }, [activeConversationId]);

  useEffect(() => {
    window.localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout));
  }, [layout]);

  useEffect(() => {
    window.localStorage.setItem(AI_EXECUTION_POLICY_KEY, executionPolicy);
  }, [executionPolicy]);

  useEffect(() => {
    window.localStorage.setItem(AI_MODEL_CHOICE_KEY, JSON.stringify(modelChoice));
  }, [modelChoice]);

  // The picker is informational: an unreachable gateway must never block chatting, so
  // failures just leave the catalog empty and the label falls back to the stored choice.
  useEffect(() => {
    if (!open || modelCatalog.length) return;
    let cancelled = false;
    void listTraceLensAiModels()
      .then((catalog) => {
        if (cancelled) return;
        setModelCatalog(catalog.models || []);
        // Adopt the server default only while the user has made no explicit choice.
        setModelChoice((current) => {
          if (current.model) return current;
          const preferred = catalog.models.find((item) => item.is_default) || catalog.models[0];
          if (!preferred) return current;
          return { model: preferred.id, effort: preferred.default_effort || '' };
        });
      })
      .catch(() => undefined);
    return () => { cancelled = true; };
  }, [open, modelCatalog.length]);

  useEffect(() => {
    window.localStorage.setItem(MOBILE_LAYOUT_KEY, JSON.stringify({ left: mobileLayout.left, top: mobileLayout.top }));
  }, [mobileLayout.left, mobileLayout.top]);

  useEffect(() => {
    window.localStorage.setItem(FAB_POSITION_KEY, JSON.stringify(fabPosition));
  }, [fabPosition]);

  useEffect(() => {
    if (!open) return;
    // Reopening the cockpit or switching conversations should resume from the
    // active end of the thread, never from the oldest stored message. Two paint
    // turns cover restored rich cards/traces whose height settles after mount.
    requestAnimationFrame(() => requestAnimationFrame(() => {
      const node = scrollRef.current;
      if (node) node.scrollTop = node.scrollHeight;
    }));
  }, [open, activeConversationId]);

  useEffect(() => {
    requestAnimationFrame(() => {
      const node = scrollRef.current;
      if (!node) return;
      const atBottom = node.scrollHeight - node.scrollTop - node.clientHeight < 60;
      if (atBottom) node.scrollTop = node.scrollHeight;
    });
  }, [messages]);

  /**
   * A case draft signal that changes on every meaningful draft transition.
   *
   * The generic auto-scroll above only follows when the user was already at the bottom, and
   * the card's own growth is what pushes it out of view — so the draft buttons would land
   * below the fold exactly when they are what the user asked for.
   */
  const caseDraftSignal = useMemo(() => (activeConversation?.messages || [])
    .filter((message) => message.caseDraft)
    .map((message) => `${message.id}:${message.caseDraft!.status}:${message.caseDraft!.result?.case_draft?.name || ''}`)
    .join('|'), [activeConversation]);

  useEffect(() => {
    if (!open || !caseDraftSignal) return;
    requestAnimationFrame(() => requestAnimationFrame(() => {
      const node = scrollRef.current;
      if (node) node.scrollTop = node.scrollHeight;
    }));
  }, [caseDraftSignal, activeConversationId, open]);

  useEffect(() => {
    const node = inputRef.current;
    if (!node) return;
    node.style.height = '0px';
    node.style.height = `${Math.min(180, Math.max(38, node.scrollHeight))}px`;
  }, [input]);

  useEffect(() => {
    const onResize = () => {
      setDockFits(assistantDockIsAvailable());
      setLayout((current) => fitLayoutToViewport(current, 'floating'));
      setMobileLayout((current) => fitLayoutToViewport(current, 'phone'));
      setFabPosition((current) => clampFabPosition(current));
    };
    onResize();
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  useEffect(() => {
    const onMove = (event: MouseEvent) => {
      const drag = dragRef.current;
      if (!drag) return;
      const dx = event.clientX - drag.x;
      const dy = event.clientY - drag.y;
      if (drag.mode === 'drag') {
        const next = { ...drag.layout, left: drag.layout.left + dx, top: drag.layout.top + dy };
        if (drag.layoutMode === 'phone') setMobileLayout(clampMobileLayout(next));
        else setLayout(clampLayout(next));
      } else if (drag.layoutMode === 'floating') {
        setLayout(clampLayout({ ...drag.layout, width: drag.layout.width + dx, height: drag.layout.height + dy }));
      }
    };
    const onUp = () => { dragRef.current = null; };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      abortRef.current?.abort();
      if (voiceTimerRef.current) window.clearInterval(voiceTimerRef.current);
      if (closeTimerRef.current) window.clearTimeout(closeTimerRef.current);
      if (mediaRecorderRef.current?.state === 'recording') mediaRecorderRef.current.stop();
      voiceStreamRef.current?.getTracks().forEach((track) => track.stop());
    };
  }, []);

  const history = useMemo(
    () => messages
      .filter((item) => item.content.trim())
      .slice(-12)
      .map((item) => ({ role: item.role, content: item.role === 'assistant' ? splitAssistantAnswer(item.content).body : item.content })),
    [messages],
  );

  function setConversationMessages(conversationId: string, updater: (current: AssistantMessage[]) => AssistantMessage[]) {
    setConversations((current) => current.map((conversation) => {
      if (conversation.id !== conversationId) return conversation;
      const nextMessages = updater(conversation.messages).slice(-MAX_MESSAGES_PER_CONVERSATION);
      const firstUser = nextMessages.find((message) => message.role === 'user')?.content.trim();
      return {
        ...conversation,
        title: conversation.title === '新对话' && firstUser ? firstUser.slice(0, 24) : conversation.title,
        updatedAt: Date.now(),
        messages: nextMessages,
      };
    }));
  }

  function updateMessage(conversationId: string, messageId: string, updater: (message: AssistantMessage) => AssistantMessage) {
    setConversationMessages(conversationId, (current) => current.map((message) => message.id === messageId ? updater(message) : message));
  }

  function newConversation(skillId: SkillId = 'auto') {
    const conversation = createConversation(skillId);
    setConversations((current) => [conversation, ...current].slice(0, MAX_CONVERSATIONS));
    setActiveConversationId(conversation.id);
    setInput('');
    setComposerReferences([]);
  }

  function deleteConversation(conversationId: string) {
    if (activeRunRef.current?.conversationId === conversationId) stopActiveRun("用户删除了此会话");
    // Drop this conversation's backlog explicitly. Otherwise its queued messages become
    // orphans that can never run and used to be discarded silently by the drain effect.
    setTaskQueue((current) => current.filter((item) => item.conversationId !== conversationId));
    setConversations((current) => {
      const remaining = current.filter((item) => item.id !== conversationId);
      const next = remaining.length ? remaining : [createConversation('auto')];
      if (activeConversationId === conversationId) setActiveConversationId(next[0].id);
      return next;
    });
  }

  function changeSkill(skillId: SkillId) {
    if (!activeConversation) return;
    setConversations((current) => current.map((conversation) => conversation.id === activeConversation.id
      ? { ...conversation, skillId, updatedAt: Date.now() }
      : conversation));
  }

  function selectComposerSkill(skillId: SkillId) {
    changeSkill(skillId);
    setComposerMenuOpen(false);
    setContextRevision((current) => current + 1);
    requestAnimationFrame(() => inputRef.current?.focus());
  }

  async function runUiActions(actions: Array<Record<string, unknown>>, signal?: AbortSignal) {
    if (!actions.length) return;
    const first = actions[0];
    const type = String(first.type || '页面操作');
    const focus = String(first.takeover_focus || (
      type === 'open_log_locator' ? 'logs'
        : type === 'open_environment_page' ? 'resources'
          : type === 'run_data_extraction' ? 'data'
            : type === 'create_data_extraction_capability' ? (first.open_rule_settings ? 'settings' : 'data')
            : type === 'create_log_semantic_rule' || type === 'create_log_anomaly_rule' || type === 'open_log_rule_settings' ? 'settings'
              : type === 'open_workspace_page' ? String(first.page || 'workspace')
                : 'workspace'
    ));
    const label = type === 'open_log_locator' ? '正在进入日志定位并同步查询条件'
      : type === 'open_environment_page' ? '正在切换到目标环境'
        : type === 'create_data_extraction_capability' ? '正在根据当前日志生成数据采集能力'
          : type === 'run_data_extraction' ? '正在从当前日志提取结构化数据'
          : type === 'create_log_semantic_rule' ? '正在创建日志语义规则'
          : type === 'create_log_anomaly_rule' ? '正在创建日志异常规则'
          : type === 'set_log_semantic_labels' ? '正在更新日志语义标签'
            : type === 'open_log_rule_settings' ? '正在打开日志规则配置'
              : type === 'open_workspace_page' ? '正在切换到目标功能页'
                : '正在调控当前页面';
    // Actions that put a full dialog on screen take the user's attention with them. The panel
    // now floats above the modal layer (so it is reachable on every page), so leaving it open
    // would cover the very window it just opened. Every other action keeps the panel on screen
    // in its narrow "phone" takeover form, so the user can watch the page being driven.
    const takesOverScreen = actions.some((action) => TAKES_OVER_SCREEN_ACTIONS.has(String(action.type || '')));
    const restoreOpen = open && takesOverScreen;
    if (restoreOpen) setOpen(false);
    setFocusTarget(focus);
    setTakeoverAnim(true);
    setTakeover(label);
    const receipts = [];
    let aborted = false;
    try {
      // Let the panel finish sliding right and the page frame light up before anything moves.
      await wait(TAKEOVER_LEAD_IN_MS);
      for (const action of actions) {
        signal?.throwIfAborted();
        receipts.push(await executeUiAction(action, signal));
        if (receipts[receipts.length - 1].status === 'failed') break;
        await wait(TAKEOVER_STEP_MS);
      }
      return receipts[receipts.length - 1];
    } catch (error) {
      if ((error as { name?: string })?.name === 'AbortError') aborted = true;
      throw error;
    } finally {
      if (!aborted) await wait(TAKEOVER_HOLD_MS);
      setTakeover(''); setFocusTarget('');
      // 面板一直在场（手机形态）才播缓慢展开；如果是被收起后重新挂载，
      // 让它走正常的打开动画，别把两套动画叠在一起。
      setTakeoverRestoring(!restoreOpen);
      if (restoreOpen) setOpen(true);
      // 复原态要活到过渡跑完；入场态顺手一起撤掉。
      window.setTimeout(() => { setTakeoverAnim(false); setTakeoverRestoring(false); }, TAKEOVER_RESTORE_MS);
    }
  }

  function patchMessage(conversationId: string, messageId: string, patch: Partial<AssistantMessage>) {
    setConversations((current) => current.map((conversation) => conversation.id === conversationId
      ? {
          ...conversation,
          messages: conversation.messages.map((message) => message.id === messageId ? { ...message, ...patch } : message),
          updatedAt: Date.now(),
        }
      : conversation));
  }

  /**
   * 把本轮结果卡片里的案例草稿取出来，直接当成本条消息的案例草稿。
   *
   * 这就是「一次回答就按这个结论来」：`draft_diagnosis_case` 已经在同一轮里同时产出了
   * 结论和结构化字段，前端不该再让用户点一次按钮去换第二次模型调用。
   */
  function caseDraftFromResultCards(cards?: TraceLensAssistantResultCard[]): AssistantCaseDraftState | undefined {
    const card = (cards || []).find((item) => item.kind === 'case_draft' && item.case_draft_result?.case_draft);
    const result = card?.case_draft_result;
    if (!result?.case_draft) return undefined;
    return { status: 'ready', result };
  }

  /**
   * 「整理成案例」 — deterministic trigger, model-assisted extraction.
   *
   * The button (not the model) decides *when* a conclusion becomes a case; this only tells
   * the backend which part of the conversation to read. Nothing is written to the case
   * library here — the user still confirms in the editor.
   */
  async function requestCaseDraft(conversationId: string, messageId: string, notes = '') {
    const conversation = conversations.find((item) => item.id === conversationId);
    if (!conversation) return;
    const index = conversation.messages.findIndex((item) => item.id === messageId);
    if (index < 0) return;
    const trimmedNotes = notes.trim();
    // Only the messages up to and including this one: a later reply in the same thread
    // must not leak into an earlier conclusion.
    const transcript = conversation.messages.slice(0, index + 1)
      .filter((item) => String(item.content || '').trim())
      .map((item) => ({ role: item.role, content: item.content }));
    if (trimmedNotes) transcript.push({ role: 'user', content: `补充说明（用户核对草稿时追加）：${trimmedNotes}` });
    if (!transcript.length) return;
    patchMessage(conversationId, messageId, { caseDraft: { status: 'loading', notes: trimmedNotes || undefined } });
    try {
      const result = await draftCaseFromConversation({
        messages: transcript,
        context: currentContext(),
      });
      if (!result?.ok) throw new Error(result?.error || '整理失败。');
      patchMessage(conversationId, messageId, { caseDraft: { status: 'ready', notes: trimmedNotes || undefined, result } });
    } catch (error) {
      patchMessage(conversationId, messageId, {
        caseDraft: {
          status: 'failed',
          notes: trimmedNotes || undefined,
          error: error instanceof Error ? error.message : '整理失败。',
        },
      });
    }
  }

  /** Hand the reviewed draft to the one real write path: the case editor. */
  async function saveCaseDraft(result: TraceLensCaseDraftResponse) {
    const receipt = await runUiActions([{
      type: 'open_case_editor',
      draft: result.case_draft,
      evidences: result.evidences,
    }]);
    if (receipt?.status === 'failed') throw new Error(receipt.detail);
  }

  function mergeCurrentPageContext() {
    if (!activeConversation) return;
    const snapshot = currentContext();
    setConversations((current) => current.map((conversation) => conversation.id === activeConversation.id
      ? {
          ...conversation,
          scopeLabel: conversation.scopeLabel || '当前页面',
          scopeContext: captureContextSnapshot(snapshot),
          updatedAt: Date.now(),
        }
      : conversation));
    const pageName = String(snapshot.page_title || snapshot.page || snapshot.route || '当前页面').trim();
    const fullText = `【引用当前页面】\n${JSON.stringify(snapshot, null, 2)}`;
    setComposerReferences((current) => [
      ...current.filter((item) => item.kind !== 'page'),
      { id: createId('composer-ref-page'), kind: 'page' as const, label: `页面 · ${pageName}`, fullText },
    ].slice(-4));
    setComposerMenuOpen(false);
    setContextRevision((current) => current + 1);
    requestAnimationFrame(() => inputRef.current?.focus());
  }

  /** The composer has four popovers; only one may be open at a time. */
  function closeComposerPopovers(keep?: 'capability' | 'attach' | 'policy' | 'model') {
    if (keep !== 'capability') setComposerMenuOpen(false);
    if (keep !== 'attach') setAttachMenuOpen(false);
    if (keep !== 'policy') setExecutionPolicyMenuOpen(false);
    if (keep !== 'model') setModelMenuOpen(false);
  }

  function openAttachMenu() {
    // Collected on demand: a snapshot of the page is only needed when the menu is shown.
    const entry = (collectPageContext() as Record<string, unknown>).selected_entry;
    setAttachSelection(entry && typeof entry === 'object' && Object.keys(entry as object).length ? entry as Record<string, unknown> : null);
    setAttachMenuOpen((current) => {
      const next = !current;
      if (next) closeComposerPopovers('attach');
      return next;
    });
  }

  function quoteSelectedLog() {
    if (!attachSelection) return;
    const label = String(attachSelection.component || attachSelection.level || attachSelection.timestamp || '选中日志').slice(0, 60);
    const fullText = `【引用当前选中日志】\n${JSON.stringify(attachSelection, null, 2)}`;
    setComposerReferences((current) => [
      ...current.filter((item) => item.kind !== 'log'),
      { id: createId('composer-ref-log'), kind: 'log' as const, label: `日志 · ${label}`, fullText },
    ].slice(-4));
    setAttachMenuOpen(false);
    requestAnimationFrame(() => inputRef.current?.focus());
  }

  function stopVoiceTracks() {
    voiceStreamRef.current?.getTracks().forEach((track) => track.stop());
    voiceStreamRef.current = undefined;
    if (voiceTimerRef.current) window.clearInterval(voiceTimerRef.current);
    voiceTimerRef.current = undefined;
  }

  async function handleRecordedVoice(blob: Blob) {
    if (voiceCancelledRef.current) {
      voiceCancelledRef.current = false;
      setVoiceState('idle');
      setVoiceSeconds(0);
      return;
    }
    if (!blob.size) {
      setVoiceState('idle');
      setVoiceError('没有采集到有效语音。');
      return;
    }
    setVoiceState('transcribing');
    setVoiceError('');
    const extension = blob.type.includes('mp4') ? 'm4a' : blob.type.includes('ogg') ? 'ogg' : 'webm';
    try {
      const result = await transcribeTraceLensVoice(blob, `tracepilot-voice-${Date.now()}.${extension}`);
      const text = String(result.text || '').trim();
      if (!text) throw new Error('未识别到语音内容。');
      setInput((current) => current.trim() ? `${current.trim()} ${text}` : text);
      requestAnimationFrame(() => inputRef.current?.focus());
    } catch (error) {
      setVoiceError(error instanceof Error ? error.message : String(error));
    } finally {
      setVoiceState('idle');
      setVoiceSeconds(0);
    }
  }

  async function startVoiceRecording() {
    if (voiceState !== 'idle') return;
    setComposerMenuOpen(false);
    setVoiceError('');
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      setVoiceError('当前浏览器不支持录音，请使用支持 MediaRecorder 的浏览器。');
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      voiceStreamRef.current = stream;
      const preferred = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus']
        .find((value) => MediaRecorder.isTypeSupported(value));
      const recorder = preferred ? new MediaRecorder(stream, { mimeType: preferred }) : new MediaRecorder(stream);
      mediaRecorderRef.current = recorder;
      voiceChunksRef.current = [];
      voiceCancelledRef.current = false;
      recorder.ondataavailable = (event) => { if (event.data.size > 0) voiceChunksRef.current.push(event.data); };
      recorder.onerror = () => {
        stopVoiceTracks();
        setVoiceState('idle');
        setVoiceError('录音失败，请检查麦克风权限。');
      };
      recorder.onstop = () => {
        const mime = recorder.mimeType || voiceChunksRef.current[0]?.type || 'audio/webm';
        const blob = new Blob(voiceChunksRef.current, { type: mime });
        voiceChunksRef.current = [];
        mediaRecorderRef.current = undefined;
        stopVoiceTracks();
        void handleRecordedVoice(blob);
      };
      recorder.start(250);
      setVoiceSeconds(0);
      setVoiceState('recording');
      voiceTimerRef.current = window.setInterval(() => setVoiceSeconds((current) => current + 1), 1000);
    } catch (error) {
      stopVoiceTracks();
      setVoiceState('idle');
      setVoiceError(error instanceof Error ? error.message : '无法访问麦克风。');
    }
  }

  function finishVoiceRecording() {
    const recorder = mediaRecorderRef.current;
    if (voiceState !== 'recording' || !recorder) return;
    if (recorder.state === 'recording') recorder.stop();
  }

  function cancelVoiceRecording() {
    const recorder = mediaRecorderRef.current;
    voiceCancelledRef.current = true;
    if (recorder?.state === 'recording') recorder.stop();
    else {
      stopVoiceTracks();
      setVoiceState('idle');
      setVoiceSeconds(0);
    }
  }

  function stopActiveRun(reason = '当前分析已由用户终止。', mode: 'stopped' | 'interrupted' = 'stopped') {
    const run = activeRunRef.current;
    if (!run) return;
    void cancelTraceLensAssistant(run.runId).catch(() => { /* stream abort is the fallback */ });
    run.controller.abort();
    setTaskQueue([]);
    setTakeover('');
    setTakeoverRestoring(true);
    window.setTimeout(() => { setTakeoverAnim(false); setTakeoverRestoring(false); }, TAKEOVER_RESTORE_MS);
    setFocusTarget('');
    activeRunRef.current = undefined;
    removeAgentTask(run.runId);
    if (abortRef.current === run.controller) abortRef.current = undefined;
    updateMessage(run.conversationId, run.assistantId, (message) => ({
      ...message,
      content: message.content || (mode === 'interrupted' ? '已停止当前路径，准备按你的新指令重新规划。' : '当前分析已按你的要求终止。'),
      task: message.task ? {
        ...message.task,
        status: mode,
        phase: mode,
        title: mode === 'interrupted' ? '已被用户干预' : '已终止当前分析',
        detail: reason,
        traces: message.task.traces.map((trace) => trace.status === 'running' ? { ...trace, status: mode } : trace),
      } : undefined,
    }));
    setSendingConversationId((current) => current === run.conversationId ? '' : current);
  }

  // AI任务与页面生命周期解耦。
  // 页面切换、点击其他功能区、打开日志定位/资源页面时，不再自动中断 Agent。
  // 只有用户点击停止按钮或明确取消任务时，才调用 stopActiveRun。

  const recoveryStarted = useRef(false);
  useEffect(() => {
    if (recoveryStarted.current) return;
    recoveryStarted.current = true;
    const saved = listAgentTasks().filter(t => t.status === 'running');
    const restore = async () => {
      for (const task of saved) {
        const conversation = conversations.find(c => c.id === task.conversationId && (!task.sessionId || c.sessionId === task.sessionId));
        if (conversation) await executeTask('', conversation, '', task);
      }
    };
    void restore();
  }, []);

  function conversationHistorySnapshot(conversation: AssistantConversation) {
    return conversation.messages
      .filter((item) => item.content.trim())
      .slice(-12)
      .map((item) => ({ role: item.role, content: item.role === 'assistant' ? splitAssistantAnswer(item.content).body : item.content }));
  }

  function enqueueTask(text: string, conversationId: string, requestText = text) {
    const task: QueuedAssistantTask = { id: createId('queued-task'), conversationId, text, requestText, createdAt: Date.now() };
    setTaskQueue((current) => [...current, task].slice(-16));
    setInput('');
    setComposerReferences([]);
  }

  function removeQueuedTask(id: string) {
    setTaskQueue((current) => current.filter((item) => item.id !== id));
  }

  function editQueuedTask(task: QueuedAssistantTask) {
    removeQueuedTask(task.id);
    setActiveConversationId(task.conversationId);
    setInput(task.text);
    window.setTimeout(() => inputRef.current?.focus(), 50);
  }

  /** Append a local user bubble without touching the running task's own stream. */
  function appendUserNote(conversationId: string, content: string, interject?: 'pending'): string {
    const id = createId('user');
    setConversations((current) => current.map((conversation) => conversation.id === conversationId
      ? { ...conversation, messages: [...conversation.messages, { id, role: 'user' as const, content, interject }], updatedAt: Date.now() }
      : conversation));
    return id;
  }

  /** Flip this run's outstanding interjections once the backend says it consumed them. */
  function settleInterjections(state: 'applied' | 'missed') {
    const ids = pendingInterjectionsRef.current;
    if (!ids.length) return;
    pendingInterjectionsRef.current = [];
    const pending = new Set(ids);
    setConversations((current) => current.map((conversation) => ({
      ...conversation,
      messages: conversation.messages.map((message) => pending.has(message.id) ? { ...message, interject: state } : message),
    })));
  }

  /**
   * 插话 — hand text to the *running* task as high-priority guidance.
   *
   * The backend already consumes guidance at every plan step (`_drain_run_guidance`) and
   * even emits "已应用用户引导" traces; the API just was never reachable from the UI.
   * Returns false when the run already ended, so callers leave the message queued
   * rather than dropping it.
   */
  async function interject(runId: string, text: string): Promise<boolean> {
    try {
      const result = await guideTraceLensAssistant(runId, text);
      return Boolean(result?.accepted);
    } catch {
      return false;
    }
  }

  async function interjectQueuedTask(task: QueuedAssistantTask) {
    const run = activeRunRef.current;
    if (!run || run.conversationId !== task.conversationId || !task.text.trim()) return;
    setInterjectingTaskId(task.id);
    try {
      if (!await interject(run.runId, task.text)) return; // run already ended: stay queued
      removeQueuedTask(task.id);
      pendingInterjectionsRef.current.push(appendUserNote(task.conversationId, `【插话】${task.text}`, 'pending'));
    } finally {
      setInterjectingTaskId('');
    }
  }

  /** ⌘/Ctrl+Enter: push everything waiting (plus the composer text, if any) into the live run. */
  async function interjectAllQueued() {
    const run = activeRunRef.current;
    if (!run) return;
    const pending = taskQueue.filter((item) => item.conversationId === run.conversationId && item.text.trim());
    const composerText = input.trim();
    const texts = [...pending.map((item) => item.text.trim()), ...(composerText ? [composerText] : [])];
    if (!texts.length) return;
    setInterjectingTaskId('__all__');
    try {
      if (!await interject(run.runId, texts.join('；'))) return;
      const ids = new Set(pending.map((item) => item.id));
      setTaskQueue((current) => current.filter((item) => !ids.has(item.id)));
      texts.forEach((text) => pendingInterjectionsRef.current.push(appendUserNote(run.conversationId, `【插话】${text}`, 'pending')));
      if (composerText) {
        setInput('');
        setComposerReferences([]);
      }
    } finally {
      setInterjectingTaskId('');
    }
  }

  async function executeTask(text: string, conversation: AssistantConversation, requestText = text, resume?: AgentTaskSnapshot) {
    const conversationId = conversation.id;
    const skillId = conversation.skillId;
    const historySnapshot = conversationHistorySnapshot(conversation);
    const userMessage: AssistantMessage = { id: createId('user'), role: 'user', content: text };
    const assistantId = resume?.assistantId || createId('assistant');
    const assistantMessage: AssistantMessage = {
      id: assistantId,
      role: 'assistant',
      content: '',
      task: { status: 'running', phase: 'planning', title: '正在处理任务', detail: '开始读取当前页面与可用能力', traces: [] },
      confirmations: [],
    };
    setConversationMessages(conversationId, (current) => resume ? current.map(m => m.id === assistantId ? assistantMessage : m) : [...current, userMessage, assistantMessage]);
    setTraceOpen((current) => ({ ...current, [assistantId]: true }));
    setSendingConversationId(conversationId);
    const controller = new AbortController();
    const runId = resume?.runId || createId('run');
    abortRef.current?.abort();
    abortRef.current = controller;
    activeRunRef.current = { runId, conversationId, assistantId, controller };
    pendingInterjectionsRef.current = [];
    saveAgentTask({ runId, sessionId:conversation.sessionId, conversationId, assistantId, status: 'running', updatedAt: Date.now() });
    // Every turn carries the page the user is on *right now*.
    //
    // Previously the conversation froze the snapshot it was created with ("a new page only
    // joins through explicit 引用当前页面"), so a chat started on 日志定位 kept sending 用例
    // context no matter where you navigated next. Live per-turn context is what makes
    // "switch to 用例分析 and keep talking" work — and it is also why no snapshot is taken
    // until a turn actually happens.
    const scopedContext = captureContextSnapshot(currentContext());
    setConversations((items) => items.map((item) => item.id === conversationId
      ? { ...item, scopeContext: scopedContext, updatedAt: Date.now() }
      : item));
    const requestContext: Record<string, unknown> = {
      ...scopedContext, ui_receipts: true,
      session_id: conversation.sessionId,
      assistant_scope: {...scopedContext, scope_key: conversation.scopeKey || '', scope_label: conversation.scopeLabel || ''},
    };

    try {
      await streamTraceLensAssistant(
        { message: requestText, history: historySnapshot, context: requestContext, skill_id: skillId, memory: conversation.memory || '', run_id: runId, session_id: conversation.sessionId, model: modelChoice.model, effort: modelChoice.effort },
        {
          resumeTaskId: resume?.runId,
          signal: controller.signal,
          onEvent: async (event) => {
            if (controller.signal.aborted) return;
            if (event.type === 'token_reset') {
              updateMessage(conversationId, assistantId, (message) => ({ ...message, content: event.text }));
              return;
            }
            if (event.type === 'token') {
              updateMessage(conversationId, assistantId, (message) => ({ ...message, content: `${message.content}${event.delta}` }));
              return;
            }
            if (event.type === 'token_usage') {
              updateMessage(conversationId, assistantId, (message) => {
                const previous = message.tokenUsage || { inputTokens: 0, outputTokens: 0, totalTokens: 0, estimated: false };
                const inputTokens = Math.max(0, Number(event.usage.input_tokens || 0));
                const outputTokens = Math.max(0, Number(event.usage.output_tokens || 0));
                const totalTokens = Math.max(0, Number(event.usage.total_tokens || inputTokens + outputTokens));
                return {
                  ...message,
                  tokenUsage: {
                    inputTokens: previous.inputTokens + inputTokens,
                    outputTokens: previous.outputTokens + outputTokens,
                    totalTokens: previous.totalTokens + totalTokens,
                    estimated: previous.estimated || Boolean(event.usage.estimated),
                  },
                };
              });
              return;
            }
            if (event.type === 'agent_start') {
              updateMessage(conversationId, assistantId, (message) => ({
                ...message,
                task: {
                  status: 'running',
                  phase: 'routing',
                  title: event.title,
                  detail: event.detail,
                  progress: event.percentage,
                  traces: message.task?.traces || [],
                },
              }));
              return;
            }
            if (event.type === 'task') {
              // The backend announces guidance consumption with phase 'guidance', on both
              // the "plan" and the "redirect mid-execution" paths.
              if (event.phase === 'guidance') settleInterjections('applied');
              updateMessage(conversationId, assistantId, (message) => ({
                ...message,
                task: {
                  status: event.status,
                  phase: event.phase,
                  title: event.title,
                  detail: event.detail,
                  progress: typeof event.progress === 'number' ? event.progress : message.task?.progress,
                  currentStep: message.task?.currentStep,
                  totalSteps: message.task?.totalSteps,
                  traces: message.task?.traces || [],
                },
              }));
              return;
            }
            if (event.type === 'thinking' || event.type === 'progress' || event.type === 'tool_call' || event.type === 'tool_result') {
              updateMessage(conversationId, assistantId, (message) => {
                const traces = [...(message.task?.traces || [])];
                const stage = event.type === 'tool_call' || event.type === 'tool_result' ? 'tool' : event.type === 'thinking' ? 'thinking' : 'evidence';
                const status = String(event.status || (event.type === 'tool_result' ? 'success' : 'running'));
                const trace: TraceLensAssistantTrace = {
                  id: event.id,
                  ...('input' in event ? { input: event.input } : {}),
                  ...('output' in event ? { output: event.output } : {}),
                  // Without this the step could only say "读取环境信息"; now it can name the
                  // tool that is actually running, which is what makes a long run auditable.
                  ...('tool_id' in event && event.tool_id ? { tool_id: String(event.tool_id) } : {}),
                  stage,
                  title: event.title,
                  status,
                  detail: event.detail,
                };
                const existing = traces.findIndex((item) => item.id === event.id);
                if (existing >= 0) traces[existing] = { ...traces[existing], ...trace };
                else traces.push(trace);
                const percentage = 'percentage' in event && typeof event.percentage === 'number' ? event.percentage : message.task?.progress;
                const currentStep = event.type === 'progress' && typeof event.current_step === 'number' ? event.current_step : message.task?.currentStep;
                const totalSteps = event.type === 'progress' && typeof event.total_steps === 'number' ? event.total_steps : message.task?.totalSteps;
                return {
                  ...message,
                  task: {
                    status: message.task?.status || 'running',
                    phase: String(('stage' in event ? event.stage : '') || stage),
                    title: event.title,
                    detail: event.detail,
                    progress: percentage,
                    currentStep,
                    totalSteps,
                    traces,
                  },
                };
              });
              return;
            }
            if (event.type === 'trace') {
              updateMessage(conversationId, assistantId, (message) => {
                const traces = [...(message.task?.traces || [])].filter((trace) => trace.title !== 'AI 规划' && trace.stage !== 'llm');
                if (event.trace.title === 'AI 规划' || event.trace.stage === 'llm') return message;
                const existing = traces.findIndex((item) => item.id === event.trace.id);
                if (existing >= 0) traces[existing] = { ...traces[existing], ...event.trace };
                else traces.push(event.trace);
                return {
                  ...message,
                  task: message.task
                    ? { ...message.task, traces }
                    : { status: 'running', phase: event.trace.stage, title: event.trace.title, traces },
                };
              });
              return;
            }
            if (event.type === 'confirmation') {
              updateMessage(conversationId, assistantId, (message) => ({
                ...message,
                confirmations: [...(message.confirmations || []).filter((item) => item.confirmation_token !== event.confirmation.confirmation_token), event.confirmation],
              }));
              return;
            }
            if (event.type === 'ui_action') {
              const id = `ui-${event.action_id || createId('action')}`;
              updateMessage(conversationId, assistantId, (message) => ({ ...message, task: message.task ? {
                ...message.task, traces: [...message.task.traces, { id, stage: 'ui', title: '正在操作共享页面', status: 'running', detail: String(event.action.type || '') }],
              } : undefined }));
              const receipt = await runUiActions([event.action], controller.signal);
              if (receipt?.status === 'success') {
                const snapshot = captureContextSnapshot();
                setConversations(current => current.map(c => c.id === conversationId ? {...c, scopeContext:snapshot} : c));
              }
              if (event.action_id && receipt) await acknowledgeAssistantUi(runId, event.action_id, { ...receipt, context: currentContext() });
              updateMessage(conversationId, assistantId, (message) => ({ ...message, task: message.task ? {
                ...message.task, traces: message.task.traces.map((trace) => trace.id === id ? { ...trace, title: '页面操作回执', status: receipt?.status || 'failed', detail: receipt?.detail, output: receipt } : trace),
              } : undefined }));
              return;
            }
            if (event.type === 'done') {
              settleInterjections('missed');
              const resultCards = Array.isArray(event.result_cards) ? event.result_cards : undefined;
              // 本轮分析已经产出案例草稿时，直接挂到消息上：用户看到的是「结论 + 可导入的
              // 案例草稿」，而不是还要再点一次「整理成案例」去换第二次模型调用。
              const draftedCase = caseDraftFromResultCards(resultCards);
              updateMessage(conversationId, assistantId, (message) => ({
                ...message,
                content: event.message || message.content || '任务已处理。',
                task: message.task ? { ...message.task, status: message.task.status === 'confirm' ? 'confirm' : 'done', progress: 100 } : message.task,
                resultCards: resultCards || message.resultCards,
                caseDraft: draftedCase && !message.caseDraft ? draftedCase : message.caseDraft,
                suggestions: Array.isArray(event.suggested_actions) ? event.suggested_actions.map(String).filter(Boolean).slice(0, 3) : message.suggestions,
              }));
              if (event.memory) {
                setConversations((current) => current.map((item) => item.id === conversationId
                  ? { ...item, memory: event.memory!.slice(0, 3200), updatedAt: Date.now() }
                  : item));
              }
              return;
            }
            if (event.type === 'error') {
              updateMessage(conversationId, assistantId, (message) => ({
                ...message,
                content: message.content || event.message,
                task: message.task ? { ...message.task, status: 'failed', title: '任务执行失败', detail: event.message } : undefined,
              }));
            }
          },
        },
      );
    } catch (error) {
      if (!(error instanceof DOMException && error.name === 'AbortError')) {
        const messageText = error instanceof Error ? error.message : String(error);
        updateMessage(conversationId, assistantId, (message) => ({
          ...message,
          content: message.content || messageText,
          task: message.task ? { ...message.task, status: 'failed', title: '任务执行失败', detail: messageText } : undefined,
        }));
      }
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = undefined;
        activeRunRef.current = undefined;
        removeAgentTask(runId);
        setSendingConversationId((current) => current === conversationId ? '' : current);
      }
    }
  }

  async function send(textOverride?: string) {
    const text = typeof textOverride === 'string' ? textOverride.trim() : input.trim();
    if (!text || !activeConversation) return;
    if (activeRunRef.current && /^(停|停止|停下|暂停|取消|stop)[。！!]?$/i.test(text)) {
      stopActiveRun('已按你的指令停止当前操作。'); setInput(''); return;
    }
    const references = typeof textOverride === 'string' ? [] : composerReferences;
    const referenceContext = references.map((item) => item.fullText).join('\n\n');
    const requestText = referenceContext ? `${referenceContext}\n\n【用户输入】\n${text}` : text;
    if (sendingConversationId || activeRunRef.current) {
      enqueueTask(text, activeConversation.id, requestText);
      return;
    }
    const pending = latestPendingConfirmation(activeConversation);
    if (pending && looksLikeConfirmationReply(text)) {
      setInput('');
      setComposerReferences([]);
      await confirmAction(activeConversation.id, pending.messageId, pending.item, text, true);
      return;
    }
    setInput('');
    setComposerReferences([]);
    await executeTask(text, activeConversation, requestText);
  }


  async function confirmAction(
    conversationId: string,
    messageId: string,
    item: TraceLensAssistantConfirmation,
    instruction = '',
    appendUserMessage = false,
  ) {
    if (confirming) return;
    const progressId = createId('assistant-confirm-run');
    const progressCopy = confirmationToolProgress(item, instruction);
    const progressTitle = progressCopy.title;
    setConfirming(item.confirmation_token);
    setConversationMessages(conversationId, (current) => [
      ...current,
      ...(appendUserMessage ? [{ id: createId('user-confirm'), role: 'user' as const, content: instruction }] : []),
      {
        id: progressId,
        role: 'assistant' as const,
        content: '',
        task: {
          status: 'running',
          phase: 'tool',
          title: progressTitle,
          detail: progressCopy.detail,
          traces: [{ id: `${progressId}-tool`, stage: 'tool', title: progressCopy.title, status: 'running', detail: progressCopy.detail }],
        },
      },
    ]);
    setTraceOpen((current) => ({ ...current, [progressId]: true }));
    try {
      const result = await confirmTraceLensAssistant(item.confirmation_token, { instruction, context: currentContext() });
      const receipt = await runUiActions(result.ui_actions || []);
      if (receipt?.status === 'failed') throw new Error(receipt.detail);
      updateMessage(conversationId, progressId, (message) => ({
        ...message,
        content: result.message || `${item.tool_name} 已执行。`,
        task: message.task ? {
          ...message.task,
          status: 'done',
          title: item.tool_name,
          detail: result.message || '已完成',
          traces: message.task.traces.map((trace) => ({ ...trace, status: 'success', detail: result.message || '已完成' })),
        } : undefined,
      }));
      updateMessage(conversationId, messageId, (message) => ({
        ...message,
        confirmations: (message.confirmations || []).filter((value) => value.confirmation_token !== item.confirmation_token),
      }));
    } catch (error) {
      const errorText = error instanceof Error ? error.message : String(error);
      updateMessage(conversationId, progressId, (message) => ({
        ...message,
        content: errorText,
        task: message.task ? {
          ...message.task,
          status: 'failed',
          title: `${item.tool_name}执行失败`,
          detail: errorText,
          traces: message.task.traces.map((trace) => ({ ...trace, status: 'failed', detail: errorText })),
        } : undefined,
      }));
    } finally {
      setConfirming('');
    }
  }

  function cancelAction(conversationId: string, messageId: string, token: string) {
    updateMessage(conversationId, messageId, (message) => ({
      ...message,
      confirmations: (message.confirmations || []).filter((value) => value.confirmation_token !== token),
    }));
  }

  function openAssistant() {
    if (closeTimerRef.current) window.clearTimeout(closeTimerRef.current);
    setClosing(false);
    setOpen(true);
  }

  function requestCloseAssistant() {
    if (closing) return;
    setComposerMenuOpen(false);
    setClosing(true);
    if (closeTimerRef.current) window.clearTimeout(closeTimerRef.current);
    closeTimerRef.current = window.setTimeout(() => {
      setOpen(false);
      setClosing(false);
      closeTimerRef.current = undefined;
    }, 220);
  }

  function beginFabDrag(event: React.PointerEvent<HTMLButtonElement>) {
    if (open) return;
    fabDraggedRef.current = false;
    fabDragRef.current = { pointerId: event.pointerId, x: event.clientX, y: event.clientY, left: fabPosition.left, top: fabPosition.top };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function moveFab(event: React.PointerEvent<HTMLButtonElement>) {
    const drag = fabDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    if (Math.hypot(dx, dy) > 4) fabDraggedRef.current = true;
    if (!fabDraggedRef.current) return;
    setFabPosition(clampFabPosition({ left: drag.left + dx, top: drag.top + dy }));
    event.preventDefault();
  }

  function endFabDrag(event: React.PointerEvent<HTMLButtonElement>) {
    const drag = fabDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    fabDragRef.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  }

  function handleFabClick(event: React.MouseEvent<HTMLButtonElement>) {
    if (fabDraggedRef.current) {
      fabDraggedRef.current = false;
      event.preventDefault();
      return;
    }
    openAssistant();
  }

  function beginDrag(event: React.MouseEvent<HTMLElement>) {
    if (docked) return;
    if ((event.target as HTMLElement).closest('button,select,input,textarea')) return;
    const activeLayout = layoutMode === 'phone' ? mobileLayout : layout;
    dragRef.current = { mode: 'drag', layoutMode, x: event.clientX, y: event.clientY, layout: activeLayout };
    event.preventDefault();
  }

  function beginResize(event: React.MouseEvent<HTMLDivElement>) {
    if (layoutMode !== 'floating') return;
    dragRef.current = { mode: 'resize', layoutMode: 'floating', x: event.clientX, y: event.clientY, layout };
    event.preventDefault();
    event.stopPropagation();
  }

  if (!activeConversation) return null;

  return (
    <>
      {takeover && (
        <div className="ai-takeover-mask" aria-live="polite">
          <div className="ai-takeover-status" aria-label="AI 接管">
            <AiAgentIcon className="ai-agent-icon" />
            <span className="ai-takeover-hint">页面正在被托管</span>
            <strong>{takeover}</strong>
          </div>
        </div>
      )}

      {!open && (
        <button
          type="button"
          className={`ai-assistant-fab ai-fab-movable ${sendingConversationId ? 'is-running' : ''}`}
          style={{ left: fabPosition.left, top: fabPosition.top }}
          onPointerDown={beginFabDrag}
          onPointerMove={moveFab}
          onPointerUp={endFabDrag}
          onPointerCancel={endFabDrag}
          onClick={handleFabClick}
          title={sendingConversationId ? 'TracePilot 执行中 · 点击恢复' : '打开 TracePilot'}
          aria-label={sendingConversationId ? 'TracePilot 正在执行，点击恢复界面' : '打开 TracePilot'}
        >
          <AiAgentIcon className="ai-agent-icon" />
          {sendingConversationId && <span className="ai-fab-running-indicator" aria-hidden="true"><i/><i/><i/></span>}
        </button>
      )}

      {open && (() => {
        const panelBox = layoutMode === 'phone' ? mobileLayout : layout;
        const panelStyle = {
          ...panelBox,
          '--ai-panel-origin-x': `${fabPosition.left + 29 - panelBox.left}px`,
          '--ai-panel-origin-y': `${fabPosition.top + 29 - panelBox.top}px`,
        } as CSSProperties;
        return (
        <aside
          className={`ai-assistant-panel ai-cockpit-panel layout-${layoutMode} ${takeover ? 'is-takeover' : ''} ${takeoverAnim ? 'is-takeover-anim' : ''} ${takeoverRestoring ? 'is-takeover-restoring' : ''} ${docked ? 'workstation-sidecar' : ''} ${docked && !dockFits ? 'dock-overlay' : ''} ${closing ? 'is-closing' : 'is-opening'}`}
          data-takeover={takeover || undefined}
          aria-label="TracePilot 智能工作台"
          style={panelStyle}
        >
          <header className="ai-assistant-header ai-cockpit-header" onMouseDown={beginDrag}>
            <div className="ai-assistant-title">
              <span className="ai-assistant-avatar"><AiAgentIcon className="ai-agent-icon" /></span>
              <div><strong>TracePilot</strong></div>
            </div>
            <div className="ai-cockpit-header-actions">
              <div className="ai-cockpit-layout-switch" role="group" aria-label="助手布局">
                <button
                  type="button"
                  className={`ai-cockpit-layout-toggle ${layoutMode === 'floating' ? 'active' : ''}`}
                  aria-pressed={layoutMode === 'floating'}
                  title="悬浮窗口：可拖动、可缩放，不占用日志区宽度"
                  aria-label="悬浮窗口"
                  onClick={() => setLayoutMode('floating')}
                ><AppWindow size={17} /></button>
                <button
                  type="button"
                  className={`ai-cockpit-layout-toggle ${docked ? 'active' : ''}`}
                  aria-pressed={docked}
                  title={dockFits ? '侧边停靠：紧靠右侧并挤压日志区，左右协同' : '侧边停靠：当前窗口过窄，将覆盖在日志区上方'}
                  aria-label="侧边停靠并挤压日志区"
                  onClick={() => setLayoutMode('docked')}
                ><PanelRight size={17} /></button>
                <button
                  type="button"
                  className={`ai-cockpit-layout-toggle ${layoutMode === 'phone' ? 'active' : ''}`}
                  aria-pressed={layoutMode === 'phone'}
                  title="手机比例：窄面板，便于单栏阅读"
                  aria-label="手机比例面板"
                  onClick={() => setLayoutMode('phone')}
                ><Smartphone size={17} /></button>
              </div>
              {layoutMode !== 'floating' && (
                <button
                  type="button"
                  className={`ai-cockpit-history-toggle ${mobileHistoryOpen ? 'active' : ''}`}
                  onClick={() => setMobileHistoryOpen((current) => !current)}
                  title="历史对话"
                  aria-label="展开或折叠历史对话"
                  aria-expanded={mobileHistoryOpen}
                ><History size={17} /></button>
              )}
              {layoutMode === 'floating' && <GripVertical size={17} aria-hidden="true" />}
              <button type="button" className="ai-assistant-close" onClick={requestCloseAssistant} aria-label="缩小 TracePilot"><X size={18} /></button>
            </div>
          </header>

          <div className="ai-cockpit-body">
            <aside className={`ai-cockpit-sidebar ${mobileHistoryOpen ? 'mobile-open' : ''}`}>
              <button type="button" className="ai-cockpit-new" onClick={() => { newConversation(); setMobileHistoryOpen(false); }}><Plus size={15} />新建对话</button>
              <div className="ai-cockpit-history-title"><History size={14} /><span>历史对话</span></div>
              <div className="ai-cockpit-history-list">
                {conversations.map((conversation) => (
                  <div key={conversation.id} className={`ai-cockpit-history-item ${conversation.id === activeConversation.id ? 'active' : ''} ${conversation.id === sendingConversationId ? 'is-running' : ''}`}>
                    <button type="button" onClick={() => { setActiveConversationId(conversation.id); setMobileHistoryOpen(false); }}>
                      <strong>{conversation.title}</strong><small>{String(conversation.scopeContext?.question || conversation.messages?.find((message) => message.role === 'user')?.content || conversation.scopeContext?.page_label || conversation.scopeContext?.page || '新会话').slice(0, 28)}{conversation.scopeContext?.environment_name ? ` · ${conversation.scopeContext.environment_name}` : ''}</small>
                      {conversation.id === sendingConversationId && (() => {
                        // The running conversation must be identifiable from the history list
                        // alone — you can switch away, let it work, and still see it is alive.
                        const lastTask = conversation.messages.at(-1)?.task;
                        return (
                          <small className="ai-history-running">
                            <span className="ai-history-running-dots" aria-hidden="true"><i /><i /><i /></span>
                            执行中 · {lastTask?.title || '处理中'} · {lastTask?.progress || 0}%
                          </small>
                        );
                      })()}
                    </button>
                    <button type="button" className="ai-cockpit-history-delete" onClick={() => deleteConversation(conversation.id)} aria-label="删除对话"><Trash2 size={13} /></button>
                  </div>
                ))}
              </div>
            </aside>
            {layoutMode !== 'floating' && mobileHistoryOpen && (
              <button type="button" className="ai-cockpit-history-backdrop" aria-label="收起历史对话" onClick={() => setMobileHistoryOpen(false)} />
            )}

            <section className="ai-cockpit-main">
              {docked && !dockFits && (
                <div className="ai-dock-notice" role="status">
                  <AlertTriangle size={13} />
                  <span>窗口较窄，侧栏改为覆盖显示，未挤压日志区。放大窗口即可并排协同。</span>
                  <button type="button" onClick={() => setLayoutMode('floating')}>切浮动</button>
                </div>
              )}
              <details className="ai-context-snapshot">
                <summary>
                  当前页面上下文 · {String(liveContext.page_label || liveContext.page || '未采集')}
                  <span className="ctx-live-dot" title="随页面自动刷新">自动</span>
                </summary>
                <p className="ctx-sub">
                  {String(liveContext.environment_name || liveContext.environment_id || '未选择环境')}
                  {' · '}
                  {String(liveContext.client_local_time || '')}
                </p>
                <div className="ctx-tree">
                  <ContextValue value={liveContext} />
                </div>
              </details>
              <div className="ai-assistant-messages" ref={scrollRef}>
                {!messages.length && (
                  <div className="ai-assistant-welcome">
                    <AiAgentIcon className="ai-assistant-welcome-icon" />
                    <strong>让 TracePilot 帮你操作</strong>
                    <span>直接描述你的目标即可，TracePilot 会执行对应操作，并实时展示处理过程。</span>
                  </div>
                )}
                {messages.map((message) => (
                  <div key={message.id} className={`ai-assistant-message ${message.role}`}>
                    {message.role === 'assistant' && message.task && (
                      <TaskTrace
                        message={message}
                        open={message.task.status === 'running' || Boolean(traceOpen[message.id])}
                        onToggle={() => setTraceOpen((current) => ({ ...current, [message.id]: !current[message.id] }))}
                      />
                    )}
                    {message.content && (
                      <div className="ai-assistant-bubble">
                        {message.role === 'assistant'
                          ? <AssistantRichContent content={message.content} extraNext={message.suggestions} disabled={Boolean(sendingConversationId)} onNext={(action) => void send(action)} />
                          : message.content}
                        {message.role === 'assistant' && message.task?.status === 'running' && <span className="ai-stream-caret" aria-hidden="true" />}
                      </div>
                    )}
                    {message.interject && (
                      <span className={`ai-interject-state ${message.interject}`} role="status">
                        {message.interject === 'pending' && <><LoaderCircle className="spin" size={11}/>等待本轮采纳</>}
                        {message.interject === 'applied' && <><Check size={11}/>本轮已采纳</>}
                        {message.interject === 'missed' && <><AlertTriangle size={11}/>本轮已结束，未采纳</>}
                      </span>
                    )}
                    {message.role === 'assistant' && Array.isArray(message.resultCards) && message.resultCards.length > 0 && (
                      <AssistantResultCards
                        cards={message.resultCards}
                        disabled={Boolean(sendingConversationId)}
                        onUiAction={(action) => runUiActions([action])}
                        onPrompt={(prompt) => void send(prompt)}
                      />
                    )}
                    {message.role === 'assistant' && String(message.content || '').trim() && message.task?.status !== 'running' && !message.caseDraft && (
                      <div className="ai-message-tools">
                        <button
                          type="button"
                          className="ai-message-tool"
                          disabled={Boolean(sendingConversationId)}
                          onClick={() => void requestCaseDraft(activeConversation.id, message.id)}
                        >
                          <FileText size={12} /> 整理成案例
                        </button>
                      </div>
                    )}
                    {message.role === 'assistant' && message.caseDraft && (
                      <AssistantCaseDraftCard
                        state={message.caseDraft}
                        disabled={Boolean(sendingConversationId)}
                        onRegenerate={(notes) => void requestCaseDraft(activeConversation.id, message.id, notes)}
                        onSave={(result) => void saveCaseDraft(result)}
                        onDismiss={() => patchMessage(activeConversation.id, message.id, { caseDraft: undefined })}
                      />
                    )}
                    {(message.confirmations || []).map((item) => (
                      <div className="ai-assistant-confirm" key={item.confirmation_token}>
                        <div className="ai-assistant-confirm-title"><ShieldCheck size={17} /><strong>需要确认：{item.tool_name}</strong></div>
                        <p>{confirmationText(item)}</p>
                        {Array.isArray(item.summary.commands) && item.summary.commands.length > 0 && (
                          <details><summary>查看命令预览</summary><pre>{(item.summary.commands as Array<Record<string, unknown>>).map((command) => `${String(command.name || command.key || '')}\n${String(command.command || '')}`).join('\n\n')}</pre></details>
                        )}
                        <div className="ai-assistant-confirm-actions">
                          <button type="button" className="button ghost compact" disabled={Boolean(confirming)} onClick={() => cancelAction(activeConversation.id, message.id, item.confirmation_token)}>取消</button>
                          <button type="button" className="button primary compact" disabled={Boolean(confirming)} onClick={() => void confirmAction(activeConversation.id, message.id, item)}>
                            {confirming === item.confirmation_token ? <LoaderCircle className="spin" size={14} /> : <ShieldCheck size={14} />} 确认执行
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                ))}
              </div>

              <div className="ai-cockpit-compose">
                <div className="ai-cockpit-next-actions ai-capability-prompts" aria-label="快捷操作建议">
                  {composerSuggestions.map((action) => (
                    <button key={action} type="button" onClick={() => void send(action)}>{action}</button>
                  ))}
                </div>
                {activeQueuedTasks.length > 0 && <div className="ai-task-queue" aria-label="待执行消息队列">
                  <div className="ai-task-queue-head">
                    <span><LoaderCircle className="spin" size={13}/> {activeQueuedTasks.length} 条消息排队中</span>
                    <small>{canInterject ? '当前分析结束后按顺序继续，或直接插话' : '当前任务完成后按顺序继续'}</small>
                  </div>
                  {activeQueuedTasks.map((task, index) => (
                    <div className={`ai-task-queue-item ${interjectingTaskId === task.id ? 'is-busy' : ''}`} key={task.id}>
                      <span className="ai-task-queue-index" aria-hidden="true">{index + 1}</span>
                      <button type="button" className="ai-task-queue-text" onClick={() => editQueuedTask(task)} title="取回输入框编辑">
                        <span>{task.text}</span>
                      </button>
                      <div className="ai-task-queue-actions">
                        <button type="button" onClick={() => editQueuedTask(task)} aria-label="取回输入框编辑" title="取回输入框编辑"><Pencil size={13}/></button>
                        <button type="button" className="danger" onClick={() => removeQueuedTask(task.id)} aria-label="移除排队消息" title="移除"><Trash2 size={13}/></button>
                        <button
                          type="button"
                          className="interject"
                          onClick={() => void interjectQueuedTask(task)}
                          disabled={!canInterject || Boolean(interjectingTaskId)}
                          aria-label="插话发送"
                          title={canInterject ? '插话：立即并入正在执行的分析' : '当前没有正在执行的分析'}
                        >
                          {interjectingTaskId === task.id ? <LoaderCircle className="spin" size={13}/> : <ArrowUp size={13}/>}
                        </button>
                      </div>
                    </div>
                  ))}
                </div>}
                {(voiceState !== 'idle' || voiceError) && (
                  <div className={`ai-voice-status ${voiceState}`} role="status">
                    {voiceState === 'recording' && <><i aria-hidden="true" /><span>正在录音 {Math.floor(voiceSeconds / 60)}:{String(voiceSeconds % 60).padStart(2, '0')}</span><button type="button" onClick={cancelVoiceRecording}>取消</button><button type="button" className="primary" onClick={finishVoiceRecording}>完成</button></>}
                    {voiceState === 'transcribing' && <><LoaderCircle className="spin" size={13}/><span>正在通过本地 STT 识别…</span></>}
                    {voiceState === 'idle' && voiceError && <><AlertTriangle size={13}/><span>{voiceError}</span><button type="button" onClick={() => setVoiceError('')}>关闭</button></>}
                  </div>
                )}
                <div className={`ai-assistant-input-wrap gpt-compose ${sending ? 'is-running' : ''}`}>
                  <div className="ai-compose-editor">
                    {selectedSkill !== 'auto' && (() => {
                      const skill = skillCatalog.find((item) => item.id === selectedSkill) || SKILLS.find((item) => item.id === selectedSkill);
                      return skill ? (
                        <button type="button" className="ai-compose-reference skill" title={skill.description} onClick={() => selectComposerSkill('auto')} aria-label={`移除技能引用 ${skill.name}`}>
                          <span>@{skill.name}</span><X size={12} />
                        </button>
                      ) : null;
                    })()}
                    {composerReferences.map((reference) => (
                      <button
                        key={reference.id}
                        type="button"
                        className={`ai-compose-reference ${reference.kind}`}
                        title={reference.label}
                        onClick={() => setComposerReferences((current) => current.filter((item) => item.id !== reference.id))}
                        aria-label={`移除引用 ${reference.label}`}
                      >
                        <span>{reference.label}</span><X size={12} />
                      </button>
                    ))}
                    <textarea
                      ref={inputRef}
                      value={input}
                      onChange={(event) => {
                        const value = event.target.value;
                        setInput(layoutMode === 'phone' ? value.replace(/\s*\n+\s*/g, ' ') : value);
                        // `/` opens the capability/command menu, `@` opens the reference menu,
                        // so the placeholder describes something that actually works.
                        const trigger = value.slice(-1);
                        if (trigger === '/') { closeComposerPopovers('capability'); setComposerMenuOpen(true); }
                        else if (trigger === '@') { openAttachMenu(); }
                        else if (trigger === ' ') { closeComposerPopovers(); }
                      }}
                      onFocus={() => closeComposerPopovers()}
                      onKeyDown={(event) => {
                        if (event.key === 'Escape') { closeComposerPopovers(); return; }
                        // ⌘/Ctrl+Enter 插话：push everything queued (plus this input) into
                        // the analysis that is running right now.
                        if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
                          event.preventDefault();
                          if (activeRunRef.current) void interjectAllQueued();
                          else void send();
                          return;
                        }
                        if (event.key === 'Enter' && !event.shiftKey) {
                          event.preventDefault();
                          void send();
                        }
                      }}
                      placeholder={sending
                        ? `执行中 · 输入即插话${activeQueuedTasks.length ? ` · ${activeQueuedTasks.length} 条待插话` : ''} · ⌘/Ctrl+Enter 全部插话`
                        : layoutMode === 'floating'
                          ? '发消息或创建任务，/ 调用指令，@ 引用页面或日志'
                          : '发消息，/ 指令，@ 引用'}
                      rows={1}
                    />
                  </div>

                  <div className="ai-compose-toolbar">
                    <div className="ai-compose-toolbar-group">
                      <div className="ai-compose-popover-wrap">
                        <button
                          type="button"
                          className={`ai-compose-tool ${composerMenuOpen ? 'active' : ''}`}
                          onClick={() => { const next = !composerMenuOpen; if (next) closeComposerPopovers('capability'); setComposerMenuOpen(next); }}
                          aria-label="添加能力或指令" title="添加能力或指令（/）" aria-expanded={composerMenuOpen}
                        >
                          <Plus size={18} strokeWidth={2.1} />
                        </button>
                        {composerMenuOpen && (
                          <div className="ai-compose-menu ai-capability-menu">
                            <div className="ai-compose-menu-title">添加能力</div>
                            {skillCatalog.filter((skill) => skill.id !== 'auto').map((skill) => (
                              <button
                                key={skill.id}
                                type="button"
                                className={skill.id === selectedSkill ? 'active' : ''}
                                onClick={() => selectComposerSkill(skill.id as SkillId)}
                              >
                                <span><strong>{skill.name}</strong><small>{skill.description}</small></span>
                                {skill.id === selectedSkill && <Check size={14} />}
                              </button>
                            ))}
                            <div className="ai-compose-menu-divider" />
                            <button type="button" onClick={mergeCurrentPageContext}><span><strong>引用当前页面</strong><small>带入当前环境、时间窗、组件和筛选结果</small></span></button>
                            <button type="button" onClick={() => { setComposerMenuOpen(false); newConversation(); }}><span><strong>新建对话</strong><small>保留当前页面，不继承历史对话</small></span></button>
                          </div>
                        )}
                      </div>

                      <div className="ai-compose-popover-wrap">
                        <button
                          type="button"
                          className={`ai-compose-tool ${attachMenuOpen ? 'active' : ''} ${composerReferences.length ? 'has-badge' : ''}`}
                          onClick={openAttachMenu}
                          aria-label="添加引用或附件" title="添加引用或附件（@）" aria-expanded={attachMenuOpen}
                        >
                          <Paperclip size={17} strokeWidth={2.1} />
                          {composerReferences.length > 0 && <span className="ai-compose-tool-badge">{composerReferences.length}</span>}
                        </button>
                        {attachMenuOpen && (
                          <div className="ai-compose-menu ai-capability-menu ai-attach-menu">
                            <div className="ai-compose-menu-title">引用与附件</div>
                            <button type="button" onClick={mergeCurrentPageContext}><span><strong>引用当前页面快照</strong><small>环境、时间窗、组件与当前筛选结果</small></span></button>
                            <button type="button" disabled={!attachSelection} onClick={quoteSelectedLog}>
                              <span>
                                <strong>引用当前选中的日志</strong>
                                <small>{attachSelection ? '把选中那一行的完整内容带进对话' : '当前没有选中日志，请先在日志区点选一行'}</small>
                              </span>
                            </button>
                            {composerReferences.length > 0 && (
                              <>
                                <div className="ai-compose-menu-divider" />
                                <button type="button" onClick={() => { setComposerReferences([]); setAttachMenuOpen(false); }}><span><strong>清空全部引用</strong><small>当前 {composerReferences.length} 条</small></span></button>
                              </>
                            )}
                          </div>
                        )}
                      </div>

                      <div className="ai-compose-popover-wrap">
                        <button
                          type="button"
                          className="ai-compose-tool ai-compose-tool-wide"
                          onClick={() => { const next = !executionPolicyMenuOpen; if (next) closeComposerPopovers('policy'); setExecutionPolicyMenuOpen(next); }}
                          title="执行权限：控制写操作是否需要你确认" aria-expanded={executionPolicyMenuOpen}
                        >
                          <ShieldCheck size={16} />
                          <span>{executionPolicy === 'confirm' ? '每次确认' : '自动执行'}</span>
                          <ChevronDown size={13} />
                        </button>
                        {executionPolicyMenuOpen && (
                          <div className="ai-compose-policy-menu">
                            <button type="button" className={executionPolicy === 'confirm' ? 'active' : ''} onClick={() => { setExecutionPolicy('confirm'); setExecutionPolicyMenuOpen(false); }}>
                              <span><strong>每次询问确认</strong><small>写操作执行前先给你确认卡</small></span>
                              {executionPolicy === 'confirm' && <Check size={14} />}
                            </button>
                            <button type="button" className={executionPolicy === 'auto' ? 'active' : ''} onClick={() => { setExecutionPolicy('auto'); setExecutionPolicyMenuOpen(false); }}>
                              <span><strong>自动执行</strong><small>只读工具直接跑，写操作仍走确认</small></span>
                              {executionPolicy === 'auto' && <Check size={14} />}
                            </button>
                          </div>
                        )}
                      </div>
                    </div>

                    <div className="ai-compose-toolbar-group ai-compose-toolbar-right">
                      <div className="ai-compose-popover-wrap">
                        <button
                          type="button"
                          className="ai-compose-tool ai-compose-model-btn"
                          onClick={() => { const next = !modelMenuOpen; if (next) closeComposerPopovers('model'); setModelMenuOpen(next); }}
                          title="本对话使用的模型与思考强度" aria-expanded={modelMenuOpen}
                        >
                          <span className="ai-compose-model-name">{activeModelName}</span>
                          {activeEffortLabel && <span className="ai-compose-model-effort">{activeEffortLabel}</span>}
                          <ChevronDown size={13} />
                        </button>
                        {modelMenuOpen && (
                          <div className="ai-compose-model-menu">
                            <div className="ai-compose-menu-title">模型</div>
                            {modelCatalog.length === 0 && <div className="ai-compose-menu-empty">未取到模型列表，将使用服务端默认模型</div>}
                            {modelCatalog.map((model) => (
                              <button
                                key={model.id}
                                type="button"
                                className={model.id === modelChoice.model ? 'active' : ''}
                                onClick={() => setModelChoice({ model: model.id, effort: model.default_effort || model.efforts[0] || '' })}
                              >
                                <span>
                                  <strong>{model.name}</strong>
                                  <small>{model.context_window ? `上下文 ${Math.round(model.context_window / 1024)}K` : model.id}</small>
                                </span>
                                {model.id === modelChoice.model && <Check size={14} />}
                              </button>
                            ))}
                            {activeModel && activeModel.efforts.length > 0 && (
                              <>
                                <div className="ai-compose-menu-divider" />
                                <div className="ai-compose-menu-title">思考强度</div>
                                <div className="ai-compose-effort-row">
                                  {activeModel.efforts.map((effort) => (
                                    <button
                                      key={effort}
                                      type="button"
                                      className={effort === modelChoice.effort ? 'active' : ''}
                                      onClick={() => setModelChoice((current) => ({ ...current, effort }))}
                                    >{EFFORT_LABELS[effort] || effort}</button>
                                  ))}
                                </div>
                              </>
                            )}
                            <div className="ai-compose-menu-divider" />
                            <div className="ai-compose-menu-title">日志证据长度上限</div>
                            <div className="ai-compose-effort-row">
                              {EVIDENCE_MAX_CHARS_PRESETS.map((preset) => (
                                <button
                                  key={preset}
                                  type="button"
                                  className={preset === evidenceMaxChars ? 'active' : ''}
                                  title={preset === DEFAULT_EVIDENCE_MAX_CHARS
                                    ? `${formatEvidenceMaxChars(preset)}（默认）：异常锚点上下文没超过就直送原文，超过才压缩`
                                    : `异常锚点上下文没超过 ${formatEvidenceMaxChars(preset)} 就直送原文`}
                                  onClick={() => setEvidenceMaxChars(saveEvidenceMaxChars(preset))}
                                >{preset / 10000} 万</button>
                              ))}
                            </div>
                            <div className="ai-compose-menu-empty">
                              异常锚点（±100 行）上下文的原文没超过 {formatEvidenceMaxChars(evidenceMaxChars)} 就原样直送模型，超过才走字典 + 模板压缩；日志分析与案例/用例分析共用这一档。
                            </div>
                            <div className="ai-compose-menu-divider" />
                            <button type="button" onClick={() => void listTraceLensAiModels(true).then((catalog) => setModelCatalog(catalog.models || [])).catch(() => undefined)}>
                              <span><strong>刷新模型列表</strong><small>重新从模型网关读取可用模型</small></span>
                            </button>
                          </div>
                        )}
                      </div>
                      <button
                        type="button"
                        className={`ai-compose-icon ai-compose-mic ${voiceState === 'recording' ? 'recording' : ''}`}
                        onClick={() => voiceState === 'recording' ? finishVoiceRecording() : void startVoiceRecording()}
                        disabled={voiceState === 'transcribing' || !voiceAvailable}
                        aria-label={voiceState === 'recording' ? '完成语音输入' : '语音输入'}
                        title={!voiceAvailable ? '本地 STT 服务未启动或不可用' : voiceState === 'recording' ? '完成录音' : '语音输入'}
                      >
                        {voiceState === 'transcribing' ? <LoaderCircle className="spin" size={20}/> : <Mic size={20} strokeWidth={2} />}
                      </button>
                      <button
                        type="button"
                        className={`ai-assistant-send ${sending && !input.trim() ? 'stop' : ''}`}
                        onClick={() => sending && !input.trim() ? stopActiveRun('用户主动终止了当前分析。', 'stopped') : void send()}
                        disabled={!sending && !input.trim()}
                        aria-label={sending && !input.trim() ? '停止当前任务' : sending ? '加入任务队列' : '发送'}
                        title={sending && !input.trim() ? '停止当前任务' : sending ? '加入任务队列' : '发送'}
                      >
                        {sending && !input.trim() ? <span className="ai-stop-square-icon" aria-hidden="true" /> : <Send size={17} />}
                      </button>
                    </div>
                  </div>
                </div>
                <footer className="ai-assistant-footer">
                  <span className="ai-token-usage" title={conversationTokenUsage.estimated ? '部分网关未返回 usage，带 ≈ 的数值为估算' : '当前对话累计 Token 消耗'}>
                    {conversationTokenUsage.estimated ? '≈ ' : ''}Token {compactTokenNumber(conversationTokenUsage.totalTokens)}
                    <i>入 {compactTokenNumber(conversationTokenUsage.inputTokens)} / 出 {compactTokenNumber(conversationTokenUsage.outputTokens)}</i>
                  </span>
                  <span className="ai-compose-hint">Enter 发送 · Shift+Enter 换行</span>
                </footer>
              </div>
            </section>
          </div>
          {layoutMode === 'floating' && <div className="ai-cockpit-resize" onMouseDown={beginResize} aria-hidden="true" />}
        </aside>
        );
      })()}
    </>
  );
}
