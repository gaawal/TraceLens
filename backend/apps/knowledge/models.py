from __future__ import annotations

from django.db import models
from django.utils import timezone

from apps.common.models import TimeStampedModel


class AbnormalCase(TimeStampedModel):
    """人工确认后的异常日志场景。

    evidences 中同时保留原始日志快照与前端生成的确定性指纹。知识库只保存案例，
    不保存后续每次分析的整批日志结果。
    """

    name = models.CharField("案例名称", max_length=255)
    category = models.CharField("故障分类", max_length=128, blank=True)
    symptom = models.TextField("故障现象", blank=True)
    root_cause = models.TextField("根因", blank=True)
    solution = models.TextField("处理建议", blank=True)
    description = models.TextField("补充说明", blank=True)
    tags = models.JSONField("标签", default=list, blank=True)
    enabled = models.BooleanField("启用", default=True, db_index=True)

    source_operation_id = models.CharField("来源日志操作ID", max_length=128, blank=True, db_index=True)
    source_task_name = models.CharField("来源任务名称", max_length=255, blank=True)
    environment = models.ForeignKey(
        "environments.Environment",
        verbose_name="来源环境",
        related_name="abnormal_cases",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    environment_name = models.CharField("环境名称快照", max_length=128, blank=True)
    query_snapshot = models.JSONField("日志查询快照", default=dict, blank=True)

    # 每一项包含 raw/message/module/subsystem/function/error_codes/template/tokens 等。
    # evidences 继续保留为扁平兼容视图；新版本以 feature_groups 作为“现场特征组”来源。
    evidences = models.JSONField("异常举证", default=list)
    feature_groups = models.JSONField("现场特征组", default=list, blank=True)
    governance_history = models.JSONField("案例治理历史", default=list, blank=True)
    fingerprint_version = models.PositiveIntegerField("指纹版本", default=4)
    evidence_count = models.PositiveIntegerField("举证条数", default=0)
    matched_count = models.PositiveBigIntegerField("历史高相似命中次数", default=0)
    last_matched_at = models.DateTimeField("最近命中时间", null=True, blank=True)

    class Meta:
        verbose_name = "异常案例"
        verbose_name_plural = "异常案例"
        ordering = ["-updated_at", "-id"]
        indexes = [
            models.Index(fields=["enabled", "-updated_at"], name="abcase_enabled_time_idx"),
            models.Index(fields=["environment", "-updated_at"], name="abcase_env_time_idx"),
            models.Index(fields=["category", "-updated_at"], name="abcase_cat_time_idx"),
        ]

    def __str__(self) -> str:
        return self.name

    def mark_matched(self) -> None:
        self.matched_count = models.F("matched_count") + 1
        self.last_matched_at = timezone.now()
        self.save(update_fields=["matched_count", "last_matched_at", "updated_at"])
        self.refresh_from_db(fields=["matched_count", "last_matched_at"])
