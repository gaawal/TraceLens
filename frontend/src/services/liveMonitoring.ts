/**
 * Live monitoring state — one observable value, not a one-shot event.
 *
 * Both subscribers (the timeline ribbon and the collector panel) mount *on demand*: the ribbon
 * only exists while the floating timeline window is open. A `CustomEvent` never reaches a
 * component that mounts after it fired, so opening the timeline while monitoring was already
 * running showed an empty ribbon forever — the "看不到变化" symptom. Subscribing replays the
 * current value, so a late mount is correct by construction.
 */
export interface CapturePanelRequest {
  /** Monotonic token: every request is a new token, so a repeated click still registers. */
  token: number;
  /** `prep` opens the panel to choose what to collect, before anything starts. */
  prep: boolean;
}

export interface LiveMonitoringState {
  on: boolean;
  environmentId?: number;
  captureRequest?: CapturePanelRequest;
}

let state: LiveMonitoringState = { on: false };
let captureToken = 0;
const listeners = new Set<(next: LiveMonitoringState) => void>();

export function setLiveMonitoring(next: Pick<LiveMonitoringState, 'on' | 'environmentId'>): void {
  if (state.on === next.on && state.environmentId === next.environmentId) return;
  state = { ...state, ...next };
  listeners.forEach((listener) => listener(state));
}

/**
 * Ask the collector panel to open without touching monitoring state.
 *
 * The panel used to appear only as a side effect of 实时监听, so there was no way to set up what
 * to collect *before* starting — the user had to start monitoring to see the panel at all.
 */
export function requestCapturePanel(prep = false): void {
  captureToken += 1;
  state = { ...state, captureRequest: { token: captureToken, prep } };
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
