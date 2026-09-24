from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models

from apps.common.models import TimeStampedModel
from apps.common.services.crypto import CredentialCipher


class MachineRole(models.TextChoices):
    UPPER = "upper", "上位机"
    LOWER = "lower", "下位机"


class MachineOrigin(models.TextChoices):
    MANUAL = "manual", "用户配置"
    XML_DISCOVERY = "xml_discovery", "XML 自动发现"


class AuthenticationType(models.TextChoices):
    NONE = "none", "未配置"
    PASSWORD = "password", "密码"
    PRIVATE_KEY = "private_key", "私钥"


class ConnectionStatus(models.TextChoices):
    UNKNOWN = "unknown", "未知"
    ONLINE = "online", "在线"
    OFFLINE = "offline", "离线"


class Machine(TimeStampedModel):
    name = models.CharField("机器名称", max_length=128)
    host = models.CharField("IP或主机名", max_length=255)
    ssh_port = models.PositiveSmallIntegerField("SSH端口", default=22)
    username = models.CharField("用户名", max_length=128, blank=True)
    station_id = models.CharField("站点ID", max_length=32, blank=True)
    station_type = models.CharField("站点类型", max_length=16, blank=True)
    station_name = models.CharField("站点名称", max_length=128, blank=True)
    role = models.CharField("机器角色", max_length=16, choices=MachineRole.choices)
    origin = models.CharField(
        "资源来源",
        max_length=32,
        choices=MachineOrigin.choices,
        default=MachineOrigin.MANUAL,
    )
    auth_type = models.CharField(
        "认证方式",
        max_length=32,
        choices=AuthenticationType.choices,
        default=AuthenticationType.NONE,
    )
    encrypted_password = models.TextField("加密密码", blank=True, editable=False)
    encrypted_private_key = models.TextField("加密私钥", blank=True, editable=False)
    encrypted_private_key_passphrase = models.TextField("加密私钥口令", blank=True, editable=False)
    dhh_credentials_managed = models.BooleanField("DHH 凭据已独立维护", default=False, editable=False)
    description = models.TextField("描述", blank=True)
    is_active = models.BooleanField("启用", default=True)
    connection_status = models.CharField(
        "连接状态",
        max_length=16,
        choices=ConnectionStatus.choices,
        default=ConnectionStatus.UNKNOWN,
    )
    last_connection_checked_at = models.DateTimeField("最后连接检查时间", null=True, blank=True)
    software_version = models.CharField("软件版本", max_length=256, blank=True)
    version_checked_at = models.DateTimeField("版本查询时间", null=True, blank=True)

    class Meta:
        verbose_name = "机器"
        verbose_name_plural = "机器"
        ordering = ["role", "name", "host"]
        constraints = [
            models.UniqueConstraint(
                fields=["host", "ssh_port", "username"],
                name="uniq_machine_connection_identity",
            )
        ]
        indexes = [
            models.Index(fields=["role", "is_active"], name="machine_role_active_idx"),
            models.Index(fields=["host"], name="machine_host_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.host})"

    @property
    def has_credential(self) -> bool:
        if self.auth_type == AuthenticationType.PASSWORD:
            return bool(self.encrypted_password)
        if self.auth_type == AuthenticationType.PRIVATE_KEY:
            return bool(self.encrypted_private_key)
        return False

    def set_password(self, value: str) -> None:
        self.encrypted_password = CredentialCipher().encrypt(value)

    def get_password(self) -> str:
        return CredentialCipher().decrypt(self.encrypted_password)

    def set_private_key(self, value: str) -> None:
        self.encrypted_private_key = CredentialCipher().encrypt(value)

    def get_private_key(self) -> str:
        return CredentialCipher().decrypt(self.encrypted_private_key)

    def set_private_key_passphrase(self, value: str) -> None:
        self.encrypted_private_key_passphrase = CredentialCipher().encrypt(value)

    def get_private_key_passphrase(self) -> str:
        return CredentialCipher().decrypt(self.encrypted_private_key_passphrase)

    def clear_credentials(self) -> None:
        self.encrypted_password = ""
        self.encrypted_private_key = ""
        self.encrypted_private_key_passphrase = ""

    def clean(self) -> None:
        super().clean()
        errors = {}
        if not 1 <= self.ssh_port <= 65535:
            errors["ssh_port"] = "SSH 端口必须在 1 到 65535 之间。"
        if self.pk and self.role == MachineRole.LOWER:
            from apps.environments.models import Environment

            if Environment.objects.filter(upper_machine_id=self.pk).exists():
                errors["role"] = "该机器仍对应一套环境，不能直接改为下位机。"
        if self.pk and self.role == MachineRole.UPPER:
            from apps.environments.models import MachineRelation

            if MachineRelation.objects.filter(target_machine_id=self.pk, is_active=True).exists():
                errors["role"] = "该机器仍作为下位机被环境引用，不能直接改为上位机。"
        if errors:
            raise ValidationError(errors)
