import { useEffect, useMemo, useRef, useState } from 'react';
import { Pause, Play, Radio } from 'lucide-react';
import { API_BASE, buildApiHeaders } from '../api/resourceApi';
import { subscribeLiveMonitoring } from '../services/liveMonitoring';
import { primeSeq, subscribeWatchHits, subscribeWatchStatus, type WatchHitEvent } from '../services/watchRealtime';
import { isCustomLabelColor, moduleColor } from '../rendering/componentColor';

/**
 * Live tag ribbon — the streaming view of watch hits.
 *
 * Lives *inside* the floating timeline window rather than as a strip above the log
 * workspace: a ribbon docked at the top squeezes the timeline and fights the layout, while
 * the floating window is already draggable, closable and out of the way.
 *
 * Append model
 * ------------
 * Hits are kept in `matched_at` order, never arrival order, so a burst that arrives out of
 * order still reads left-to-right as a timeline.
 *
 * Scroll model (the recording-console behaviour)
 * ---------------------------------------------
 * While the chips fit, they simply fill the track left to right. Once the newest chip would
 * pass the right edge, the track starts advancing and keeps the newest pinned to the edge.
 * Scrolling back by hand pauses following — the ribbon never yanks the view away from
 * something you are reading — and the play button resumes it.
 */
export function LiveRibbon({ environmentId }: { environmentId?: number }) {
  const [hits, setHits] = useState<WatchHitEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [following, setFollowing] = useState(true);
  const [monitoring, setMonitoring] = useState(false);
  const trackRef = useRef<HTMLDivElement | null>(null);
  const followingRef = useRef(true);

  useEffect(() => {
    followingRef.current = following;
  }, [following]);

  useEffect(() => subscribeLiveMonitoring((next) => {
    setMonitoring(next.on);
    // Stopping monitoring clears the track so a restart does not show a stale burst.
    if (!next.on) setHits([]);
  }), []);

  useEffect(() => {
    if (!monitoring) return;
    const wanted = environmentId;
    let cancelled = false;
    void fetch(`${API_BASE}/log-watches/?enabled=1`, { headers: buildApiHeaders() })
      .then((response) => (response.ok ? response.json() : null))
      .then((payload: { watches?: Array<{ id: number; hit_count: number }> } | null) => {
        if (cancelled || !payload) return;
        for (const watch of payload.watches || []) {
          if (watch.hit_count) {
            void fetch(`${API_BASE}/log-watches/${watch.id}/hits/?limit=1`, { headers: buildApiHeaders() })
              .then((response) => (response.ok ? response.json() : null))
              .then((page: { max_seq?: number } | null) => {
                if (page?.max_seq) primeSeq(watch.id, page.max_seq);
              })
              .catch(() => undefined);
          }
        }
      })
      .catch(() => undefined);
    const offHit = subscribeWatchHits((hit) => {
      if (wanted && hit.environment_id && hit.environment_id !== wanted) return;
      // 实时采集 hits are numbers for the collector panel, not symptoms. The server marks
      // the surface on every hit (and on replayed ones), so this stays a filter, not a guess.
      if (hit.show_on_timeline === false || hit.display_mode === 'data') return;
      setHits((current) => {
        const key = `${hit.watch_id}-${hit.seq}`;
        if (current.some((item) => `${item.watch_id}-${item.seq}` === key)) return current;
        // Time order, not arrival order: a burst can arrive slightly out of sequence.
        const next = [...current, hit].sort((a, b) => String(a.matched_at).localeCompare(String(b.matched_at)));
        return next.slice(-300);
      });
    });
    const offStatus = subscribeWatchStatus(setConnected);
    return () => {
      cancelled = true;
      offHit();
      offStatus();
    };
  }, [monitoring, environmentId]);

  // Advance the track only when the newest chip would cross the right edge.
  useEffect(() => {
    const node = trackRef.current;
    if (!node || !followingRef.current) return;
    const overflowing = node.scrollWidth > node.clientWidth + 4;
    if (!overflowing) return;
    node.scrollTo({ left: node.scrollWidth - node.clientWidth, behavior: 'smooth' });
  }, [hits]);

  const summary = useMemo(() => {
    const bursts = hits.filter((hit) => typeof hit.burst_count === 'number').length;
    return { total: hits.length, bursts };
  }, [hits]);

  if (!monitoring) return null;

  return (
    <div className="live-ribbon" aria-label="实时标签流">
      <div className="live-ribbon-head">
        <span className={`live-ribbon-status ${connected ? 'on' : 'off'}`}>
          <Radio size={12} />
          {connected ? '实时标签流' : '未连接'}
        </span>
        <span className="live-ribbon-count">
          {summary.total} 个标签
          {summary.bursts > 0 && <em> · {summary.bursts} 次突发</em>}
        </span>
        <button
          type="button"
          className={following ? 'active' : ''}
          onClick={() => setFollowing((value) => !value)}
          title={following ? '暂停跟随（可自由回看）' : '恢复跟随最新标签'}
        >
          {following ? <Pause size={12} /> : <Play size={12} />}
          {following ? '跟随中' : '已暂停'}
        </button>
      </div>
      <div
        className="live-ribbon-track"
        ref={trackRef}
        onScroll={() => {
          const node = trackRef.current;
          if (!node) return;
          const atEnd = node.scrollWidth - node.scrollLeft - node.clientWidth < 24;
          // Manual scroll back pauses following; returning to the edge resumes it.
          if (!atEnd && followingRef.current) setFollowing(false);
          if (atEnd && !followingRef.current) setFollowing(true);
        }}
      >
        {hits.length === 0 && <span className="live-ribbon-empty">监控中，等待标签命中…（服务端持续监听，关掉页面也在跑）</span>}
        {hits.map((hit) => {
          // 普通 INFO 命中（没有自定义标签色的规则）按**模块**上色，和日志列表里的
          // 模块徽标同一个 hash —— 一条全是蓝色的标签流等于没有颜色信息，
          // 看不出哪个模块在推进。只有用户真的给规则挑过颜色时才用那个颜色。
          const module = String(hit.fm || hit.subsystem || '').trim();
          const custom = isCustomLabelColor(hit.label_color);
          const tone = moduleColor(module || hit.watch_name || 'default');
          const chip = custom
            ? { border: hit.label_color, dot: hit.label_color, text: '#2f3e50' }
            : tone;
          return (
            <span
              key={`${hit.watch_id}-${hit.seq}-${hit.hit_id}`}
              className={`live-ribbon-tag ${custom ? 'is-labeled' : 'is-module'}`}
              style={{ borderColor: chip.border, borderLeftColor: chip.dot }}
              title={[hit.display_text || hit.label, module ? `模块：${module}` : '', hit.line_text || ''].filter(Boolean).join('\n')}
            >
              <i style={{ background: chip.dot }} aria-hidden="true" />
              <time>{String(hit.matched_at || '').slice(11, 19)}</time>
              {module && <b className="live-ribbon-module" style={{ color: chip.text }}>{module}</b>}
              <strong>{hit.label || hit.watch_name}</strong>
              {typeof hit.burst_count === 'number' && <em>×{hit.burst_count}</em>}
            </span>
          );
        })}
      </div>
    </div>
  );
}
