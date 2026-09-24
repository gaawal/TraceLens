from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("machines", "0003_machine_dhh_credentials_managed")]

    operations = [
        migrations.AddField(
            model_name="machine",
            name="software_version",
            field=models.CharField(blank=True, max_length=256, verbose_name="软件版本"),
        ),
        migrations.AddField(
            model_name="machine",
            name="version_checked_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="版本查询时间"),
        ),
    ]
