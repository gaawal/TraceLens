from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models

from apps.common.models import TimeStampedModel
from apps.common.services.crypto import CredentialCipher
from apps.machines.models import AuthenticationType, MachineRole




class LogPathCategory(models.TextChoices):
    DEBUG = "debug", "调试日志"
    RUN = "run", "运行日志"
    EXECUTOR = "executor", "执行器日志"
    HELF = "helf", "HELF日志"
    SIL = "sil", "SIL仿真日志"


class LogPathScope(models.TextChoices):
    UPPER_ONLY = "upper_only", "上位机"
    LOWER_ONLY = "lower_only", "下位机"
    EACH_MACHINE = "each_machine", "上位机 + 下位机"
    UPPER_FOR_LOWER = "upper_for_lower", "上位机按下位机展开"


class EnvironmentStatus(models.TextChoices):
    PENDING = "pending", "待发现"
    READY = "ready", "已就绪"
    ERROR = "error", "发现失败"
    STALE = "stale", "待刷新"


class DiscoveryStatus(models.TextChoices):
    PENDING = "pending", "等待中"
    RUNNING = "running", "执行中"
    SUCCESS = "success", "成功"
    FAILED = "failed", "失败"


class RelationSource(models.TextChoices):
    XML = "xml", "XML 自动发现"
    MANUAL = "manual", "手工维护"




class EnvironmentFolder(TimeStampedModel):
    name = models.CharField("文件夹名称", max_length=128)
    parent = models.ForeignKey(
        "self",
        verbose_name="父文件夹",
        related_name="children",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    sort_order = models.PositiveSmallIntegerField("排序", default=0)

    class Meta:
        verbose_name = "环境资源文件夹"
        verbose_name_plural = "环境资源文件夹"
        ordering = ["sort_order", "name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["parent", "name"], name="uniq_environment_folder_sibling_name")
        ]

    def __str__(self) -> str:
        return self.name

class Environment(TimeStampedModel):
    name = models.CharField("环境名称", max_length=128)
    upper_machine = models.OneToOneField(
        "machines.Machine",
        verbose_name="上位机",
        related_name="owned_environment",
        on_delete=models.PROTECT,
        limit_choices_to={"role": MachineRole.UPPER},
    )
    folder = models.ForeignKey(
        EnvironmentFolder,
        verbose_name="资源文件夹",
        related_name="environments",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    status = models.CharField(
        "状态",
        max_length=16,
        choices=EnvironmentStatus.choices,
        default=EnvironmentStatus.PENDING,
    )
    last_discovered_at = models.DateTimeField("最后发现时间", null=True, blank=True)
    station_user_id = models.CharField("stations userId", max_length=32, blank=True)
    software_version = models.CharField("软件版本", max_length=256, blank=True)
    version_checked_at = models.DateTimeField("版本查询时间", null=True, blank=True)
    deployment_scripts = models.JSONField("部署后脚本历史", default=list, blank=True)
    description = models.TextField("描述", blank=True)
    # 收藏（五角星）。存在环境上而不是浏览器里：资源是团队共享的，
    # 「收藏了几台」这种统计只有在共享的前提下才有意义。
    is_favorite = models.BooleanField("收藏", default=False, db_index=True)

    class Meta:
        verbose_name = "环境"
        verbose_name_plural = "环境"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        super().clean()
        if self.upper_machine_id and self.upper_machine.role != MachineRole.UPPER:
            raise ValidationError({"upper_machine": "环境只能绑定上位机。"})


class MachineRelation(TimeStampedModel):
    environment = models.ForeignKey(
        Environment,
        verbose_name="环境",
        related_name="machine_relations",
        on_delete=models.CASCADE,
    )
    source_machine = models.ForeignKey(
        "machines.Machine",
        verbose_name="来源上位机",
        related_name="outgoing_relations",
        on_delete=models.PROTECT,
        limit_choices_to={"role": MachineRole.UPPER},
    )
    target_machine = models.ForeignKey(
        "machines.Machine",
        verbose_name="下位机",
        related_name="incoming_relations",
        on_delete=models.PROTECT,
        limit_choices_to={"role": MachineRole.LOWER},
    )
    source = models.CharField(
        "关系来源",
        max_length=16,
        choices=RelationSource.choices,
        default=RelationSource.XML,
    )
    is_active = models.BooleanField("有效", default=True)
    discovered_at = models.DateTimeField("发现时间", null=True, blank=True)
    metadata = models.JSONField("扩展信息", default=dict, blank=True)

    class Meta:
        verbose_name = "上下位机关系"
        verbose_name_plural = "上下位机关系"
        ordering = ["environment", "target_machine"]
        constraints = [
            models.UniqueConstraint(
                fields=["environment", "target_machine"],
                name="uniq_environment_lower_machine",
            )
        ]
        indexes = [
            models.Index(fields=["environment", "is_active"], name="relation_env_active_idx")
        ]

    def __str__(self) -> str:
        return f"{self.source_machine} -> {self.target_machine}"

    def clean(self) -> None:
        super().clean()
        errors = {}
        if self.source_machine_id and self.source_machine.role != MachineRole.UPPER:
            errors["source_machine"] = "来源机器必须是上位机。"
        if self.target_machine_id and self.target_machine.role != MachineRole.LOWER:
            errors["target_machine"] = "目标机器必须是下位机。"
        if self.environment_id and self.source_machine_id:
            if self.environment.upper_machine_id != self.source_machine_id:
                errors["source_machine"] = "来源上位机必须与环境的上位机一致。"
        if self.source_machine_id and self.target_machine_id:
            if self.source_machine_id == self.target_machine_id:
                errors["target_machine"] = "上位机和下位机不能是同一台机器。"
        if errors:
            raise ValidationError(errors)


class EnvironmentDiscovery(TimeStampedModel):
    environment = models.ForeignKey(
        Environment,
        verbose_name="环境",
        related_name="discoveries",
        on_delete=models.CASCADE,
    )
    status = models.CharField(
        "执行状态",
        max_length=16,
        choices=DiscoveryStatus.choices,
        default=DiscoveryStatus.PENDING,
    )
    xml_path = models.CharField("XML文件路径", max_length=512, blank=True)
    started_at = models.DateTimeField("开始时间", null=True, blank=True)
    finished_at = models.DateTimeField("结束时间", null=True, blank=True)
    found_count = models.PositiveIntegerField("发现数量", default=0)
    created_count = models.PositiveIntegerField("新增数量", default=0)
    updated_count = models.PositiveIntegerField("更新数量", default=0)
    message = models.TextField("执行信息", blank=True)
    summary = models.JSONField("摘要", default=dict, blank=True)

    class Meta:
        verbose_name = "环境发现记录"
        verbose_name_plural = "环境发现记录"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["environment", "-created_at"], name="discover_env_time_idx")]

    def __str__(self) -> str:
        return f"{self.environment.name} - {self.get_status_display()}"


class DeploymentStatus(models.TextChoices):
    SCHEDULED = "scheduled", "待执行"
    PENDING = "pending", "等待中"
    RUNNING = "running", "部署中"
    STOPPING = "stopping", "停止中"
    STOPPED = "stopped", "已停止"
    SUCCESS = "success", "成功"
    FAILED = "failed", "失败"


class DeploymentStepStatus(models.TextChoices):
    PENDING = "pending", "等待中"
    RUNNING = "running", "执行中"
    STOPPED = "stopped", "已停止"
    SUCCESS = "success", "成功"
    FAILED = "failed", "失败"
    SKIPPED = "skipped", "已跳过"


class EnvironmentDeployment(TimeStampedModel):
    environment = models.ForeignKey(
        Environment,
        verbose_name="环境",
        related_name="deployments",
        on_delete=models.CASCADE,
    )
    task_name = models.CharField("任务名", max_length=128)
    target_version = models.CharField("目标版本", max_length=256)
    simulation_mode = models.CharField("仿真模式", max_length=32, default="sim0_sil")
    include_sdk = models.BooleanField("拉取 SDK", default=True)
    upper_ip = models.CharField("上位机 IP", max_length=255)
    gpb_ips = models.JSONField("GPB IP 列表", default=list, blank=True)
    tb_mode = models.CharField("TB 部署模式", max_length=128, blank=True)
    install_mode = models.CharField("安装 GPB 模式", max_length=128, blank=True)
    install_port = models.PositiveSmallIntegerField("安装端口", default=0)
    include_dhh = models.BooleanField("部署 DHH", default=False)
    dhh_ip = models.CharField("DHH IP", max_length=255, blank=True)
    dhh_user = models.CharField("DHH 用户", max_length=128, blank=True, default="root")
    dhh_machine_id = models.CharField("DHH machineId", max_length=128, blank=True)
    configuration = models.JSONField("部署参数快照", default=dict, blank=True)
    command_snapshot = models.JSONField("部署命令快照", default=list, blank=True)
    status = models.CharField("状态", max_length=16, choices=DeploymentStatus.choices, default=DeploymentStatus.PENDING)
    current_step = models.CharField("当前步骤", max_length=32, blank=True)
    message = models.TextField("状态说明", blank=True)
    scheduled_at = models.DateTimeField("计划执行时间", null=True, blank=True, db_index=True)
    started_at = models.DateTimeField("开始时间", null=True, blank=True)
    finished_at = models.DateTimeField("结束时间", null=True, blank=True)

    class Meta:
        verbose_name = "环境部署记录"
        verbose_name_plural = "环境部署记录"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["environment", "-created_at"], name="env_deploy_time_idx")]

    def __str__(self) -> str:
        return f"{self.environment.name} - {self.task_name}"


class DeploymentStep(TimeStampedModel):
    deployment = models.ForeignKey(
        EnvironmentDeployment,
        verbose_name="部署任务",
        related_name="steps",
        on_delete=models.CASCADE,
    )
    key = models.CharField("步骤标识", max_length=32)
    name = models.CharField("步骤名称", max_length=64)
    sort_order = models.PositiveSmallIntegerField("顺序", default=0)
    status = models.CharField("状态", max_length=16, choices=DeploymentStepStatus.choices, default=DeploymentStepStatus.PENDING)
    command = models.TextField("执行命令", blank=True)
    success_marker = models.CharField("成功标志", max_length=256, blank=True)
    stdout = models.TextField("标准输出", blank=True)
    stderr = models.TextField("错误输出", blank=True)
    process_log = models.TextField("顺序过程日志", blank=True)
    exit_status = models.IntegerField("退出码", null=True, blank=True)
    retry_count = models.PositiveSmallIntegerField("重试次数", default=0)
    message = models.TextField("步骤说明", blank=True)
    started_at = models.DateTimeField("开始时间", null=True, blank=True)
    finished_at = models.DateTimeField("结束时间", null=True, blank=True)

    class Meta:
        verbose_name = "环境部署步骤"
        verbose_name_plural = "环境部署步骤"
        ordering = ["sort_order", "id"]
        constraints = [models.UniqueConstraint(fields=["deployment", "key"], name="uniq_deployment_step_key")]

    def __str__(self) -> str:
        return f"{self.deployment_id}:{self.name}"


class ResourceSettings(TimeStampedModel):
    station_xml_path = models.CharField(
        "stations.xml 路径", max_length=512, default="~/SW/config/sw/slcm/stations.xml"
    )
    version_file_path = models.CharField("版本文件路径", max_length=512, default="~/SW/version")
    source_code_path_template = models.CharField(
        "Python 源码目录模板",
        max_length=512,
        default="/home/{username}/SW/lib/python/{subsystem}/{module}",
    )
    source_code_public_paths = models.TextField(
        "Python 公共源码目录",
        default="/home/{username}/SW/lib/python/me/cpfr/\n/home/{username}/SW/lib/python/sw/adf/",
        blank=True,
    )
    log_root_template = models.CharField(
        "日志根目录模板", max_length=512, default="/log/{username}/debug"
    )
    dhh_debug_log_root = models.CharField(
        "DHH 调试日志根目录",
        max_length=512,
        default="/data/sync/log/debug/",
        help_text="DHH 环境调试日志专用根目录；不继承普通上位机日志路径。",
    )
    dhh_executor_log_root = models.CharField(
        "DHH 执行器日志根目录",
        max_length=512,
        default="/data/sync/log/debug/elog/",
        help_text="DHH 环境执行器日志专用根目录；不继承普通上位机日志路径。",
    )
    dhh_run_log_root = models.CharField(
        "DHH 运行日志根目录",
        max_length=512,
        default="/data/sync/log/run/",
        help_text="DHH 环境运行日志专用根目录；不继承普通上位机日志路径。",
    )
    lower_username = models.CharField("下位机用户名", max_length=128, blank=True)
    lower_ssh_port = models.PositiveSmallIntegerField("下位机 SSH 端口", default=22)
    lower_auth_type = models.CharField(
        "下位机认证方式", max_length=32, choices=AuthenticationType.choices, default=AuthenticationType.PASSWORD
    )
    encrypted_lower_password = models.TextField("加密下位机密码", blank=True, editable=False)
    encrypted_lower_private_key = models.TextField("加密下位机私钥", blank=True, editable=False)
    encrypted_lower_private_key_passphrase = models.TextField("加密下位机私钥口令", blank=True, editable=False)
    display_rules = models.JSONField("日志语义规则", default=None, null=True, blank=True)
    display_rules_initialized = models.BooleanField("日志语义规则已初始化", default=False)
    data_extraction_rules = models.JSONField("数据提取器规则", default=None, null=True, blank=True)

    class Meta:
        verbose_name = "资源设置"
        verbose_name_plural = "资源设置"

    @classmethod
    def get_solo(cls):
        instance, _ = cls.objects.get_or_create(pk=1)
        if not instance.log_path_profiles.exists():
            defaults = [
                (LogPathCategory.DEBUG, "调试日志", "/log/{username}/debug", True, LogPathScope.EACH_MACHINE, 10),
                (LogPathCategory.RUN, "运行日志", "/log/{username}/run/", False, LogPathScope.EACH_MACHINE, 20),
                (LogPathCategory.EXECUTOR, "执行器日志", "/log/{username}/debug/elog", True, LogPathScope.UPPER_ONLY, 30),
                (LogPathCategory.HELF, "HELF日志", "", False, LogPathScope.EACH_MACHINE, 40),
                (LogPathCategory.SIL, "SIL仿真日志", "", False, LogPathScope.EACH_MACHINE, 50),
            ]
            for category, name, template, enabled, scope, order in defaults:
                LogPathProfile.objects.get_or_create(
                    settings=instance,
                    category=category,
                    defaults={
                        "display_name": name,
                        "path_template": template,
                        "enabled": enabled,
                        "scope": scope,
                        "match_rules": (
                            ["executor_tree"] if category == LogPathCategory.EXECUTOR
                            else ["run_flat"] if category == LogPathCategory.RUN
                            else ["fm", "fm_timestamp", "archive"]
                        ),
                        "sort_order": order,
                    },
                )
        return instance

    def set_lower_password(self, value: str) -> None:
        self.encrypted_lower_password = CredentialCipher().encrypt(value)

    def get_lower_password(self) -> str:
        return CredentialCipher().decrypt(self.encrypted_lower_password)

    def set_lower_private_key(self, value: str) -> None:
        self.encrypted_lower_private_key = CredentialCipher().encrypt(value)

    def get_lower_private_key(self) -> str:
        return CredentialCipher().decrypt(self.encrypted_lower_private_key)

    def set_lower_private_key_passphrase(self, value: str) -> None:
        self.encrypted_lower_private_key_passphrase = CredentialCipher().encrypt(value)

    def get_lower_private_key_passphrase(self) -> str:
        return CredentialCipher().decrypt(self.encrypted_lower_private_key_passphrase)


class LogPathProfile(TimeStampedModel):
    settings = models.ForeignKey(
        ResourceSettings,
        verbose_name="资源设置",
        related_name="log_path_profiles",
        on_delete=models.CASCADE,
    )
    category = models.CharField("日志类型标识", max_length=64)
    display_name = models.CharField("显示名称", max_length=64)
    path_template = models.CharField("目录模板", max_length=512, blank=True)
    enabled = models.BooleanField("启用检索", default=False)
    scope = models.CharField(
        "机器范围",
        max_length=32,
        choices=LogPathScope.choices,
        default=LogPathScope.EACH_MACHINE,
    )
    match_rules = models.JSONField(
        "文件匹配规则",
        default=list,
        blank=True,
        help_text="支持 fm / fm_timestamp / archive / executor_tree / run_flat；executor_tree 表示执行器根目录/IP/子系统/任意日志，run_flat 表示运行日志根目录下 event.log/归档。",
    )
    sort_order = models.PositiveSmallIntegerField("排序", default=0)

    class Meta:
        verbose_name = "日志路径配置"
        verbose_name_plural = "日志路径配置"
        ordering = ["sort_order", "id"]
        constraints = [
            models.UniqueConstraint(fields=["settings", "category"], name="uniq_resource_log_path_category")
        ]

    def __str__(self) -> str:
        return self.display_name
