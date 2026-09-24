from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from rest_framework import serializers

from apps.knowledge.models import AbnormalCase


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_evidences(value, *, allow_empty: bool = False):
    if not isinstance(value, list) or (not value and not allow_empty):
        raise serializers.ValidationError("至少需要一条异常日志作为举证。")
    if len(value) > 100:
        raise serializers.ValidationError("单个现场特征组最多保存 100 条异常举证。")
    normalized = []
    has_runtime_rule_evidence = False
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise serializers.ValidationError(f"第 {index + 1} 条举证格式无效。")
        raw = str(item.get("raw") or "").strip()
        template = str(item.get("template") or "").strip()
        anomaly_rules = item.get("anomaly_rules") or []
        tokens = item.get("tokens") or []
        source_category = str(item.get("source_category") or "").strip().lower()
        report_evidence = source_category in {"case_report", "pytest", "xytest", "case-metadata"}
        if not raw or not template:
            raise serializers.ValidationError(f"第 {index + 1} 条举证缺少原始日志或指纹模板。")
        if not isinstance(anomaly_rules, list):
            raise serializers.ValidationError(f"第 {index + 1} 条举证异常规则格式无效。")
        if not anomaly_rules and not report_evidence:
            raise serializers.ValidationError(f"第 {index + 1} 条运行日志举证没有命中异常规则，不能录入异常知识库。")
        if anomaly_rules and not report_evidence:
            has_runtime_rule_evidence = True
        if not isinstance(tokens, list):
            raise serializers.ValidationError(f"第 {index + 1} 条举证 Token 指纹格式无效。")
        normalized.append(item)
    if normalized and not has_runtime_rule_evidence:
        raise serializers.ValidationError("案例至少需要一条命中异常规则的运行日志证据；pytest/xytest/用例报告证据只能作为补充证据。")
    return normalized


def _make_default_group(evidences, *, source_operation_id: str = "", source_task_name: str = "", environment_name: str = ""):
    return {
        "id": uuid4().hex,
        "title": "首次现场",
        "enabled": True,
        "created_at": _now_iso(),
        "source_operation_id": source_operation_id,
        "source_task_name": source_task_name,
        "environment_name": environment_name,
        "note": "",
        "evidences": evidences,
    }


def _flatten_groups(groups):
    result = []
    for group in groups or []:
        if isinstance(group, dict):
            result.extend(group.get("evidences") or [])
    return result


class AbnormalCaseSerializer(serializers.ModelSerializer):
    class Meta:
        model = AbnormalCase
        fields = [
            "id", "name", "category", "symptom", "root_cause", "solution", "description",
            "tags", "enabled", "source_operation_id", "source_task_name", "environment",
            "environment_name", "query_snapshot", "evidences", "feature_groups", "governance_history",
            "fingerprint_version", "evidence_count", "matched_count", "last_matched_at", "created_at", "updated_at",
        ]
        read_only_fields = ["evidence_count", "matched_count", "last_matched_at", "governance_history", "created_at", "updated_at"]

    def validate_name(self, value: str) -> str:
        value = value.strip()
        if not value:
            raise serializers.ValidationError("案例名称不能为空。")
        return value

    def validate_tags(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("标签必须是数组。")
        result: list[str] = []
        seen: set[str] = set()
        for raw in value:
            text = str(raw or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text[:64])
        return result[:30]

    def validate_evidences(self, value):
        return _normalize_evidences(value)

    def validate_feature_groups(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("现场特征组必须是数组。")
        if len(value) > 30:
            raise serializers.ValidationError("单个案例最多保存 30 组现场特征。")
        normalized = []
        seen_ids: set[str] = set()
        for index, raw in enumerate(value):
            if not isinstance(raw, dict):
                raise serializers.ValidationError(f"第 {index + 1} 组现场特征格式无效。")
            evidences = _normalize_evidences(raw.get("evidences") or [])
            group_id = str(raw.get("id") or uuid4().hex).strip()[:80] or uuid4().hex
            if group_id in seen_ids:
                group_id = uuid4().hex
            seen_ids.add(group_id)
            normalized.append({
                "id": group_id,
                "title": str(raw.get("title") or f"现场特征 {index + 1}").strip()[:128] or f"现场特征 {index + 1}",
                "enabled": bool(raw.get("enabled", True)),
                "created_at": str(raw.get("created_at") or _now_iso())[:64],
                "source_operation_id": str(raw.get("source_operation_id") or "")[:128],
                "source_task_name": str(raw.get("source_task_name") or "")[:255],
                "environment_name": str(raw.get("environment_name") or "")[:128],
                "note": str(raw.get("note") or "")[:1000],
                "evidences": evidences,
            })
        if value and not normalized:
            raise serializers.ValidationError("至少需要一组有效现场特征。")
        if normalized and not any(item.get("enabled", True) for item in normalized):
            raise serializers.ValidationError("至少需要保留一组启用的现场特征。")
        if sum(len(item.get("evidences") or []) for item in normalized) > 500:
            raise serializers.ValidationError("单个案例最多保存 500 条现场举证。")
        return normalized

    def create(self, validated_data):
        evidences = validated_data.get("evidences") or []
        groups = validated_data.get("feature_groups") or []
        if not groups:
            groups = [_make_default_group(
                evidences,
                source_operation_id=validated_data.get("source_operation_id") or "",
                source_task_name=validated_data.get("source_task_name") or "",
                environment_name=validated_data.get("environment_name") or "",
            )]
            validated_data["feature_groups"] = groups
        validated_data["evidences"] = _flatten_groups(groups)
        validated_data["evidence_count"] = len(validated_data["evidences"])
        validated_data["fingerprint_version"] = max(4, int(validated_data.get("fingerprint_version") or 4))
        instance = super().create(validated_data)
        instance.governance_history = [{
            "action": "case_created",
            "created_at": _now_iso(),
            "feature_group_id": groups[0].get("id") if groups else "",
            "feature_group_title": groups[0].get("title") if groups else "",
            "evidence_count": len(validated_data["evidences"]),
        }]
        instance.save(update_fields=["governance_history", "updated_at"])
        return instance

    def update(self, instance, validated_data):
        previous_groups = instance.feature_groups or []
        if "feature_groups" in validated_data:
            groups = validated_data.get("feature_groups") or []
            if not groups:
                raise serializers.ValidationError({"feature_groups": "案例至少需要保留一组现场特征。"})
            flattened = _flatten_groups(groups)
            validated_data["evidences"] = flattened
            validated_data["evidence_count"] = len(flattened)
            validated_data["fingerprint_version"] = max(4, int(validated_data.get("fingerprint_version") or instance.fingerprint_version or 4))
        elif "evidences" in validated_data:
            # 兼容旧客户端：直接修改 evidences 时，把结果同步为单个“历史现场”特征组。
            flattened = validated_data.get("evidences") or []
            validated_data["evidence_count"] = len(flattened)
            validated_data["feature_groups"] = [_make_default_group(
                flattened,
                source_operation_id=validated_data.get("source_operation_id", instance.source_operation_id) or "",
                source_task_name=validated_data.get("source_task_name", instance.source_task_name) or "",
                environment_name=validated_data.get("environment_name", instance.environment_name) or "",
            )]

        next_groups = validated_data.get("feature_groups")
        history = list(instance.governance_history or [])[-199:]
        if next_groups is not None:
            old = {str(item.get("id")): item for item in previous_groups if isinstance(item, dict)}
            new = {str(item.get("id")): item for item in next_groups if isinstance(item, dict)}
            for group_id, group in new.items():
                if group_id not in old:
                    history.append({
                        "action": "feature_added", "created_at": _now_iso(), "feature_group_id": group_id,
                        "feature_group_title": group.get("title") or "", "evidence_count": len(group.get("evidences") or []),
                    })
                elif bool(old[group_id].get("enabled", True)) != bool(group.get("enabled", True)):
                    history.append({
                        "action": "feature_enabled" if group.get("enabled", True) else "feature_disabled",
                        "created_at": _now_iso(), "feature_group_id": group_id, "feature_group_title": group.get("title") or "",
                    })
            for group_id, group in old.items():
                if group_id not in new:
                    history.append({
                        "action": "feature_deleted", "created_at": _now_iso(), "feature_group_id": group_id,
                        "feature_group_title": group.get("title") or "", "evidence_count": len(group.get("evidences") or []),
                    })
            validated_data["governance_history"] = history[-200:]

        return super().update(instance, validated_data)
