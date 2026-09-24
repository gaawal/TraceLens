from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("environments", "0001_initial")]
    operations = [
        migrations.AddField(model_name="environment", name="station_user_id", field=models.CharField(blank=True, max_length=32, verbose_name="stations userId")),
        migrations.AddField(model_name="environment", name="software_version", field=models.CharField(blank=True, max_length=256, verbose_name="软件版本")),
        migrations.AddField(model_name="environment", name="version_checked_at", field=models.DateTimeField(blank=True, null=True, verbose_name="版本查询时间")),
        migrations.CreateModel(
            name="ResourceSettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("station_xml_path", models.CharField(default="~/SW/config/sw/slcm/stations.xml", max_length=512, verbose_name="stations.xml 路径")),
                ("version_file_path", models.CharField(default="~/SW/version", max_length=512, verbose_name="版本文件路径")),
                ("log_root_template", models.CharField(default="/log/{username}/debug", max_length=512, verbose_name="日志根目录模板")),
                ("lower_username", models.CharField(blank=True, max_length=128, verbose_name="下位机用户名")),
                ("lower_ssh_port", models.PositiveSmallIntegerField(default=22, verbose_name="下位机 SSH 端口")),
                ("lower_auth_type", models.CharField(choices=[("none", "未配置"), ("password", "密码"), ("private_key", "私钥")], default="password", max_length=32, verbose_name="下位机认证方式")),
                ("encrypted_lower_password", models.TextField(blank=True, editable=False, verbose_name="加密下位机密码")),
                ("encrypted_lower_private_key", models.TextField(blank=True, editable=False, verbose_name="加密下位机私钥")),
                ("encrypted_lower_private_key_passphrase", models.TextField(blank=True, editable=False, verbose_name="加密下位机私钥口令")),
            ],
            options={"verbose_name": "资源设置", "verbose_name_plural": "资源设置"},
        ),
    ]
