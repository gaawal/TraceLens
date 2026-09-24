from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("environments", "0017_deploymentstep_stopped_status")]

    operations = [
        migrations.AddField(
            model_name="environmentdeployment",
            name="scheduled_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True, verbose_name="计划执行时间"),
        ),
        migrations.AlterField(
            model_name="environmentdeployment",
            name="status",
            field=models.CharField(
                choices=[
                    ("scheduled", "待执行"),
                    ("pending", "等待中"),
                    ("running", "部署中"),
                    ("stopping", "停止中"),
                    ("stopped", "已停止"),
                    ("success", "成功"),
                    ("failed", "失败"),
                ],
                default="pending",
                max_length=16,
                verbose_name="状态",
            ),
        ),
    ]
