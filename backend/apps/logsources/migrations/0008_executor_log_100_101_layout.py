from django.db import migrations


OLD_DEBUG_PATTERN = r"^\[(?P<timestamp>[^\]]+)]\s+\[(?P<level>[^\]]+)]\s+\[(?P<component>[^\]]+)]\s+\[(?P<process_id>[^\]]+)]\s+\[(?P<thread_id>[^\]]+)]\s+\[(?P<source>[^\]]+)]\s+\[(?P<mode>[^\]]+)]\s+\[(?P<rpc>[^\]]+)]\s*(?P<message>.*)$"
EXECUTOR_PATTERN = r"^\[(?P<timestamp>[^\]]+)]\s+\[(?P<level>[^\]]+)]\s+\[(?P<component>[^\]]+)]\s+\[(?P<process_id>[^\]]+)]\s+\[(?P<thread_id>[^\]]+)]\s+\[(?P<source>[^\]]+)]\s+(?=(?:\[[^\]]+]\s+){0,2}\[(?P<mode>100|101)](?:\s|$))(?=\[[^\]]+]\s+\[(?P<rpc>[^\]]*:[^\]]*:[^\]]*)])(?:\[[^\]]+]\s+\[[^\]]+]\s+\[100]|\[(?:100|101)]\s+\[[^\]]+])\s*(?P<message>.*)$"
OLD_DESCRIPTION = "执行器日志默认沿用标准八字段格式。"
NEW_DESCRIPTION = "执行器日志：兼容 100 内部（context/rpc/mode）与 101 外部（mode/rpc）字段布局。"


def forward(apps, schema_editor):
    Rule = apps.get_model("logsources", "LogFormatParserRule")
    for rule in Rule.objects.filter(category="executor", name="标准执行器日志", built_in=True):
        # 只升级仍保持旧系统默认值的规则；用户手工改过的内置规则不覆盖。
        if rule.pattern == OLD_DEBUG_PATTERN:
            rule.pattern = EXECUTOR_PATTERN
            if rule.description == OLD_DESCRIPTION:
                rule.description = NEW_DESCRIPTION
            rule.save(update_fields=["pattern", "description", "updated_at"])


def backward(apps, schema_editor):
    Rule = apps.get_model("logsources", "LogFormatParserRule")
    for rule in Rule.objects.filter(category="executor", name="标准执行器日志", built_in=True):
        if rule.pattern == EXECUTOR_PATTERN:
            rule.pattern = OLD_DEBUG_PATTERN
            if rule.description == NEW_DESCRIPTION:
                rule.description = OLD_DESCRIPTION
            rule.save(update_fields=["pattern", "description", "updated_at"])


class Migration(migrations.Migration):
    dependencies = [("logsources", "0007_log_format_parser_rules")]
    operations = [migrations.RunPython(forward, backward)]
