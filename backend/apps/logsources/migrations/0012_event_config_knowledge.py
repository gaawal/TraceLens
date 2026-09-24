from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("environments", "0023_resource_settings_display_rules_initialization"),
        ("logsources", "0011_log_query_skill"),
    ]

    operations = [
        migrations.AlterField(
            model_name="rootcausecomponentmapping",
            name="event_component",
            field=models.CharField(db_index=True, max_length=128, verbose_name="Event组件名"),
        ),
        migrations.AlterField(
            model_name="rootcausecomponentmapping",
            name="module",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="root_cause_component_mappings",
                to="logsources.logfmdefinition",
                verbose_name="兼容主目标模块",
            ),
        ),
        migrations.AddField(
            model_name="rootcausecomponentmapping",
            name="event_subsystem",
            field=models.CharField(blank=True, db_index=True, max_length=128, verbose_name="Event来源子系统"),
        ),
        migrations.AddField(
            model_name="rootcausecomponentmapping",
            name="auto_discovered",
            field=models.BooleanField(db_index=True, default=False, verbose_name="环境事件配置自动发现"),
        ),
        migrations.AddField(
            model_name="rootcausecomponentmapping",
            name="event_config_files",
            field=models.JSONField(blank=True, default=list, verbose_name="事件配置文件"),
        ),
        migrations.AddField(
            model_name="rootcausecomponentmapping",
            name="event_display_codes",
            field=models.JSONField(blank=True, default=list, verbose_name="DisplayCode列表"),
        ),
        migrations.AddField(
            model_name="rootcausecomponentmapping",
            name="event_code_count",
            field=models.PositiveIntegerField(default=0, verbose_name="事件码数量"),
        ),
        migrations.AddField(
            model_name="rootcausecomponentmapping",
            name="last_discovered_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="最近事件配置发现时间"),
        ),
        migrations.AddConstraint(
            model_name="rootcausecomponentmapping",
            constraint=models.UniqueConstraint(
                fields=("event_subsystem", "event_component"),
                name="uniq_rootcomp_event_sub_component",
            ),
        ),
        migrations.AddIndex(
            model_name="rootcausecomponentmapping",
            index=models.Index(fields=["event_subsystem", "event_component"], name="rootcomp_event_source_idx"),
        ),
        migrations.CreateModel(
            name="EventConfigSource",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("component_code", models.CharField(db_index=True, max_length=128, verbose_name="组件代码仓")),
                ("source", models.CharField(blank=True, max_length=64, verbose_name="JSON Source")),
                ("file_name", models.CharField(max_length=256, verbose_name="配置文件")),
                ("file_path", models.CharField(max_length=1024, verbose_name="配置路径")),
                ("version", models.CharField(blank=True, max_length=32, verbose_name="配置版本")),
                ("file_hash", models.CharField(blank=True, max_length=64, verbose_name="内容哈希")),
                ("remote_mtime", models.FloatField(default=0, verbose_name="远端修改时间")),
                ("remote_size", models.PositiveBigIntegerField(default=0, verbose_name="远端文件大小")),
                ("active", models.BooleanField(db_index=True, default=True, verbose_name="有效")),
                ("sync_message", models.TextField(blank=True, verbose_name="同步信息")),
                ("last_seen_at", models.DateTimeField(blank=True, null=True, verbose_name="最近发现时间")),
                ("environment", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="event_config_sources", to="environments.environment", verbose_name="环境")),
                ("subsystem", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="event_config_sources", to="logsources.logsubsystemdefinition", verbose_name="来源子系统")),
            ],
            options={
                "verbose_name": "环境事件配置文件",
                "verbose_name_plural": "环境事件配置文件",
                "ordering": ["environment", "subsystem__name", "component_code", "file_name"],
            },
        ),
        migrations.AddConstraint(
            model_name="eventconfigsource",
            constraint=models.UniqueConstraint(fields=("environment", "file_path"), name="uniq_env_event_config_path"),
        ),
        migrations.AddIndex(
            model_name="eventconfigsource",
            index=models.Index(fields=["environment", "active"], name="eventcfg_env_active_idx"),
        ),
        migrations.AddIndex(
            model_name="eventconfigsource",
            index=models.Index(fields=["environment", "component_code"], name="eventcfg_env_component_idx"),
        ),
        migrations.CreateModel(
            name="EventCodeDefinition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("component_code", models.CharField(db_index=True, max_length=128, verbose_name="组件代码仓")),
                ("source", models.CharField(blank=True, max_length=64, verbose_name="Source")),
                ("code", models.CharField(blank=True, db_index=True, max_length=64, verbose_name="Code")),
                ("display_code", models.CharField(db_index=True, max_length=128, verbose_name="DisplayCode")),
                ("code_string", models.CharField(blank=True, db_index=True, max_length=256, verbose_name="CodeString")),
                ("severity", models.CharField(blank=True, max_length=32, verbose_name="Severity")),
                ("category", models.CharField(blank=True, max_length=64, verbose_name="Category")),
                ("recovery_class", models.CharField(blank=True, max_length=64, verbose_name="RecoveryClass")),
                ("auto_clear", models.BooleanField(default=False, verbose_name="AutoClear")),
                ("send_to_host", models.BooleanField(default=False, verbose_name="SendToHost")),
                ("send_to_active_exception_gui", models.BooleanField(default=False, verbose_name="SendToActiveExceptionGUI")),
                ("description", models.TextField(blank=True, verbose_name="Description")),
                ("object_params", models.JSONField(blank=True, default=list, verbose_name="ObjectParams")),
                ("raw_config", models.JSONField(blank=True, default=dict, verbose_name="原始配置")),
                ("active", models.BooleanField(db_index=True, default=True, verbose_name="有效")),
                ("last_seen_at", models.DateTimeField(blank=True, null=True, verbose_name="最近发现时间")),
                ("environment", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="event_code_definitions", to="environments.environment", verbose_name="环境")),
                ("source_config", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="event_codes", to="logsources.eventconfigsource", verbose_name="配置文件")),
                ("subsystem", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="event_code_definitions", to="logsources.logsubsystemdefinition", verbose_name="来源子系统")),
            ],
            options={
                "verbose_name": "环境事件码定义",
                "verbose_name_plural": "环境事件码定义",
                "ordering": ["environment", "subsystem__name", "component_code", "display_code"],
            },
        ),
        migrations.AddConstraint(
            model_name="eventcodedefinition",
            constraint=models.UniqueConstraint(fields=("environment", "display_code"), name="uniq_env_event_display_code"),
        ),
        migrations.AddIndex(
            model_name="eventcodedefinition",
            index=models.Index(fields=["environment", "display_code"], name="eventcode_env_display_idx"),
        ),
        migrations.AddIndex(
            model_name="eventcodedefinition",
            index=models.Index(fields=["environment", "code"], name="eventcode_env_code_idx"),
        ),
        migrations.AddIndex(
            model_name="eventcodedefinition",
            index=models.Index(fields=["environment", "component_code"], name="eventcode_env_component_idx"),
        ),
    ]
