from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def text(rel):
    return (ROOT / rel).read_text(encoding="utf-8")

def test_executor_log_strategy_contracts():
    models = text("apps/environments/models.py")
    serializers = text("apps/environments/serializers.py")
    remote = text("apps/logsources/services/remote_logs.py")
    index = text("apps/logsources/services/file_index.py")
    migration = text("apps/environments/migrations/0009_executor_log_path_strategy.py")

    assert '/log/{username}/debug/elog' in models
    assert '["executor_tree"] if category == LogPathCategory.EXECUTOR' in models
    assert '"executor_tree"' in serializers
    assert 'if "executor_tree" in target_rules' in remote
    assert 'collect_executor_tree_artifacts' in remote
    assert 'environment.machine_relations.select_related("target_machine")' in index
    assert 'lower_root = f"{root}/{lower_ip}"' in index
    assert 'discover_executor_candidate_stats(' in index
    assert 'modules={fm for values in modules_by_subsystem.values() for fm in values}' in index
    assert 'window_start=window_start, window_end=window_end' in index
    assert 'item.filename in subsystems' in index
    assert 'endswith(".log")' in index
    assert 'endswith(".tar.gz")' in index
    assert '_probe_tar_member_range' in index
    assert '_indexed_range_overlaps' in index
    assert 'profile.scope = "upper_only"' in migration
    assert 'profile.match_rules = ["executor_tree"]' in migration

if __name__ == "__main__":
    test_executor_log_strategy_contracts()
    print("executor log strategy static contracts passed")
