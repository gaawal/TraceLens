from rest_framework import serializers

from apps.audits.models import DataExtractionRecord, LogSearchAudit, LogSearchAuditTarget, OperationAudit


class LogSearchAuditTargetSerializer(serializers.ModelSerializer):
    class Meta:
        model = LogSearchAuditTarget
        fields = ["subsystem", "module", "kind"]


class LogSearchAuditSerializer(serializers.ModelSerializer):
    targets = LogSearchAuditTargetSerializer(many=True, read_only=True)
    result_display = serializers.CharField(source="get_result_display", read_only=True)
    data_extraction_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = LogSearchAudit
        fields = [
            "id", "operation_id", "action", "operator_username", "client_ip",
            "environment", "environment_name", "target_host", "target_username",
            "start_time", "end_time", "source_categories", "keyword", "request_payload",
            "targets", "result", "result_display", "error_message", "artifact_count", "matched_files", "diagnostics",
            "result_count", "output_bytes", "data_extraction_count", "created_at", "finished_at", "duration_ms",
        ]


class DataExtractionRecordSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = DataExtractionRecord
        fields = [
            "id", "source_audit", "source_operation_id", "environment", "environment_name",
            "task_name", "name", "query_snapshot", "rule_snapshots", "status", "status_display",
            "matched_rule_count", "row_count", "result_summary", "hour_summary", "error_message",
            "created_at", "updated_at", "finished_at",
        ]
        read_only_fields = ["source_audit", "created_at", "updated_at"]

    def create(self, validated_data):
        operation_id = str(validated_data.get("source_operation_id") or "").strip()
        if operation_id:
            validated_data["source_audit"] = LogSearchAudit.objects.filter(operation_id=operation_id).order_by("-id").first()
        return super().create(validated_data)


class OperationAuditSerializer(serializers.ModelSerializer):
    """**注意：不输出 URL。** 界面只展示功能名，接口路径属于后端留档。"""

    outcome_display = serializers.CharField(source="get_outcome_display", read_only=True)
    trigger_display = serializers.CharField(source="get_trigger_display", read_only=True)

    class Meta:
        model = OperationAudit
        fields = [
            "id", "created_at", "operation_id", "operator_username", "client_ip", "session_id",
            "trigger", "trigger_display",
            "feature_group", "feature_name", "summary",
            "environment", "environment_name", "target_kind", "target_name",
            "log_search_audit", "http_method", "status_code",
            "outcome", "outcome_display", "error_message", "duration_ms", "request_payload",
        ]
