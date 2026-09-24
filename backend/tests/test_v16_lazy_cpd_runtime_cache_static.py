from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = (ROOT / "apps/reports/services.py").read_text(encoding="utf-8")
ENV = (ROOT / "apps/environments/views.py").read_text(encoding="utf-8")
REDIS = (ROOT / "apps/logsources/services/redis_store.py").read_text(encoding="utf-8")
SETTINGS = (ROOT / "config/settings.py").read_text(encoding="utf-8")

assert "cpd.snapshot.progress" not in REPORT
assert 'f"summary:{identity}:{fingerprint}"' in REPORT
assert 'index_mode = "page"' in REPORT
assert 'index_mode = "range"' in REPORT
assert "cpd.query.page.cache_miss" in REPORT
assert "get_json_many" in REPORT
assert "client.mget(keys)" in REDIS
assert "TRACELENS_RUNTIME_STATUS_TTL" in SETTINGS
assert "environment.runtime_status.cache_hit" in ENV
assert "topology-" in ENV
assert 'f"host-{safe_component(upper.host)}:user-{safe_component(upper.username)}' in ENV
assert "environment_id" not in ENV[ENV.index("def _runtime_status_cache_key"):ENV.index("def _hydrate_runtime_status")]
print("v0.16 lazy CPD + runtime status cache static test passed")
