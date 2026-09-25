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

/**
 * 实时采集清单里有几个提取器 —— 采集进度框的**唯一**显示依据。
 *
 * 以前工具栏上单独有个「采集」按钮来开这个面板，于是「清单是空的」也会开出一个空盒子，
 * 而「清单里有东西」时又得记得去点它。现在改成：有提取器被加入实时采集就浮出来，
 * 一个都没有就不显示。订阅会重放当前值，晚挂载的面板也能拿到正确状态。
 */
let liveCaptureCount = 0;
const countListeners = new Set<(count: number) => void>();

export function publishLiveCaptureCount(count: number): void {
  const next = Math.max(0, Number(count) || 0);
  if (next === liveCaptureCount) return;
  liveCaptureCount = next;
  countListeners.forEach((listener) => listener(liveCaptureCount));
}

export function getLiveCaptureCount(): number {
  return liveCaptureCount;
}

/** Subscribe and immediately receive the current count. */
export function subscribeLiveCaptureCount(listener: (count: number) => void): () => void {
  countListeners.add(listener);
  listener(liveCaptureCount);
  return () => {
    countListeners.delete(listener);
  };
}

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
