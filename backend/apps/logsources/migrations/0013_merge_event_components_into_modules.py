from django.db import migrations, models


def migrate_root_component_mappings(apps, schema_editor):
    LogSubsystemDefinition = apps.get_model("logsources", "LogSubsystemDefinition")
    LogFmDefinition = apps.get_model("logsources", "LogFmDefinition")
    RootCauseComponentMapping = apps.get_model("logsources", "RootCauseComponentMapping")

    for mapping in RootCauseComponentMapping.objects.all().iterator():
        component = str(mapping.event_component or "").strip()
        if not component:
            continue
        source_name = str(mapping.event_subsystem or "").strip()
        if source_name:
            source_subsystem = LogSubsystemDefinition.objects.filter(name__iexact=source_name).first()
            if source_subsystem is None:
                source_subsystem = LogSubsystemDefinition.objects.create(
                    name=source_name.upper(),
                    display_name="",
                    enabled=True,
                )
        else:
            source_subsystem = mapping.subsystem

        source_module = (
            LogFmDefinition.objects.filter(
                subsystem=source_subsystem,
                name__iexact=component,
                kind="normal",
            ).first()
        )
        if source_module is None:
            source_module = LogFmDefinition.objects.create(
                subsystem=source_subsystem,
                name=component,
                kind="normal",
                display_name="",
                enabled=bool(mapping.enabled),
                event_component=True,
            )

        source_module.event_component = True
        source_module.event_config_files = list(mapping.event_config_files or [])
        source_module.event_display_codes = list(mapping.event_display_codes or [])
        source_module.event_code_count = int(mapping.event_code_count or 0)
        source_module.last_event_discovered_at = mapping.last_discovered_at
        source_module.matched_count = int(mapping.matched_count or 0)
        source_module.last_matched_at = mapping.last_matched_at
        source_module.save(update_fields=[
            "event_component",
            "event_config_files",
            "event_display_codes",
            "event_code_count",
            "last_event_discovered_at",
            "matched_count",
            "last_matched_at",
            "updated_at",
        ])

        target_ids = set(mapping.modules.values_list("id", flat=True))
        if mapping.module_id:
            target_ids.add(mapping.module_id)
        if target_ids:
            source_module.target_modules.add(*LogFmDefinition.objects.filter(id__in=target_ids))


class Migration(migrations.Migration):
    dependencies = [
        ("logsources", "0012_event_config_knowledge"),
    ]

    operations = [
        migrations.AddField(
            model_name="logfmdefinition",
            name="query_priority",
            field=models.PositiveIntegerField(db_index=True, default=100, verbose_name="查询优先级"),
        ),
        migrations.AddField(
            model_name="logfmdefinition",
            name="event_component",
            field=models.BooleanField(db_index=True, default=False, verbose_name="Event组件"),
        ),
        migrations.AddField(
            model_name="logfmdefinition",
            name="event_config_files",
            field=models.JSONField(blank=True, default=list, verbose_name="事件配置文件"),
        ),
        migrations.AddField(
            model_name="logfmdefinition",
            name="event_display_codes",
            field=models.JSONField(blank=True, default=list, verbose_name="DisplayCode列表"),
        ),
        migrations.AddField(
            model_name="logfmdefinition",
            name="event_code_count",
            field=models.PositiveIntegerField(default=0, verbose_name="事件码数量"),
        ),
        migrations.AddField(
            model_name="logfmdefinition",
            name="last_event_discovered_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="最近事件配置发现时间"),
        ),
        migrations.AddField(
            model_name="logfmdefinition",
            name="matched_count",
            field=models.PositiveBigIntegerField(default=0, verbose_name="命中次数"),
        ),
        migrations.AddField(
            model_name="logfmdefinition",
            name="last_matched_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="最近命中时间"),
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(
                    model_name="logfmdefinition",
                    name="dependencies",
                ),
                migrations.AddField(
                    model_name="logfmdefinition",
                    name="target_modules",
                    field=models.ManyToManyField(
                        blank=True,
                        db_table="logsources_logfmdefinition_dependencies",
                        related_name="targeted_by_modules",
                        symmetrical=False,
                        to="logsources.logfmdefinition",
                        verbose_name="目标模块",
                    ),
                ),
            ],
            database_operations=[],
        ),
        migrations.AddIndex(
            model_name="logfmdefinition",
            index=models.Index(fields=["subsystem", "event_component", "name"], name="logfm_event_component_idx"),
        ),
        migrations.RunPython(migrate_root_component_mappings, migrations.RunPython.noop),
        migrations.DeleteModel(
            name="RootCauseComponentMapping",
        ),
    ]
