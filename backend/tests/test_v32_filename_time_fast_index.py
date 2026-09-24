from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_file_index():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    django = types.ModuleType("django")
    django_conf = types.ModuleType("django.conf")
    django_conf.settings = types.SimpleNamespace()
    sys.modules.setdefault("django", django)
    sys.modules["django.conf"] = django_conf

    ssh_stub = types.ModuleType("apps.common.services.ssh")
    ssh_stub.SshOperationError = RuntimeError
    sys.modules["apps.common.services.ssh"] = ssh_stub

    redis_stub = types.ModuleType("apps.logsources.services.redis_store")
    class RedisLogStore:
        @classmethod
        def get_json(cls, key): return None
        @classmethod
        def set_json(cls, key, value, ttl): return True
    redis_stub.RedisLogStore = RedisLogStore
    sys.modules["apps.logsources.services.redis_store"] = redis_stub
    return _load("file_index_runtime_v32", ROOT / "apps/logsources/services/file_index.py")


def test_daily_archive_yyyymmdd_and_legacy_8_digit_are_disambiguated():
    selector = _load("archive_selector_runtime_v32", ROOT / "apps/logsources/services/archive_selector.py")
    reference = datetime(2026, 8, 10, 10, 0)
    assert selector.parse_archive_timestamp("20260801", reference) == datetime(2026, 8, 1, 0, 0)
    assert selector.parse_archive_timestamp("08010329", reference) == datetime(2026, 8, 1, 3, 29)
    assert selector.parse_archive_timestamp("20260810032944317", reference) == datetime(2026, 8, 10, 3, 29, 44, 317000)


def test_candidate_expression_never_admits_other_fm_tar():
    module = _load_file_index()
    expr = module._candidate_expression({"rspmp"})
    assert "rspmp.log" in expr
    assert "rspmp_*.log" in expr
    assert "rspmp_*.tar.gz" in expr
    assert "-name '*.tar.gz'" not in expr
    assert module._name_is_candidate("rspmp_20260801.tar.gz", {"rspmp"})
    assert not module._name_is_candidate("spwipm_20260801.tar.gz", {"rspmp"})


def test_daily_tar_wins_and_unrelated_dates_are_excluded():
    module = _load_file_index()
    fm = "rspmp"
    tar_aug1 = module.PhysicalCandidate(
        module.RemoteFileStat("/rspmp_20260801.tar.gz", "rspmp_20260801.tar.gz", 1, 10, "1"),
        datetime(2026, 8, 1), "tar", frozenset({fm}),
    )
    tar_aug2 = module.PhysicalCandidate(
        module.RemoteFileStat("/rspmp_20260802.tar.gz", "rspmp_20260802.tar.gz", 2, 10, "1"),
        datetime(2026, 8, 2), "tar", frozenset({fm}),
    )
    loose_next = module.PhysicalCandidate(
        module.RemoteFileStat("/rspmp_20260802010000000.log", "rspmp_20260802010000000.log", 3, 10, "1"),
        datetime(2026, 8, 2, 1), "archived", frozenset({fm}),
    )
    current = module.PhysicalCandidate(
        module.RemoteFileStat("/rspmp.log", "rspmp.log", 4, 10, "1"),
        None, "current", frozenset({fm}),
    )
    selected = module._select_physical_candidates(
        [tar_aug1, tar_aug2, loose_next, current], fms={fm},
        start=datetime(2026, 8, 1, 10), end=datetime(2026, 8, 1, 11),
    )
    assert [item.stat.name for item in selected] == ["rspmp_20260801.tar.gz"]


def test_rotated_log_uses_close_boundary_and_current_only_after_latest():
    module = _load_file_index()
    fm = "spwipm"
    a = module.PhysicalCandidate(
        module.RemoteFileStat("/spwipm_20260810030000000.log", "spwipm_20260810030000000.log", 1, 10, "1"),
        datetime(2026, 8, 10, 3, 0), "archived", frozenset({fm}),
    )
    b = module.PhysicalCandidate(
        module.RemoteFileStat("/spwipm_20260810032944317.log", "spwipm_20260810032944317.log", 2, 10, "1"),
        datetime(2026, 8, 10, 3, 29, 44, 317000), "archived", frozenset({fm}),
    )
    current = module.PhysicalCandidate(
        module.RemoteFileStat("/spwipm.log", "spwipm.log", 3, 10, "1"),
        None, "current", frozenset({fm}),
    )
    archived_window = module._select_physical_candidates(
        [a, b, current], fms={fm},
        start=datetime(2026, 8, 10, 3, 10), end=datetime(2026, 8, 10, 3, 20),
    )
    assert [item.stat.name for item in archived_window] == ["spwipm_20260810032944317.log"]

    live_window = module._select_physical_candidates(
        [a, b, current], fms={fm},
        start=datetime(2026, 8, 10, 3, 40), end=datetime(2026, 8, 10, 3, 50),
    )
    assert [item.stat.name for item in live_window] == ["spwipm.log"]


def test_window_expression_does_not_enumerate_unrelated_daily_archives():
    module = _load_file_index()
    expr = module._window_candidate_expression(
        {"rspmp"},
        start=datetime(2026, 8, 10, 1, 0),
        end=datetime(2026, 8, 10, 1, 10),
    )
    assert "rspmp_20260810.tar.gz" in expr
    assert "rspmp_20260810*.log" in expr
    # Next-day rotated logs are allowed only as close-boundary fallback.
    assert "rspmp_20260811*.log" in expr
    assert "rspmp_*.tar.gz" not in expr
    assert "20260809.tar.gz" not in expr
    assert "20260811.tar.gz" not in expr


def test_run_and_executor_tar_indexing_is_filename_only_before_streaming():
    source = (ROOT / "apps/logsources/services/file_index.py").read_text(encoding="utf-8")
    run_index = source[source.index("    def _load_or_probe_run_tar("):source.index("    def collect_run_flat_artifacts(")]
    executor_index = source[source.index("    def _load_or_probe_executor_tar("):source.index("    def collect_executor_tree_artifacts(")]
    executor_collect = source[source.index("    def collect_executor_tree_artifacts("):source.index("    def collect_artifacts(")]
    assert "_probe_tar_member_range" not in run_index
    assert "run_nested_filename_only_v2" in run_index
    assert "_probe_tar_member_range" not in executor_index
    assert "executor_tree_module_nested_filename_only_v2" in executor_index
    assert "selected_tar_paths" in executor_collect
    assert "stat.path not in selected_tar_paths" in executor_collect


def test_selected_daily_archive_recursively_indexes_nested_tar_members(tmp_path):
    import io
    import subprocess
    import tarfile

    module = _load_file_index()

    leaf_log = tmp_path / "RSRCP.log"
    leaf_log.write_text(
        "[2026-09-05 19:10:00.100] nested log line\n",
        encoding="utf-8",
    )
    inner_tar = tmp_path / "RSRCP_20260905191137164.tar.gz"
    with tarfile.open(inner_tar, "w:gz") as tf:
        tf.add(leaf_log, arcname="RSRCP.log")

    direct_log = tmp_path / "RSRCP_20260905120000000.log"
    direct_log.write_text(
        "[2026-09-05 11:59:59.900] direct log line\n",
        encoding="utf-8",
    )
    outer_tar = tmp_path / "RSRCP_20260905.tar.gz"
    with tarfile.open(outer_tar, "w:gz") as tf:
        tf.add(direct_log, arcname=direct_log.name)
        tf.add(inner_tar, arcname=inner_tar.name)

    class _Channel:
        def __init__(self, code): self.code = code
        def recv_exit_status(self): return self.code

    class _Stream(io.BytesIO):
        def __init__(self, data, code):
            super().__init__(data)
            self.channel = _Channel(code)

    class _Client:
        def exec_command(self, command, timeout=None):
            completed = subprocess.run(
                ["bash", "-lc", command], capture_output=True, check=False,
            )
            return None, _Stream(completed.stdout, completed.returncode), _Stream(completed.stderr, completed.returncode)

    walked = module._walk_nested_tar_members(_Client(), str(outer_tar), max_nested_depth=3)
    encoded = {
        module._encode_archive_member_chain((*chain, member))
        for chain, member in walked
    }
    assert direct_log.name in encoded
    assert f"{inner_tar.name}::RSRCP.log" in encoded

    # A boundary-less RSRCP.log inside the timestamped inner archive inherits
    # the inner archive close time rather than being treated as an unbounded
    # current member of the outer daily package.
    match = module._match_tar_fm(inner_tar.name, {"RSRCP"}, datetime(2026, 9, 5, 20, 0))
    assert match == ("RSRCP", datetime(2026, 9, 5, 19, 11, 37, 164000))


def test_remote_tar_stream_supports_member_chain():
    source = (ROOT / "apps/logsources/services/remote_logs.py").read_text(encoding="utf-8")
    assert 'def _tar_member_extract_pipeline(' in source
    assert 'split(_ARCHIVE_CHAIN_SEPARATOR)' in source
    assert 'command += f" | tar -xOzf - {shlex.quote(member)}"' in source
    assert 'display_name = " → ".join' in source
