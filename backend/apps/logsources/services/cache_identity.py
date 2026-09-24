from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_component(value: object, *, limit: int = 80) -> str:
    text = _SAFE_RE.sub("_", str(value or "").strip()) or "-"
    if len(text) <= limit:
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"{text[: max(1, limit - 13)]}-{digest}"


def short_digest(value: str, length: int = 20) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


@dataclass(frozen=True, slots=True)
class LogCacheScope:
    """Redis namespace for one physical resource/FM.

    Environment database ids are deliberately excluded. An Environment is only a
    temporary workspace binding; cache ownership follows the physical upper-machine
    identity (host + username), then source/subsystem/FM. This allows the same
    machine cache to survive rebinding while preventing cross-resource pollution.
    """

    host: str
    username: str
    source_category: str
    subsystem: str
    fm: str

    @property
    def redis_prefix(self) -> str:
        return ":".join(
            [
                "tracelens",
                "v3",
                f"host-{safe_component(self.host)}",
                f"user-{safe_component(self.username)}",
                f"src-{safe_component(self.source_category)}",
                f"sub-{safe_component(self.subsystem)}",
                f"fm-{safe_component(self.fm)}",
            ]
        )

    def file_index_key(self, path: str) -> str:
        return f"{self.redis_prefix}:file:{short_digest(path, 24)}"

    def archive_members_key(self, path: str) -> str:
        return f"{self.redis_prefix}:archive:{short_digest(path, 24)}"

    def content_key(
        self,
        *,
        path: str,
        member_name: str,
        generation: str,
        start_token: str,
        end_token: str,
    ) -> str:
        identity = f"{path}::{member_name}" if member_name else path
        return (
            f"{self.redis_prefix}:content:{short_digest(identity, 24)}:"
            f"gen-{safe_component(generation, limit=48)}:{start_token}:{end_token}"
        )
