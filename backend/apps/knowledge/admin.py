from django.contrib import admin

from apps.knowledge.models import AbnormalCase


@admin.register(AbnormalCase)
class AbnormalCaseAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "enabled", "evidence_count", "matched_count", "environment_name", "updated_at")
    list_filter = ("enabled", "category")
    search_fields = ("name", "symptom", "root_cause", "solution", "environment_name")
