from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("audits", "0002_logsearchaudit_matched_files"),
        ("environments", "0012_resource_settings_dhh_debug_run_roots"),
    ]

    operations = [
        migrations.CreateModel(
            name="DataExtractionRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("source_operation_id", models.CharField(blank=True, db_index=True, max_length=128, verbose_name="来源操作ID")),
                ("environment_name", models.CharField(blank=True, max_length=128, verbose_name="环境名称快照")),
                ("task_name", models.CharField(blank=True, max_length=255, verbose_name="来源任务名称")),
                ("name", models.CharField(max_length=255, verbose_name="提取记录名称")),
                ("query_snapshot", models.JSONField(blank=True, default=dict, verbose_name="日志查询快照")),
                ("rule_snapshots", models.JSONField(blank=True, default=list, verbose_name="数据提取规则快照")),
                ("status", models.CharField(choices=[("running", "提取中"), ("success", "成功"), ("no_result", "未命中"), ("failed", "失败"), ("cancelled", "已停止")], db_index=True, default="running", max_length=16, verbose_name="执行状态")),
                ("matched_rule_count", models.PositiveIntegerField(default=0, verbose_name="命中规则数")),
                ("row_count", models.PositiveBigIntegerField(default=0, verbose_name="命中数据行数")),
                ("result_summary", models.JSONField(blank=True, default=list, verbose_name="规则命中摘要")),
                ("hour_summary", models.JSONField(blank=True, default=list, verbose_name="小时分片摘要")),
                ("error_message", models.TextField(blank=True, verbose_name="错误信息")),
                ("finished_at", models.DateTimeField(blank=True, null=True, verbose_name="完成时间")),
                ("environment", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="data_extraction_records", to="environments.environment", verbose_name="目标环境")),
                ("source_audit", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="data_extractions", to="audits.logsearchaudit", verbose_name="来源日志审计")),
            ],
            options={"verbose_name": "数据提取记录", "verbose_name_plural": "数据提取记录", "ordering": ["-created_at", "-id"]},
        ),
        migrations.AddIndex(model_name="dataextractionrecord", index=models.Index(fields=["-created_at", "status"], name="data_extract_time_status_idx")),
        migrations.AddIndex(model_name="dataextractionrecord", index=models.Index(fields=["environment", "-created_at"], name="data_extract_env_time_idx")),
    ]
