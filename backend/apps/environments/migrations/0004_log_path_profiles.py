from django.db import migrations, models
import django.db.models.deletion


def seed_profiles(apps, schema_editor):
    ResourceSettings = apps.get_model("environments", "ResourceSettings")
    LogPathProfile = apps.get_model("environments", "LogPathProfile")
    settings_obj, _ = ResourceSettings.objects.get_or_create(pk=1)
    defaults = [
        ("debug", "调试日志", "/log/{username}/debug", True, "each_machine", 10),
        ("run", "运行日志", "/log/{username}/run", False, "each_machine", 20),
        ("executor", "执行器日志", "/log/l00021753/debug/elog/{lower_machine_ip}", True, "upper_for_lower", 30),
        ("helf", "HELF日志", "", False, "each_machine", 40),
        ("sil", "SIL仿真日志", "", False, "each_machine", 50),
    ]
    for category, name, template, enabled, scope, order in defaults:
        LogPathProfile.objects.get_or_create(
            settings=settings_obj,
            category=category,
            defaults={
                "display_name": name,
                "path_template": template,
                "enabled": enabled,
                "scope": scope,
                "sort_order": order,
            },
        )


class Migration(migrations.Migration):
    dependencies = [("environments", "0003_remove_legacy_discovery_configuration")]
    operations = [
        migrations.CreateModel(
            name="LogPathProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("category", models.CharField(choices=[("debug", "调试日志"), ("run", "运行日志"), ("executor", "执行器日志"), ("helf", "HELF日志"), ("sil", "SIL仿真日志")], max_length=32, verbose_name="日志类型")),
                ("display_name", models.CharField(max_length=64, verbose_name="显示名称")),
                ("path_template", models.CharField(blank=True, max_length=512, verbose_name="目录模板")),
                ("enabled", models.BooleanField(default=False, verbose_name="启用检索")),
                ("scope", models.CharField(choices=[("each_machine", "每台机器"), ("upper_for_lower", "上位机按下位机展开")], default="each_machine", max_length=32, verbose_name="展开范围")),
                ("sort_order", models.PositiveSmallIntegerField(default=0, verbose_name="排序")),
                ("settings", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="log_path_profiles", to="environments.resourcesettings", verbose_name="资源设置")),
            ],
            options={"verbose_name": "日志路径配置", "verbose_name_plural": "日志路径配置", "ordering": ["sort_order", "id"]},
        ),
        migrations.AddConstraint(
            model_name="logpathprofile",
            constraint=models.UniqueConstraint(fields=("settings", "category"), name="uniq_resource_log_path_category"),
        ),
        migrations.RunPython(seed_profiles, migrations.RunPython.noop),
    ]
