import django.db.models.deletion
from django.db import migrations, models


def copy_existing_module(apps, schema_editor):
    RootCauseComponentMapping = apps.get_model("logsources", "RootCauseComponentMapping")
    through = RootCauseComponentMapping.modules.through
    rows = []
    for mapping in RootCauseComponentMapping.objects.exclude(module_id=None).iterator():
        rows.append(through(rootcausecomponentmapping_id=mapping.id, logfmdefinition_id=mapping.module_id))
    if rows:
        through.objects.bulk_create(rows, ignore_conflicts=True)


class Migration(migrations.Migration):
    dependencies = [
        ("logsources", "0009_root_cause_component_mapping"),
    ]

    operations = [
        migrations.AlterField(
            model_name="rootcausecomponentmapping",
            name="module",
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="root_cause_component_mappings", to="logsources.logfmdefinition", verbose_name="兼容主目标模块"),
        ),
        migrations.AddField(
            model_name="rootcausecomponentmapping",
            name="modules",
            field=models.ManyToManyField(blank=True, related_name="root_cause_component_multi_mappings", to="logsources.logfmdefinition", verbose_name="目标模块"),
        ),
        migrations.RunPython(copy_existing_module, migrations.RunPython.noop),
    ]
