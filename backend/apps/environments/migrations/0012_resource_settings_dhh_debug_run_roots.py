from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("environments", "0011_resource_settings_dhh_executor_log_root"),
    ]

    operations = [
        migrations.AddField(
            model_name="resourcesettings",
            name="dhh_debug_log_root",
            field=models.CharField(
                default="/data/sync/log/debug/",
                help_text="DHH 环境调试日志专用根目录；不继承普通上位机日志路径。",
                max_length=512,
                verbose_name="DHH 调试日志根目录",
            ),
        ),
        migrations.AddField(
            model_name="resourcesettings",
            name="dhh_run_log_root",
            field=models.CharField(
                default="/data/sync/log/run/",
                help_text="DHH 环境运行日志专用根目录；不继承普通上位机日志路径。",
                max_length=512,
                verbose_name="DHH 运行日志根目录",
            ),
        ),
        migrations.AlterField(
            model_name="resourcesettings",
            name="dhh_executor_log_root",
            field=models.CharField(
                default="/data/sync/log/debug/elog/",
                help_text="DHH 环境执行器日志专用根目录；不继承普通上位机日志路径。",
                max_length=512,
                verbose_name="DHH 执行器日志根目录",
            ),
        ),
    ]
