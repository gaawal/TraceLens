"""Realtime transport for log watches.

Mirrors the proven shape of ``DeploymentEventBus`` (Redis pub/sub + short-lived leases +
a worker heartbeat) with two additions a "源源不断" stream needs:

* **A per-watch monotonic ``seq``**, allocated with ``INCR``. A reconnecting browser sends
  ``since=<seq>`` and gets exactly the hits it missed. Timestamps cannot do this — they
  collide at millisecond resolution, and a clock change would silently skip or replay.
* **A capped ring buffer per watch.** Redis pub/sub is fire-and-forget: with no subscriber
  the message is gone. The buffer is what makes the stream replayable, and the durable
  ``LogWatchHit`` rows are the long-term record.

Nothing here is the source of truth. The database is: a hit is written to ``LogWatchHit``
first, then announced. If Redis is down the watcher keeps detecting and recording, and only
the live push degrades.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from apps.logsources.services.redis_store import RedisLogStore

logger = logging.getLogger("tracelens.logsources.watch_events")

WATCH_CHANNEL = f"tracelens:{RedisLogStore.CACHE_SCHEMA}:watch:events"
# How many recent hits stay replayable per watch. Big enough to cover a browser refresh or
# a short network drop, small enough that Redis is not used as a database.
RING_SIZE = 500
# A worker that stops refreshing its lease for this long loses ownership, so a crashed
# worker cannot park a watch forever.
LEASE_TTL_SECONDS = 45
SEQUENCE_TTL_SECONDS = 7 * 24 * 3600


def _seq_key(watch_id: int) -> str:
    return f"tracelens:{RedisLogStore.CACHE_SCHEMA}:watch:{watch_id}:seq"


def _ring_key(watch_id: int) -> str:
    return f"tracelens:{RedisLogStore.CACHE_SCHEMA}:watch:{watch_id}:ring"


def _lease_key(watch_id: int) -> str:
    return f"tracelens:{RedisLogStore.CACHE_SCHEMA}:watch:{watch_id}:lease"


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


class WatchEventBus:
    """Pub/sub + replay buffer + ownership leases for log watches."""

    @classmethod
    def enabled(cls) -> bool:
        return RedisLogStore.enabled()

    @classmethod
    def _client(cls):
        return RedisLogStore._get_client()

    # ---------------------------------------------------------------- sequencing
    @classmethod
    def next_seq(cls, watch_id: int) -> int:
        """Allocate the next hit sequence for a watch.

        Falls back to 0 when Redis is unavailable; callers then store the hit without a
        replay anchor rather than failing the detection.
        """
        client = cls._client()
        if client is None:
            return 0
        try:
            seq = int(client.incr(_seq_key(watch_id)))
            client.expire(_seq_key(watch_id), SEQUENCE_TTL_SECONDS)
            return seq
        except Exception as exc:  # noqa: BLE001
            RedisLogStore._mark_unavailable(exc)
            return 0

    # ------------------------------------------------------------------ publishing
    @classmethod
    def publish_hit(cls, payload: dict[str, Any]) -> bool:
        """Announce a hit and append it to the watch's replay ring."""
        client = cls._client()
        if client is None:
            return False
        try:
            body = _json_bytes(payload)
            watch_id = payload.get("watch_id")
            if watch_id is not None:
                key = _ring_key(int(watch_id))
                pipe = client.pipeline()
                pipe.lpush(key, body)
                pipe.ltrim(key, 0, RING_SIZE - 1)
                pipe.expire(key, SEQUENCE_TTL_SECONDS)
                pipe.execute()
            client.publish(WATCH_CHANNEL, body)
            return True
        except Exception as exc:  # noqa: BLE001
            RedisLogStore._mark_unavailable(exc)
            return False

    @classmethod
    def recent_hits(cls, watch_id: int, since_seq: int = 0, limit: int = RING_SIZE) -> list[dict[str, Any]]:
        """Replay buffered hits with ``seq > since_seq``, oldest first."""
        client = cls._client()
        if client is None:
            return []
        try:
            raw_items = client.lrange(_ring_key(int(watch_id)), 0, min(limit, RING_SIZE) - 1)
        except Exception as exc:  # noqa: BLE001
            RedisLogStore._mark_unavailable(exc)
            return []
        hits: list[dict[str, Any]] = []
        for raw in raw_items:
            try:
                payload = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
            except Exception:  # noqa: BLE001
                continue
            if int(payload.get("seq") or 0) > int(since_seq or 0):
                hits.append(payload)
        hits.sort(key=lambda item: int(item.get("seq") or 0))
        return hits

    # --------------------------------------------------------------------- leases
    @classmethod
    def try_claim_watch(cls, watch_id: int, token: str) -> bool | None:
        """True = this worker now owns the watch; False = someone else does; None = no Redis."""
        client = cls._client()
        if client is None:
            return None
        try:
            return bool(client.set(_lease_key(watch_id), str(token).encode("utf-8"), nx=True, ex=LEASE_TTL_SECONDS))
        except Exception as exc:  # noqa: BLE001
            RedisLogStore._mark_unavailable(exc)
            return None

    @classmethod
    def refresh_lease(cls, watch_id: int, token: str) -> bool:
        client = cls._client()
        if client is None:
            return False
        try:
            client.setex(_lease_key(watch_id), LEASE_TTL_SECONDS, str(token).encode("utf-8"))
            return True
        except Exception as exc:  # noqa: BLE001
            RedisLogStore._mark_unavailable(exc)
            return False

    @classmethod
    def release_lease(cls, watch_id: int, token: str) -> None:
        """Release only if we still hold it, so a takeover is not undone by the loser."""
        client = cls._client()
        if client is None:
            return
        key = _lease_key(watch_id)
        expected = str(token).encode("utf-8")
        try:
            if client.get(key) == expected:
                client.delete(key)
        except Exception as exc:  # noqa: BLE001
            RedisLogStore._mark_unavailable(exc)

    @classmethod
    def lease_holder(cls, watch_id: int) -> str:
        client = cls._client()
        if client is None:
            return ""
        try:
            value = client.get(_lease_key(watch_id))
        except Exception as exc:  # noqa: BLE001
            RedisLogStore._mark_unavailable(exc)
            return ""
        if value is None:
            return ""
        return value.decode("utf-8") if isinstance(value, (bytes, bytearray)) else str(value)

    # ------------------------------------------------------------------- streaming
    @classmethod
    async def event_stream(cls, since: int = 0) -> AsyncIterator[bytes]:
        """SSE frames for watch hits plus keepalives.

        Yields a ``watch-ready`` frame first so the browser knows the channel is live
        before it decides that "nothing happened" means "nothing is happening".
        """
        if not cls.enabled():
            yield b'event: watch-unavailable\ndata: {"reason":"redis-disabled"}\n\n'
            return
        client = RedisLogStore._async_client() if hasattr(RedisLogStore, "_async_client") else None
        if client is None:
            # Reuse the sync client through a small adapter: pub/sub is the one place we
            # need a long-lived connection and the sync client cannot await.
            async for frame in cls._sync_pubsub_stream(since):
                yield frame
            return
        pubsub = client.pubsub()
        try:
            await client.ping()
            await pubsub.subscribe(WATCH_CHANNEL)
            yield b'event: watch-ready\ndata: {"transport":"redis-pubsub"}\n\n'
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15.0)
                if message is None:
                    yield b": keepalive\n\n"
                    await asyncio.sleep(0)
                    continue
                raw = message.get("data")
                if isinstance(raw, (bytes, bytearray)):
                    yield b"event: watch\ndata: " + bytes(raw) + b"\n\n"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("watch.event_stream.failed")
            yield b'event: watch-error\ndata: ' + _json_bytes({"message": str(exc)[:400]}) + b"\n\n"
        finally:
            try:
                await pubsub.unsubscribe(WATCH_CHANNEL)
                await pubsub.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass

    @classmethod
    async def _sync_pubsub_stream(cls, since: int) -> AsyncIterator[bytes]:
        """Fallback when no async Redis client is configured.

        Runs the blocking pub/sub loop on a worker thread and forwards frames, so a slow or
        disconnecting client cannot block the event loop.
        """
        from apps.common.streaming import iter_sync_stream_in_thread

        def blocking() -> Any:
            client = cls._client()
            if client is None:
                return
            pubsub = client.pubsub(ignore_subscribe_messages=True)
            try:
                pubsub.subscribe(WATCH_CHANNEL)
                while True:
                    message = pubsub.get_message(timeout=15.0)
                    if message is None:
                        yield b": keepalive\n\n"
                        continue
                    raw = message.get("data")
                    if isinstance(raw, (bytes, bytearray)):
                        yield b"event: watch\ndata: " + bytes(raw) + b"\n\n"
            finally:
                try:
                    pubsub.unsubscribe(WATCH_CHANNEL)
                    pubsub.close()
                except Exception:  # noqa: BLE001
                    pass

        yield b'event: watch-ready\ndata: {"transport":"redis-pubsub-sync"}\n\n'
        async for frame in iter_sync_stream_in_thread(blocking(), thread_name="watch-events"):
            yield frame
