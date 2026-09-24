# Generated manually for TraceLens log audit feature.
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("environments", "0010_run_log_flat_strategy"),
    ]

    operations = [
        migrations.CreateModel(
            name="LogSearchAudit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("operation_id", models.CharField(db_index=True, max_length=128, verbose_name="操作ID")),
                ("action", models.CharField(choices=[("log_search", "日志检索")], db_index=True, default="log_search", max_length=32, verbose_name="操作类型")),
                ("operator_username", models.CharField(blank=True, max_length=150, verbose_name="操作用户")),
                ("client_ip", models.CharField(blank=True, db_index=True, max_length=64, verbose_name="操作IP")),
                ("environment_name", models.CharField(blank=True, max_length=128, verbose_name="环境名称快照")),
                ("target_host", models.CharField(blank=True, db_index=True, max_length=255, verbose_name="环境IP快照")),
                ("target_username", models.CharField(blank=True, max_length=128, verbose_name="环境用户快照")),
                ("start_time", models.DateTimeField(verbose_name="检索开始时间")),
                ("end_time", models.DateTimeField(verbose_name="检索结束时间")),
                ("source_categories", models.JSONField(blank=True, default=list, verbose_name="日志类型")),
                ("source_categories_text", models.TextField(blank=True, verbose_name="日志类型检索索引")),
                ("keyword", models.TextField(blank=True, verbose_name="关键字")),
                ("request_payload", models.JSONField(blank=True, default=dict, verbose_name="检索参数")),
                ("result", models.CharField(choices=[("running", "执行中"), ("success", "成功"), ("no_result", "无结果"), ("failed", "失败"), ("cancelled", "已停止")], db_index=True, default="running", max_length=16, verbose_name="执行结果")),
                ("error_message", models.TextField(blank=True, verbose_name="错误信息")),
                ("artifact_count", models.PositiveIntegerField(default=0, verbose_name="候选日志文件数")),
                ("result_count", models.PositiveIntegerField(default=0, verbose_name="检索结果条数")),
                ("output_bytes", models.BigIntegerField(default=0, verbose_name="输出字节数")),
                ("finished_at", models.DateTimeField(blank=True, null=True, verbose_name="完成时间")),
                ("duration_ms", models.PositiveBigIntegerField(blank=True, null=True, verbose_name="耗时毫秒")),
                ("environment", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="log_search_audits", to="environments.environment", verbose_name="目标环境")),
            ],
            options={"verbose_name": "日志检索审计", "verbose_name_plural": "日志检索审计", "ordering": ["-created_at", "-id"]},
        ),
        migrations.CreateModel(
            name="LogSearchAuditTarget",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("subsystem", models.CharField(max_length=128, verbose_name="子系统")),
                ("module", models.CharField(max_length=128, verbose_name="模块")),
                ("kind", models.CharField(default="normal", max_length=32, verbose_name="模块类型")),
                ("audit", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="targets", to="audits.logsearchaudit", verbose_name="审计记录")),
            ],
            options={"verbose_name": "日志检索审计目标", "verbose_name_plural": "日志检索审计目标", "ordering": ["subsystem", "module", "kind", "id"]},
        ),
        migrations.AddIndex(model_name="logsearchaudit", index=models.Index(fields=["-created_at", "result"], name="audit_time_result_idx")),
        migrations.AddIndex(model_name="logsearchaudit", index=models.Index(fields=["environment", "-created_at"], name="audit_env_time_idx")),
        migrations.AddIndex(model_name="logsearchaudittarget", index=models.Index(fields=["subsystem", "module"], name="audit_target_sm_idx")),
        migrations.AddConstraint(model_name="logsearchaudittarget", constraint=models.UniqueConstraint(fields=("audit", "subsystem", "module", "kind"), name="uniq_audit_target")),
    ]
