from __future__ import annotations

from django.db.models import Count, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.audits.models import AuditResult, DataExtractionRecord, LogSearchAudit
from apps.audits.serializers import DataExtractionRecordSerializer, LogSearchAuditSerializer
from apps.audits.services import record_log_search_client_result
from apps.common.pagination import StandardResultsSetPagination


def _parse_filter_datetime(value: str | None):
    text = str(value or "").strip().replace(" ", "T")
    if not text:
        return None
    parsed = parse_datetime(text)
    if parsed is not None and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


class LogSearchAuditViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = LogSearchAuditSerializer
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        qs = LogSearchAudit.objects.select_related("environment").prefetch_related("targets").annotate(data_extraction_count=Count("data_extractions", distinct=True)).all()
        params = self.request.query_params
        start = _parse_filter_datetime(params.get("start_time"))
        end = _parse_filter_datetime(params.get("end_time"))
        if start:
            qs = qs.filter(created_at__gte=start)
        if end:
            qs = qs.filter(created_at__lte=end)
        result = str(params.get("result") or "").strip()
        if result:
            if result == "failed":
                qs = qs.filter(result__in=[AuditResult.FAILED, AuditResult.CANCELLED])
            else:
                qs = qs.filter(result=result)
        environment_id = str(params.get("environment_id") or "").strip()
        if environment_id.isdigit():
            qs = qs.filter(environment_id=int(environment_id))
        subsystem = str(params.get("subsystem") or "").strip()
        module = str(params.get("module") or "").strip()
        if subsystem:
            qs = qs.filter(targets__subsystem=subsystem)
        if module:
            qs = qs.filter(targets__module=module)
        source_category = str(params.get("source_category") or "").strip()
        if source_category:
            qs = qs.filter(source_categories_text__contains=f"|{source_category}|")
        query = str(params.get("query") or "").strip()
        if query:
            qs = qs.filter(
                Q(operation_id__icontains=query)
                | Q(operator_username__icontains=query)
                | Q(client_ip__icontains=query)
                | Q(environment_name__icontains=query)
                | Q(target_host__icontains=query)
                | Q(target_username__icontains=query)
                | Q(keyword__icontains=query)
                | Q(error_message__icontains=query)
                | Q(targets__subsystem__icontains=query)
                | Q(targets__module__icontains=query)
            )
        order = str(params.get("order") or "desc").strip().lower()
        if order == "asc":
            return qs.distinct().order_by("created_at", "id")
        return qs.distinct().order_by("-created_at", "-id")

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page if page is not None else queryset, many=True)
        if page is not None:
            response = self.get_paginated_response(serializer.data)
            # Return lightweight facets from the current time/result/environment scope for dropdowns.
            facet_qs = LogSearchAudit.objects.prefetch_related("targets").all()
            start = _parse_filter_datetime(request.query_params.get("start_time"))
            end = _parse_filter_datetime(request.query_params.get("end_time"))
            if start: facet_qs = facet_qs.filter(created_at__gte=start)
            if end: facet_qs = facet_qs.filter(created_at__lte=end)
            result = str(request.query_params.get("result") or "").strip()
            if result:
                facet_qs = facet_qs.filter(result__in=[AuditResult.FAILED, AuditResult.CANCELLED] if result == "failed" else [result])
            env_id = str(request.query_params.get("environment_id") or "").strip()
            if env_id.isdigit(): facet_qs = facet_qs.filter(environment_id=int(env_id))
            environments = list(facet_qs.values("environment_id", "environment_name", "target_host", "target_username").distinct().order_by("target_host", "target_username"))
            targets = list(facet_qs.values("targets__subsystem", "targets__module", "targets__kind").exclude(targets__subsystem__isnull=True).distinct().order_by("targets__subsystem", "targets__module"))
            response.data["facets"] = {"environments": environments, "targets": targets}
            return response
        return Response({"count": len(serializer.data), "results": serializer.data, "facets": {"environments": [], "targets": []}}, status=status.HTTP_200_OK)
    @action(detail=False, methods=["post"], url_path="client-result")
    def client_result(self, request):
        operation_id = str(request.data.get("operation_id") or "").strip()
        if not operation_id:
            return Response({"message": "缺少 operation_id。"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            result_count = max(0, int(request.data.get("result_count") or 0))
        except (TypeError, ValueError):
            return Response({"message": "result_count 必须是非负整数。"}, status=status.HTTP_400_BAD_REQUEST)
        audit = LogSearchAudit.objects.filter(operation_id=operation_id).order_by("-id").first()
        if audit is None:
            return Response({"message": "未找到对应的日志审计记录。"}, status=status.HTTP_404_NOT_FOUND)
        # 客户端最终解析/关键字过滤结果只校正正常完成的检索，失败/停止不能被覆盖。
        if audit.result not in [AuditResult.FAILED, AuditResult.CANCELLED]:
            audit.result_count = result_count
            audit.result = AuditResult.SUCCESS if result_count > 0 else AuditResult.NO_RESULT
            record_log_search_client_result(audit, result_count)
            audit.save(update_fields=["result_count", "result", "diagnostics", "updated_at"])
        return Response(LogSearchAuditSerializer(audit).data, status=status.HTTP_200_OK)



class DataExtractionRecordViewSet(viewsets.ModelViewSet):
    serializer_class = DataExtractionRecordSerializer
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        qs = DataExtractionRecord.objects.select_related("environment", "source_audit").all()
        params = self.request.query_params
        environment_id = str(params.get("environment_id") or "").strip()
        if environment_id.isdigit():
            qs = qs.filter(environment_id=int(environment_id))
        operation_id = str(params.get("source_operation_id") or "").strip()
        if operation_id:
            qs = qs.filter(source_operation_id=operation_id)
        status_value = str(params.get("status") or "").strip()
        if status_value:
            qs = qs.filter(status=status_value)
        query = str(params.get("query") or "").strip()
        if query:
            qs = qs.filter(
                Q(name__icontains=query)
                | Q(task_name__icontains=query)
                | Q(environment_name__icontains=query)
                | Q(source_operation_id__icontains=query)
            )
        return qs
