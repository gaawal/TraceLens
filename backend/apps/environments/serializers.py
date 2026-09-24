from __future__ import annotations

from rest_framework import serializers

from apps.environments.models import (
    Environment,
    EnvironmentFolder,
    EnvironmentDiscovery,
    MachineRelation,
    ResourceSettings,
    LogPathProfile,
    EnvironmentDeployment,
    DeploymentStep,
)
from apps.machines.models import AuthenticationType, MachineRole
from apps.machines.serializers import MachineSummarySerializer




class LogPathProfileSerializer(serializers.ModelSerializer):
    category_label = serializers.SerializerMethodField()
    scope_label = serializers.CharField(source="get_scope_display", read_only=True)

    def get_category_label(self, obj):
        known = {"debug": "调试日志", "run": "运行日志", "executor": "执行器日志", "helf": "HELF日志", "sil": "SIL仿真日志"}
        return known.get(obj.category, obj.display_name or obj.category)

    def validate_category(self, value):
        import re
        value = value.strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
            raise serializers.ValidationError("日志类型标识仅支持小写字母、数字、下划线和短横线，且必须以字母开头。")
        return value

    def validate_match_rules(self, value):
        allowed = {"fm", "fm_timestamp", "archive", "executor_tree", "run_flat"}
        normalized = []
        for item in value or []:
            item = str(item).strip()
            if item not in allowed:
                raise serializers.ValidationError(f"不支持的匹配规则：{item}")
            if item not in normalized:
                normalized.append(item)
        return normalized or ["fm", "fm_timestamp", "archive"]

    class Meta:
        model = LogPathProfile
        fields = [
            "id", "category", "category_label", "display_name", "path_template",
            "enabled", "scope", "scope_label", "match_rules", "sort_order",
        ]
        read_only_fields = ["id", "category_label", "scope_label"]


class ResourceSettingsSerializer(serializers.ModelSerializer):
    lower_password = serializers.CharField(write_only=True, required=False, allow_blank=True, trim_whitespace=False)
    lower_private_key = serializers.CharField(write_only=True, required=False, allow_blank=True, trim_whitespace=False)
    lower_private_key_passphrase = serializers.CharField(write_only=True, required=False, allow_blank=True, trim_whitespace=False)
    has_lower_credential = serializers.SerializerMethodField()
    log_paths = LogPathProfileSerializer(source="log_path_profiles", many=True, required=False)
    path_parameters = serializers.SerializerMethodField()

    class Meta:
        model = ResourceSettings
        fields = [
            "id", "station_xml_path", "version_file_path", "source_code_path_template", "source_code_public_paths", "log_root_template",
            "dhh_debug_log_root", "dhh_executor_log_root", "dhh_run_log_root",
            "lower_username", "lower_ssh_port", "lower_auth_type",
            "lower_password", "lower_private_key", "lower_private_key_passphrase",
            "has_lower_credential", "display_rules", "display_rules_initialized", "data_extraction_rules", "log_paths", "path_parameters", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "display_rules_initialized", "created_at", "updated_at"]

    def get_path_parameters(self, obj):
        return [
            {"token": "{username}", "meaning": "当前目标机器 SSH 用户名", "example": "root"},
            {"token": "{machine_ip}", "meaning": "当前实际读取日志的机器 IP", "example": "10.10.1.20"},
            {"token": "{upper_machine_ip}", "meaning": "环境当前绑定的上位机 IP", "example": "10.10.1.10"},
            {"token": "{lower_machine_ip}", "meaning": "当前下位机 IP；上位机按下位机展开时使用", "example": "10.10.1.31"},
            {"token": "{subsystem}", "meaning": "当前日志子系统", "example": "rspm"},
            {"token": "{module}", "meaning": "当前日志模块 / FM", "example": "cspwszp"},
        ]

    def get_has_lower_credential(self, obj):
        if obj.lower_auth_type == AuthenticationType.PASSWORD:
            return bool(obj.encrypted_lower_password)
        if obj.lower_auth_type == AuthenticationType.PRIVATE_KEY:
            return bool(obj.encrypted_lower_private_key)
        return False

    def update(self, instance, validated_data):
        password = validated_data.pop("lower_password", None)
        private_key = validated_data.pop("lower_private_key", None)
        passphrase = validated_data.pop("lower_private_key_passphrase", None)
        log_paths = validated_data.pop("log_path_profiles", None)
        for key, value in validated_data.items():
            setattr(instance, key, value)
        if password is not None:
            instance.set_lower_password(password)
        if private_key is not None:
            instance.set_lower_private_key(private_key)
        if passphrase is not None:
            instance.set_lower_private_key_passphrase(passphrase)
        instance.save()
        # Apply changed fixed credentials immediately to XML-discovered lower machines.
        from apps.common.services.ssh import SSH_SESSION_POOL
        from apps.machines.models import Machine, MachineOrigin, MachineRole

        for machine in Machine.objects.filter(role=MachineRole.LOWER, origin=MachineOrigin.XML_DISCOVERY):
            machine.username = instance.lower_username
            machine.ssh_port = instance.lower_ssh_port
            machine.auth_type = instance.lower_auth_type
            if instance.lower_auth_type == AuthenticationType.PASSWORD and instance.encrypted_lower_password:
                machine.set_password(instance.get_lower_password())
                machine.encrypted_private_key = ""
                machine.encrypted_private_key_passphrase = ""
            elif instance.lower_auth_type == AuthenticationType.PRIVATE_KEY and instance.encrypted_lower_private_key:
                machine.set_private_key(instance.get_lower_private_key())
                machine.set_private_key_passphrase(instance.get_lower_private_key_passphrase())
                machine.encrypted_password = ""
            machine.save()
            SSH_SESSION_POOL.invalidate(machine, "lower_credentials_updated")
        if log_paths is not None:
            existing = {item.category: item for item in instance.log_path_profiles.all()}
            seen: set[str] = set()
            for payload in log_paths:
                category = str(payload.get("category") or "").strip().lower()
                if not category:
                    continue
                seen.add(category)
                profile = existing.get(category)
                if profile is None:
                    profile = LogPathProfile(settings=instance, category=category)
                for key, value in payload.items():
                    setattr(profile, key, value)
                profile.category = category
                profile.full_clean()
                profile.save()
            instance.log_path_profiles.exclude(category__in=seen).delete()
        return instance




class EnvironmentFolderSerializer(serializers.ModelSerializer):
    environment_count = serializers.IntegerField(read_only=True, required=False)

    class Meta:
        model = EnvironmentFolder
        fields = ["id", "name", "parent", "sort_order", "environment_count", "created_at", "updated_at"]
        read_only_fields = ["created_at", "updated_at"]

class MachineRelationSerializer(serializers.ModelSerializer):
    source_machine_summary = MachineSummarySerializer(source="source_machine", read_only=True)
    target_machine_summary = MachineSummarySerializer(source="target_machine", read_only=True)
    source_label = serializers.CharField(source="get_source_display", read_only=True)

    class Meta:
        model = MachineRelation
        fields = [
            "id", "environment", "source_machine", "source_machine_summary",
            "target_machine", "target_machine_summary", "source", "source_label",
            "is_active", "discovered_at", "metadata", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


class EnvironmentDiscoverySerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = EnvironmentDiscovery
        fields = [
            "id", "environment", "status", "status_label", "xml_path", "started_at",
            "finished_at", "found_count", "created_count", "updated_count", "message",
            "summary", "created_at", "updated_at",
        ]
        read_only_fields = fields






def _deployment_display_command(value):
    command = str(value or "")
    prefix = "source ~/.bashrc && "
    while command.startswith(prefix):
        command = command[len(prefix):]
    return command

class DeploymentStepSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    logs_included = serializers.SerializerMethodField()

    def get_logs_included(self, obj):
        return bool(self.context.get("include_logs", True))

    def to_representation(self, instance):
        payload = super().to_representation(instance)
        payload["command"] = _deployment_display_command(payload.get("command"))
        if not self.context.get("include_logs", True):
            payload["stdout"] = ""
            payload["stderr"] = ""
            payload["process_log"] = ""
        return payload

    class Meta:
        model = DeploymentStep
        fields = [
            "id", "key", "name", "sort_order", "status", "status_label", "command",
            "success_marker", "stdout", "stderr", "process_log", "logs_included", "exit_status", "retry_count", "message",
            "started_at", "finished_at", "created_at", "updated_at",
        ]
        read_only_fields = fields


class EnvironmentDeploymentSummarySerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = EnvironmentDeployment
        fields = [
            "id", "environment", "task_name", "target_version", "simulation_mode",
            "include_sdk", "upper_ip", "gpb_ips", "tb_mode", "install_mode",
            "install_port", "include_dhh", "dhh_ip", "dhh_user", "dhh_machine_id",
            "status", "status_label", "current_step", "message", "scheduled_at", "started_at",
            "finished_at", "created_at", "updated_at",
        ]
        read_only_fields = fields


class EnvironmentDeploymentSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    steps = serializers.SerializerMethodField()
    command_snapshot = serializers.SerializerMethodField()
    message = serializers.SerializerMethodField()

    def get_message(self, obj):
        message = str(obj.message or "")
        if obj.status == "pending":
            # Existing tasks created before the worker heartbeat check was added also
            # get a useful diagnosis when the progress dialog is reopened.
            try:
                from apps.environments.services.deployment_events import DeploymentEventBus
                if DeploymentEventBus.worker_alive() is False:
                    return "等待部署执行器接管；当前未检测到 deployment_worker 心跳，请确认部署执行器已启动。"
            except Exception:
                pass
        return message

    def get_command_snapshot(self, obj):
        return [
            {**item, "command": _deployment_display_command(item.get("command"))}
            for item in (obj.command_snapshot or [])
            if isinstance(item, dict)
        ]

    def get_steps(self, obj):
        selected_key = str(self.context.get("log_step_key") or obj.current_step or "").strip()
        result = []
        for step in obj.steps.all():
            include_logs = not selected_key or step.key == selected_key
            result.append(DeploymentStepSerializer(step, context={"include_logs": include_logs}).data)
        return result

    class Meta:
        model = EnvironmentDeployment
        fields = [
            "id", "environment", "task_name", "target_version", "simulation_mode",
            "include_sdk", "upper_ip", "gpb_ips", "tb_mode", "install_mode",
            "install_port", "include_dhh", "dhh_ip", "dhh_user", "dhh_machine_id",
            "configuration", "command_snapshot", "status", "status_label",
            "current_step", "message", "scheduled_at", "started_at", "finished_at", "steps",
            "created_at", "updated_at",
        ]
        read_only_fields = fields

class EnvironmentSerializer(serializers.ModelSerializer):
    upper_machine = MachineSummarySerializer(read_only=True)
    lower_machines = serializers.SerializerMethodField()
    dhh_machine = serializers.SerializerMethodField()
    is_dhh_environment = serializers.SerializerMethodField()
    version_mismatch = serializers.SerializerMethodField()
    version_mismatch_hosts = serializers.SerializerMethodField()
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    folder_name = serializers.CharField(source="folder.name", read_only=True, allow_null=True)

    class Meta:
        model = Environment
        fields = [
            "id", "name", "folder", "folder_name", "upper_machine", "lower_machines", "dhh_machine", "is_dhh_environment", "status", "status_label",
            "station_user_id", "software_version", "version_checked_at", "version_mismatch", "version_mismatch_hosts", "last_discovered_at",
            "description", "created_at", "updated_at",
        ]
        read_only_fields = [
            "upper_machine", "status", "station_user_id", "software_version",
            "version_checked_at", "last_discovered_at", "created_at", "updated_at",
        ]


    def _dhh_relation(self, obj):
        for relation in obj.machine_relations.select_related("target_machine").filter(is_active=True):
            machine = relation.target_machine
            metadata = relation.metadata or {}
            station_name = str(metadata.get("station_name") or machine.station_name or machine.name or "").strip().lower()
            station_type = str(metadata.get("station_type") or machine.station_type or "").strip().upper()
            if station_name == "dhh" or station_type == "DHH":
                return relation
        return None

    def get_dhh_machine(self, obj):
        relation = self._dhh_relation(obj)
        if relation is None:
            return None
        item = MachineSummarySerializer(relation.target_machine).data
        item["station"] = relation.metadata
        return item

    def get_is_dhh_environment(self, obj):
        return self._dhh_relation(obj) is not None

    def _version_mismatch_hosts(self, obj):
        cache_name = "_tracelens_version_mismatch_hosts"
        cached = getattr(obj, cache_name, None)
        if cached is not None:
            return cached
        upper_version = str(obj.software_version or "").strip()
        if not upper_version:
            setattr(obj, cache_name, [])
            return []
        hosts = []
        for relation in obj.machine_relations.select_related("target_machine").filter(is_active=True):
            machine = relation.target_machine
            metadata = relation.metadata or {}
            station_name = str(metadata.get("station_name") or machine.station_name or machine.name or "").strip().lower()
            station_type = str(metadata.get("station_type") or machine.station_type or "").strip().upper()
            if station_name == "dhh" or station_type == "DHH":
                continue
            lower_version = str(machine.software_version or "").strip()
            if lower_version and lower_version != upper_version:
                hosts.append(machine.host)
        setattr(obj, cache_name, hosts)
        return hosts

    def get_version_mismatch_hosts(self, obj):
        return self._version_mismatch_hosts(obj)

    def get_version_mismatch(self, obj):
        return bool(self._version_mismatch_hosts(obj))

    def get_lower_machines(self, obj):
        relations = obj.machine_relations.select_related("target_machine").filter(is_active=True)
        payload = []
        for relation in relations:
            machine = relation.target_machine
            metadata = relation.metadata or {}
            station_name = str(metadata.get("station_name") or machine.station_name or machine.name or "").strip().lower()
            station_type = str(metadata.get("station_type") or machine.station_type or "").strip().upper()
            if station_name == "dhh" or station_type == "DHH":
                continue
            item = MachineSummarySerializer(machine).data
            item["station"] = metadata
            payload.append(item)
        return payload


class XmlPreviewRequestSerializer(serializers.Serializer):
    xml_content = serializers.CharField(trim_whitespace=False, max_length=2_000_000)
