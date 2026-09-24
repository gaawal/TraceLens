import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = [("machines", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="LogSourceRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("name", models.CharField(max_length=128, verbose_name="规则名称")),
                ("root_path_template", models.CharField(default="/log/{username}/debug", max_length=512, verbose_name="根目录模板")),
                ("subsystem", models.CharField(blank=True, max_length=128, verbose_name="子系统")),
                ("component", models.CharField(blank=True, max_length=128, verbose_name="组件")),
                ("module", models.CharField(blank=True, max_length=128, verbose_name="功能模块")),
                ("file_pattern", models.CharField(default="*.log*", max_length=128, verbose_name="文件匹配模式")),
                ("auto_discovery", models.BooleanField(default=True, verbose_name="自动发现目录")),
                ("enabled", models.BooleanField(default=True, verbose_name="启用")),
                ("description", models.TextField(blank=True, verbose_name="说明")),
                ("machine", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="log_source_rules", to="machines.machine", verbose_name="机器")),
            ],
            options={"verbose_name": "日志路径规则", "verbose_name_plural": "日志路径规则", "ordering": ["machine", "name"]},
        ),
        migrations.AddConstraint(
            model_name="logsourcerule",
            constraint=models.UniqueConstraint(fields=("machine", "name"), name="uniq_machine_log_source_name"),
        ),
        migrations.AddIndex(
            model_name="logsourcerule",
            index=models.Index(fields=["machine", "enabled"], name="logsrc_machine_enabled_idx"),
        ),
    ]
