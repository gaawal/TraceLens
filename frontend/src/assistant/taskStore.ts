export interface AgentTaskSnapshot {
  runId: string;
  sessionId?: string;
  conversationId: string;
  assistantId: string;
  status: string;
  updatedAt: number;
}

const KEY = 'tracelens-agent-task-snapshots-v1';

function load(): Record<string, AgentTaskSnapshot> {
  try {
    return JSON.parse(localStorage.getItem(KEY) || '{}');
  } catch {
    return {};
  }
}

function save(data: Record<string, AgentTaskSnapshot>) {
  try {localStorage.setItem(KEY, JSON.stringify(data));} catch { /* Storage denial must not interrupt a task. */ }
}

export function saveAgentTask(snapshot: AgentTaskSnapshot) {
  const data = load();
  data[snapshot.runId] = snapshot;
  save(data);
}

export function updateAgentTask(runId: string, patch: Partial<AgentTaskSnapshot>) {
  const data = load();
  if (!data[runId]) return;
  data[runId] = { ...data[runId], ...patch, updatedAt: Date.now() };
  save(data);
}

export function removeAgentTask(runId: string) {
  const data = load();
  delete data[runId];
  save(data);
}

export function listAgentTasks() {
  return Object.values(load());
}
