from django.db import migrations, models


def seed_match_rules(apps, schema_editor):
    LogPathProfile = apps.get_model("environments", "LogPathProfile")
    LogPathProfile.objects.filter(match_rules=[]).update(match_rules=["fm", "fm_timestamp", "archive"])


class Migration(migrations.Migration):
    dependencies = [("environments", "0005_environment_folder_and_log_path_defaults")]

    operations = [
        migrations.AlterField(
            model_name="logpathprofile",
            name="category",
            field=models.CharField(max_length=64, verbose_name="日志类型标识"),
        ),
        migrations.AlterField(
            model_name="logpathprofile",
            name="scope",
            field=models.CharField(
                choices=[
                    ("upper_only", "上位机"),
                    ("lower_only", "下位机"),
                    ("each_machine", "上位机 + 下位机"),
                    ("upper_for_lower", "上位机按下位机展开"),
                ],
                default="each_machine",
                max_length=32,
                verbose_name="机器范围",
            ),
        ),
        migrations.AddField(
            model_name="logpathprofile",
            name="match_rules",
            field=models.JSONField(blank=True, default=list, help_text="支持 fm / fm_timestamp / archive；空列表等价于全部。", verbose_name="文件匹配规则"),
        ),
        migrations.RunPython(seed_match_rules, migrations.RunPython.noop),
    ]
