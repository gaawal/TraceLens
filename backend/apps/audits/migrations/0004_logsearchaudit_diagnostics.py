from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("audits", "0003_data_extraction_record")]

    operations = [
        migrations.AddField(
            model_name="logsearchaudit",
            name="diagnostics",
            field=models.JSONField(blank=True, default=dict, verbose_name="日志定位诊断"),
        ),
    ]
