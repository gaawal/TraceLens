from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("environments", "0015_environmentdeployment_dhh_user")]

    operations = [
        migrations.AlterField(
            model_name="environmentdeployment",
            name="status",
            field=models.CharField(
                choices=[
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
