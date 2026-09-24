from django.db import migrations


def configure_run_log_path(apps, schema_editor):
    LogPathProfile = apps.get_model("environments", "LogPathProfile")
    for profile in LogPathProfile.objects.filter(category="run"):
        profile.display_name = "运行日志"
        profile.path_template = "/log/{username}/run"
        profile.enabled = True
        profile.scope = "each_machine"
        profile.match_rules = ["run_flat"]
        profile.save(update_fields=["display_name", "path_template", "enabled", "scope", "match_rules", "updated_at"])


class Migration(migrations.Migration):
    dependencies = [("environments", "0009_executor_log_path_strategy")]
    operations = [migrations.RunPython(configure_run_log_path, migrations.RunPython.noop)]
