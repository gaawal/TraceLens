from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("environments", "0021_resource_settings_display_rules"),
    ]

    operations = [
        migrations.AddField(
            model_name="resourcesettings",
            name="data_extraction_rules",
            field=models.JSONField(blank=True, default=None, null=True, verbose_name="数据提取器规则"),
        ),
    ]
