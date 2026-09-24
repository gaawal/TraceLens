import { API_BASE } from '../api/resourceApi';

/**
 * Realtime log-watch stream.
 *
 * Server-driven, unlike the log viewer's live mode: the watcher runs in its own process, so
 * hits keep arriving (and keep being recorded) whether or not this tab is open. One
 * `EventSource` is shared by the whole app and replayed from the watch's sequence number on
 * reconnect, so no hit is lost in the gap.
 */
export interface WatchHitEvent {
  type: 'watch.hit';
  watch_id: number;
  watch_name: string;
  environment_id?: number;
  level: string;
  seq: number;
  hit_id: number;
  matched_at: string;
  signature: string;
  label: string;
  label_color: string;
  display_text?: string;
  /** Which surface the hit belongs to: 'data' = 实时采集 (collector panel), else a symptom lane. */
  display_mode?: string;
  show_on_timeline?: boolean;
  subsystem?: string;
  fm?: string;
  machine?: string;
  line_text?: string;
  burst_count?: number;
  silence_seconds?: number;
}

type HitListener = (hit: WatchHitEvent) => void;
type StatusListener = (connected: boolean) => void;

let source: EventSource | null = null;
let connected = false;
/** Highest seq seen per watch: the resume anchor and the duplicate filter. */
const lastSeqByWatch = new Map<number, number>();
const listeners = new Set<HitListener>();
const statusListeners = new Set<StatusListener>();

function streamUrl() {
  if (typeof window === 'undefined') return `${API_BASE}/watch-events/`;
  return new URL(`${API_BASE}/watch-events/`, window.location.origin).toString();
}

function setConnected(value: boolean) {
  if (connected === value) return;
  connected = value;
  statusListeners.forEach((listener) => listener(value));
}

function accept(hit: WatchHitEvent) {
  const previous = lastSeqByWatch.get(hit.watch_id) ?? 0;
  // seq is monotonic per watch, so a replay after reconnect cannot double-append.
  if (hit.seq && hit.seq <= previous) return;
  if (hit.seq) lastSeqByWatch.set(hit.watch_id, hit.seq);
  listeners.forEach((listener) => listener(hit));
}

export function ensureWatchStream(): void {
  if (source || typeof window === 'undefined' || typeof EventSource === 'undefined') return;
  source = new EventSource(streamUrl());
  source.addEventListener('watch-ready', () => setConnected(true));
  source.addEventListener('watch-unavailable', () => setConnected(false));
  source.addEventListener('watch', (raw) => {
    try {
      const hit = JSON.parse((raw as MessageEvent<string>).data) as WatchHitEvent;
      if (hit && hit.type === 'watch.hit') accept(hit);
    } catch {
      // A malformed frame must not tear down the stream.
    }
  });
  source.onerror = () => setConnected(false);
}

export function subscribeWatchHits(listener: HitListener): () => void {
  ensureWatchStream();
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function subscribeWatchStatus(listener: StatusListener): () => void {
  ensureWatchStream();
  statusListeners.add(listener);
  // Replay the current value: the stream is opened once for the whole app, so a component
  // that mounts after `watch-ready` (the ribbon only exists while the timeline is open) would
  // otherwise show "未连接" forever while hits are clearly arriving.
  listener(connected);
  return () => {
    statusListeners.delete(listener);
  };
}

export function lastSeqFor(watchId: number): number {
  return lastSeqByWatch.get(watchId) ?? 0;
}

export function primeSeq(watchId: number, seq: number): void {
  if (seq > (lastSeqByWatch.get(watchId) ?? 0)) lastSeqByWatch.set(watchId, seq);
}

export function watchStreamConnected(): boolean {
  return connected;
}
