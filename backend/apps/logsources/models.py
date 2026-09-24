from django.db import models

from apps.common.models import TimeStampedModel


class LogSourceRule(TimeStampedModel):
    machine = models.ForeignKey(
        "machines.Machine",
        verbose_name="机器",
        related_name="log_source_rules",
        on_delete=models.CASCADE,
    )
    name = models.CharField("规则名称", max_length=128)
    root_path_template = models.CharField(
        "根目录模板",
        max_length=512,
        default="/log/{username}/debug",
    )
    subsystem = models.CharField("子系统", max_length=128, blank=True)
    component = models.CharField("组件", max_length=128, blank=True)
    module = models.CharField("功能模块", max_length=128, blank=True)
    file_pattern = models.CharField("文件匹配模式", max_length=128, default="*.log*")
    auto_discovery = models.BooleanField("自动发现目录", default=True)
    enabled = models.BooleanField("启用", default=True)
    description = models.TextField("说明", blank=True)

    class Meta:
        verbose_name = "日志路径规则"
        verbose_name_plural = "日志路径规则"
        ordering = ["machine", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["machine", "name"],
                name="uniq_machine_log_source_name",
            )
        ]
        indexes = [
            models.Index(fields=["machine", "enabled"], name="logsrc_machine_enabled_idx")
        ]

    def __str__(self) -> str:
        return f"{self.machine.name} - {self.name}"

    def resolve_root_path(self) -> str:
        return self.root_path_template.format_map(
            {
                "username": self.machine.username,
                "host": self.machine.host,
                "role": self.machine.role,
                "subsystem": self.subsystem,
                "component": self.component,
                "module": self.module,
            }
        )


class LogSubsystemDefinition(TimeStampedModel):
    """所有环境共用的子系统定义。

    环境扫描只能新增或更新时间，不能自动删除。人工维护可以在设置页面中
    修改显示名称、说明、排序和启用状态。
    """

    name = models.CharField("子系统标识", max_length=128, unique=True)
    display_name = models.CharField("显示名称", max_length=128, blank=True)
    enabled = models.BooleanField("启用", default=True)
    sort_order = models.IntegerField("排序", default=0)
    description = models.TextField("说明", blank=True)
    last_discovered_at = models.DateTimeField("最近发现时间", null=True, blank=True)

    class Meta:
        verbose_name = "全局日志子系统"
        verbose_name_plural = "全局日志子系统"
        ordering = ["sort_order", "name"]
        indexes = [
            models.Index(fields=["enabled", "sort_order"], name="logsub_enabled_sort_idx")
        ]

    def __str__(self) -> str:
        return self.display_name or self.name


class LogModuleKind(models.TextChoices):
    NORMAL = "normal", "普通模块"
    EXECUTOR = "executor", "执行器模块"


class LogFmDefinition(TimeStampedModel):
    """全局子系统下的日志模块 / Event 组件定义。

    ``name`` 同时承载真实日志模块名和从 ``*_event.json`` 自动发现的 Event
    组件名。Event 组件可通过 ``target_modules`` 配置诊断时优先查询的日志
    模块，Event 组件与日志目标统一维护在同一张子系统模块表中。
    """

    subsystem = models.ForeignKey(
        LogSubsystemDefinition,
        verbose_name="子系统",
        related_name="fms",
        on_delete=models.CASCADE,
    )
    name = models.CharField("模块标识", max_length=128)
    kind = models.CharField("模块类型", max_length=16, choices=LogModuleKind.choices, default=LogModuleKind.NORMAL)
    display_name = models.CharField("显示名称", max_length=128, blank=True)
    enabled = models.BooleanField("启用", default=True)
    sort_order = models.IntegerField("排序", default=0)
    query_priority = models.PositiveIntegerField("查询优先级", default=100, db_index=True)
    description = models.TextField("说明", blank=True)
    target_modules = models.ManyToManyField(
        "self",
        verbose_name="目标模块",
        symmetrical=False,
        blank=True,
        related_name="targeted_by_modules",
        db_table="logsources_logfmdefinition_dependencies",
    )
    event_component = models.BooleanField("Event组件", default=False, db_index=True)
    event_config_files = models.JSONField("事件配置文件", default=list, blank=True)
    event_display_codes = models.JSONField("DisplayCode列表", default=list, blank=True)
    event_code_count = models.PositiveIntegerField("事件码数量", default=0)
    last_event_discovered_at = models.DateTimeField("最近事件配置发现时间", null=True, blank=True)
    matched_count = models.PositiveBigIntegerField("命中次数", default=0)
    last_matched_at = models.DateTimeField("最近命中时间", null=True, blank=True)
    last_discovered_at = models.DateTimeField("最近发现时间", null=True, blank=True)

    class Meta:
        verbose_name = "全局日志模块"
        verbose_name_plural = "全局日志模块"
        ordering = ["subsystem__sort_order", "subsystem__name", "sort_order", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["subsystem", "name", "kind"],
                name="uniq_global_subsystem_fm_kind",
            )
        ]
        indexes = [
            models.Index(fields=["subsystem", "enabled", "sort_order"], name="logfm_sub_enabled_idx"),
            models.Index(fields=["subsystem", "event_component", "name"], name="logfm_event_component_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.subsystem.name}/{self.display_name or self.name}"


class EventConfigSource(TimeStampedModel):
    """Environment-scoped index of one ``*_event.json`` file.

    The directory provides the subsystem and the filename provides the component
    repository code.  ``Source`` inside the JSON is intentionally not treated as
    a component because all configs in one subsystem normally share the same
    Source value.
    """

    environment = models.ForeignKey(
        "environments.Environment",
        verbose_name="环境",
        related_name="event_config_sources",
        on_delete=models.CASCADE,
    )
    subsystem = models.ForeignKey(
        LogSubsystemDefinition,
        verbose_name="来源子系统",
        related_name="event_config_sources",
        on_delete=models.PROTECT,
    )
    component_code = models.CharField("组件代码仓", max_length=128, db_index=True)
    source = models.CharField("JSON Source", max_length=64, blank=True)
    file_name = models.CharField("配置文件", max_length=256)
    file_path = models.CharField("配置路径", max_length=1024)
    version = models.CharField("配置版本", max_length=32, blank=True)
    file_hash = models.CharField("内容哈希", max_length=64, blank=True)
    remote_mtime = models.FloatField("远端修改时间", default=0)
    remote_size = models.PositiveBigIntegerField("远端文件大小", default=0)
    active = models.BooleanField("有效", default=True, db_index=True)
    sync_message = models.TextField("同步信息", blank=True)
    last_seen_at = models.DateTimeField("最近发现时间", null=True, blank=True)

    class Meta:
        verbose_name = "环境事件配置文件"
        verbose_name_plural = "环境事件配置文件"
        ordering = ["environment", "subsystem__name", "component_code", "file_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["environment", "file_path"],
                name="uniq_env_event_config_path",
            )
        ]
        indexes = [
            models.Index(fields=["environment", "active"], name="eventcfg_env_active_idx"),
            models.Index(fields=["environment", "component_code"], name="eventcfg_env_component_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.environment.name} - {self.subsystem.name}/{self.component_code}"


class EventCodeDefinition(TimeStampedModel):
    """One event-code dictionary entry from an environment's event JSON.

    DisplayCode is the stable runtime lookup key.  It is unique per environment
    rather than globally so multiple environments can keep independent versioned
    snapshots of the same event definition.
    """

    environment = models.ForeignKey(
        "environments.Environment",
        verbose_name="环境",
        related_name="event_code_definitions",
        on_delete=models.CASCADE,
    )
    source_config = models.ForeignKey(
        EventConfigSource,
        verbose_name="配置文件",
        related_name="event_codes",
        on_delete=models.CASCADE,
    )
    subsystem = models.ForeignKey(
        LogSubsystemDefinition,
        verbose_name="来源子系统",
        related_name="event_code_definitions",
        on_delete=models.PROTECT,
    )
    component_code = models.CharField("组件代码仓", max_length=128, db_index=True)
    source = models.CharField("Source", max_length=64, blank=True)
    code = models.CharField("Code", max_length=64, blank=True, db_index=True)
    display_code = models.CharField("DisplayCode", max_length=128, db_index=True)
    code_string = models.CharField("CodeString", max_length=256, blank=True, db_index=True)
    severity = models.CharField("Severity", max_length=32, blank=True)
    category = models.CharField("Category", max_length=64, blank=True)
    recovery_class = models.CharField("RecoveryClass", max_length=64, blank=True)
    auto_clear = models.BooleanField("AutoClear", default=False)
    send_to_host = models.BooleanField("SendToHost", default=False)
    send_to_active_exception_gui = models.BooleanField("SendToActiveExceptionGUI", default=False)
    description = models.TextField("Description", blank=True)
    object_params = models.JSONField("ObjectParams", default=list, blank=True)
    raw_config = models.JSONField("原始配置", default=dict, blank=True)
    active = models.BooleanField("有效", default=True, db_index=True)
    last_seen_at = models.DateTimeField("最近发现时间", null=True, blank=True)

    class Meta:
        verbose_name = "环境事件码定义"
        verbose_name_plural = "环境事件码定义"
        ordering = ["environment", "subsystem__name", "component_code", "display_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["environment", "display_code"],
                name="uniq_env_event_display_code",
            )
        ]
        indexes = [
            models.Index(fields=["environment", "display_code"], name="eventcode_env_display_idx"),
            models.Index(fields=["environment", "code"], name="eventcode_env_code_idx"),
            models.Index(fields=["environment", "component_code"], name="eventcode_env_component_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.display_code} - {self.subsystem.name}/{self.component_code}"


class LogCatalogStatus(models.TextChoices):
    SUCCESS = "success", "成功"
    ERROR = "error", "失败"


class LogResourceCatalog(TimeStampedModel):
    """某个环境日志根目录的轻量扫描记录。

    该表只负责记录资源是否扫描过、扫描状态和本环境实际发现过的条目，
    供多用户复用 SSH 扫描结果。真正用于筛选的统一字典由
    LogSubsystemDefinition / LogFmDefinition 提供（界面统一称为模块）。
    """

    environment = models.ForeignKey(
        "environments.Environment",
        verbose_name="环境",
        related_name="log_resource_catalogs",
        on_delete=models.CASCADE,
    )
    machine = models.ForeignKey(
        "machines.Machine",
        verbose_name="实际访问机器",
        related_name="log_resource_catalogs",
        on_delete=models.CASCADE,
    )
    source_category = models.CharField("日志类型", max_length=32)
    source_name = models.CharField("日志类型名称", max_length=64)
    root = models.CharField("日志根目录", max_length=512)
    status = models.CharField(
        "最近扫描状态",
        max_length=16,
        choices=LogCatalogStatus.choices,
        default=LogCatalogStatus.SUCCESS,
    )
    message = models.TextField("最近扫描信息", blank=True)
    scanned_at = models.DateTimeField("最近扫描时间", null=True, blank=True)

    class Meta:
        verbose_name = "环境日志资源扫描记录"
        verbose_name_plural = "环境日志资源扫描记录"
        ordering = ["environment", "source_category", "machine", "root"]
        constraints = [
            models.UniqueConstraint(
                fields=["environment", "machine", "source_category", "root"],
                name="uniq_environment_log_catalog_root",
            )
        ]
        indexes = [
            models.Index(
                fields=["environment", "source_category"],
                name="logcat_env_category_idx",
            )
        ]

    def __str__(self) -> str:
        return f"{self.environment} - {self.source_name} - {self.root}"


class LogResourceCatalogItem(TimeStampedModel):
    catalog = models.ForeignKey(
        LogResourceCatalog,
        verbose_name="资源扫描记录",
        related_name="items",
        on_delete=models.CASCADE,
    )
    subsystem = models.CharField("子系统", max_length=128)
    fm = models.CharField("模块", max_length=128)
    kind = models.CharField("模块类型", max_length=16, choices=LogModuleKind.choices, default=LogModuleKind.NORMAL)

    class Meta:
        verbose_name = "环境日志资源发现项"
        verbose_name_plural = "环境日志资源发现项"
        ordering = ["subsystem", "fm"]
        constraints = [
            models.UniqueConstraint(
                fields=["catalog", "subsystem", "fm", "kind"],
                name="uniq_log_catalog_subsystem_fm_kind",
            )
        ]
        indexes = [
            models.Index(fields=["catalog", "subsystem"], name="logcat_item_subsys_idx")
        ]

    def __str__(self) -> str:
        return f"{self.catalog} - {self.subsystem}/{self.fm}"


class LogFormatParserRule(TimeStampedModel):
    """可配置的日志行格式解析规则。

    pattern 使用 Python ``re`` 语法；命名捕获组通过 field_map 映射到 TraceLens
    标准字段。前端运行时只使用后端校验后返回的 portable client_pattern。
    """

    name = models.CharField("规则名称", max_length=128)
    category = models.CharField("日志类型", max_length=32, default="debug")
    enabled = models.BooleanField("启用", default=True)
    priority = models.IntegerField("优先级", default=100)
    file_pattern = models.CharField("适用文件", max_length=256, blank=True, default="*.log*")
    pattern = models.TextField("正则表达式")
    ignore_case = models.BooleanField("忽略大小写", default=False)
    field_map = models.JSONField("标准字段映射", default=dict, blank=True)
    timestamp_format = models.CharField("时间格式", max_length=64, default="auto")
    built_in = models.BooleanField("系统内置", default=False)
    description = models.TextField("说明", blank=True)

    class Meta:
        verbose_name = "日志格式解析规则"
        verbose_name_plural = "日志格式解析规则"
        ordering = ["category", "-priority", "id"]
        constraints = [
            models.UniqueConstraint(fields=["category", "name"], name="uniq_log_format_rule_category_name")
        ]
        indexes = [
            models.Index(fields=["category", "enabled", "priority"], name="logfmt_cat_enabled_pri_idx")
        ]

    def __str__(self) -> str:
        return f"{self.category} - {self.name}"


class LogQuerySkill(TimeStampedModel):
    """Subsystem-scoped diagnostic retrieval strategy used by TracePilot.

    The normal log locator keeps using the fast deterministic path rules.  A
    query skill is only consulted when an AI diagnosis needs extra evidence
    (for example when the standard module log is empty or only exposes an
    upper-layer timeout).  Keeping the skill bound to one subsystem prevents a
    generic troubleshooting prompt from silently pulling unrelated logs.
    """

    subsystem = models.ForeignKey(
        LogSubsystemDefinition,
        verbose_name="子系统",
        related_name="query_skills",
        on_delete=models.CASCADE,
    )
    name = models.CharField("Skill名称", max_length=128)
    enabled = models.BooleanField("启用", default=True, db_index=True)
    priority = models.IntegerField("优先级", default=100)
    trigger_modules = models.JSONField("触发模块", default=list, blank=True)
    trigger_keywords = models.JSONField("触发关键字", default=list, blank=True)
    description = models.TextField("规则说明", blank=True)
    steps = models.JSONField("补充检索步骤", default=list, blank=True)

    class Meta:
        verbose_name = "日志查询Skill"
        verbose_name_plural = "日志查询Skill"
        ordering = ["subsystem__sort_order", "subsystem__name", "-priority", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["subsystem", "name"],
                name="uniq_log_query_skill_subsystem_name",
            )
        ]
        indexes = [
            models.Index(fields=["subsystem", "enabled", "priority"], name="logskill_sub_enabled_pri_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.subsystem.name} - {self.name}"


class LogWatchLevel(models.TextChoices):
    """Escalating cost tiers; a watch must declare which one it is (see 观察/采集/告警)."""

    OBSERVE = "observe", "只观察（时间线打标）"
    CAPTURE = "capture", "采集数据"
    ALERT = "alert", "告警通知"


class LogWatchTriggerKind(models.TextChoices):
    """How a watch decides that something happened.

    Line matching alone misses the two signals that matter most in practice: a burst of
    a noisy error, and the *absence* of a log that should keep coming.
    """

    APPEAR = "appear", "出现即命中"
    BURST = "burst", "窗口内出现 N 次"
    SILENCE = "silence", "静默（应有日志却没有）"


class LogWatch(TimeStampedModel):
    """A server-side, rule-tagged monitor over one environment's live logs.

    Deliberately a *thin* object: it references rule definitions in
    ``ResourceSettings.display_rules`` by id instead of copying the rule body, so the
    rule settings page stays the single place where "what does this log line mean" lives.
    """

    environment = models.ForeignKey(
        "environments.Environment",
        verbose_name="环境",
        related_name="log_watches",
        on_delete=models.CASCADE,
    )
    name = models.CharField("监控名称", max_length=200)
    # Reference into ResourceSettings.display_rules[].id — never a copy of the rule.
    source_rule_id = models.CharField("来源语义规则ID", max_length=128, blank=True, db_index=True)
    # Reference into ResourceSettings.data_extraction_rules[].id, for 实时采集: a watch that
    # exists only to feed one data extractor. Mutually exclusive with `source_rule_id` in
    # practice — a watch is either "tell me about this symptom" or "collect this number".
    extraction_rule_id = models.CharField("来源数据提取器ID", max_length=128, blank=True, db_index=True)
    level = models.CharField("级别", max_length=16, choices=LogWatchLevel.choices, default=LogWatchLevel.OBSERVE)
    trigger_kind = models.CharField(
        "触发方式", max_length=16, choices=LogWatchTriggerKind.choices, default=LogWatchTriggerKind.APPEAR
    )
    trigger_config = models.JSONField("触发参数", default=dict, blank=True)
    # [{"subsystem": "...", "fm": "...", "kind": "normal"}]
    targets = models.JSONField("监听目标", default=list, blank=True)
    source_categories = models.JSONField("日志类型", default=list, blank=True)
    capture_config = models.JSONField("采集参数", default=dict, blank=True)
    enabled = models.BooleanField("启用", default=True, db_index=True)
    created_by = models.CharField("创建人", max_length=150, blank=True)
    last_hit_at = models.DateTimeField("最近命中", null=True, blank=True)
    hit_count = models.PositiveIntegerField("命中次数", default=0)
    dropped_count = models.PositiveIntegerField("因配额丢弃", default=0)
    last_error = models.TextField("最近错误", blank=True)
    last_worker_id = models.CharField("当前持有worker", max_length=64, blank=True)
    last_heartbeat_at = models.DateTimeField("worker心跳", null=True, blank=True)

    class Meta:
        verbose_name = "日志监控"
        verbose_name_plural = "日志监控"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["environment", "name"], name="uniq_log_watch_environment_name"),
        ]
        indexes = [
            models.Index(fields=["enabled", "environment"], name="logwatch_enabled_env_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.environment_id}:{self.name}"


class LogWatchHit(TimeStampedModel):
    """One durable detection.

    ``seq`` is a per-watch monotonic counter, not a timestamp: it is the resume anchor a
    reconnecting browser uses (`?since=<seq>`), and timestamps collide at millisecond
    resolution while an incrementing counter cannot.
    """

    watch = models.ForeignKey(
        LogWatch,
        verbose_name="监控",
        related_name="hits",
        on_delete=models.CASCADE,
    )
    seq = models.BigIntegerField("序号", db_index=True)
    matched_at = models.DateTimeField("命中时间", db_index=True)
    signature = models.CharField("归一化签名", max_length=255, db_index=True)
    level = models.CharField("日志级别", max_length=16, blank=True)
    machine = models.CharField("机器", max_length=128, blank=True)
    subsystem = models.CharField("子系统", max_length=128, blank=True)
    fm = models.CharField("模块", max_length=128, blank=True)
    source_path = models.CharField("来源文件", max_length=512, blank=True)
    source_category = models.CharField("日志类型", max_length=32, blank=True)
    line_text = models.TextField("命中原文", blank=True)
    window_lines = models.JSONField("采集窗口", default=list, blank=True)
    extracted = models.JSONField("提取字段", default=dict, blank=True)
    # hash(watch, signature, window_start) — makes a worker restart idempotent instead of
    # recording the same incident twice.
    dedup_key = models.CharField("去重键", max_length=64, unique=True)

    @staticmethod
    def build_dedup_key(watch_id: int, signature: str, matched_at) -> str:
        """Idempotency key: one hit per (watch, signature, second).

        Without this a worker restart, or two workers racing a lease, would record the same
        incident twice. Truncating to the second is deliberate: a genuinely repeated incident
        one second later is the same event for alerting purposes.
        """
        import hashlib

        bucket = matched_at.replace(microsecond=0).isoformat() if matched_at else ""
        return hashlib.sha1(f"{watch_id}|{signature}|{bucket}".encode("utf-8")).hexdigest()

    class Meta:
        verbose_name = "日志监控命中"
        verbose_name_plural = "日志监控命中"
        ordering = ["-seq"]
        indexes = [
            models.Index(fields=["watch", "-seq"], name="logwatchhit_watch_seq_idx"),
            models.Index(fields=["watch", "signature", "matched_at"], name="logwatchhit_sig_time_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.watch_id}#{self.seq} {self.signature[:40]}"
