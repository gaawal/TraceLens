from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("audits", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="logsearchaudit",
            name="matched_files",
            field=models.JSONField(blank=True, default=list, verbose_name="命中文件"),
        ),
    ]
