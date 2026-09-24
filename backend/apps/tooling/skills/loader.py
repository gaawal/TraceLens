from __future__ import annotations

from functools import lru_cache
from pathlib import Path


_SKILLS_ROOT = Path(__file__).resolve().parent

_DOMAIN_SKILL_MAP = {
    "atlog": "atlog-analysis",
    "logs": "logs",
    "deployment": "deployment",
    "data": "data",
}


def skill_path(skill_id: str) -> Path:
    safe = str(skill_id or "").strip()
    if not safe or any(part in safe for part in ("..", "/", "\\")):
        raise ValueError("Skill 名称非法")
    return (_SKILLS_ROOT / safe / "SKILL.md").resolve()


@lru_cache(maxsize=32)
def load_skill(skill_id: str) -> str:
    path = skill_path(skill_id)
    if _SKILLS_ROOT not in path.parents or not path.is_file():
        raise FileNotFoundError(f"Skill 不存在: {skill_id}")
    return path.read_text(encoding="utf-8").strip()


def domain_skill_id(skill_id: str | None) -> str | None:
    key = str(skill_id or "").strip().lower()
    return _DOMAIN_SKILL_MAP.get(key)


def load_domain_skill(skill_id: str | None) -> str:
    mapped = domain_skill_id(skill_id)
    if not mapped:
        return ""
    try:
        return load_skill(mapped)
    except FileNotFoundError:
        return ""
