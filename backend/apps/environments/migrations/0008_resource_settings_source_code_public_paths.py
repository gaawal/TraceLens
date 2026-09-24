from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("environments", "0007_resource_settings_source_code_path")]

    operations = [
        migrations.AddField(
            model_name="resourcesettings",
            name="source_code_public_paths",
            field=models.TextField(
                blank=True,
                default="/home/{username}/SW/lib/python/me/cpfr/\n/home/{username}/SW/lib/python/sw/adf/",
                verbose_name="Python 公共源码目录",
            ),
        ),
    ]
