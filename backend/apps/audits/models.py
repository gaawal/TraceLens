from __future__ import annotations

from django.db import models

from apps.common.models import TimeStampedModel


class AuditAction(models.TextChoices):
    LOG_SEARCH = "log_search", "日志检索"


class AuditResult(models.TextChoices):
    RUNNING = "running", "执行中"
    SUCCESS = "success", "成功"
    NO_RESULT = "no_result", "无结果"
    FAILED = "failed", "失败"
    CANCELLED = "cancelled", "已停止"


class LogSearchAudit(TimeStampedModel):
    operation_id = models.CharField("操作ID", max_length=128, db_index=True)
    action = models.CharField("操作类型", max_length=32, choices=AuditAction.choices, default=AuditAction.LOG_SEARCH, db_index=True)
    operator_username = models.CharField("操作用户", max_length=150, blank=True)
    client_ip = models.CharField("操作IP", max_length=64, blank=True, db_index=True)

    environment = models.ForeignKey(
        "environments.Environment",
        verbose_name="目标环境",
        related_name="log_search_audits",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    environment_name = models.CharField("环境名称快照", max_length=128, blank=True)
    target_host = models.CharField("环境IP快照", max_length=255, blank=True, db_index=True)
    target_username = models.CharField("环境用户快照", max_length=128, blank=True)

    start_time = models.DateTimeField("检索开始时间")
    end_time = models.DateTimeField("检索结束时间")
    source_categories = models.JSONField("日志类型", default=list, blank=True)
    source_categories_text = models.TextField("日志类型检索索引", blank=True)
    keyword = models.TextField("关键字", blank=True)
    request_payload = models.JSONField("检索参数", default=dict, blank=True)

    result = models.CharField("执行结果", max_length=16, choices=AuditResult.choices, default=AuditResult.RUNNING, db_index=True)
    error_message = models.TextField("错误信息", blank=True)
    artifact_count = models.PositiveIntegerField("候选日志文件数", default=0)
    matched_files = models.JSONField("命中文件", default=list, blank=True)
    diagnostics = models.JSONField("日志定位诊断", default=dict, blank=True)
    result_count = models.PositiveIntegerField("检索结果条数", default=0)
    output_bytes = models.BigIntegerField("输出字节数", default=0)
    finished_at = models.DateTimeField("完成时间", null=True, blank=True)
    duration_ms = models.PositiveBigIntegerField("耗时毫秒", null=True, blank=True)

    class Meta:
        verbose_name = "日志检索审计"
        verbose_name_plural = "日志检索审计"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["-created_at", "result"], name="audit_time_result_idx"),
            models.Index(fields=["environment", "-created_at"], name="audit_env_time_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.operation_id} {self.target_host} {self.result}"


class LogSearchAuditTarget(models.Model):
    audit = models.ForeignKey(LogSearchAudit, verbose_name="审计记录", related_name="targets", on_delete=models.CASCADE)
    subsystem = models.CharField("子系统", max_length=128)
    module = models.CharField("模块", max_length=128)
    kind = models.CharField("模块类型", max_length=32, default="normal")

    class Meta:
        verbose_name = "日志检索审计目标"
        verbose_name_plural = "日志检索审计目标"
        ordering = ["subsystem", "module", "kind", "id"]
        constraints = [
            models.UniqueConstraint(fields=["audit", "subsystem", "module", "kind"], name="uniq_audit_target")
        ]
        indexes = [
            models.Index(fields=["subsystem", "module"], name="audit_target_sm_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.subsystem}/{self.module}"


class DataExtractionStatus(models.TextChoices):
    RUNNING = "running", "提取中"
    SUCCESS = "success", "成功"
    NO_RESULT = "no_result", "未命中"
    FAILED = "failed", "失败"
    CANCELLED = "cancelled", "已停止"


class DataExtractionRecord(TimeStampedModel):
    """
    仅记录一次“数据提取过程”的可重放元数据。

    注意：这里绝不保存真实提取数据。真正的 DataPoint 只在浏览器内存中生成，
    用户需要时重新按 query_snapshot 读取日志并使用 rule_snapshots 还原。
    """

    source_audit = models.ForeignKey(
        LogSearchAudit,
        verbose_name="来源日志审计",
        related_name="data_extractions",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    source_operation_id = models.CharField("来源操作ID", max_length=128, blank=True, db_index=True)
    environment = models.ForeignKey(
        "environments.Environment",
        verbose_name="目标环境",
        related_name="data_extraction_records",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    environment_name = models.CharField("环境名称快照", max_length=128, blank=True)
    task_name = models.CharField("来源任务名称", max_length=255, blank=True)
    name = models.CharField("提取记录名称", max_length=255)
    query_snapshot = models.JSONField("日志查询快照", default=dict, blank=True)
    rule_snapshots = models.JSONField("数据提取规则快照", default=list, blank=True)
    status = models.CharField("执行状态", max_length=16, choices=DataExtractionStatus.choices, default=DataExtractionStatus.RUNNING, db_index=True)
    matched_rule_count = models.PositiveIntegerField("命中规则数", default=0)
    row_count = models.PositiveBigIntegerField("命中数据行数", default=0)
    result_summary = models.JSONField("规则命中摘要", default=list, blank=True)
    hour_summary = models.JSONField("小时分片摘要", default=list, blank=True)
    error_message = models.TextField("错误信息", blank=True)
    finished_at = models.DateTimeField("完成时间", null=True, blank=True)

    class Meta:
        verbose_name = "数据提取记录"
        verbose_name_plural = "数据提取记录"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["-created_at", "status"], name="data_extract_time_status_idx"),
            models.Index(fields=["environment", "-created_at"], name="data_extract_env_time_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.name} {self.status} {self.row_count}"


class OperationTrigger(models.TextChoices):
    USER = "user", "用户操作"
    AI = "ai", "AI 代操作"
    SYSTEM = "system", "系统自动"


class OperationOutcome(models.TextChoices):
    SUCCESS = "success", "成功"
    FAILED = "failed", "失败"


class OperationAudit(TimeStampedModel):
    """**功能级**的操作审计：用户（或 AI 代用户）点出来的每一个功能都落一条。

    和 :class:`LogSearchAudit` 的分工：那张表记"日志检索这一次到底查了什么、命中了什么"，
    是检索诊断；这张表记"谁在什么时候用了哪个功能、结果如何"，是操作留痕。
    检索功能在这里也会出现一条（功能名「日志定位（检索）」），靠 ``log_search_audit`` 关联。

    这里**只记用户发起的**：系统自己跑的（流式、轮询、后台任务、前端自动回执）在
    ``apps/audits/features.py`` 里明确排除，不在库里留噪音。
    """

    operation_id = models.CharField("操作ID", max_length=128, db_index=True)
    """同一次 HTTP 请求的 id（``X-TraceLens-Request-ID`` / RequestIdMiddleware）。"""

    operator_username = models.CharField("操作用户", max_length=150, blank=True, db_index=True)
    client_ip = models.CharField("操作IP", max_length=64, blank=True, db_index=True)
    session_id = models.CharField("浏览器会话", max_length=64, blank=True, db_index=True)
    """本工具没有登录体系，用前端的 ``X-Session-Id`` 区分"是不是同一个人/同一个浏览器"。"""
    trigger = models.CharField("发起方", max_length=16, choices=OperationTrigger.choices, default=OperationTrigger.USER, db_index=True)

    feature_group = models.CharField("功能分组", max_length=64, db_index=True)
    feature_name = models.CharField("功能名", max_length=128, db_index=True)
    summary = models.CharField("操作记录", max_length=255, blank=True)

    environment = models.ForeignKey(
        "environments.Environment",
        verbose_name="目标环境",
        related_name="operation_audits",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    environment_name = models.CharField("环境名称快照", max_length=128, blank=True, db_index=True)
    target_kind = models.CharField("目标类型", max_length=32, blank=True)
    target_name = models.CharField("目标名称", max_length=255, blank=True)

    log_search_audit = models.ForeignKey(
        "audits.LogSearchAudit",
        verbose_name="关联日志检索审计",
        related_name="operation_audits",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    http_method = models.CharField("请求方法", max_length=8)
    path = models.CharField("接口路径", max_length=512, blank=True)
    """后端留档用：界面上**不显示 URL**，只显示功能名。"""
    status_code = models.PositiveSmallIntegerField("响应状态码", default=0)
    outcome = models.CharField("执行结果", max_length=16, choices=OperationOutcome.choices, default=OperationOutcome.SUCCESS, db_index=True)
    error_message = models.TextField("错误信息", blank=True)
    duration_ms = models.PositiveBigIntegerField("耗时毫秒", null=True, blank=True)
    request_payload = models.JSONField("请求参数（已脱敏）", default=dict, blank=True)

    class Meta:
        verbose_name = "操作审计"
        verbose_name_plural = "操作审计"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["-created_at", "outcome"], name="op_audit_time_result_idx"),
            models.Index(fields=["feature_group", "-created_at"], name="op_audit_group_time_idx"),
            models.Index(fields=["client_ip", "-created_at"], name="op_audit_ip_time_idx"),
            models.Index(fields=["operator_username", "-created_at"], name="op_audit_user_time_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.feature_name} {self.summary} {self.outcome}"
