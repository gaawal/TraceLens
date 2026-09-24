from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from django.conf import settings


class CpdReportDiskCache:
    """Persistent fallback cache for CPD catalog, summaries and range datasets.

    Redis remains the fast shared cache. This disk layer prevents a page reopen
    from turning into a remote SSH scan when Redis is empty/unavailable. Manual
    refresh still bypasses cached catalog data and writes a fresh snapshot.
    """

    @classmethod
    def directory(cls) -> Path:
        configured = str(getattr(settings, "TRACELENS_CPD_DISK_CACHE_DIR", "") or "").strip()
        base = Path(configured) if configured else Path(settings.BASE_DIR) / "runtime" / "cpd-report-cache"
        base.mkdir(parents=True, exist_ok=True)
        return base

    @classmethod
    def ttl(cls) -> int:
        return max(60, int(getattr(settings, "TRACELENS_CPD_SNAPSHOT_TTL", 30 * 24 * 60 * 60)))

    @classmethod
    def _path(cls, key: str) -> Path:
        digest = hashlib.sha256(str(key).encode("utf-8", errors="replace")).hexdigest()
        return cls.directory() / f"{digest}.json.gz"

    @classmethod
    def get_json(cls, key: str) -> dict[str, Any] | list[Any] | None:
        path = cls._path(key)
        try:
            if not path.is_file():
                return None
            if time.time() - path.stat().st_mtime > cls.ttl():
                path.unlink(missing_ok=True)
                return None
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, (dict, list)) else None
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None

    @classmethod
    def get_json_many(cls, keys: list[str]) -> list[Any | None]:
        return [cls.get_json(key) for key in keys]

    @classmethod
    def set_json(cls, key: str, value: Any) -> bool:
        path = cls._path(key)
        temp = path.with_suffix(f".{os.getpid()}.tmp")
        try:
            with gzip.open(temp, "wt", encoding="utf-8", compresslevel=3) as handle:
                json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            os.replace(temp, path)
            return True
        except (OSError, TypeError, ValueError):
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            return False
