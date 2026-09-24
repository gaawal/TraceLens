from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("environments", "0016_environmentdeployment_stopping_status")]

    operations = [
        migrations.AlterField(
            model_name="deploymentstep",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "等待中"),
                    ("running", "执行中"),
                    ("stopped", "已停止"),
                    ("success", "成功"),
                    ("failed", "失败"),
                    ("skipped", "已跳过"),
                ],
                default="pending",
                max_length=16,
                verbose_name="状态",
            ),
        ),
    ]
