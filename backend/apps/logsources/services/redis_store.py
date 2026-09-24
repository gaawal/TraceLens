from __future__ import annotations

import json
import logging
import threading
import time
import zlib
from typing import Any
from urllib.parse import urlparse, urlunparse

from django.conf import settings

logger = logging.getLogger("tracelens.redis")

try:  # Redis is optional at import time so local/offline development still starts.
    import redis
except ImportError:  # pragma: no cover - exercised only without optional dependency.
    redis = None


class RedisLogStore:
    CACHE_SCHEMA = "v3"
    """Redis facade used by the file-index and compressed-content caches.

    Redis is deliberately an acceleration layer. Authentication/connection
    failures become cache misses so remote log search remains available, while
    status() exposes the failure reason for deployment diagnostics.
    """

    _client = None
    _lock = threading.RLock()
    _unavailable_until = 0.0
    _last_error = ""
    _connected_endpoint = ""

    @classmethod
    def enabled(cls) -> bool:
        return bool(getattr(settings, "TRACELENS_REDIS_ENABLED", True))

    @classmethod
    def _normalized_url(cls) -> str:
        raw = str(getattr(settings, "TRACELENS_REDIS_URL", "") or "").strip()
        if not raw:
            return ""
        if "://" not in raw:
            raw = f"redis://{raw}"
        parsed = urlparse(raw)
        # A raw host:port has no DB path. Apply the configured DB without
        # forcing users to manually construct a Redis URI.
        if not parsed.path or parsed.path == "/":
            parsed = parsed._replace(path=f"/{int(getattr(settings, 'TRACELENS_REDIS_DB', 0))}")
        return urlunparse(parsed)

    @classmethod
    def _effective_db(cls) -> int:
        url = cls._normalized_url()
        if url:
            path = (urlparse(url).path or "/0").lstrip("/") or "0"
            try:
                return int(path.split("/", 1)[0])
            except ValueError:
                return int(getattr(settings, "TRACELENS_REDIS_DB", 0))
        return int(getattr(settings, "TRACELENS_REDIS_DB", 0))

    @classmethod
    def _endpoint_label(cls) -> str:
        url = cls._normalized_url()
        if url:
            parsed = urlparse(url)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or 6379
            db = (parsed.path or "/0").lstrip("/") or "0"
            return f"{host}:{port}/{db}"
        return (
            f"{getattr(settings, 'TRACELENS_REDIS_HOST', '127.0.0.1')}:"
            f"{int(getattr(settings, 'TRACELENS_REDIS_PORT', 6379))}/"
            f"{int(getattr(settings, 'TRACELENS_REDIS_DB', 0))}"
        )

    @classmethod
    def _new_client(cls):
        if redis is None:
            raise RuntimeError("Python redis package is not installed")
        common = {
            "socket_connect_timeout": getattr(settings, "TRACELENS_REDIS_CONNECT_TIMEOUT", 1.0),
            "socket_timeout": getattr(settings, "TRACELENS_REDIS_SOCKET_TIMEOUT", 2.0),
            "decode_responses": False,
        }
        username = str(getattr(settings, "TRACELENS_REDIS_USERNAME", "") or "").strip() or None
        password = str(getattr(settings, "TRACELENS_REDIS_PASSWORD", "") or "") or None
        url = cls._normalized_url()
        if url:
            kwargs = dict(common)
            if username is not None:
                kwargs["username"] = username
            if password is not None:
                kwargs["password"] = password
            return redis.Redis.from_url(url, **kwargs)
        return redis.Redis(
            host=getattr(settings, "TRACELENS_REDIS_HOST", "127.0.0.1"),
            port=int(getattr(settings, "TRACELENS_REDIS_PORT", 6379)),
            db=int(getattr(settings, "TRACELENS_REDIS_DB", 0)),
            username=username,
            password=password,
            **common,
        )

    @classmethod
    def _get_client(cls):
        if not cls.enabled() or redis is None:
            return None
        if time.monotonic() < cls._unavailable_until:
            return None
        with cls._lock:
            if cls._client is not None:
                return cls._client
            try:
                client = cls._new_client()
                client.ping()
                cls._client = client
                cls._last_error = ""
                cls._connected_endpoint = cls._endpoint_label()
                logger.info(
                    "redis.cache.connected endpoint=%s auth=%s",
                    cls._connected_endpoint,
                    bool(getattr(settings, "TRACELENS_REDIS_PASSWORD", "")),
                )
                return client
            except Exception as exc:
                cls._mark_unavailable(exc)
                return None

    @classmethod
    def _mark_unavailable(cls, exc: Exception) -> None:
        cls._client = None
        cls._last_error = f"{exc.__class__.__name__}: {exc}"
        cls._unavailable_until = time.monotonic() + float(
            getattr(settings, "TRACELENS_REDIS_RETRY_SECONDS", 30)
        )
        logger.warning(
            "redis.cache.unavailable endpoint=%s error=%s",
            cls._endpoint_label(),
            cls._last_error,
        )

    @classmethod
    def status(cls) -> dict[str, Any]:
        result: dict[str, Any] = {
            "enabled": cls.enabled(),
            "connected": False,
            "endpoint": cls._endpoint_label(),
            "database": cls._effective_db(),
            "authenticated": bool(
                getattr(settings, "TRACELENS_REDIS_PASSWORD", "")
                or getattr(settings, "TRACELENS_REDIS_USERNAME", "")
                or (urlparse(cls._normalized_url()).password if cls._normalized_url() else None)
            ),
            "tracelens_keys": 0,
            "database_keys": 0,
            "last_error": cls._last_error,
            "cache_schema": cls.CACHE_SCHEMA,
        }
        if not cls.enabled():
            return result
        client = cls._get_client()
        if client is None:
            result["last_error"] = cls._last_error or "Redis client unavailable"
            return result
        try:
            result["connected"] = bool(client.ping())
            result["database_keys"] = int(client.dbsize())
            count = 0
            # Health is called on demand; SCAN is non-blocking and capped so a
            # shared Redis instance cannot make health checks expensive.
            for _ in client.scan_iter(match=f"tracelens:{cls.CACHE_SCHEMA}:*", count=200):
                count += 1
                if count >= 50_000:
                    break
            result["tracelens_keys"] = count
            result["last_error"] = ""
        except Exception as exc:
            cls._mark_unavailable(exc)
            result["last_error"] = cls._last_error
        return result

    @classmethod
    def get_json(cls, key: str) -> dict[str, Any] | list[Any] | None:
        client = cls._get_client()
        if client is None:
            return None
        try:
            raw = client.get(key)
            if raw is None:
                return None
            return json.loads(raw.decode("utf-8", errors="replace"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            # A single legacy/corrupt cache value is only a cache miss.  It must
            # never disable Redis or fail the user's log search.
            logger.warning("redis.cache.invalid_json key=%s error=%s", key, exc)
            return None
        except Exception as exc:
            cls._mark_unavailable(exc)
            return None

    @classmethod
    def get_json_many(cls, keys: list[str]) -> list[Any | None]:
        if not keys:
            return []
        client = cls._get_client()
        if client is None:
            return [None] * len(keys)
        try:
            values = client.mget(keys)
            result: list[Any | None] = []
            for raw in values:
                if raw is None:
                    result.append(None)
                    continue
                try:
                    result.append(json.loads(raw.decode("utf-8", errors="replace")))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    result.append(None)
            return result
        except Exception as exc:
            cls._mark_unavailable(exc)
            return [None] * len(keys)

    @classmethod
    def increment(cls, key: str) -> int | None:
        client = cls._get_client()
        if client is None:
            return None
        try:
            return int(client.incr(key))
        except Exception as exc:
            cls._mark_unavailable(exc)
            return None

    @classmethod
    def set_json(cls, key: str, value: Any, ttl_seconds: int) -> bool:
        client = cls._get_client()
        if client is None:
            return False
        try:
            payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            client.setex(key, ttl_seconds, payload)
            logger.debug("redis.cache.store_json key=%s bytes=%d ttl=%d", key, len(payload), ttl_seconds)
            return True
        except Exception as exc:
            cls._mark_unavailable(exc)
            return False

    @classmethod
    def get_compressed(cls, key: str) -> bytes | None:
        client = cls._get_client()
        if client is None:
            return None
        try:
            raw = client.get(key)
            if raw is None:
                return None
            return zlib.decompress(raw)
        except Exception as exc:
            cls._mark_unavailable(exc)
            return None

    @classmethod
    def set_compressed(cls, key: str, value: bytes, ttl_seconds: int) -> bool:
        client = cls._get_client()
        if client is None:
            return False
        try:
            compressed = zlib.compress(value, level=3)
            client.setex(key, ttl_seconds, compressed)
            logger.debug(
                "redis.cache.store_content key=%s raw_bytes=%d compressed_bytes=%d ttl=%d",
                key,
                len(value),
                len(compressed),
                ttl_seconds,
            )
            return True
        except Exception as exc:
            cls._mark_unavailable(exc)
            return False
