from django.db import migrations, models


def preserve_existing_rules(apps, schema_editor):
    ResourceSettings = apps.get_model("environments", "ResourceSettings")
    for settings in ResourceSettings.objects.all():
        rules = settings.display_rules
        # v268/v269 无法区分“从未迁移”与“明确删除为空”。
        # 非空规则可以安全认定已初始化；空数组保持未初始化，给旧浏览器一次恢复机会。
        settings.display_rules_initialized = bool(isinstance(rules, list) and len(rules) > 0)
        if isinstance(rules, list) and len(rules) == 0:
            settings.display_rules = None
        settings.save(update_fields=["display_rules", "display_rules_initialized"])


class Migration(migrations.Migration):
    dependencies = [("environments", "0022_resource_settings_data_extraction_rules")]

    operations = [
        migrations.AlterField(
            model_name="resourcesettings",
            name="display_rules",
            field=models.JSONField(blank=True, default=None, null=True, verbose_name="日志语义规则"),
        ),
        migrations.AddField(
            model_name="resourcesettings",
            name="display_rules_initialized",
            field=models.BooleanField(default=False, verbose_name="日志语义规则已初始化"),
        ),
        migrations.RunPython(preserve_existing_rules, migrations.RunPython.noop),
    ]
