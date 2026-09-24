from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("logsources", "0005_executor_module_kind"),
    ]

    operations = [
        migrations.AddField(
            model_name="logfmdefinition",
            name="dependencies",
            field=models.ManyToManyField(
                blank=True,
                related_name="referenced_by_modules",
                symmetrical=False,
                to="logsources.logfmdefinition",
                verbose_name="依赖模块",
            ),
        ),
    ]
