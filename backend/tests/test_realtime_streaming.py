"""Regression guard for realtime streaming.

The realtime log endpoint was silently dead: `views.py` handed a **synchronous**
generator to `StreamingHttpResponse`, and Django serves those by materializing them::

    for part in await sync_to_async(list)(self.streaming_content):
        yield part

(django/http/response.py). `list()` on an endless live-log generator never returns, so the
browser received **zero bytes** no matter how much the remote machine printed — the
connection simply hung. These tests pin the bridge that fixes it.
"""
from __future__ import annotations

import asyncio
import pathlib
import time

import pytest

from apps.common.streaming import iter_sync_stream_in_thread


def _run(coro):
    return asyncio.run(coro)


def test_endless_generator_yields_before_finishing():
    """The whole point: the first items must arrive while the source is still running."""
    released = []
    progress = []

    def endless():
        i = 0
        while not released:
            progress.append(i)
            yield i
            i += 1
            if i > 4:
                time.sleep(0.01)

    async def consume():
        got = []
        async for item in iter_sync_stream_in_thread(endless()):
            got.append(item)
            if len(got) >= 3:
                break
        return got

    got = _run(consume())
    released.append(True)

    assert got == [0, 1, 2], "the bridge must not wait for the source to be exhausted"
    assert progress, "producer never ran"


def test_producer_exception_reaches_the_consumer():
    def boom():
        yield 1
        raise RuntimeError("producer failed")

    async def consume():
        got = []
        with pytest.raises(RuntimeError, match="producer failed"):
            async for item in iter_sync_stream_in_thread(boom()):
                got.append(item)
        return got

    assert _run(consume()) == [1]


def test_cancelling_the_consumer_closes_the_source():
    """A disconnecting client must tear down the producer, not leak a tail -F process."""
    closed = []

    def source():
        try:
            i = 0
            while True:
                yield i
                i += 1
        finally:
            closed.append(True)

    async def consume():
        agen = iter_sync_stream_in_thread(source())
        async for _ in agen:
            break
        await agen.aclose()

    _run(consume())
    for _ in range(100):
        if closed:
            break
        time.sleep(0.02)
    assert closed, "the source generator was never closed"


def test_content_is_not_reordered_or_lost():
    items = [f"line-{i}" for i in range(40)]

    async def consume():
        return [item async for item in iter_sync_stream_in_thread(iter(items), max_buffer=4)]

    assert _run(consume()) == items


def test_realtime_endpoints_use_the_bridge():
    """Static guard: the async bridge is what makes these endpoints stream at all."""
    views = (pathlib.Path(__file__).resolve().parents[1] / "apps/logsources/views.py").read_text(encoding="utf-8")
    assert views.count("iter_sync_stream_in_thread(") >= 3, (
        "live + both stream responses must be bridged; a raw sync generator delivers nothing"
    )
    # A bare sync generator here is the exact bug that was fixed.
    assert "StreamingHttpResponse(event_stream()" not in views
    assert "StreamingHttpResponse(audited_stream()" not in views
    assert "StreamingHttpResponse(cached_stream()" not in views
