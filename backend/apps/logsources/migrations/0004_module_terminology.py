from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("logsources", "0003_global_log_dictionary")]

    operations = [
        migrations.AlterModelOptions(
            name="logfmdefinition",
            options={
                "ordering": ["subsystem__sort_order", "subsystem__name", "sort_order", "name"],
                "verbose_name": "全局日志模块",
                "verbose_name_plural": "全局日志模块",
            },
        ),
        migrations.AlterField(
            model_name="logfmdefinition",
            name="name",
            field=models.CharField(max_length=128, verbose_name="模块标识"),
        ),
        migrations.AlterField(
            model_name="logresourcecatalogitem",
            name="fm",
            field=models.CharField(max_length=128, verbose_name="模块"),
        ),
    ]
