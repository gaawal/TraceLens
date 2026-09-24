from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Machine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("name", models.CharField(max_length=128, verbose_name="机器名称")),
                ("host", models.CharField(max_length=255, verbose_name="IP或主机名")),
                ("ssh_port", models.PositiveSmallIntegerField(default=22, verbose_name="SSH端口")),
                ("username", models.CharField(blank=True, max_length=128, verbose_name="用户名")),
                ("role", models.CharField(choices=[("upper", "上位机"), ("lower", "下位机")], max_length=16, verbose_name="机器角色")),
                ("origin", models.CharField(choices=[("manual", "用户配置"), ("xml_discovery", "XML 自动发现")], default="manual", max_length=32, verbose_name="资源来源")),
                ("auth_type", models.CharField(choices=[("none", "未配置"), ("password", "密码"), ("private_key", "私钥")], default="none", max_length=32, verbose_name="认证方式")),
                ("encrypted_password", models.TextField(blank=True, editable=False, verbose_name="加密密码")),
                ("encrypted_private_key", models.TextField(blank=True, editable=False, verbose_name="加密私钥")),
                ("encrypted_private_key_passphrase", models.TextField(blank=True, editable=False, verbose_name="加密私钥口令")),
                ("description", models.TextField(blank=True, verbose_name="描述")),
                ("is_active", models.BooleanField(default=True, verbose_name="启用")),
                ("connection_status", models.CharField(choices=[("unknown", "未知"), ("online", "在线"), ("offline", "离线")], default="unknown", max_length=16, verbose_name="连接状态")),
                ("last_connection_checked_at", models.DateTimeField(blank=True, null=True, verbose_name="最后连接检查时间")),
            ],
            options={
                "verbose_name": "机器",
                "verbose_name_plural": "机器",
                "ordering": ["role", "name", "host"],
            },
        ),
        migrations.AddConstraint(
            model_name="machine",
            constraint=models.UniqueConstraint(fields=("host", "ssh_port", "username"), name="uniq_machine_connection_identity"),
        ),
        migrations.AddIndex(
            model_name="machine",
            index=models.Index(fields=["role", "is_active"], name="machine_role_active_idx"),
        ),
        migrations.AddIndex(
            model_name="machine",
            index=models.Index(fields=["host"], name="machine_host_idx"),
        ),
    ]
