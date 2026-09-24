from django.db import migrations, models
import django.db.models.deletion


def normalize_log_paths(apps, schema_editor):
    ResourceSettings = apps.get_model('environments', 'ResourceSettings')
    LogPathProfile = apps.get_model('environments', 'LogPathProfile')
    settings_obj, _ = ResourceSettings.objects.get_or_create(pk=1)
    defaults = [
        ('debug', '调试日志', '/log/{username}/debug', True, 'each_machine', 10),
        ('run', '运行日志', '/log/{username}/run/', False, 'each_machine', 20),
        ('executor', '执行器日志', '/log/l00021753/debug/elog/{lower_machine_ip}/', True, 'upper_for_lower', 30),
        ('helf', 'HELF日志', '', False, 'each_machine', 40),
        ('sil', 'SIL仿真日志', '', False, 'each_machine', 50),
    ]
    for category, display_name, path_template, enabled, scope, sort_order in defaults:
        LogPathProfile.objects.update_or_create(
            settings=settings_obj,
            category=category,
            defaults={
                'display_name': display_name,
                'path_template': path_template,
                'enabled': enabled,
                'scope': scope,
                'sort_order': sort_order,
            },
        )


class Migration(migrations.Migration):
    dependencies = [('environments', '0004_log_path_profiles')]

    operations = [
        migrations.CreateModel(
            name='EnvironmentFolder',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='更新时间')),
                ('name', models.CharField(max_length=128, verbose_name='文件夹名称')),
                ('sort_order', models.PositiveSmallIntegerField(default=0, verbose_name='排序')),
                ('parent', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='children', to='environments.environmentfolder', verbose_name='父文件夹')),
            ],
            options={
                'verbose_name': '环境资源文件夹',
                'verbose_name_plural': '环境资源文件夹',
                'ordering': ['sort_order', 'name', 'id'],
            },
        ),
        migrations.AddConstraint(
            model_name='environmentfolder',
            constraint=models.UniqueConstraint(fields=('parent', 'name'), name='uniq_environment_folder_sibling_name'),
        ),
        migrations.AddField(
            model_name='environment',
            name='folder',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='environments', to='environments.environmentfolder', verbose_name='资源文件夹'),
        ),
        migrations.RunPython(normalize_log_paths, migrations.RunPython.noop),
    ]
