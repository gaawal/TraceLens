/**
 * Live monitoring state — one observable value, not a one-shot event.
 *
 * Both subscribers (the timeline ribbon and the collector panel) mount *on demand*: the ribbon
 * only exists while the floating timeline window is open. A `CustomEvent` never reaches a
 * component that mounts after it fired, so opening the timeline while monitoring was already
 * running showed an empty ribbon forever — the "看不到变化" symptom. Subscribing replays the
 * current value, so a late mount is correct by construction.
 */
export interface LiveMonitoringState {
  on: boolean;
  environmentId?: number;
}

let state: LiveMonitoringState = { on: false };
const listeners = new Set<(next: LiveMonitoringState) => void>();

export function setLiveMonitoring(next: Pick<LiveMonitoringState, 'on' | 'environmentId'>): void {
  if (state.on === next.on && state.environmentId === next.environmentId) return;
  state = { ...state, ...next };
  listeners.forEach((listener) => listener(state));
}

export function getLiveMonitoring(): LiveMonitoringState {
  return state;
}

/** Subscribe and immediately receive the current state. */
export function subscribeLiveMonitoring(listener: (next: LiveMonitoringState) => void): () => void {
  listeners.add(listener);
  listener(state);
  return () => {
    listeners.delete(listener);
  };
}
