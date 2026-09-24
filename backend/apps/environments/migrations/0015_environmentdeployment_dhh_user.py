from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("environments", "0014_deploymentstep_retry_count")]

    operations = [
        migrations.AddField(
            model_name="environmentdeployment",
            name="dhh_user",
            field=models.CharField(blank=True, default="root", max_length=128, verbose_name="DHH 用户"),
        ),
    ]
