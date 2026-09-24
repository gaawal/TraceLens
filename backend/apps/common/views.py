from django.db import connection
from django.urls import reverse
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.common.services.ssh import SSH_SESSION_POOL
from apps.logsources.services.redis_store import RedisLogStore


@api_view(["GET"])
@permission_classes([AllowAny])
def api_root(request):
    return Response(
        {
            "name": "TraceLens Backend API",
            "version": "0.11.0",
            "machines": request.build_absolute_uri(reverse("machine-list")),
            "environments": request.build_absolute_uri(reverse("environment-list")),
            "environment_folders": request.build_absolute_uri(reverse("environment-folder-list")),
            "resource_settings": request.build_absolute_uri("/api/resource-settings/current/"),
            "remote_logs": request.build_absolute_uri("/api/environment-logs/{environment_id}/subsystems/"),
            "cpd_reports": request.build_absolute_uri("/api/cpd-reports/{environment_id}/tree/"),
            "log_audits": request.build_absolute_uri(reverse("log-audit-list")),
            "log_source_rules": request.build_absolute_uri(reverse("log-source-rule-list")),
            "tools": request.build_absolute_uri(reverse("tool-list")),
            "atlog_analysis": request.build_absolute_uri(reverse("atlog-analysis-list")),
            "swagger": request.build_absolute_uri(reverse("swagger-ui")),
        }
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def health_check(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        database_ok = cursor.fetchone()[0] == 1
    return Response(
        {
            "status": "ok" if database_ok else "degraded",
            "database": "ok" if database_ok else "error",
            "time": timezone.now(),
            "ssh_sessions": len(SSH_SESSION_POOL.stats()),
            "redis": RedisLogStore.status(),
        }
    )
