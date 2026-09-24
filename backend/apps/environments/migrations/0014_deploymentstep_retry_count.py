from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("environments", "0013_environment_deployment")]

    operations = [
        migrations.AddField(
            model_name="deploymentstep",
            name="retry_count",
            field=models.PositiveSmallIntegerField(default=0, verbose_name="重试次数"),
        ),
    ]
