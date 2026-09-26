from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    import sys
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_physical_resource_scoped_cache_identity():
    module = _load("cache_identity_standalone", ROOT / "apps/logsources/services/cache_identity.py")
    Scope = module.LogCacheScope
    a = Scope("10.0.0.8", "root", "debug", "rs", "RSSSD")
    b = Scope("10.0.0.8", "root", "debug", "rs", "RSSSD")
    c = Scope("10.0.0.8", "tester", "debug", "rs", "RSSSD")
    d = Scope("10.0.0.9", "root", "debug", "rs", "RSSSD")
    assert a.redis_prefix == b.redis_prefix
    assert len({a.redis_prefix, c.redis_prefix, d.redis_prefix}) == 3
    assert "env-" not in a.redis_prefix
    assert "host-10.0.0.8" in a.redis_prefix
    assert "user-root" in a.redis_prefix
    assert "fm-RSSSD" in a.redis_prefix
    assert a.file_index_key("/log/root/debug/rs/RSSSD.log") != a.file_index_key("/other/RSSSD.log")

    # 同一 host 上的两台机器（同网段/NAT/模拟机群）必须落在不同命名空间，
    # 否则一家的内容缓存会被另一家命中。
    upper = Scope("10.0.0.8", "root", "debug", "rs", "RSSSD", 2222)
    lower = Scope("10.0.0.8", "root", "debug", "rs", "RSSSD", 2223)
    assert upper.redis_prefix != lower.redis_prefix
    assert "host-10.0.0.8_2222" in upper.redis_prefix
    # 不给端口时保持历史命名空间，旧缓存仍可读。
    assert Scope("10.0.0.8", "root", "debug", "rs", "RSSSD", None).redis_prefix == a.redis_prefix


def test_fingerprint_and_cache_contracts():
    source = (ROOT / "apps/logsources/services/file_index.py").read_text(encoding="utf-8")
    content = (ROOT / "apps/logsources/services/content_cache.py").read_text(encoding="utf-8")
    redis_store = (ROOT / "apps/logsources/services/redis_store.py").read_text(encoding="utf-8")
    remote = (ROOT / "apps/logsources/services/remote_logs.py").read_text(encoding="utf-8")

    assert 'return f"{self.inode}:{self.size}:{self.mtime}"' in source
    assert "-name {shlex.quote(fm + '.log')}" in source
    assert "-name {shlex.quote(fm + '_*.log')}" in source
    assert "fm + '_*.tar.gz'" in source
    assert "clauses.append(\"-name '*.tar.gz'\")" not in source
    assert "stat.size > old_size" in source
    assert "generation = str(cached.get(\"generation\")" in source
    assert "mode=nested_filename_only_v2" in source
    assert "_select_physical_candidates" in source
    assert "indexed_fms" not in source
    assert "missing_fms" not in source
    assert "self._scope(environment, target, subsystem, archive_fm)" in source
    assert "ordered_physical" in source
    assert "strategy=filename_time_fast_index" in source
    assert "fallback=sftp_listdir_attr" in source
    assert "fingerprint" in source and "start_time" in source and "end_time" in source

    assert "zlib.compress" in redis_store and "zlib.decompress" in redis_store
    assert "TRACELENS_LOG_CONTENT_CACHE_MAX_BYTES" in content
    assert "log_content_cache.get" in remote and "log_content_cache.put" in remote
    assert "content_window_stable" in remote
    assert 'artifact.kind == "current"' in remote
    assert "host=machine.host" in remote
    assert "username=machine.username" in remote


def test_actual_time_range_selection():
    archive = _load("archive_selector_standalone_v08", ROOT / "apps/logsources/services/archive_selector.py")
    Artifact = archive.LogArtifact
    items = [
        Artifact(1, "m", "rs", "RSSSD", "/a.log", "archived", None,
                 start_time=datetime(2026, 8, 7, 9, 0), end_time=datetime(2026, 8, 7, 9, 30)),
        Artifact(1, "m", "rs", "RSSSD", "/b.log", "archived", None,
                 start_time=datetime(2026, 8, 7, 9, 30), end_time=datetime(2026, 8, 7, 10, 0)),
    ]
    selected = archive.select_artifacts_for_window(
        items, datetime(2026, 8, 7, 9, 25), datetime(2026, 8, 7, 9, 35)
    )
    assert [item.path for item in selected] == ["/a.log", "/b.log"]


def test_file_index_helpers_runtime():
    import sys
    import types
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    django = types.ModuleType("django")
    django_conf = types.ModuleType("django.conf")
    django_conf.settings = types.SimpleNamespace()
    sys.modules.setdefault("django", django)
    sys.modules["django.conf"] = django_conf

    ssh_stub = types.ModuleType("apps.common.services.ssh")
    class SshOperationError(RuntimeError):
        pass
    ssh_stub.SshOperationError = SshOperationError
    sys.modules["apps.common.services.ssh"] = ssh_stub

    redis_stub = types.ModuleType("apps.logsources.services.redis_store")
    class RedisLogStore:
        data = {}
        @classmethod
        def get_json(cls, key):
            return cls.data.get(key)
        @classmethod
        def set_json(cls, key, value, ttl):
            cls.data[key] = value
            return True
    redis_stub.RedisLogStore = RedisLogStore
    sys.modules["apps.logsources.services.redis_store"] = redis_stub

    module = _load("file_index_runtime_v08", ROOT / "apps/logsources/services/file_index.py")
    expr = module._candidate_expression({"RSSSD", "ABC"})
    assert "RSSSD.log" in expr and "RSSSD_*.log" in expr
    assert "*.tar.gz" in expr

    raw = b"123\t456\t1780000000.1\t/log/debug/rs/RSSSD.log\0"
    stats = module._parse_find_output(raw)
    assert len(stats) == 1
    assert stats[0].fingerprint == "123:456:1780000000.1"

    reference = datetime(2026, 8, 7, 9, 0)
    assert module._parse_tar_boundary("archive_080102.tar.gz", reference) == datetime(2026, 8, 1, 2, 0)

    a = module.PhysicalCandidate(
        module.RemoteFileStat("/a", "RSSSD_080109.log", 1, 10, "1"),
        datetime(2026, 8, 1, 9, 0), "archived", frozenset({"RSSSD"})
    )
    b = module.PhysicalCandidate(
        module.RemoteFileStat("/b", "RSSSD_080110.log", 2, 10, "1"),
        datetime(2026, 8, 1, 10, 0), "archived", frozenset({"RSSSD"})
    )
    c = module.PhysicalCandidate(
        module.RemoteFileStat("/c", "RSSSD.log", 3, 10, "1"),
        None, "current", frozenset({"RSSSD"})
    )
    selected = module._select_physical_candidates(
        [a, b, c], fms={"RSSSD"},
        start=datetime(2026, 8, 1, 9, 20), end=datetime(2026, 8, 1, 9, 40)
    )
    # Filename close-boundaries are authoritative: 09:20~09:40 is contained
    # by the file closed at 10:00. Older 09:00 and live current.log are excluded.
    assert [item.stat.path for item in selected] == ["/b"]

    # current FM.log append keeps generation; truncate/rotate invalidates it.
    class FakeHandle:
        def __init__(self, payload):
            self.payload = payload
            self.pos = 0
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def stat(self): return types.SimpleNamespace(st_size=len(self.payload))
        def seek(self, pos): self.pos = pos
        def read(self, size=-1):
            if size < 0:
                out = self.payload[self.pos:]
                self.pos = len(self.payload)
            else:
                out = self.payload[self.pos:self.pos+size]
                self.pos += len(out)
            return out

    class FakeSftp:
        def __init__(self, payload): self.payload = payload
        def open(self, path, mode): return FakeHandle(self.payload)

    machine = types.SimpleNamespace(host="10.0.0.8", username="root", ssh_port=22)
    target = types.SimpleNamespace(machine=machine, source_category="debug")
    environment = types.SimpleNamespace(id=9)
    initial = b"[2026-08-07 09:00:00.000] a\n[2026-08-07 09:01:00.000] b\n"
    sftp = FakeSftp(initial)
    lease = types.SimpleNamespace(sftp=sftp)
    service = module.LogFileIndexService()
    stat1 = module.RemoteFileStat("/log/debug/rs/RSSSD.log", "RSSSD.log", 77, len(initial), "1.0")
    first = service._load_or_probe_direct(
        environment=environment, target=target, lease=lease, subsystem="rs", fm="RSSSD", stat=stat1, kind="current"
    )
    gen1 = first["generation"]
    assert first["start_time"].startswith("2026-08-07T09:00:00")

    appended = initial + b"[2026-08-07 09:02:00.000] c\n"
    sftp.payload = appended
    stat2 = module.RemoteFileStat("/log/debug/rs/RSSSD.log", "RSSSD.log", 77, len(appended), "2.0")
    second = service._load_or_probe_direct(
        environment=environment, target=target, lease=lease, subsystem="rs", fm="RSSSD", stat=stat2, kind="current"
    )
    assert second["generation"] == gen1
    assert second["end_time"].startswith("2026-08-07T09:02:00")

    truncated = b"[2026-08-07 10:00:00.000] new\n"
    sftp.payload = truncated
    stat3 = module.RemoteFileStat("/log/debug/rs/RSSSD.log", "RSSSD.log", 77, len(truncated), "3.0")
    third = service._load_or_probe_direct(
        environment=environment, target=target, lease=lease, subsystem="rs", fm="RSSSD", stat=stat3, kind="current"
    )
    assert third["generation"] != gen1
    assert third["start_time"].startswith("2026-08-07T10:00:00")


if __name__ == "__main__":
    test_physical_resource_scoped_cache_identity()
    test_fingerprint_and_cache_contracts()
    test_actual_time_range_selection()
    test_file_index_helpers_runtime()
    print("v0.8 Redis fingerprint/content cache contracts passed")
