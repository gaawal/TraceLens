from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("environments", "0012_resource_settings_dhh_debug_run_roots")]

    operations = [
        migrations.CreateModel(
            name="EnvironmentDeployment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("task_name", models.CharField(max_length=128, verbose_name="任务名")),
                ("target_version", models.CharField(max_length=256, verbose_name="目标版本")),
                ("simulation_mode", models.CharField(default="sim0_sil", max_length=32, verbose_name="仿真模式")),
                ("include_sdk", models.BooleanField(default=True, verbose_name="拉取 SDK")),
                ("upper_ip", models.CharField(max_length=255, verbose_name="上位机 IP")),
                ("gpb_ips", models.JSONField(blank=True, default=list, verbose_name="GPB IP 列表")),
                ("tb_mode", models.CharField(blank=True, max_length=128, verbose_name="TB 部署模式")),
                ("install_mode", models.CharField(blank=True, max_length=128, verbose_name="安装 GPB 模式")),
                ("install_port", models.PositiveSmallIntegerField(default=0, verbose_name="安装端口")),
                ("include_dhh", models.BooleanField(default=False, verbose_name="部署 DHH")),
                ("dhh_ip", models.CharField(blank=True, max_length=255, verbose_name="DHH IP")),
                ("dhh_machine_id", models.CharField(blank=True, max_length=128, verbose_name="DHH machineId")),
                ("configuration", models.JSONField(blank=True, default=dict, verbose_name="部署参数快照")),
                ("command_snapshot", models.JSONField(blank=True, default=list, verbose_name="部署命令快照")),
                ("status", models.CharField(choices=[("pending", "等待中"), ("running", "部署中"), ("success", "成功"), ("failed", "失败")], default="pending", max_length=16, verbose_name="状态")),
                ("current_step", models.CharField(blank=True, max_length=32, verbose_name="当前步骤")),
                ("message", models.TextField(blank=True, verbose_name="状态说明")),
                ("started_at", models.DateTimeField(blank=True, null=True, verbose_name="开始时间")),
                ("finished_at", models.DateTimeField(blank=True, null=True, verbose_name="结束时间")),
                ("environment", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="deployments", to="environments.environment", verbose_name="环境")),
            ],
            options={"verbose_name": "环境部署记录", "verbose_name_plural": "环境部署记录", "ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="DeploymentStep",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("key", models.CharField(max_length=32, verbose_name="步骤标识")),
                ("name", models.CharField(max_length=64, verbose_name="步骤名称")),
                ("sort_order", models.PositiveSmallIntegerField(default=0, verbose_name="顺序")),
                ("status", models.CharField(choices=[("pending", "等待中"), ("running", "执行中"), ("success", "成功"), ("failed", "失败"), ("skipped", "已跳过")], default="pending", max_length=16, verbose_name="状态")),
                ("command", models.TextField(blank=True, verbose_name="执行命令")),
                ("success_marker", models.CharField(blank=True, max_length=256, verbose_name="成功标志")),
                ("stdout", models.TextField(blank=True, verbose_name="标准输出")),
                ("stderr", models.TextField(blank=True, verbose_name="错误输出")),
                ("exit_status", models.IntegerField(blank=True, null=True, verbose_name="退出码")),
                ("message", models.TextField(blank=True, verbose_name="步骤说明")),
                ("started_at", models.DateTimeField(blank=True, null=True, verbose_name="开始时间")),
                ("finished_at", models.DateTimeField(blank=True, null=True, verbose_name="结束时间")),
                ("deployment", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="steps", to="environments.environmentdeployment", verbose_name="部署任务")),
            ],
            options={"verbose_name": "环境部署步骤", "verbose_name_plural": "环境部署步骤", "ordering": ["sort_order", "id"]},
        ),
        migrations.AddIndex(model_name="environmentdeployment", index=models.Index(fields=["environment", "-created_at"], name="env_deploy_time_idx")),
        migrations.AddConstraint(model_name="deploymentstep", constraint=models.UniqueConstraint(fields=("deployment", "key"), name="uniq_deployment_step_key")),
    ]
