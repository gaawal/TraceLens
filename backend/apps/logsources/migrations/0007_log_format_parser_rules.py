from django.db import migrations, models


DEBUG_PATTERN = r"^\[(?P<timestamp>[^\]]+)]\s+\[(?P<level>[^\]]+)]\s+\[(?P<component>[^\]]+)]\s+\[(?P<process_id>[^\]]+)]\s+\[(?P<thread_id>[^\]]+)]\s+\[(?P<source>[^\]]+)]\s+\[(?P<mode>[^\]]+)]\s+\[(?P<rpc>[^\]]+)]\s*(?P<message>.*)$"
RUN_PATTERN = r"^\[(?P<timestamp>[^\]]+)]\s+\[(?P<component>[^\]]+)]\s+\[(?P<process_id>[^\]]+)]\s+\[(?P<source>[^\]]+)]\s+\[(?P<event_category>[^\]]+)]\s+\[(?P<event_level>[^\]]+)]\s+\[(?P<current_event_code>[^\]]*)]\s+\[(?P<linked_event_codes>[^\]]*)]\s+\[(?P<current_err_iid>[^\]]*)]\s+\[(?P<linked_err_iids>[^\]]*)]\s+\[(?P<display_code>[^\]]*)]\s+\[(?P<linked_display_codes>[^\]]*)]\s+\[(?P<event_type>[^\]]+)]\s*(?P<message>.*)$"
DEBUG_MAP = {field: field for field in ("timestamp", "level", "component", "process_id", "thread_id", "source", "mode", "rpc", "message")}
RUN_MAP = {
    "timestamp": "timestamp", "level": "event_level", "component": "component", "process_id": "process_id",
    "source": "source", "mode": "event_type", "message": "message", "event_category": "event_category",
    "event_level": "event_level", "current_event_code": "current_event_code", "linked_event_codes": "linked_event_codes",
    "current_err_iid": "current_err_iid", "linked_err_iids": "linked_err_iids", "display_code": "display_code",
    "linked_display_codes": "linked_display_codes", "event_type": "event_type",
}


def seed_rules(apps, schema_editor):
    Rule = apps.get_model("logsources", "LogFormatParserRule")
    rows = [
        dict(name="标准调试日志", category="debug", priority=100, file_pattern="*.log*", pattern=DEBUG_PATTERN, field_map=DEBUG_MAP, description="TraceLens 默认八字段调试日志格式。"),
        dict(name="标准执行器日志", category="executor", priority=100, file_pattern="*.log*", pattern=DEBUG_PATTERN, field_map=DEBUG_MAP, description="执行器日志默认沿用标准八字段格式。"),
        dict(name="运行事件日志", category="run", priority=120, file_pattern="event.log,*.log*", pattern=RUN_PATTERN, field_map=RUN_MAP, description="运行 event 日志结构化事件格式。"),
    ]
    for row in rows:
        Rule.objects.update_or_create(
            category=row["category"], name=row["name"],
            defaults={**row, "enabled": True, "timestamp_format": "auto", "ignore_case": False, "built_in": True},
        )


class Migration(migrations.Migration):
    dependencies = [("logsources", "0006_logfm_dependencies")]
    operations = [
        migrations.CreateModel(
            name="LogFormatParserRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("name", models.CharField(max_length=128, verbose_name="规则名称")),
                ("category", models.CharField(default="debug", max_length=32, verbose_name="日志类型")),
                ("enabled", models.BooleanField(default=True, verbose_name="启用")),
                ("priority", models.IntegerField(default=100, verbose_name="优先级")),
                ("file_pattern", models.CharField(blank=True, default="*.log*", max_length=256, verbose_name="适用文件")),
                ("pattern", models.TextField(verbose_name="正则表达式")),
                ("ignore_case", models.BooleanField(default=False, verbose_name="忽略大小写")),
                ("field_map", models.JSONField(blank=True, default=dict, verbose_name="标准字段映射")),
                ("timestamp_format", models.CharField(default="auto", max_length=64, verbose_name="时间格式")),
                ("built_in", models.BooleanField(default=False, verbose_name="系统内置")),
                ("description", models.TextField(blank=True, verbose_name="说明")),
            ],
            options={"verbose_name": "日志格式解析规则", "verbose_name_plural": "日志格式解析规则", "ordering": ["category", "-priority", "id"]},
        ),
        migrations.AddConstraint(model_name="logformatparserrule", constraint=models.UniqueConstraint(fields=("category", "name"), name="uniq_log_format_rule_category_name")),
        migrations.AddIndex(model_name="logformatparserrule", index=models.Index(fields=["category", "enabled", "priority"], name="logfmt_cat_enabled_pri_idx")),
        migrations.RunPython(seed_rules, migrations.RunPython.noop),
    ]
