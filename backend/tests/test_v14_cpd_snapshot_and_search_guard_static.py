from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
services = (ROOT / 'apps/reports/services.py').read_text(encoding='utf-8')
serializers = (ROOT / 'apps/logsources/serializers.py').read_text(encoding='utf-8')

assert 'host-{safe_component(machine.host)}:user-{safe_component(machine.username)}:catalog' in services
assert 'refresh_unchanged' in services
assert r"-printf '%s\\t%T@\\t%p\\n'" in services
assert 'RedisLogStore.set_json(cache_key, stored, ttl)' in services
key_fn = services.split('def _report_catalog_cache_key', 1)[1].split('def _report_summary_cache_key', 1)[0]
assert 'environment.id' not in key_fn
assert '请至少选择一个子系统/模块' in serializers
assert 'fm_targets' in serializers
print('v0.14 CPD snapshot / no-global-scan contracts passed')
