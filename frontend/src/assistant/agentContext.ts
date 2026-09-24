export interface TracePilotAgentContext {
  page?: string;
  environmentId?: number;
  deployment?: {
    action?: string;
    confirmed?: boolean;
    targetVersion?: string;
  };
  permission?: {
    autoExecute?: boolean;
  };
}

const KEY = 'tracelens-agent-context-v1';

export function saveTracePilotAgentContext(context: TracePilotAgentContext) {
  if (typeof window === 'undefined') return;
  window.sessionStorage.setItem(KEY, JSON.stringify(context));
}

export function loadTracePilotAgentContext(): TracePilotAgentContext {
  if (typeof window === 'undefined') return {};
  try {
    return JSON.parse(window.sessionStorage.getItem(KEY) || '{}');
  } catch {
    return {};
  }
}
