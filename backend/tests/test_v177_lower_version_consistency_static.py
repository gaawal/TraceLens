from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
models = (ROOT / "apps/machines/models.py").read_text(encoding="utf-8")
serializers = (ROOT / "apps/machines/serializers.py").read_text(encoding="utf-8")
discovery = (ROOT / "apps/environments/services/discovery.py").read_text(encoding="utf-8")
environment_serializers = (ROOT / "apps/environments/serializers.py").read_text(encoding="utf-8")
views = (ROOT / "apps/environments/views.py").read_text(encoding="utf-8")
migration = (ROOT / "apps/machines/migrations/0004_machine_version_fields.py").read_text(encoding="utf-8")

assert 'software_version = models.CharField("软件版本"' in models
assert 'version_checked_at = models.DateTimeField("版本查询时间"' in models
assert '"software_version", "version_checked_at"' in serializers
assert "def read_machine_software_version" in discovery
assert "def read_environment_versions" in discovery
assert 'environment.machine_relations.select_related("target_machine").filter(is_active=True)' in discovery
assert 'version.strip() != upper_version.strip()' in discovery
assert '"mismatched_machine_ids": mismatched_machine_ids' in discovery
assert 'version_mismatch = serializers.SerializerMethodField()' in environment_serializers
assert 'version_mismatch_hosts = serializers.SerializerMethodField()' in environment_serializers
assert 'result = read_environment_versions(environment)' in views
assert 'name="software_version"' in migration
assert 'name="version_checked_at"' in migration

print("v177 lower version consistency static test passed")
