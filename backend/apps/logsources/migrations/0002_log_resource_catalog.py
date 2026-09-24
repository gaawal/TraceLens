from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("environments", "0004_log_path_profiles"),
        ("logsources", "0001_initial"),
        ("machines", "0002_station_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="LogResourceCatalog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("source_category", models.CharField(max_length=32, verbose_name="日志类型")),
                ("source_name", models.CharField(max_length=64, verbose_name="日志类型名称")),
                ("root", models.CharField(max_length=512, verbose_name="日志根目录")),
                ("status", models.CharField(choices=[("success", "成功"), ("error", "失败")], default="success", max_length=16, verbose_name="最近扫描状态")),
                ("message", models.TextField(blank=True, verbose_name="最近扫描信息")),
                ("scanned_at", models.DateTimeField(blank=True, null=True, verbose_name="最近扫描时间")),
                ("environment", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="log_resource_catalogs", to="environments.environment", verbose_name="环境")),
                ("machine", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="log_resource_catalogs", to="machines.machine", verbose_name="实际访问机器")),
            ],
            options={
                "verbose_name": "日志资源目录快照",
                "verbose_name_plural": "日志资源目录快照",
                "ordering": ["environment", "source_category", "machine", "root"],
            },
        ),
        migrations.CreateModel(
            name="LogResourceCatalogItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("subsystem", models.CharField(max_length=128, verbose_name="子系统")),
                ("fm", models.CharField(max_length=128, verbose_name="FM")),
                ("catalog", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="items", to="logsources.logresourcecatalog", verbose_name="资源快照")),
            ],
            options={
                "verbose_name": "日志资源目录项",
                "verbose_name_plural": "日志资源目录项",
                "ordering": ["subsystem", "fm"],
            },
        ),
        migrations.AddConstraint(
            model_name="logresourcecatalog",
            constraint=models.UniqueConstraint(fields=("environment", "machine", "source_category", "root"), name="uniq_environment_log_catalog_root"),
        ),
        migrations.AddIndex(
            model_name="logresourcecatalog",
            index=models.Index(fields=["environment", "source_category"], name="logcat_env_category_idx"),
        ),
        migrations.AddConstraint(
            model_name="logresourcecatalogitem",
            constraint=models.UniqueConstraint(fields=("catalog", "subsystem", "fm"), name="uniq_log_catalog_subsystem_fm"),
        ),
        migrations.AddIndex(
            model_name="logresourcecatalogitem",
            index=models.Index(fields=["catalog", "subsystem"], name="logcat_item_subsys_idx"),
        ),
    ]
