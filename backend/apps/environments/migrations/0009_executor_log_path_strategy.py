from django.db import migrations


def configure_executor_log_path(apps, schema_editor):
    LogPathProfile = apps.get_model("environments", "LogPathProfile")
    for profile in LogPathProfile.objects.filter(category="executor"):
        profile.path_template = "/log/{username}/debug/elog"
        profile.scope = "upper_only"
        profile.match_rules = ["executor_tree"]
        profile.save(update_fields=["path_template", "scope", "match_rules", "updated_at"])


class Migration(migrations.Migration):
    dependencies = [("environments", "0008_resource_settings_source_code_public_paths")]
    operations = [migrations.RunPython(configure_executor_log_path, migrations.RunPython.noop)]
