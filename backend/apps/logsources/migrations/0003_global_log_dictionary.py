from django.db import migrations, models
import django.db.models.deletion


def seed_global_dictionary(apps, schema_editor):
    CatalogItem = apps.get_model("logsources", "LogResourceCatalogItem")
    Subsystem = apps.get_model("logsources", "LogSubsystemDefinition")
    Fm = apps.get_model("logsources", "LogFmDefinition")
    for item in CatalogItem.objects.select_related("catalog").iterator(chunk_size=1000):
        subsystem, _ = Subsystem.objects.get_or_create(
            name=item.subsystem,
            defaults={"last_discovered_at": item.catalog.scanned_at},
        )
        if item.catalog.scanned_at and not subsystem.last_discovered_at:
            subsystem.last_discovered_at = item.catalog.scanned_at
            subsystem.save(update_fields=["last_discovered_at"])
        Fm.objects.get_or_create(
            subsystem=subsystem,
            name=item.fm,
            defaults={"last_discovered_at": item.catalog.scanned_at},
        )


def reverse_seed(apps, schema_editor):
    # 回滚迁移时模型表会被删除，无需单独清理。
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("logsources", "0002_log_resource_catalog"),
    ]

    operations = [
        migrations.CreateModel(
            name="LogSubsystemDefinition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("name", models.CharField(max_length=128, unique=True, verbose_name="子系统标识")),
                ("display_name", models.CharField(blank=True, max_length=128, verbose_name="显示名称")),
                ("enabled", models.BooleanField(default=True, verbose_name="启用")),
                ("sort_order", models.IntegerField(default=0, verbose_name="排序")),
                ("description", models.TextField(blank=True, verbose_name="说明")),
                ("last_discovered_at", models.DateTimeField(blank=True, null=True, verbose_name="最近发现时间")),
            ],
            options={
                "verbose_name": "全局日志子系统",
                "verbose_name_plural": "全局日志子系统",
                "ordering": ["sort_order", "name"],
            },
        ),
        migrations.CreateModel(
            name="LogFmDefinition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("name", models.CharField(max_length=128, verbose_name="FM 标识")),
                ("display_name", models.CharField(blank=True, max_length=128, verbose_name="显示名称")),
                ("enabled", models.BooleanField(default=True, verbose_name="启用")),
                ("sort_order", models.IntegerField(default=0, verbose_name="排序")),
                ("description", models.TextField(blank=True, verbose_name="说明")),
                ("last_discovered_at", models.DateTimeField(blank=True, null=True, verbose_name="最近发现时间")),
                ("subsystem", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="fms", to="logsources.logsubsystemdefinition", verbose_name="子系统")),
            ],
            options={
                "verbose_name": "全局日志 FM",
                "verbose_name_plural": "全局日志 FM",
                "ordering": ["subsystem__sort_order", "subsystem__name", "sort_order", "name"],
            },
        ),
        migrations.AddIndex(
            model_name="logsubsystemdefinition",
            index=models.Index(fields=["enabled", "sort_order"], name="logsub_enabled_sort_idx"),
        ),
        migrations.AddConstraint(
            model_name="logfmdefinition",
            constraint=models.UniqueConstraint(fields=("subsystem", "name"), name="uniq_global_subsystem_fm"),
        ),
        migrations.AddIndex(
            model_name="logfmdefinition",
            index=models.Index(fields=["subsystem", "enabled", "sort_order"], name="logfm_sub_enabled_idx"),
        ),
        migrations.RunPython(seed_global_dictionary, reverse_seed),
    ]
