from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("environments", "0006_log_path_matching_rules")]

    operations = [
        migrations.AddField(
            model_name="resourcesettings",
            name="source_code_path_template",
            field=models.CharField(
                default="/home/{username}/SW/lib/python/{subsystem}/{module}",
                max_length=512,
                verbose_name="Python 源码目录模板",
            ),
        ),
    ]
