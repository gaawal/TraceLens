from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("environments", "0010_run_log_flat_strategy")]

    operations = [
        migrations.AddField(
            model_name="resourcesettings",
            name="dhh_executor_log_root",
            field=models.CharField(
                default="/data/sync/log/debug/elog/",
                help_text="DHH 环境执行器日志专用根目录；与上位机用户名、DHH SSH 登录用户名无关。",
                max_length=512,
                verbose_name="DHH 执行器日志根目录",
            ),
        ),
    ]
