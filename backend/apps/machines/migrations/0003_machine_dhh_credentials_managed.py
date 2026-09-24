from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("machines", "0002_station_fields")]
    operations = [
        migrations.AddField(
            model_name="machine",
            name="dhh_credentials_managed",
            field=models.BooleanField(default=False, editable=False, verbose_name="DHH 凭据已独立维护"),
        ),
    ]
