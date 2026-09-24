from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("environments", "0018_environmentdeployment_schedule"),
    ]

    operations = [
        migrations.AddField(
            model_name="deploymentstep",
            name="process_log",
            field=models.TextField(blank=True, verbose_name="顺序过程日志"),
        ),
    ]
