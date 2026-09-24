from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("machines", "0001_initial")]
    operations = [
        migrations.AddField(model_name="machine", name="station_id", field=models.CharField(blank=True, max_length=32, verbose_name="站点ID")),
        migrations.AddField(model_name="machine", name="station_type", field=models.CharField(blank=True, max_length=16, verbose_name="站点类型")),
        migrations.AddField(model_name="machine", name="station_name", field=models.CharField(blank=True, max_length=128, verbose_name="站点名称")),
    ]
