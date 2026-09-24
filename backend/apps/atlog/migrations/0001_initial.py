from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="AtLogCaseAnalysisSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("case_url", models.URLField(max_length=2048, unique=True, verbose_name="用例链接")),
                ("case_id", models.CharField(blank=True, db_index=True, max_length=255, verbose_name="用例编号")),
                ("case_name", models.CharField(blank=True, max_length=255, verbose_name="用例名称")),
                ("case_status", models.CharField(blank=True, db_index=True, max_length=32, verbose_name="用例状态")),
                ("analysis_snapshot", models.JSONField(blank=True, default=dict, verbose_name="用例解析快照")),
                ("workspace_state", models.JSONField(blank=True, default=dict, verbose_name="定位工作区状态")),
                ("query_result_snapshot", models.JSONField(blank=True, default=dict, verbose_name="最近查询结果缓存")),
                ("ai_result_snapshot", models.JSONField(blank=True, default=dict, verbose_name="最近 AI 诊断结果")),
                ("ai_thinking_text", models.TextField(blank=True, verbose_name="最近 AI 分析过程")),
                ("ai_token_usage", models.JSONField(blank=True, default=dict, verbose_name="最近 AI Token 统计")),
                ("ai_job_id", models.CharField(blank=True, max_length=64, verbose_name="最近 AI 任务 ID")),
                ("ai_completed_at", models.DateTimeField(blank=True, null=True, verbose_name="最近 AI 完成时间")),
                ("ai_revision", models.PositiveIntegerField(default=0, verbose_name="AI 诊断版本")),
            ],
            options={"verbose_name": "ATLog 用例分析快照", "verbose_name_plural": "ATLog 用例分析快照", "ordering": ["-updated_at", "-id"]},
        ),
        migrations.AddIndex(model_name="atlogcaseanalysissnapshot", index=models.Index(fields=["case_status", "-updated_at"], name="atlog_snap_status_idx")),
        migrations.AddIndex(model_name="atlogcaseanalysissnapshot", index=models.Index(fields=["case_id", "-updated_at"], name="atlog_snap_caseid_idx")),
    ]
