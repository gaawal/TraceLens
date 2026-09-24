from __future__ import annotations

from django.db import transaction
from rest_framework import serializers

from apps.machines.models import AuthenticationType, Machine, MachineRole
from apps.machines.services.connection_probe import verify_probe_token


class MachineSummarySerializer(serializers.ModelSerializer):
    role_label = serializers.CharField(source="get_role_display", read_only=True)

    class Meta:
        model = Machine
        fields = ["id", "name", "host", "ssh_port", "username", "station_id", "station_type", "station_name", "role", "role_label", "auth_type", "has_credential", "connection_status", "last_connection_checked_at", "software_version", "version_checked_at"]


class MachineSerializer(serializers.ModelSerializer):
    connection_test_token = serializers.CharField(write_only=True, required=False, allow_blank=True)
    password = serializers.CharField(write_only=True, required=False, allow_blank=True, trim_whitespace=False)
    private_key = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        trim_whitespace=False,
        style={"base_template": "textarea.html"},
    )
    private_key_passphrase = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        trim_whitespace=False,
    )
    has_credential = serializers.BooleanField(read_only=True)
    role_label = serializers.CharField(source="get_role_display", read_only=True)
    origin_label = serializers.CharField(source="get_origin_display", read_only=True)
    auth_type_label = serializers.CharField(source="get_auth_type_display", read_only=True)
    environment_id = serializers.SerializerMethodField()

    class Meta:
        model = Machine
        fields = [
            "id",
            "name",
            "host",
            "ssh_port",
            "username",
            "station_id",
            "station_type",
            "station_name",
            "role",
            "role_label",
            "origin",
            "origin_label",
            "auth_type",
            "auth_type_label",
            "password",
            "private_key",
            "private_key_passphrase",
            "connection_test_token",
            "has_credential",
            "description",
            "is_active",
            "connection_status",
            "last_connection_checked_at",
            "software_version",
            "version_checked_at",
            "environment_id",
            "created_at",
            "updated_at",
        ]
        extra_kwargs = {"name": {"required": False, "allow_blank": True}}
        read_only_fields = [
            "origin",
            "connection_status",
            "last_connection_checked_at",
            "software_version",
            "version_checked_at",
            "created_at",
            "updated_at",
        ]

    def get_environment_id(self, obj: Machine):
        environment = getattr(obj, "owned_environment", None)
        return environment.pk if environment else None

    def validate(self, attrs):
        instance = self.instance
        new_role = attrs.get("role", instance.role if instance else None)
        new_auth_type = attrs.get("auth_type", instance.auth_type if instance else AuthenticationType.NONE)

        if instance and instance.role == MachineRole.UPPER and new_role == MachineRole.LOWER:
            if hasattr(instance, "owned_environment"):
                raise serializers.ValidationError(
                    {"role": "该上位机已经对应环境。请先删除环境后再修改角色。"}
                )

        if instance and instance.role == MachineRole.LOWER and new_role == MachineRole.UPPER:
            if instance.incoming_relations.exists():
                raise serializers.ValidationError(
                    {"role": "该下位机仍被环境引用，请先删除关联关系。"}
                )

        password = attrs.get("password")
        private_key = attrs.get("private_key")
        token = attrs.get("connection_test_token", "")
        if not instance:
            if not token:
                raise serializers.ValidationError({"connection_test_token": "保存前必须先测试 SSH 连接。"})
            try:
                verify_probe_token(token, attrs)
            except ValueError as exc:
                raise serializers.ValidationError({"connection_test_token": str(exc)}) from exc
        else:
            connection_fields = {"host", "ssh_port", "username", "auth_type", "password", "private_key", "private_key_passphrase"}
            connection_changed = any(field in attrs for field in connection_fields) and (
                attrs.get("host", instance.host) != instance.host
                or attrs.get("ssh_port", instance.ssh_port) != instance.ssh_port
                or attrs.get("username", instance.username) != instance.username
                or attrs.get("auth_type", instance.auth_type) != instance.auth_type
                or bool(attrs.get("password"))
                or bool(attrs.get("private_key"))
                or "private_key_passphrase" in attrs
            )
            if connection_changed:
                if not token:
                    raise serializers.ValidationError({"connection_test_token": "连接参数发生变化，保存前必须重新测试 SSH 连接。"})
                probe_data = {
                    "host": attrs.get("host", instance.host),
                    "ssh_port": attrs.get("ssh_port", instance.ssh_port),
                    "username": attrs.get("username", instance.username),
                    "auth_type": attrs.get("auth_type", instance.auth_type),
                    "password": attrs.get("password", ""),
                    "private_key": attrs.get("private_key", ""),
                    "private_key_passphrase": attrs.get("private_key_passphrase", ""),
                }
                if probe_data["auth_type"] == AuthenticationType.PASSWORD and not probe_data["password"]:
                    raise serializers.ValidationError({"password": "修改连接参数时请重新输入密码并测试连接。"})
                try:
                    verify_probe_token(token, probe_data)
                except ValueError as exc:
                    raise serializers.ValidationError({"connection_test_token": str(exc)}) from exc
        if new_auth_type == AuthenticationType.PASSWORD and private_key:
            raise serializers.ValidationError({"private_key": "密码认证不能同时提交私钥。"})
        if new_auth_type == AuthenticationType.PRIVATE_KEY and password:
            raise serializers.ValidationError({"password": "私钥认证不能同时提交密码。"})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        validated_data.pop("connection_test_token", None)
        password = validated_data.pop("password", "")
        private_key = validated_data.pop("private_key", "")
        passphrase = validated_data.pop("private_key_passphrase", "")
        if not validated_data.get("name"):
            validated_data["name"] = f"{validated_data.get('host', '')}({validated_data.get('username', '')})"
        machine = Machine(**validated_data)
        self._apply_credentials(machine, password, private_key, passphrase)
        machine.full_clean()
        machine.save()
        # The mandatory pre-save SSH probe keeps a pooled draft session. Re-key
        # it to the database machine so discovery and log scans do not reconnect.
        from apps.common.services.ssh import SSH_SESSION_POOL

        SSH_SESSION_POOL.promote_draft(machine)
        return machine

    @transaction.atomic
    def update(self, instance, validated_data):
        validated_data.pop("connection_test_token", None)
        password = validated_data.pop("password", None)
        private_key = validated_data.pop("private_key", None)
        passphrase = validated_data.pop("private_key_passphrase", None)
        previous_auth_type = instance.auth_type
        previous_auto_name = f"{instance.host}({instance.username})"
        is_dhh = instance.station_type == "DHH" or instance.station_name.strip().lower() == "dhh"
        dhh_connection_edited = is_dhh and (
            bool(password)
            or "username" in validated_data
            or "ssh_port" in validated_data
            or "auth_type" in validated_data
        )

        for key, value in validated_data.items():
            setattr(instance, key, value)
        if instance.name == previous_auto_name and "name" not in validated_data:
            instance.name = f"{instance.host}({instance.username})"

        if previous_auth_type != instance.auth_type:
            instance.clear_credentials()
        self._apply_credentials(instance, password, private_key, passphrase)
        if dhh_connection_edited:
            instance.dhh_credentials_managed = True
        instance.full_clean()
        instance.save()

        return instance

    @staticmethod
    def _apply_credentials(machine, password, private_key, passphrase):
        if password is not None and password != "":
            machine.set_password(password)
        if private_key is not None and private_key != "":
            machine.set_private_key(private_key)
        if passphrase is not None:
            machine.set_private_key_passphrase(passphrase)


class MachineConnectionProbeSerializer(serializers.Serializer):
    host = serializers.CharField(max_length=255)
    ssh_port = serializers.IntegerField(min_value=1, max_value=65535, default=22)
    username = serializers.CharField(max_length=128)
    role = serializers.ChoiceField(choices=MachineRole.choices, default=MachineRole.UPPER)
    auth_type = serializers.ChoiceField(choices=AuthenticationType.choices)
    password = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False)
    private_key = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False)
    private_key_passphrase = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False)

    def validate(self, attrs):
        if attrs["auth_type"] == AuthenticationType.PASSWORD and not attrs.get("password"):
            raise serializers.ValidationError({"password": "密码认证必须填写密码。"})
        if attrs["auth_type"] == AuthenticationType.PRIVATE_KEY and not attrs.get("private_key"):
            raise serializers.ValidationError({"private_key": "私钥认证必须填写私钥。"})
        return attrs
