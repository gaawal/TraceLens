from __future__ import annotations

from django.db import models

from apps.common.models import TimeStampedModel


class AtLogCaseAnalysisSnapshot(TimeStampedModel):
    """用例 URL 级最新分析快照。

    与案例库不同：这里只保存某一个 ATLog 用例最近一次定位现场/查询缓存/AI 诊断结果，
    供任意用户再次打开同一 URL 时直接恢复，不参与跨用例知识匹配。
    """

    case_url = models.URLField("用例链接", max_length=2048, unique=True)
    case_id = models.CharField("用例编号", max_length=255, blank=True, db_index=True)
    case_name = models.CharField("用例名称", max_length=255, blank=True)
    case_status = models.CharField("用例状态", max_length=32, blank=True, db_index=True)

    analysis_snapshot = models.JSONField("用例解析快照", default=dict, blank=True)
    workspace_state = models.JSONField("定位工作区状态", default=dict, blank=True)
    query_result_snapshot = models.JSONField("最近查询结果缓存", default=dict, blank=True)

    ai_result_snapshot = models.JSONField("最近 AI 诊断结果", default=dict, blank=True)
    ai_thinking_text = models.TextField("最近 AI 分析过程", blank=True)
    ai_token_usage = models.JSONField("最近 AI Token 统计", default=dict, blank=True)
    ai_job_id = models.CharField("最近 AI 任务 ID", max_length=64, blank=True)
    ai_completed_at = models.DateTimeField("最近 AI 完成时间", null=True, blank=True)
    ai_revision = models.PositiveIntegerField("AI 诊断版本", default=0)

    class Meta:
        verbose_name = "ATLog 用例分析快照"
        verbose_name_plural = "ATLog 用例分析快照"
        ordering = ["-updated_at", "-id"]
        indexes = [
            models.Index(fields=["case_status", "-updated_at"], name="atlog_snap_status_idx"),
            models.Index(fields=["case_id", "-updated_at"], name="atlog_snap_caseid_idx"),
        ]

    def __str__(self) -> str:
        return self.case_id or self.case_name or self.case_url
