from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("environments", "0020_environment_deployment_scripts")]

    operations = [
        migrations.AddField(
            model_name="resourcesettings",
            name="display_rules",
            field=models.JSONField(blank=True, default=list, verbose_name="日志语义规则"),
        ),
    ]
