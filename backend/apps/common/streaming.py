"""Bridge blocking synchronous generators to async iterators.

Django's ``StreamingHttpResponse`` can only serve a *synchronous* ``streaming_content``
by first materializing it::

    for part in await sync_to_async(list)(self.streaming_content):
        yield part

(django/http/response.py). For a finite stream that merely destroys incremental delivery;
for an endless one — live logs, SSE — **nothing is ever sent at all**, because ``list()``
never returns. That is why the realtime log endpoint stayed silent no matter how much the
remote machine printed.

Wrapping the producer in an async generator is the fix. The producer keeps running on its
own thread (it does blocking SSH reads), and items cross to the event loop through a
bounded queue so a slow client applies backpressure instead of growing memory.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, AsyncIterator, Iterable, TypeVar

logger = logging.getLogger("tracelens.common.streaming")

T = TypeVar("T")

_DONE = object()
DEFAULT_MAX_BUFFER = 256


async def iter_sync_stream_in_thread(
    source: Iterable[T],
    *,
    max_buffer: int = DEFAULT_MAX_BUFFER,
    thread_name: str = "tracelens-sync-stream",
) -> AsyncIterator[T]:
    """Yield items from a blocking iterable without materializing it.

    Cancelling the consumer (client disconnect) stops the producer thread and closes the
    source generator, so the underlying SSH/``tail -F`` process is torn down rather than
    left running.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=max_buffer)
    stopped = threading.Event()

    def push(item: Any) -> bool:
        """Returns False once the consumer is gone, so the producer can stop early.

        A disconnecting client must abort the producer promptly — otherwise a finite
        log search would keep reading the whole remote result set for nobody.
        """
        if stopped.is_set():
            return False
        # ``put`` awaits queue space; blocking the producer here is the backpressure.
        asyncio.run_coroutine_threadsafe(queue.put(item), loop).result()
        return not stopped.is_set()

    def pump() -> None:
        try:
            for item in source:
                if not push(item):
                    break
        except BaseException as exc:  # noqa: BLE001 - forwarded to the consumer
            logger.debug("sync stream producer failed: %s", exc, exc_info=True)
            push(exc)
        finally:
            # Closed here, on the producer's own thread, so generator finalisers
            # (audit rows, temp files) run exactly once and never concurrently with a
            # `close()` issued from the event loop.
            closer = getattr(source, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    logger.debug("closing sync stream source failed", exc_info=True)
            try:
                from django.db import close_old_connections

                close_old_connections()
            except Exception:  # noqa: BLE001
                pass
            push(_DONE)

    thread = threading.Thread(target=pump, name=thread_name, daemon=True)
    thread.start()
    try:
        while True:
            item = await queue.get()
            if item is _DONE:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        stopped.set()
        # Unblock a producer parked inside `push`, otherwise it would sit there until the
        # queue drained on its own.
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
