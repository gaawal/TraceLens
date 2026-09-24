from __future__ import annotations

from datetime import datetime, timedelta

from rest_framework import serializers

from apps.logsources.models import (
    LogFmDefinition,
    LogFormatParserRule,
    LogModuleKind,
    LogQuerySkill,
    LogSourceRule,
    LogSubsystemDefinition,
    LogWatch,
)


class LogSourceRuleSerializer(serializers.ModelSerializer):
    resolved_root_path = serializers.CharField(source="resolve_root_path", read_only=True)

    class Meta:
        model = LogSourceRule
        fields = [
            "id", "machine", "name", "root_path_template", "resolved_root_path",
            "subsystem", "component", "module", "file_pattern", "auto_discovery",
            "enabled", "description", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


_QUERY_SKILL_WHEN = {"always", "no_match", "source_empty", "source_not_found", "insufficient_evidence", "keyword_match"}
_QUERY_SKILL_SOURCE_TYPES = {"standard", "custom_path"}
_QUERY_SKILL_MACHINE_SCOPES = {"upper", "lower", "all_lower", "dhh"}


def _clean_string_list(value, *, limit: int = 32, item_limit: int = 128):
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise serializers.ValidationError("必须是字符串数组。")
    result = []
    seen = set()
    for raw in value[:limit]:
        text = str(raw or "").strip()[:item_limit]
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


class LogQuerySkillSerializer(serializers.ModelSerializer):
    subsystem_name = serializers.CharField(source="subsystem.name", read_only=True)
    subsystem_display_name = serializers.CharField(source="subsystem.display_name", read_only=True)

    class Meta:
        model = LogQuerySkill
        fields = [
            "id", "subsystem", "subsystem_name", "subsystem_display_name", "name",
            "enabled", "priority", "trigger_modules", "trigger_keywords", "description",
            "steps", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def validate_trigger_modules(self, value):
        return _clean_string_list(value, limit=32, item_limit=128)

    def validate_trigger_keywords(self, value):
        return _clean_string_list(value, limit=48, item_limit=160)

    def validate_steps(self, value):
        if value in (None, ""):
            return []
        if not isinstance(value, list):
            raise serializers.ValidationError("检索步骤必须是数组。")
        if len(value) > 8:
            raise serializers.ValidationError("一个 Skill 最多配置 8 个补充检索步骤。")
        result = []
        for index, raw in enumerate(value):
            if not isinstance(raw, dict):
                raise serializers.ValidationError(f"第 {index + 1} 个检索步骤必须是对象。")
            source_type = str(raw.get("source_type") or "standard").strip().lower()
            if source_type not in _QUERY_SKILL_SOURCE_TYPES:
                raise serializers.ValidationError(f"第 {index + 1} 个步骤 source_type 仅支持 standard/custom_path。")
            machine_scope = str(raw.get("machine_scope") or "upper").strip().lower()
            if machine_scope not in _QUERY_SKILL_MACHINE_SCOPES:
                raise serializers.ValidationError(f"第 {index + 1} 个步骤 machine_scope 不支持。")
            when = str(raw.get("when") or "insufficient_evidence").strip().lower()
            if when not in _QUERY_SKILL_WHEN:
                raise serializers.ValidationError(f"第 {index + 1} 个步骤 when 不支持。")
            path_template = str(raw.get("path_template") or "").strip()
            if source_type == "custom_path":
                if not path_template:
                    raise serializers.ValidationError(f"第 {index + 1} 个自定义日志步骤必须填写 path_template。")
                if ".." in path_template.replace("\\", "/").split("/"):
                    raise serializers.ValidationError(f"第 {index + 1} 个 path_template 不能包含 ..。")
                if not (path_template.startswith("/") or path_template.startswith("~/")):
                    raise serializers.ValidationError(f"第 {index + 1} 个 path_template 必须是 / 或 ~/ 开头的远端路径。")
            before = max(0, min(int(raw.get("time_before_seconds") or 5), 600))
            after = max(0, min(int(raw.get("time_after_seconds") or 5), 600))
            result.append({
                "name": str(raw.get("name") or f"补充日志 {index + 1}").strip()[:120],
                "when": when,
                "source_type": source_type,
                "machine_scope": machine_scope,
                "source_category": str(raw.get("source_category") or "debug").strip()[:64],
                "module": str(raw.get("module") or "").strip()[:128],
                "path_template": path_template[:512],
                "file_pattern": str(raw.get("file_pattern") or "*.log*").strip()[:128] or "*.log*",
                "keywords": _clean_string_list(raw.get("keywords") or [], limit=24, item_limit=160),
                "time_before_seconds": before,
                "time_after_seconds": after,
                "note": str(raw.get("note") or "").strip()[:800],
            })
        return result

    def validate(self, attrs):
        subsystem = attrs.get("subsystem") or getattr(self.instance, "subsystem", None)
        name = str(attrs.get("name", getattr(self.instance, "name", ""))).strip()
        if not subsystem:
            raise serializers.ValidationError({"subsystem": "日志查询 Skill 必须绑定一个子系统。"})
        if not name:
            raise serializers.ValidationError({"name": "Skill 名称不能为空。"})
        queryset = LogQuerySkill.objects.filter(subsystem=subsystem, name__iexact=name)
        if self.instance is not None:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise serializers.ValidationError({"name": "该子系统下已存在同名 Skill。"})

        # A query Skill is a subsystem-local strategy.  Standard log steps and
        # trigger modules must resolve inside the bound subsystem; otherwise a
        # typo or copied Skill could silently jump to an unrelated log tree.
        fm_rows = list(subsystem.fms.all())
        fm_aliases = {
            str(alias).strip().casefold(): fm.name
            for fm in fm_rows
            for alias in (fm.name, fm.display_name)
            if str(alias or "").strip()
        }
        trigger_modules = attrs.get("trigger_modules", getattr(self.instance, "trigger_modules", [])) or []
        invalid_triggers = [module for module in trigger_modules if str(module).casefold() not in fm_aliases]
        if invalid_triggers:
            raise serializers.ValidationError({
                "trigger_modules": "触发模块必须属于当前子系统：" + "、".join(str(item) for item in invalid_triggers[:8])
            })
        normalized_triggers = [fm_aliases[str(module).casefold()] for module in trigger_modules]
        if "trigger_modules" in attrs:
            attrs["trigger_modules"] = normalized_triggers

        steps = attrs.get("steps", getattr(self.instance, "steps", [])) or []
        normalized_steps = []
        invalid_step_modules = []
        for index, step in enumerate(steps):
            item = dict(step) if isinstance(step, dict) else {}
            if str(item.get("source_type") or "standard") == "standard":
                module = str(item.get("module") or "").strip()
                resolved = fm_aliases.get(module.casefold()) if module else ""
                if not resolved:
                    invalid_step_modules.append(f"第{index + 1}步:{module or '未填写'}")
                else:
                    item["module"] = resolved
            normalized_steps.append(item)
        if invalid_step_modules:
            raise serializers.ValidationError({
                "steps": "标准日志步骤的目标模块必须属于当前子系统：" + "、".join(invalid_step_modules[:8])
            })
        if "steps" in attrs:
            attrs["steps"] = normalized_steps

        attrs["name"] = name
        return attrs


class LogFormatParserRuleSerializer(serializers.ModelSerializer):
    client_pattern = serializers.SerializerMethodField()
    group_names = serializers.SerializerMethodField()

    class Meta:
        model = LogFormatParserRule
        fields = [
            "id", "name", "category", "enabled", "priority", "file_pattern",
            "pattern", "client_pattern", "ignore_case", "field_map", "group_names",
            "timestamp_format", "built_in", "description", "created_at", "updated_at",
        ]
        read_only_fields = ["built_in", "client_pattern", "group_names", "created_at", "updated_at"]

    def validate(self, attrs):
        from apps.logsources.services.log_format_parser import LogFormatRuleError, validate_rule_config

        instance = self.instance
        config = {
            "pattern": attrs.get("pattern", getattr(instance, "pattern", "")),
            "ignore_case": attrs.get("ignore_case", getattr(instance, "ignore_case", False)),
            "field_map": attrs.get("field_map", getattr(instance, "field_map", {})),
            "timestamp_format": attrs.get("timestamp_format", getattr(instance, "timestamp_format", "auto")),
        }
        try:
            validated = validate_rule_config(config)
        except LogFormatRuleError as exc:
            raise serializers.ValidationError({"pattern": str(exc)}) from exc
        attrs["field_map"] = validated["field_map"]
        category = str(attrs.get("category", getattr(instance, "category", "debug"))).strip().lower()
        name = str(attrs.get("name", getattr(instance, "name", ""))).strip()
        if instance is not None and instance.built_in:
            if category != instance.category:
                raise serializers.ValidationError({"category": "系统内置规则不能修改日志类型；可复制为自定义规则后调整。"})
            if name != instance.name:
                raise serializers.ValidationError({"name": "系统内置规则不能修改规则名称；可复制为自定义规则后调整。"})
        if not category:
            raise serializers.ValidationError({"category": "日志类型不能为空。"})
        if not name:
            raise serializers.ValidationError({"name": "规则名称不能为空。"})
        queryset = LogFormatParserRule.objects.filter(category=category, name__iexact=name)
        if instance is not None:
            queryset = queryset.exclude(pk=instance.pk)
        if queryset.exists():
            raise serializers.ValidationError({"name": "该日志类型下已存在同名解析规则。"})
        attrs["category"] = category
        attrs["name"] = name
        return attrs

    def _validated(self, obj):
        from apps.logsources.services.log_format_parser import validate_rule_config
        return validate_rule_config({
            "pattern": obj.pattern,
            "ignore_case": obj.ignore_case,
            "field_map": obj.field_map,
            "timestamp_format": obj.timestamp_format,
        })

    def get_client_pattern(self, obj):
        try:
            return self._validated(obj)["client_pattern"]
        except Exception:
            return ""

    def get_group_names(self, obj):
        try:
            return self._validated(obj)["group_names"]
        except Exception:
            return []


class LogFormatRuleDraftSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=128, required=False, allow_blank=True, default="测试规则")
    category = serializers.CharField(max_length=32, required=False, default="debug")
    pattern = serializers.CharField(max_length=10000)
    ignore_case = serializers.BooleanField(required=False, default=False)
    field_map = serializers.DictField(child=serializers.CharField(allow_blank=True), required=False, default=dict)
    timestamp_format = serializers.CharField(max_length=64, required=False, default="auto")
    text = serializers.CharField(allow_blank=False, max_length=2_000_000)

    def validate(self, attrs):
        from apps.logsources.services.log_format_parser import LogFormatRuleError, validate_rule_config
        try:
            validated = validate_rule_config(attrs)
        except LogFormatRuleError as exc:
            raise serializers.ValidationError({"pattern": str(exc)}) from exc
        attrs["field_map"] = validated["field_map"]
        return attrs


class CaseInsensitiveUniqueNameMixin:
    model = None
    parent_field = None

    def validate_name(self, value: str) -> str:
        value = value.strip()
        if not value:
            raise serializers.ValidationError("名称不能为空。")
        queryset = self.model.objects.all()
        if self.parent_field:
            parent = self.initial_data.get(self.parent_field)
            if parent is None and self.instance is not None:
                parent = getattr(self.instance, f"{self.parent_field}_id", None)
            if parent is not None:
                queryset = queryset.filter(**{f"{self.parent_field}_id": parent})
        queryset = queryset.filter(name__iexact=value)
        if self.instance is not None:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise serializers.ValidationError("已存在同名配置。")
        return value


class LogFmDefinitionSerializer(serializers.ModelSerializer):
    effective_name = serializers.SerializerMethodField()
    target_module_ids = serializers.PrimaryKeyRelatedField(
        source="target_modules",
        queryset=LogFmDefinition.objects.all(),
        many=True,
        required=False,
    )
    target_module_names = serializers.SerializerMethodField()

    def validate(self, attrs):
        subsystem = attrs.get("subsystem") or getattr(self.instance, "subsystem", None)
        name = str(attrs.get("name", getattr(self.instance, "name", ""))).strip()
        kind = attrs.get("kind", getattr(self.instance, "kind", LogModuleKind.NORMAL))
        if not name:
            raise serializers.ValidationError({"name": "名称不能为空。"})
        queryset = LogFmDefinition.objects.filter(subsystem=subsystem, name__iexact=name, kind=kind)
        if self.instance is not None:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise serializers.ValidationError({"name": "该子系统下已存在同类型同名模块。"})
        attrs["name"] = name
        return attrs

    class Meta:
        model = LogFmDefinition
        fields = [
            "id", "subsystem", "name", "kind", "display_name", "effective_name",
            "enabled", "sort_order", "query_priority", "description",
            "target_module_ids", "target_module_names",
            "event_component", "event_config_files", "event_display_codes", "event_code_count",
            "last_event_discovered_at", "matched_count", "last_matched_at",
            "last_discovered_at", "created_at", "updated_at",
        ]
        read_only_fields = [
            "event_component", "event_config_files", "event_display_codes", "event_code_count",
            "last_event_discovered_at", "matched_count", "last_matched_at",
            "last_discovered_at", "created_at", "updated_at",
        ]

    def get_effective_name(self, obj):
        return obj.display_name or obj.name

    def get_target_module_names(self, obj):
        targets = list(obj.target_modules.all())
        targets.sort(key=lambda item: (int(item.query_priority or 100), item.subsystem.name.casefold(), item.name.casefold()))
        return [f"{item.subsystem.name}/{item.name}" for item in targets]


class LogSubsystemDefinitionSerializer(CaseInsensitiveUniqueNameMixin, serializers.ModelSerializer):
    model = LogSubsystemDefinition
    fms = LogFmDefinitionSerializer(many=True, read_only=True)
    fm_count = serializers.IntegerField(read_only=True)
    effective_name = serializers.SerializerMethodField()

    class Meta:
        model = LogSubsystemDefinition
        fields = [
            "id", "name", "display_name", "effective_name", "enabled",
            "sort_order", "description", "last_discovered_at", "fm_count",
            "fms", "created_at", "updated_at",
        ]
        read_only_fields = ["last_discovered_at", "created_at", "updated_at", "fm_count"]

    def get_effective_name(self, obj):
        return obj.display_name or obj.name


class LogFmTargetSerializer(serializers.Serializer):
    subsystem = serializers.CharField(max_length=128)
    fm = serializers.CharField(max_length=128)
    kind = serializers.ChoiceField(choices=LogModuleKind.choices, required=False, default=LogModuleKind.NORMAL)


class LogWindowRequestSerializer(serializers.Serializer):
    start_time = serializers.DateTimeField()
    end_time = serializers.DateTimeField()
    subsystems = serializers.ListField(child=serializers.CharField(max_length=128), required=False, default=list)
    fms = serializers.ListField(child=serializers.CharField(max_length=128), required=False, default=list)
    fm_targets = LogFmTargetSerializer(many=True, required=False, default=list)
    source_categories = serializers.ListField(child=serializers.CharField(max_length=32), required=False, default=list)
    keyword = serializers.CharField(required=False, allow_blank=True, max_length=2048, default="")
    raw_text = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs):
        if attrs["end_time"] < attrs["start_time"]:
            raise serializers.ValidationError({"end_time": "结束时间不能早于开始时间。"})
        if attrs["end_time"] - attrs["start_time"] > timedelta(days=3):
            raise serializers.ValidationError({"end_time": "单次日志检索时间范围最多支持 3 天，请缩小开始/结束时间。"})
        if not attrs.get("fm_targets"):
            categories = {str(item).strip() for item in attrs.get("source_categories", []) if str(item).strip()}
            targetless = bool(categories)
            if targetless:
                from apps.environments.models import ResourceSettings
                profiles = {
                    item.category: set(item.match_rules or [])
                    for item in ResourceSettings.get_solo().log_path_profiles.filter(category__in=categories, enabled=True)
                }
                targetless = len(profiles) == len(categories) and all("run_flat" in profiles.get(category, set()) for category in categories)
            if not targetless:
                raise serializers.ValidationError({"fm_targets": "当前日志类型请至少选择一个子系统/模块，禁止空选择触发全局扫描。"})
        return attrs

    @staticmethod
    def naive(value: datetime) -> datetime:
        return value.replace(tzinfo=None) if value.tzinfo else value


class LiveLogRequestSerializer(serializers.Serializer):
    """Realtime monitoring request.

    Live mode deliberately has no start/end time: it subscribes to the selected
    current log file(s) at EOF and only emits bytes appended afterwards.
    """

    subsystems = serializers.ListField(child=serializers.CharField(max_length=128), required=False, default=list)
    fms = serializers.ListField(child=serializers.CharField(max_length=128), required=False, default=list)
    fm_targets = LogFmTargetSerializer(many=True, required=False, default=list)
    source_categories = serializers.ListField(child=serializers.CharField(max_length=32), required=False, default=list)
    keyword = serializers.CharField(required=False, allow_blank=True, max_length=2048, default="")
    raw_text = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs):
        targets = list(attrs.get("fm_targets") or [])
        if len(targets) != 1:
            raise serializers.ValidationError({"fm_targets": "实时日志监听仅支持单模块，请只选择一个模块。"})
        return attrs


class SemanticSourceRequestSerializer(serializers.Serializer):
    source_file = serializers.CharField(max_length=512)
    source_line = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    function_name = serializers.CharField(required=False, allow_blank=True, max_length=256)
    fm_targets = serializers.ListField(child=serializers.DictField(), allow_empty=False)

    def validate_source_file(self, value: str) -> str:
        value = value.strip()
        if not value.lower().endswith('.py'):
            raise serializers.ValidationError('自动识别语义仅支持 .py 源码。')
        return value

    def validate_fm_targets(self, value):
        normalized = []
        for item in value:
            subsystem = str(item.get('subsystem') or '').strip()
            module = str(item.get('module') or item.get('fm') or '').strip()
            if not subsystem or not module:
                raise serializers.ValidationError('fm_targets 中 subsystem/module 不能为空。')
            normalized.append({'subsystem': subsystem, 'module': module})
        return normalized


class SemanticSourceBatchItemSerializer(serializers.Serializer):
    key = serializers.CharField(max_length=768)
    source_file = serializers.CharField(max_length=512)
    source_line = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    function_name = serializers.CharField(required=False, allow_blank=True, max_length=256)

    def validate_source_file(self, value: str) -> str:
        value = value.strip()
        if not value.lower().endswith('.py'):
            raise serializers.ValidationError('批量语义识别仅支持 .py 源码。')
        return value


class SemanticSourceBatchRequestSerializer(serializers.Serializer):
    items = SemanticSourceBatchItemSerializer(many=True, allow_empty=False, max_length=500)
    fm_targets = serializers.ListField(child=serializers.DictField(), allow_empty=False)

    def validate_fm_targets(self, value):
        normalized = []
        for item in value:
            subsystem = str(item.get('subsystem') or '').strip()
            module = str(item.get('module') or item.get('fm') or '').strip()
            if not subsystem or not module:
                raise serializers.ValidationError('fm_targets 中 subsystem/module 不能为空。')
            normalized.append({'subsystem': subsystem, 'module': module})
        return normalized


class LogWatchSerializer(serializers.ModelSerializer):
    environment_name = serializers.CharField(source="environment.name", read_only=True)

    class Meta:
        model = LogWatch
        fields = [
            "id", "environment", "environment_name", "name", "source_rule_id",
            "extraction_rule_id", "level",
            "trigger_kind", "trigger_config", "targets", "source_categories",
            "capture_config", "enabled", "created_by",
            "last_hit_at", "hit_count", "dropped_count", "last_error",
            "last_worker_id", "last_heartbeat_at", "created_at", "updated_at",
        ]
        read_only_fields = [
            "last_hit_at", "hit_count", "dropped_count", "last_error",
            "last_worker_id", "last_heartbeat_at", "created_at", "updated_at",
        ]


class LogWatchWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = LogWatch
        fields = [
            "id", "environment", "name", "source_rule_id", "extraction_rule_id", "level",
            "trigger_kind",
            "trigger_config", "targets", "source_categories", "capture_config", "enabled", "created_by",
        ]

    def validate_targets(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("targets 必须是数组。")
        cleaned = []
        for item in value:
            if not isinstance(item, dict):
                raise serializers.ValidationError("targets 每项必须是对象。")
            subsystem = str(item.get("subsystem") or "").strip()
            fm = str(item.get("fm") or "").strip()
            if not subsystem or not fm:
                raise serializers.ValidationError("targets 每项必须包含 subsystem 和 fm。")
            cleaned.append({"subsystem": subsystem, "fm": fm, "kind": str(item.get("kind") or "normal")})
        if not cleaned:
            raise serializers.ValidationError("至少要有一个监听目标。")
        return cleaned
