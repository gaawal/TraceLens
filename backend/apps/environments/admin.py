from django.contrib import admin

from apps.environments.models import (
    Environment,
    EnvironmentDiscovery,
    MachineRelation,
    ResourceSettings,
    LogPathProfile,
    EnvironmentDeployment,
    DeploymentStep,
)


@admin.register(Environment)
class EnvironmentAdmin(admin.ModelAdmin):
    list_display = ("name", "upper_machine", "status", "last_discovered_at", "updated_at")
    list_filter = ("status",)
    search_fields = ("name", "upper_machine__name", "upper_machine__host")


@admin.register(MachineRelation)
class MachineRelationAdmin(admin.ModelAdmin):
    list_display = ("environment", "source_machine", "target_machine", "source", "is_active", "discovered_at")
    list_filter = ("source", "is_active")
    search_fields = ("environment__name", "source_machine__host", "target_machine__host")


@admin.register(EnvironmentDiscovery)
class EnvironmentDiscoveryAdmin(admin.ModelAdmin):
    list_display = ("environment", "status", "found_count", "created_count", "updated_count", "created_at")
    list_filter = ("status",)
    search_fields = ("environment__name", "message")
    readonly_fields = ("created_at", "updated_at")

admin.site.register(ResourceSettings)
admin.site.register(LogPathProfile)

admin.site.register(EnvironmentDeployment)
admin.site.register(DeploymentStep)
