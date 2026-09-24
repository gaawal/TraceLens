from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_with_settings(**values):
    django = types.ModuleType("django")
    django_conf = types.ModuleType("django.conf")
    defaults = dict(
        TRACELENS_REDIS_ENABLED=True,
        TRACELENS_REDIS_URL="127.0.0.1:6379",
        TRACELENS_REDIS_HOST="127.0.0.1",
        TRACELENS_REDIS_PORT=6379,
        TRACELENS_REDIS_DB=3,
        TRACELENS_REDIS_USERNAME="",
        TRACELENS_REDIS_PASSWORD="secret",
        TRACELENS_REDIS_CONNECT_TIMEOUT=1.0,
        TRACELENS_REDIS_SOCKET_TIMEOUT=2.0,
        TRACELENS_REDIS_RETRY_SECONDS=30,
    )
    defaults.update(values)
    django_conf.settings = types.SimpleNamespace(**defaults)
    sys.modules.setdefault("django", django)
    sys.modules["django.conf"] = django_conf
    spec = importlib.util.spec_from_file_location(
        "redis_store_v09", ROOT / "apps/logsources/services/redis_store.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["redis_store_v09"] = module
    spec.loader.exec_module(module)
    return module


def test_raw_host_port_is_normalized_and_password_is_forwarded():
    module = _load_with_settings()
    calls = {}

    class FakeClient:
        def ping(self): return True

    class FakeRedisClass:
        @classmethod
        def from_url(cls, url, **kwargs):
            calls["url"] = url
            calls["kwargs"] = kwargs
            return FakeClient()

    module.redis = types.SimpleNamespace(Redis=FakeRedisClass)
    module.RedisLogStore._client = None
    module.RedisLogStore._unavailable_until = 0
    client = module.RedisLogStore._get_client()
    assert client is not None
    assert calls["url"] == "redis://127.0.0.1:6379/3"
    assert calls["kwargs"]["password"] == "secret"
    assert "secret" not in module.RedisLogStore._endpoint_label()


def test_discrete_host_config_is_supported():
    module = _load_with_settings(TRACELENS_REDIS_URL="", TRACELENS_REDIS_PASSWORD="pw")
    calls = {}

    class FakeClient:
        def ping(self): return True

    class FakeRedisClass:
        def __init__(self, **kwargs):
            calls.update(kwargs)
        @classmethod
        def from_url(cls, url, **kwargs):
            raise AssertionError("from_url should not be used")
        def ping(self): return True

    module.redis = types.SimpleNamespace(Redis=FakeRedisClass)
    module.RedisLogStore._client = None
    module.RedisLogStore._unavailable_until = 0
    module.RedisLogStore._get_client()
    assert calls["host"] == "127.0.0.1"
    assert calls["port"] == 6379
    assert calls["db"] == 3
    assert calls["password"] == "pw"
