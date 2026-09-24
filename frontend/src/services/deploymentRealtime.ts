import { API_BASE, type EnvironmentDeploymentSummary, type DeploymentStepStatus } from '../api/resourceApi';

export interface DeploymentStepStateEvent {
  id: number;
  key: string;
  name: string;
  sort_order: number;
  status: DeploymentStepStatus;
  status_label: string;
  command: string;
  success_marker: string;
  process_log?: string;
  stdout_length?: number | null;
  stderr_length?: number | null;
  exit_status?: number | null;
  retry_count: number;
  message: string;
  parameters?: Record<string, unknown>;
  started_at?: string | null;
  finished_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export type DeploymentRealtimeEvent = {
  type: 'deployment.state';
  deployment: EnvironmentDeploymentSummary;
  steps: DeploymentStepStateEvent[];
} | {type: 'deployment.log'; deployment_id: number; step_key: string; stdout: string; stderr: string; chunks: Array<{stream: string; text: string}>; stdout_length?: number; stderr_length?: number; process_log_length?: number};

type EventListener = (event: DeploymentRealtimeEvent) => void;
type StatusListener = (connected: boolean) => void;

let source: EventSource | null = null;
let connected = false;
const latestStateEvents = new Map<number, DeploymentRealtimeEvent>();
const listeners = new Set<EventListener>();
const statusListeners = new Set<StatusListener>();

function eventStreamUrl() {
  if (typeof window === 'undefined') return `${API_BASE}/deployment-events/`;
  return new URL(`${API_BASE}/deployment-events/`, window.location.origin).toString();
}

function setConnected(value: boolean) {
  if (connected === value) return;
  connected = value;
  statusListeners.forEach((listener) => listener(value));
}

function ensureSource() {
  if (source || typeof window === 'undefined' || typeof EventSource === 'undefined') return;
  source = new EventSource(eventStreamUrl());
  source.addEventListener('realtime-ready', () => setConnected(true));
  source.addEventListener('realtime-unavailable', () => setConnected(false));
  source.addEventListener('deployment', (raw) => {
    try {
      const event = JSON.parse((raw as MessageEvent<string>).data) as DeploymentRealtimeEvent;
      if (event.type === 'deployment.state') {
        latestStateEvents.delete(event.deployment.id);
        latestStateEvents.set(event.deployment.id, event);
        while (latestStateEvents.size > 100) {
          const oldest = latestStateEvents.keys().next().value;
          if (oldest === undefined) break;
          latestStateEvents.delete(oldest);
        }
      }
      listeners.forEach((listener) => listener(event));
    } catch (exc) {
      console.warn('deployment realtime event parse failed', exc);
    }
  });
  source.onopen = () => setConnected(true);
  source.onerror = () => setConnected(false);
}

function closeIfUnused() {
  if (listeners.size || statusListeners.size) return;
  source?.close();
  source = null;
  connected = false;
}

export function subscribeDeploymentRealtime(listener: EventListener, statusListener?: StatusListener) {
  listeners.add(listener);
  latestStateEvents.forEach((event) => queueMicrotask(() => listener(event)));
  if (statusListener) {
    statusListeners.add(statusListener);
    statusListener(connected);
  }
  ensureSource();
  return () => {
    listeners.delete(listener);
    if (statusListener) statusListeners.delete(statusListener);
    closeIfUnused();
  };
}

/** End offsets deduplicate chunks already included in a state/reconnect snapshot. */
export function appendDeploymentLog(current: string, event: Extract<DeploymentRealtimeEvent, {type: 'deployment.log'}>): {text: string; gap: boolean} {
  const chunk = event.chunks?.length ? event.chunks.map(c => c.text).join('') : (event.stdout || '')+(event.stderr || '');
  // Backend offsets count Unicode code points; JavaScript length counts UTF-16 units.
  const length = Array.from(current).length;
  const end = event.process_log_length;
  if (end === undefined || end === null) return {text: current + chunk, gap: false};
  if (end <= length) return {text: current, gap: false};
  const start = end - Array.from(chunk).length;
  if (start > length) return {text: current, gap: true};
  return {text: current + Array.from(chunk).slice(Math.max(0,length-start)).join(''), gap: false};
}
