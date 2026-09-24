import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = [("machines", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="Environment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("name", models.CharField(max_length=128, verbose_name="环境名称")),
                ("status", models.CharField(choices=[("pending", "待发现"), ("ready", "已就绪"), ("error", "发现失败"), ("stale", "待刷新")], default="pending", max_length=16, verbose_name="状态")),
                ("last_discovered_at", models.DateTimeField(blank=True, null=True, verbose_name="最后发现时间")),
                ("description", models.TextField(blank=True, verbose_name="描述")),
                ("upper_machine", models.OneToOneField(limit_choices_to={"role": "upper"}, on_delete=django.db.models.deletion.PROTECT, related_name="owned_environment", to="machines.machine", verbose_name="上位机")),
            ],
            options={"verbose_name": "环境", "verbose_name_plural": "环境", "ordering": ["name"]},
        ),
        migrations.CreateModel(
            name="DiscoveryConfiguration",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("xml_path", models.CharField(blank=True, max_length=512, verbose_name="XML文件路径")),
                ("lower_machine_xpath", models.CharField(blank=True, max_length=512, verbose_name="下位机节点XPath")),
                ("host_selector", models.CharField(default="@ip", help_text="@attr 表示属性；text() 表示当前节点文本；其他值表示子节点路径。", max_length=256, verbose_name="主机地址提取规则")),
                ("name_selector", models.CharField(blank=True, default="@name", max_length=256, verbose_name="机器名称提取规则")),
                ("enabled", models.BooleanField(default=True, verbose_name="启用")),
                ("description", models.TextField(blank=True, verbose_name="说明")),
                ("upper_machine", models.OneToOneField(limit_choices_to={"role": "upper"}, on_delete=django.db.models.deletion.CASCADE, related_name="discovery_configuration", to="machines.machine", verbose_name="上位机")),
            ],
            options={"verbose_name": "XML发现配置", "verbose_name_plural": "XML发现配置"},
        ),
        migrations.CreateModel(
            name="EnvironmentDiscovery",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("status", models.CharField(choices=[("pending", "等待中"), ("running", "执行中"), ("success", "成功"), ("failed", "失败")], default="pending", max_length=16, verbose_name="执行状态")),
                ("xml_path", models.CharField(blank=True, max_length=512, verbose_name="XML文件路径")),
                ("started_at", models.DateTimeField(blank=True, null=True, verbose_name="开始时间")),
                ("finished_at", models.DateTimeField(blank=True, null=True, verbose_name="结束时间")),
                ("found_count", models.PositiveIntegerField(default=0, verbose_name="发现数量")),
                ("created_count", models.PositiveIntegerField(default=0, verbose_name="新增数量")),
                ("updated_count", models.PositiveIntegerField(default=0, verbose_name="更新数量")),
                ("message", models.TextField(blank=True, verbose_name="执行信息")),
                ("summary", models.JSONField(blank=True, default=dict, verbose_name="摘要")),
                ("environment", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="discoveries", to="environments.environment", verbose_name="环境")),
            ],
            options={"verbose_name": "环境发现记录", "verbose_name_plural": "环境发现记录", "ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="MachineRelation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("source", models.CharField(choices=[("xml", "XML 自动发现"), ("manual", "手工维护")], default="xml", max_length=16, verbose_name="关系来源")),
                ("is_active", models.BooleanField(default=True, verbose_name="有效")),
                ("discovered_at", models.DateTimeField(blank=True, null=True, verbose_name="发现时间")),
                ("metadata", models.JSONField(blank=True, default=dict, verbose_name="扩展信息")),
                ("environment", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="machine_relations", to="environments.environment", verbose_name="环境")),
                ("source_machine", models.ForeignKey(limit_choices_to={"role": "upper"}, on_delete=django.db.models.deletion.PROTECT, related_name="outgoing_relations", to="machines.machine", verbose_name="来源上位机")),
                ("target_machine", models.ForeignKey(limit_choices_to={"role": "lower"}, on_delete=django.db.models.deletion.PROTECT, related_name="incoming_relations", to="machines.machine", verbose_name="下位机")),
            ],
            options={"verbose_name": "上下位机关系", "verbose_name_plural": "上下位机关系", "ordering": ["environment", "target_machine"]},
        ),
        migrations.AddConstraint(
            model_name="machinerelation",
            constraint=models.UniqueConstraint(fields=("environment", "target_machine"), name="uniq_environment_lower_machine"),
        ),
        migrations.AddIndex(
            model_name="machinerelation",
            index=models.Index(fields=["environment", "is_active"], name="relation_env_active_idx"),
        ),
        migrations.AddIndex(
            model_name="environmentdiscovery",
            index=models.Index(fields=["environment", "-created_at"], name="discover_env_time_idx"),
        ),
    ]
