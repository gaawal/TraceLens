from __future__ import annotations

import logging
from datetime import datetime

from django.conf import settings

from apps.logsources.services.cache_identity import LogCacheScope
from apps.logsources.services.redis_store import RedisLogStore

logger = logging.getLogger("tracelens.log_content_cache")


def _time_token(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%S.%f")


#: 内容窗口缓存版本。窗口内容是「按时间窗裁剪后的原文」，
#: 解析侧开始保留多行日志的续行（见 remote_logs 的读取器）之后，旧缓存里缺续行的
#: 窗口必须失效，否则用户会继续看到被截断的正文。改这个版本号即可整体失效。
_CONTENT_CACHE_VERSION = "cont-v2"


class LogContentCacheService:
    """Compressed Redis cache for an artifact's requested time-window chunk."""

    @property
    def ttl_seconds(self) -> int:
        return int(getattr(settings, "TRACELENS_LOG_CONTENT_CACHE_TTL", 7200))

    @property
    def max_bytes(self) -> int:
        return int(getattr(settings, "TRACELENS_LOG_CONTENT_CACHE_MAX_BYTES", 16 * 1024 * 1024))

    def build_key(
        self,
        *,
        scope: LogCacheScope,
        path: str,
        member_name: str,
        generation: str,
        start: datetime,
        end: datetime,
    ) -> str:
        return scope.content_key(
            path=path,
            member_name=member_name,
            generation=f"{_CONTENT_CACHE_VERSION}-{generation}",
            start_token=_time_token(start),
            end_token=_time_token(end),
        )

    def get(self, **kwargs) -> bytes | None:
        key = self.build_key(**kwargs)
        value = RedisLogStore.get_compressed(key)
        if value is not None:
            logger.info("log.content_cache.hit key=%s bytes=%d", key, len(value))
        else:
            logger.info("log.content_cache.miss key=%s", key)
        return value

    def put(self, *, content: bytes, **kwargs) -> bool:
        if not content:
            return False
        if len(content) > self.max_bytes:
            logger.info(
                "log.content_cache.skip_large bytes=%d max_bytes=%d path=%s",
                len(content),
                self.max_bytes,
                kwargs.get("path"),
            )
            return False
        key = self.build_key(**kwargs)
        stored = RedisLogStore.set_compressed(key, content, self.ttl_seconds)
        if stored:
            logger.info("log.content_cache.store key=%s bytes=%d", key, len(content))
        return stored


log_content_cache = LogContentCacheService()
