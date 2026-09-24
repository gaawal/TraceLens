from __future__ import annotations

import copy
import threading
import time
from typing import Any

from django.conf import settings

from apps.logsources.services.redis_store import RedisLogStore


class LogSceneStore:
    """Short-id storage for a shared log-view scene.

    Redis is preferred so a copied URL can be opened from another browser. A
    small in-process fallback keeps local development usable when Redis is off.
    """

    _local: dict[str, tuple[float, dict[str, Any]]] = {}
    _lock = threading.RLock()
    _local_sequence = 0

    @classmethod
    def ttl(cls) -> int:
        return int(getattr(settings, "TRACELENS_LOG_SCENE_TTL", 24 * 3600))

    @classmethod
    def _key(cls, scene_id: str) -> str:
        return f"tracelens:scene:{scene_id}"

    @classmethod
    def _remember_local(cls, scene_id: str, payload: dict[str, Any]) -> None:
        now = time.time()
        with cls._lock:
            expired = [key for key, (deadline, _) in cls._local.items() if deadline <= now]
            for key in expired:
                cls._local.pop(key, None)
            cls._local[scene_id] = (now + cls.ttl(), copy.deepcopy(payload))

    @classmethod
    def create(cls, payload: dict[str, Any]) -> str:
        # Keep the URL human-readable and short: ?s=123 instead of a random
        # token.  The scene body still lives server-side and can evolve without
        # making the browser URL longer.
        sequence = RedisLogStore.increment("tracelens:scene:sequence")
        if sequence is not None:
            scene_id = str(sequence)
            cls.put(scene_id, payload)
            return scene_id
        with cls._lock:
            cls._local_sequence += 1
            scene_id = str(cls._local_sequence)
            while scene_id in cls._local:
                cls._local_sequence += 1
                scene_id = str(cls._local_sequence)
        cls.put(scene_id, payload)
        return scene_id

    @classmethod
    def put(cls, scene_id: str, payload: dict[str, Any]) -> None:
        clean = copy.deepcopy(payload)
        RedisLogStore.set_json(cls._key(scene_id), clean, cls.ttl())
        cls._remember_local(scene_id, clean)

    @classmethod
    def get(cls, scene_id: str) -> dict[str, Any] | None:
        if not scene_id:
            return None
        value = RedisLogStore.get_json(cls._key(scene_id))
        if isinstance(value, dict):
            cls._remember_local(scene_id, value)
            return value
        now = time.time()
        with cls._lock:
            item = cls._local.get(scene_id)
            if not item:
                return None
            deadline, payload = item
            if deadline <= now:
                cls._local.pop(scene_id, None)
                return None
            return copy.deepcopy(payload)
