from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_normal_archive_cache_is_scoped_to_real_fm():
    index = read("apps/logsources/services/file_index.py")
    assert 'self._scope(environment, target, subsystem, archive_fm)' in index
    assert 'self._scope(environment, target, subsystem, "__archive__")' not in index
    assert '"fm": archive_fm' in index
    assert '"indexed_fms"' not in index


def test_executor_archive_cache_is_scoped_to_lower_machine_and_module():
    index = read("apps/logsources/services/file_index.py")
    assert 'f"{lower_ip}/{module_name}"' in index
    assert '__executor_archive__' not in index
    assert 'mode") == "executor_tree_module_nested_filename_only_v2"' in index
    assert 'module_name not in missing_modules' in index


def test_executor_queries_every_configured_lower_machine_folder():
    index = read("apps/logsources/services/file_index.py")
    remote = read("apps/logsources/services/remote_logs.py")
    assert 'environment.machine_relations.select_related("target_machine")' in index
    assert 'for lower_index, lower_ip in enumerate(lower_dirs' in index
    assert 'environment.machine_relations.select_related("target_machine")' in remote
    assert 'for lower_host in sorted(executor_lower_hosts)' in remote
