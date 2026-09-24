from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from django.conf import settings


class LogSearchResultCache:
    """Short-lived final-result cache for exact repeated log-window searches.

    It sits above the file-index/content caches. A hit can be streamed without
    reconnecting to SSH or enumerating remote files, which is important when a
    shared scene URL is reopened.
    """

    @classmethod
    def directory(cls) -> Path:
        configured = str(getattr(settings, "TRACELENS_SEARCH_RESULT_CACHE_DIR", "") or "").strip()
        base = Path(configured) if configured else Path(settings.BASE_DIR) / "runtime" / "search-result-cache"
        base.mkdir(parents=True, exist_ok=True)
        return base

    @classmethod
    def ttl(cls) -> int:
        # Final search results are disk-backed and are specifically intended to
        # avoid repeating SSH/file-plan work for the same historical window.
        return int(getattr(settings, "TRACELENS_SEARCH_RESULT_CACHE_TTL", 24 * 3600))

    @classmethod
    def max_bytes(cls) -> int:
        # This is a disk-cache limit, not a browser-memory limit.  Keep it well
        # above the 256 MiB browser safety guard so a completed search can still
        # be reused by later requests or shared-scene restores.
        return int(getattr(settings, "TRACELENS_SEARCH_RESULT_CACHE_MAX_BYTES", 2 * 1024 * 1024 * 1024))

    @classmethod
    def max_total_bytes(cls) -> int:
        return int(getattr(settings, "TRACELENS_SEARCH_RESULT_CACHE_TOTAL_BYTES", 10 * 1024 * 1024 * 1024))

    @classmethod
    def recent_guard_seconds(cls) -> int:
        # Never reuse a final-result snapshot for a recent window. Current .log
        # files are append-only and can gain new rows after an earlier identical
        # request; file-index/content caches still accelerate the fresh query.
        return int(getattr(settings, "TRACELENS_SEARCH_RESULT_CACHE_RECENT_GUARD_SECONDS", 15 * 60))

    @classmethod
    def allow_final_cache(cls, end_time: Any) -> bool:
        if not isinstance(end_time, datetime):
            return False
        now = datetime.now(end_time.tzinfo) if end_time.tzinfo is not None else datetime.now()
        return end_time < now - timedelta(seconds=max(0, cls.recent_guard_seconds()))

    @staticmethod
    def _text(value: Any) -> str:
        return str(value or "").strip()

    @classmethod
    def normalize_request(cls, payload: dict[str, Any]) -> dict[str, Any]:
        """Canonicalize semantically equivalent requests before hashing.

        UI selection order must not create a different final-result cache key.
        Datetime objects and strings are also normalized to one representation.
        """
        def normalize_time(value: Any) -> str:
            if isinstance(value, (datetime, date)):
                return value.isoformat()
            return cls._text(value)

        def sorted_text(values: Any) -> list[str]:
            if not isinstance(values, (list, tuple, set)):
                return []
            return sorted({cls._text(item) for item in values if cls._text(item)})

        targets: list[dict[str, str]] = []
        raw_targets = payload.get("fm_targets") or []
        if isinstance(raw_targets, (list, tuple)):
            seen: set[tuple[str, str, str]] = set()
            for item in raw_targets:
                if not isinstance(item, dict):
                    try:
                        item = dict(item)
                    except Exception:
                        continue
                subsystem = cls._text(item.get("subsystem"))
                fm = cls._text(item.get("fm") or item.get("module"))
                kind = cls._text(item.get("kind") or "normal")
                if not subsystem or not fm:
                    continue
                marker = (subsystem, fm, kind)
                if marker in seen:
                    continue
                seen.add(marker)
                targets.append({"subsystem": subsystem, "fm": fm, "kind": kind})
            targets.sort(key=lambda item: (item["subsystem"], item["fm"], item["kind"]))

        normalized = {
            "start_time": normalize_time(payload.get("start_time")),
            "end_time": normalize_time(payload.get("end_time")),
            "source_categories": sorted_text(payload.get("source_categories")),
            "fm_targets": targets,
            "keyword": str(payload.get("keyword") or ""),
        }
        # subsystems/fms are redundant when explicit fm_targets exist.  Ignore
        # their order/noise in that common path; retain them for flat log types.
        if not targets:
            normalized["subsystems"] = sorted_text(payload.get("subsystems"))
            normalized["fms"] = sorted_text(payload.get("fms"))
        return normalized

    @classmethod
    def identity(cls, environment_id: int, payload: dict[str, Any]) -> str:
        canonical = json.dumps(
            {"environment_id": int(environment_id), "request": cls.normalize_request(payload)},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def _paths(cls, key: str) -> tuple[Path, Path]:
        base = cls.directory()
        return base / f"{key}.log", base / f"{key}.json"

    @classmethod
    def get(cls, environment_id: int, payload: dict[str, Any]) -> tuple[Path, dict[str, Any]] | None:
        key = cls.identity(environment_id, payload)
        data_path, meta_path = cls._paths(key)
        try:
            if not data_path.is_file() or not meta_path.is_file():
                return None
            age = time.time() - min(data_path.stat().st_mtime, meta_path.stat().st_mtime)
            if age > cls.ttl():
                data_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
                return None
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta = meta if isinstance(meta, dict) else {}
            # A zero-row result is not stable: log rotation, delayed report upload or
            # a newly discovered module can make the same historical window valid
            # moments later. Never let an old empty result short-circuit planning.
            if max(0, int(meta.get("result_count") or 0)) == 0:
                data_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
                return None
            return data_path, meta
        except (OSError, ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
            return None

    @classmethod
    def invalidate_environment(cls, environment_id: int) -> int:
        """Drop final-result cache entries owned by one environment.

        Directory rescans change the resource catalog and therefore invalidate
        assumptions made by existing exact-window result caches.
        """
        removed = 0
        try:
            for meta_path in cls.directory().glob("*.json"):
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    if not isinstance(meta, dict) or int(meta.get("environment_id") or 0) != int(environment_id):
                        continue
                    data_path = meta_path.with_suffix(".log")
                    data_path.unlink(missing_ok=True)
                    meta_path.unlink(missing_ok=True)
                    removed += 1
                except (OSError, ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
                    continue
        except OSError:
            return removed
        return removed

    @classmethod
    def open_writer(cls, environment_id: int, payload: dict[str, Any], operation_id: str):
        key = cls.identity(environment_id, payload)
        data_path, meta_path = cls._paths(key)
        part_path = data_path.with_suffix(f".{operation_id or os.getpid()}.part")
        return key, data_path, meta_path, part_path

    @classmethod
    def _prune(cls, keep_path: Path | None = None) -> None:
        try:
            base = cls.directory()
            files = [path for path in base.glob("*.log") if path.is_file() and path != keep_path]
            now = time.time()
            for path in list(files):
                meta = path.with_suffix(".json")
                try:
                    if now - path.stat().st_mtime > cls.ttl():
                        path.unlink(missing_ok=True)
                        meta.unlink(missing_ok=True)
                        files.remove(path)
                except OSError:
                    continue
            total = sum(path.stat().st_size for path in files if path.exists())
            if keep_path is not None and keep_path.exists():
                total += keep_path.stat().st_size
            if total <= cls.max_total_bytes():
                return
            for path in sorted(files, key=lambda item: item.stat().st_mtime):
                if total <= cls.max_total_bytes():
                    break
                size = path.stat().st_size
                path.unlink(missing_ok=True)
                path.with_suffix(".json").unlink(missing_ok=True)
                total -= size
        except OSError:
            return

    @classmethod
    def commit(cls, *, data_path: Path, meta_path: Path, part_path: Path, metadata: dict[str, Any]) -> bool:
        try:
            if not part_path.is_file() or part_path.stat().st_size > cls.max_bytes():
                part_path.unlink(missing_ok=True)
                return False
            os.replace(part_path, data_path)
            temp_meta = meta_path.with_suffix(".json.part")
            temp_meta.write_text(json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(temp_meta, meta_path)
            cls._prune(keep_path=data_path)
            return True
        except OSError:
            try:
                part_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    @staticmethod
    def iter_file(path: Path, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                yield chunk
