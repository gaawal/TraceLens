from django.contrib import admin

from apps.logsources.models import (
    EventCodeDefinition,
    EventConfigSource,
    LogFmDefinition,
    LogResourceCatalog,
    LogResourceCatalogItem,
    LogSourceRule,
    LogSubsystemDefinition,
)


@admin.register(LogSourceRule)
class LogSourceRuleAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "machine",
        "root_path_template",
        "subsystem",
        "component",
        "module",
        "auto_discovery",
        "enabled",
    )
    list_filter = ("auto_discovery", "enabled", "machine__role")
    search_fields = ("name", "machine__name", "machine__host", "component", "module")


class LogFmDefinitionInline(admin.TabularInline):
    model = LogFmDefinition
    extra = 0
    fields = ("name", "kind", "display_name", "enabled", "sort_order", "query_priority", "event_component", "event_code_count", "description", "last_discovered_at")
    readonly_fields = ("event_component", "event_code_count", "last_discovered_at",)


@admin.register(LogSubsystemDefinition)
class LogSubsystemDefinitionAdmin(admin.ModelAdmin):
    list_display = ("name", "display_name", "enabled", "sort_order", "last_discovered_at", "updated_at")
    list_filter = ("enabled",)
    search_fields = ("name", "display_name", "description", "fms__name", "fms__display_name")
    ordering = ("sort_order", "name")
    inlines = [LogFmDefinitionInline]


@admin.register(LogFmDefinition)
class LogFmDefinitionAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "display_name", "subsystem", "event_component", "event_code_count", "query_priority", "enabled", "sort_order", "last_discovered_at")
    list_filter = ("kind", "event_component", "enabled", "subsystem")
    search_fields = ("name", "display_name", "description", "subsystem__name")
    ordering = ("subsystem__sort_order", "subsystem__name", "query_priority", "sort_order", "name")
    filter_horizontal = ("target_modules",)


class LogResourceCatalogItemInline(admin.TabularInline):
    model = LogResourceCatalogItem
    extra = 0
    readonly_fields = ("subsystem", "fm", "kind", "created_at", "updated_at")
    can_delete = False


@admin.register(LogResourceCatalog)
class LogResourceCatalogAdmin(admin.ModelAdmin):
    list_display = (
        "environment",
        "machine",
        "source_name",
        "root",
        "status",
        "scanned_at",
        "updated_at",
    )
    list_filter = ("status", "source_category", "machine__role")
    search_fields = ("environment__name", "machine__host", "source_name", "root")
    readonly_fields = ("created_at", "updated_at")
    inlines = [LogResourceCatalogItemInline]


@admin.register(EventConfigSource)
class EventConfigSourceAdmin(admin.ModelAdmin):
    list_display = ("environment", "subsystem", "component_code", "file_name", "source", "active", "last_seen_at")
    list_filter = ("active", "subsystem")
    search_fields = ("environment__name", "environment__upper_machine__host", "subsystem__name", "component_code", "file_name", "source")
    readonly_fields = ("file_hash", "remote_mtime", "remote_size", "last_seen_at", "created_at", "updated_at")


@admin.register(EventCodeDefinition)
class EventCodeDefinitionAdmin(admin.ModelAdmin):
    list_display = ("display_code", "environment", "subsystem", "component_code", "severity", "code", "active", "last_seen_at")
    list_filter = ("active", "severity", "category", "subsystem")
    search_fields = ("display_code", "code", "code_string", "component_code", "source", "description", "environment__name")
    readonly_fields = ("raw_config", "last_seen_at", "created_at", "updated_at")
