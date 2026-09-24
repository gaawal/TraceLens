from django.contrib import admin

from apps.machines.models import Machine


@admin.register(Machine)
class MachineAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "host",
        "ssh_port",
        "username",
        "role",
        "origin",
        "auth_type",
        "connection_status",
        "is_active",
    )
    list_filter = ("role", "origin", "auth_type", "connection_status", "is_active")
    search_fields = ("name", "host", "username")
    readonly_fields = (
        "encrypted_password",
        "encrypted_private_key",
        "encrypted_private_key_passphrase",
        "created_at",
        "updated_at",
    )
