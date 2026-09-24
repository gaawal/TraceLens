from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True
    dependencies = [("environments", "0012_resource_settings_dhh_debug_run_roots")]

    operations = [
        migrations.CreateModel(
            name="AbnormalCase",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("name", models.CharField(max_length=255, verbose_name="案例名称")),
                ("category", models.CharField(blank=True, max_length=128, verbose_name="故障分类")),
                ("symptom", models.TextField(blank=True, verbose_name="故障现象")),
                ("root_cause", models.TextField(blank=True, verbose_name="根因")),
                ("solution", models.TextField(blank=True, verbose_name="处理建议")),
                ("description", models.TextField(blank=True, verbose_name="补充说明")),
                ("tags", models.JSONField(blank=True, default=list, verbose_name="标签")),
                ("enabled", models.BooleanField(db_index=True, default=True, verbose_name="启用")),
                ("source_operation_id", models.CharField(blank=True, db_index=True, max_length=128, verbose_name="来源日志操作ID")),
                ("source_task_name", models.CharField(blank=True, max_length=255, verbose_name="来源任务名称")),
                ("environment_name", models.CharField(blank=True, max_length=128, verbose_name="环境名称快照")),
                ("query_snapshot", models.JSONField(blank=True, default=dict, verbose_name="日志查询快照")),
                ("evidences", models.JSONField(default=list, verbose_name="异常举证")),
                ("fingerprint_version", models.PositiveIntegerField(default=3, verbose_name="指纹版本")),
                ("evidence_count", models.PositiveIntegerField(default=0, verbose_name="举证条数")),
                ("matched_count", models.PositiveBigIntegerField(default=0, verbose_name="历史高相似命中次数")),
                ("last_matched_at", models.DateTimeField(blank=True, null=True, verbose_name="最近命中时间")),
                ("environment", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="abnormal_cases", to="environments.environment", verbose_name="来源环境")),
            ],
            options={"verbose_name": "异常案例", "verbose_name_plural": "异常案例", "ordering": ["-updated_at", "-id"]},
        ),
        migrations.AddIndex(model_name="abnormalcase", index=models.Index(fields=["enabled", "-updated_at"], name="abcase_enabled_time_idx")),
        migrations.AddIndex(model_name="abnormalcase", index=models.Index(fields=["environment", "-updated_at"], name="abcase_env_time_idx")),
        migrations.AddIndex(model_name="abnormalcase", index=models.Index(fields=["category", "-updated_at"], name="abcase_cat_time_idx")),
    ]
