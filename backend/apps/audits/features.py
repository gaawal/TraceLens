"""「哪个接口 = 哪个功能」的唯一出处。

用户要的审计是**功能级**的：表格里不显示 URL，显示"部署环境 / 启动环境进程 / 查询环境信息"
这样的功能名。所以这里维护四张表：

1. ``RESOURCE_LABELS``：按路由 basename 给出（分组, 资源中文名）。
2. ``ACTION_LABELS``：按动作名（``list`` / ``detail`` / 自定义动作）+ HTTP 方法给出动作模板。
3. ``FEATURE_OVERRIDES``：需要精确命名的接口（部署、启停进程、AI 对话…），优先于前两张表。
4. ``SKIP_FEATURES``：**不该记的**，每条都写明原因（流式 / 高频轮询 / 审计自身 / 前端自动回执）。

解析入口是 :func:`resolve_feature`：拿 DRF 的 ``url_name`` + HTTP 方法，返回
:class:`FeatureAudit`（分组 + 功能名）或 ``None``（= 不记录）。新增接口时**不用**改这里也能
落一个合理的中文名（见 :func:`_fallback_feature`），但重要功能建议在 overrides 里写清楚。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeatureAudit:
    """一个可以被审计到的用户功能。"""

    group: str
    name: str


@dataclass(frozen=True)
class SkippedFeature:
    """明确不记录的接口，``reason`` 会写进代码注释/测试，方便解释"为什么这条没有审计"。"""

    reason: str


# ---------------------------------------------------------------------------
# 1. 资源：路由 basename → （分组, 资源中文名）
# ---------------------------------------------------------------------------
RESOURCE_LABELS: dict[str, tuple[str, str]] = {
    "environment": ("环境资源", "环境"),
    "environment-folder": ("环境资源", "环境分组"),
    "environment-discovery": ("环境资源", "环境探测"),
    "machine": ("环境资源", "机器"),
    "machine-relation": ("环境资源", "上下位机关系"),
    "resource-settings": ("平台设置", "平台路径设置"),
    "environment-log": ("日志定位", "日志检索"),
    "log-scene": ("日志定位", "日志场景"),
    "log-url-import": ("日志定位", "URL 日志导入"),
    "log-source-rule": ("平台设置", "日志类型规则"),
    "log-format-rule": ("平台设置", "日志格式解析规则"),
    "log-query-skill": ("平台设置", "日志检索技能"),
    "log-subsystem": ("平台设置", "子系统定义"),
    "log-fm": ("平台设置", "FM 定义"),
    "log-watch": ("数据提取", "实时采集"),
    "data-extraction": ("数据提取", "提取记录"),
    "log-audit": ("操作审计", "操作审计"),
    "cpd-report": ("CPD 测校报告", "测校报告"),
    "abnormal-case": ("案例与分析", "案例"),
    "atlog-analysis": ("用例分析", "用例诊断"),
    "tool": ("AI 助手", "AI 助手"),
}

# ---------------------------------------------------------------------------
# 2. 动作：动作名 → {HTTP 方法: 模板}，模板里的 {} 换成资源中文名
# ---------------------------------------------------------------------------
ACTION_LABELS: dict[str, dict[str, str]] = {
    "list": {"GET": "查询{}列表", "POST": "新增{}"},
    "detail": {"GET": "查看{}详情", "PATCH": "修改{}", "PUT": "修改{}", "DELETE": "删除{}"},
    "bulk": {"POST": "批量操作{}", "DELETE": "批量删除{}"},
    "runtime": {"GET": "查询{}"},
    "tree": {"GET": "查询{}结构树"},
    "subsystems": {"GET": "查询{}子系统"},
    "current": {"GET": "查询{}", "PATCH": "修改{}", "PUT": "修改{}"},
    "hits": {"GET": "查询{}命中数据"},
    "replay": {"GET": "回放{}数据"},
    "rule-health": {"GET": "检查{}规则健康度"},
    "progress": {"GET": "查询{}进度"},
    "cancel": {"POST": "停止{}"},
    "restore-default": {"POST": "恢复{}默认值"},
    "test": {"POST": "测试{}"},
    "test-draft": {"POST": "测试{}草稿"},
    "snapshot": {"GET": "查看{}快照", "POST": "保存{}快照"},
    "match": {"POST": "匹配{}"},
    "mark-match": {"POST": "标记{}命中结果"},
    "connect-test": {"POST": "测试{}连接"},
    "connect-test-draft": {"POST": "测试{}连接"},
    "disconnect-session": {"POST": "断开{}会话"},
    "discover": {"POST": "重新探测{}"},
    "query-version": {"POST": "查询{}软件版本"},
    "preview-xml": {"POST": "预览{}拓扑"},
    "content": {"GET": "读取{}内容", "POST": "读取{}内容"},
    "data-file": {"GET": "下载{}数据文件"},
    "data-files": {"GET": "查询{}数据文件"},
    "data-browser": {"GET": "浏览{}数据目录"},
    "dataset": {"GET": "读取{}数据集"},
    "sheet": {"GET": "读取{}数据表"},
    "excel-preview": {"GET": "预览{}数据表"},
    "reports": {"GET": "查询{}列表"},
    "query": {"GET": "查询{}"},
}

# ---------------------------------------------------------------------------
# 3. 精确命名：需要一眼看懂"用户到底干了什么"的接口
# ---------------------------------------------------------------------------
FEATURE_OVERRIDES: dict[tuple[str, str], FeatureAudit] = {
    # 环境资源
    ("environment-list", "GET"): FeatureAudit("环境资源", "查询环境列表"),
    ("environment-list", "POST"): FeatureAudit("环境资源", "新增环境"),
    ("environment-detail", "GET"): FeatureAudit("环境资源", "查询环境详情"),
    ("environment-detail", "PATCH"): FeatureAudit("环境资源", "修改环境"),
    ("environment-detail", "PUT"): FeatureAudit("环境资源", "修改环境"),
    ("environment-detail", "DELETE"): FeatureAudit("环境资源", "删除环境"),
    ("environment-bulk", "POST"): FeatureAudit("环境资源", "批量操作环境"),
    ("environment-discover", "POST"): FeatureAudit("环境资源", "重新探测环境拓扑"),
    ("environment-preview-xml", "POST"): FeatureAudit("环境资源", "预览上下位机拓扑"),
    ("environment-query-version", "POST"): FeatureAudit("环境资源", "查询软件版本"),
    ("environment-runtime-status", "GET"): FeatureAudit("环境资源", "查询环境运行状态"),
    ("environment-runtime-status-all", "GET"): FeatureAudit("环境资源", "查询全部环境运行状态"),
    ("environment-process-control", "POST"): FeatureAudit("环境资源", "启动/停止环境进程"),
    ("environment-sync-lower-time", "POST"): FeatureAudit("环境资源", "同步下位机时间"),
    ("environment-deployments", "GET"): FeatureAudit("环境资源", "查询部署记录"),
    ("environment-deployments", "POST"): FeatureAudit("环境资源", "部署环境"),
    ("environment-deployment-defaults", "GET"): FeatureAudit("环境资源", "查询部署默认配置"),
    ("environment-deployment-preview", "POST"): FeatureAudit("环境资源", "预览部署方案"),
    ("environment-deployment-parse", "POST"): FeatureAudit("环境资源", "解析部署配置"),
    ("environment-deployment-ssh-trust", "POST"): FeatureAudit("环境资源", "配置免密登录"),
    ("environment-deployment-time-sync", "POST"): FeatureAudit("环境资源", "同步部署时间"),
    ("environment-deployment-detail", "GET"): FeatureAudit("环境资源", "查看部署详情"),
    ("environment-deployment-retry", "POST"): FeatureAudit("环境资源", "重试部署"),
    ("environment-deployment-stop", "POST"): FeatureAudit("环境资源", "停止部署"),
    ("environment-latest-deployment", "GET"): FeatureAudit("环境资源", "查询最近一次部署"),
    ("environment-active-deployments", "GET"): FeatureAudit("环境资源", "查询进行中的部署"),
    # 日志定位
    ("environment-log-stream", "POST"): FeatureAudit("日志定位", "日志定位（检索）"),
    ("environment-log-plan", "POST"): FeatureAudit("日志定位", "预演日志检索范围"),
    ("environment-log-live", "POST"): FeatureAudit("日志定位", "启动实时监听"),
    ("environment-log-cancel", "POST"): FeatureAudit("日志定位", "停止日志检索"),
    ("environment-log-subsystems", "GET"): FeatureAudit("日志定位", "查询日志子系统"),
    ("environment-log-semantic-source", "POST"): FeatureAudit("日志定位", "识别日志语义来源"),
    ("environment-log-semantic-source-batch", "POST"): FeatureAudit("日志定位", "批量识别日志语义来源"),
    # 数据提取
    ("log-watch-list", "GET"): FeatureAudit("数据提取", "查询实时采集项"),
    ("log-watch-list", "POST"): FeatureAudit("数据提取", "新增实时采集项"),
    ("log-watch-detail", "PATCH"): FeatureAudit("数据提取", "修改实时采集项"),
    ("log-watch-detail", "PUT"): FeatureAudit("数据提取", "修改实时采集项"),
    ("log-watch-detail", "DELETE"): FeatureAudit("数据提取", "删除实时采集项"),
    ("log-watch-detail", "GET"): FeatureAudit("数据提取", "查询实时采集项详情"),
    ("log-watch-sync-capture", "POST"): FeatureAudit("数据提取", "执行一次数据采集"),
    ("log-watch-hits", "GET"): FeatureAudit("数据提取", "查询采集命中数据"),
    ("log-watch-replay", "GET"): FeatureAudit("数据提取", "回放采集数据"),
    ("log-watch-rule-health", "GET"): FeatureAudit("数据提取", "检查提取规则健康度"),
    ("data-extraction-list", "GET"): FeatureAudit("数据提取", "查询提取记录"),
    ("data-extraction-list", "POST"): FeatureAudit("数据提取", "保存提取记录"),
    ("data-extraction-detail", "DELETE"): FeatureAudit("数据提取", "删除提取记录"),
    ("data-extraction-detail", "GET"): FeatureAudit("数据提取", "查看提取记录详情"),
    # AI 助手
    ("tool-assistant-chat-stream", "POST"): FeatureAudit("AI 助手", "AI 对话"),
    ("tool-assistant-chat", "POST"): FeatureAudit("AI 助手", "AI 对话"),
    ("tool-assistant-cancel", "POST"): FeatureAudit("AI 助手", "停止 AI 任务"),
    ("tool-assistant-guide", "POST"): FeatureAudit("AI 助手", "请求 AI 引导"),
    ("tool-assistant-confirm", "POST"): FeatureAudit("AI 助手", "确认 AI 待执行动作"),
    ("tool-assistant-transcribe", "POST"): FeatureAudit("AI 助手", "语音转写"),
    ("tool-rule-autoconfig", "POST"): FeatureAudit("AI 助手", "AI 一键配置规则"),
    ("tool-case-draft", "POST"): FeatureAudit("AI 助手", "AI 生成案例草稿"),
    ("tool-invoke", "POST"): FeatureAudit("AI 助手", "调用 AI 能力"),
    ("tool-list", "GET"): FeatureAudit("AI 助手", "查询 AI 能力清单"),
    ("tool-detail", "GET"): FeatureAudit("AI 助手", "查看 AI 能力详情"),
    ("tool-ai-models", "GET"): FeatureAudit("AI 助手", "查询可用模型"),
    # 用例分析
    ("atlog-analysis-list", "GET"): FeatureAudit("用例分析", "查询用例列表"),
    ("atlog-analysis-analyze", "POST"): FeatureAudit("用例分析", "分析用例"),
    ("atlog-analysis-analyze-report", "POST"): FeatureAudit("用例分析", "生成用例分析报告"),
    ("atlog-analysis-ai-diagnose", "POST"): FeatureAudit("用例分析", "AI 诊断用例"),
    ("atlog-analysis-ai-diagnose-start", "POST"): FeatureAudit("用例分析", "AI 诊断用例"),
    ("atlog-analysis-ai-accept", "POST"): FeatureAudit("用例分析", "采纳 AI 诊断结论"),
    ("atlog-analysis-knowledge-match", "POST"): FeatureAudit("用例分析", "匹配历史案例"),
    ("atlog-analysis-logs", "POST"): FeatureAudit("用例分析", "读取用例日志"),
    ("atlog-analysis-logs-start", "POST"): FeatureAudit("用例分析", "读取用例日志"),
    ("atlog-analysis-logs-cancel", "POST"): FeatureAudit("用例分析", "停止读取用例日志"),
    ("atlog-analysis-read-file", "POST"): FeatureAudit("用例分析", "查看用例文件"),
    ("atlog-analysis-browse", "POST"): FeatureAudit("用例分析", "浏览用例目录"),
    ("atlog-analysis-excel-import-debug", "POST"): FeatureAudit("用例分析", "导入用例 Excel"),
    ("atlog-analysis-event", "POST"): FeatureAudit("用例分析", "记录用例事件"),
    # 案例与分析
    ("abnormal-case-list", "GET"): FeatureAudit("案例与分析", "查询案例列表"),
    ("abnormal-case-match", "POST"): FeatureAudit("案例与分析", "匹配历史案例"),
    ("abnormal-case-mark-match", "POST"): FeatureAudit("案例与分析", "标记案例命中结果"),
    # CPD 测校报告
    ("cpd-report-tree", "GET"): FeatureAudit("CPD 测校报告", "查看报告目录"),
    ("cpd-report-query", "GET"): FeatureAudit("CPD 测校报告", "查询测校报告"),
    ("cpd-report-snapshot", "GET"): FeatureAudit("CPD 测校报告", "查看测校报告"),
    ("cpd-report-reports", "GET"): FeatureAudit("CPD 测校报告", "查询报告文件列表"),
    ("cpd-report-content", "GET"): FeatureAudit("CPD 测校报告", "读取报告文件"),
    ("cpd-report-dataset", "GET"): FeatureAudit("CPD 测校报告", "读取测校数据集"),
    ("cpd-report-excel-preview", "GET"): FeatureAudit("CPD 测校报告", "预览测校数据表"),
    ("cpd-report-sheet", "GET"): FeatureAudit("CPD 测校报告", "读取测校数据表"),
    ("cpd-report-data-browser", "GET"): FeatureAudit("CPD 测校报告", "浏览测校数据目录"),
    ("cpd-report-data-file", "GET"): FeatureAudit("CPD 测校报告", "下载测校数据文件"),
    ("cpd-report-data-files", "GET"): FeatureAudit("CPD 测校报告", "查询测校数据文件"),
    # 平台设置
    ("resource-settings-current", "GET"): FeatureAudit("平台设置", "查询平台路径设置"),
    ("resource-settings-current", "PATCH"): FeatureAudit("平台设置", "修改平台路径设置"),
    ("resource-settings-current", "PUT"): FeatureAudit("平台设置", "修改平台路径设置"),
    ("log-url-import-content", "POST"): FeatureAudit("日志定位", "通过 URL 读取日志"),
    ("log-format-rule-test", "POST"): FeatureAudit("平台设置", "测试日志解析规则"),
    ("log-format-rule-test-draft", "POST"): FeatureAudit("平台设置", "测试日志解析规则"),
    ("log-format-rule-restore-default", "POST"): FeatureAudit("平台设置", "恢复日志解析规则默认值"),
    # 机器
    ("machine-connect-test", "POST"): FeatureAudit("环境资源", "测试机器连接"),
    ("machine-connect-test-draft", "POST"): FeatureAudit("环境资源", "测试机器连接"),
    ("machine-disconnect-session", "POST"): FeatureAudit("环境资源", "断开机器会话"),
}

# ---------------------------------------------------------------------------
# 4. 不记录：流式 / 高频轮询 / 审计自身 / 前端自动回执
# ---------------------------------------------------------------------------
SKIP_FEATURES: dict[str, str] = {
    # 审计自己：每翻一页都会新增一条"查询审计"，会把真正的操作挤下去。
    "log-audit-list": "审计列表自身的查询，记录了会自我放大",
    "log-audit-detail": "审计详情自身的查询，记录了会自我放大",
    "operation-audit-list": "审计列表自身的查询，记录了会自我放大",
    "operation-audit-detail": "审计详情自身的查询，记录了会自我放大",
    "operation-audit-facets": "审计筛选项自身的查询，记录了会自我放大",
    # 前端自动回执：日志检索结果解析完、AI 动作执行完，由页面自动回传，不是用户操作。
    "log-audit-client-result": "前端解析完日志后自动回传的校正结果",
    "tool-assistant-ui-receipt": "前端执行完 AI 动作后自动回执",
    "tool-assistant-voice-capabilities": "AI 面板加载时自动探测语音能力，不是用户点的功能",
    # 流式 / 轮询：这些是"系统自己在跑"，不是用户点出来的功能。
    "environment-log-progress": "检索进度轮询（前端自动，秒级）",
    "atlog-analysis-ai-diagnose-status": "AI 诊断进度轮询（前端自动）",
    "atlog-analysis-ai-diagnose-stream": "AI 诊断流式输出（SSE）",
    "atlog-analysis-logs-status": "用例日志读取进度轮询（前端自动）",
    "deployment-events": "部署进度事件流（SSE）",
    "watch-events": "实时采集事件流（SSE）",
    "running-tasks": "进行中任务轮询（前端自动）",
    "task-events": "AI 任务事件轮询（前端自动，0.25s）",
    # 非功能接口
    "api-root": "接口索引",
    "health-check": "健康检查",
    "schema": "OpenAPI schema",
    "swagger-ui": "接口文档页面",
    "redoc": "接口文档页面",
}

# HTTP 方法归一：HEAD/OPTIONS 跟随 GET 语义，但一般不记录（见 SKIP_METHODS）。
SKIP_METHODS = frozenset({"OPTIONS", "HEAD"})

# 兜底动作名：没有登记过的自定义动作也要有个像样的中文名。
_FALLBACK_ACTION = "执行操作"


def _match_basename(url_name: str) -> str | None:
    """在已知 basename 里找最长前缀（``environment-log-stream`` → ``environment-log``）。"""
    best: str | None = None
    for basename in RESOURCE_LABELS:
        if url_name == basename or url_name.startswith(f"{basename}-"):
            if best is None or len(basename) > len(best):
                best = basename
    return best


def _fallback_feature(url_name: str) -> FeatureAudit:
    """没登记过的接口兜底：分组取资源分组，功能名尽量拼成中文。"""
    basename = _match_basename(url_name)
    if basename is None:
        return FeatureAudit("其他", f"未知操作（{url_name}）")
    group, resource = RESOURCE_LABELS[basename]
    action = url_name[len(basename):].lstrip("-")
    if not action:
        return FeatureAudit(group, f"操作{resource}")
    label = action.replace("-", " ")
    return FeatureAudit(group, f"{resource}·{label}")


def resolve_feature(url_name: str, method: str) -> FeatureAudit | None:
    """url_name + HTTP 方法 → 该记的功能；返回 ``None`` 表示这条请求不记录。"""
    name = str(url_name or "").strip()
    verb = str(method or "GET").strip().upper()
    if not name or verb in SKIP_METHODS:
        return None
    if name in SKIP_FEATURES:
        return None
    override = FEATURE_OVERRIDES.get((name, verb))
    if override is not None:
        return override

    basename = _match_basename(name)
    if basename is None:
        return _fallback_feature(name)
    group, resource = RESOURCE_LABELS[basename]
    action = name[len(basename):].lstrip("-")
    templates = ACTION_LABELS.get(action)
    if templates:
        template = templates.get(verb) or templates.get("default")
        if template:
            return FeatureAudit(group, template.format(resource))
    return _fallback_feature(name)


def skip_reason(url_name: str) -> str:
    """给测试/排查用：这个 url_name 为什么不记录。"""
    return SKIP_FEATURES.get(str(url_name or "").strip(), "")
