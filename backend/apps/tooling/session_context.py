import json
import uuid
from django.core.cache import cache

PREFIX = "tracelens:ai:session:"

def ensure_session_id(value: str | None):
    return value or f"ses_{uuid.uuid4().hex[:20]}"

def save_session_context(session_id, context=None, messages=None):
    if not session_id:
        return
    key = PREFIX + session_id
    old = cache.get(key) or {}
    if context is not None:
        old["context"] = context
    if messages is not None:
        old["messages"] = messages[-50:]
    old["session_id"] = session_id
    cache.set(key, old, timeout=86400)
    return old

def load_session_context(session_id):
    return cache.get(PREFIX + session_id) if session_id else None
