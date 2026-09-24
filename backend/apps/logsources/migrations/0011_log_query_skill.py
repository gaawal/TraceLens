from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("logsources", "0010_root_cause_component_multi_modules"),
    ]

    operations = [
        migrations.CreateModel(
            name="LogQuerySkill",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("name", models.CharField(max_length=128, verbose_name="Skill名称")),
                ("enabled", models.BooleanField(db_index=True, default=True, verbose_name="启用")),
                ("priority", models.IntegerField(default=100, verbose_name="优先级")),
                ("trigger_modules", models.JSONField(blank=True, default=list, verbose_name="触发模块")),
                ("trigger_keywords", models.JSONField(blank=True, default=list, verbose_name="触发关键字")),
                ("description", models.TextField(blank=True, verbose_name="规则说明")),
                ("steps", models.JSONField(blank=True, default=list, verbose_name="补充检索步骤")),
                ("subsystem", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="query_skills", to="logsources.logsubsystemdefinition", verbose_name="子系统")),
            ],
            options={
                "verbose_name": "日志查询Skill",
                "verbose_name_plural": "日志查询Skill",
                "ordering": ["subsystem__sort_order", "subsystem__name", "-priority", "name"],
            },
        ),
        migrations.AddConstraint(
            model_name="logqueryskill",
            constraint=models.UniqueConstraint(fields=("subsystem", "name"), name="uniq_log_query_skill_subsystem_name"),
        ),
        migrations.AddIndex(
            model_name="logqueryskill",
            index=models.Index(fields=["subsystem", "enabled", "priority"], name="logskill_sub_enabled_pri_idx"),
        ),
    ]
