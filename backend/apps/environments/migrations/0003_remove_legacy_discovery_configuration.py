from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("environments", "0002_resource_settings_and_version")]
    operations = [migrations.DeleteModel(name="DiscoveryConfiguration")]
