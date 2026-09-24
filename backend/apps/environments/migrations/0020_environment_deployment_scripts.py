from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("environments", "0019_deploymentstep_process_log")]

    operations = [
        migrations.AddField(
            model_name="environment",
            name="deployment_scripts",
            field=models.JSONField(blank=True, default=list, verbose_name="部署后脚本历史"),
        ),
    ]
