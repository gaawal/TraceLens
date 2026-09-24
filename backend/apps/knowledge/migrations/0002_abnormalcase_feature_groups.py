from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("knowledge", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="abnormalcase",
            name="feature_groups",
            field=models.JSONField(blank=True, default=list, verbose_name="现场特征组"),
        ),
        migrations.AddField(
            model_name="abnormalcase",
            name="governance_history",
            field=models.JSONField(blank=True, default=list, verbose_name="案例治理历史"),
        ),
        migrations.AlterField(
            model_name="abnormalcase",
            name="fingerprint_version",
            field=models.PositiveIntegerField(default=4, verbose_name="指纹版本"),
        ),
    ]
