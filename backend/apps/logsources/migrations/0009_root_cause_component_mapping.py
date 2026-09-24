from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("logsources", "0008_executor_log_100_101_layout")]

    operations = [
        migrations.CreateModel(
            name="RootCauseComponentMapping",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("event_component", models.CharField(db_index=True, max_length=128, unique=True, verbose_name="Event组件名")),
                ("enabled", models.BooleanField(db_index=True, default=True, verbose_name="启用")),
                ("description", models.TextField(blank=True, verbose_name="说明")),
                ("matched_count", models.PositiveBigIntegerField(default=0, verbose_name="命中次数")),
                ("last_matched_at", models.DateTimeField(blank=True, null=True, verbose_name="最近命中时间")),
                ("module", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="root_cause_component_mappings", to="logsources.logfmdefinition", verbose_name="目标模块")),
                ("subsystem", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="root_cause_component_mappings", to="logsources.logsubsystemdefinition", verbose_name="目标子系统")),
            ],
            options={
                "verbose_name": "根因组件映射",
                "verbose_name_plural": "根因组件表",
                "ordering": ["event_component"],
            },
        ),
        migrations.AddIndex(
            model_name="rootcausecomponentmapping",
            index=models.Index(fields=["enabled", "event_component"], name="rootcomp_enabled_name_idx"),
        ),
    ]
