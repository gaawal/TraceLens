from django.db import migrations, models


def reset_executor_catalogs(apps, schema_editor):
    Catalog = apps.get_model("logsources", "LogResourceCatalog")
    CatalogItem = apps.get_model("logsources", "LogResourceCatalogItem")
    Subsystem = apps.get_model("logsources", "LogSubsystemDefinition")
    Catalog.objects.filter(source_category="executor").delete()
    CatalogItem.objects.filter(subsystem__iexact="elog").delete()
    Subsystem.objects.filter(name__iexact="elog").delete()


class Migration(migrations.Migration):
    dependencies = [("logsources", "0004_module_terminology")]
    operations = [
        migrations.RemoveConstraint(model_name="logfmdefinition", name="uniq_global_subsystem_fm"),
        migrations.RemoveConstraint(model_name="logresourcecatalogitem", name="uniq_log_catalog_subsystem_fm"),
        migrations.AddField(
            model_name="logfmdefinition", name="kind",
            field=models.CharField(choices=[("normal", "普通模块"), ("executor", "执行器模块")], default="normal", max_length=16, verbose_name="模块类型"),
        ),
        migrations.AddField(
            model_name="logresourcecatalogitem", name="kind",
            field=models.CharField(choices=[("normal", "普通模块"), ("executor", "执行器模块")], default="normal", max_length=16, verbose_name="模块类型"),
        ),
        migrations.AddConstraint(
            model_name="logfmdefinition",
            constraint=models.UniqueConstraint(fields=("subsystem", "name", "kind"), name="uniq_global_subsystem_fm_kind"),
        ),
        migrations.AddConstraint(
            model_name="logresourcecatalogitem",
            constraint=models.UniqueConstraint(fields=("catalog", "subsystem", "fm", "kind"), name="uniq_log_catalog_subsystem_fm_kind"),
        ),
        migrations.RunPython(reset_executor_catalogs, migrations.RunPython.noop),
    ]
