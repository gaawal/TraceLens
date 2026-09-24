from django.contrib import admin

from apps.audits.models import LogSearchAudit, LogSearchAuditTarget


class LogSearchAuditTargetInline(admin.TabularInline):
    model = LogSearchAuditTarget
    extra = 0
    can_delete = False
    readonly_fields = ("subsystem", "module", "kind")


@admin.register(LogSearchAudit)
class LogSearchAuditAdmin(admin.ModelAdmin):
    list_display = ("created_at", "client_ip", "operator_username", "target_host", "target_username", "result", "keyword")
    list_filter = ("result", "action", "created_at")
    search_fields = ("operation_id", "client_ip", "operator_username", "target_host", "target_username", "keyword", "error_message")
    readonly_fields = tuple(field.name for field in LogSearchAudit._meta.fields)
    inlines = [LogSearchAuditTargetInline]
