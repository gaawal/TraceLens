from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from apps.tooling import services
from apps.atlog import services as atlog_services
from apps.tooling import workstation, diagnostic_retrieval

logger = logging.getLogger("tracelens.tooling.registry")

ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ToolDefinition:
    id: str
    name: str
    description: str
    category: str
    handler: ToolHandler | None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})
    tags: tuple[str, ...] = ()
    read_only: bool = True
    risk_level: str = "read_only"
    transport: str = "json"
    endpoint_template: str = ""
    implementation: str = ""
    version: str = "1.0"
    status: str = "ready"
    use_when: str = ""
    do_not_use_when: str = ""
    validation_rules: tuple[str, ...] = ()
    max_llm_output_chars: int = 16000
    agent_exposed: bool = True
    # Atomic capability metadata. Existing tool IDs remain stable for API compatibility;
    # new Agent orchestration uses the normalized metadata below.
    atomic_id: str = ""
    kind: str = ""
    domain: str = ""
    skills: tuple[str, ...] = ()

    def capability_id(self) -> str:
        return self.atomic_id.strip() or _atomic_id_for(self.id, self.capability_domain())

    def capability_kind(self) -> str:
        return self.kind.strip() or _kind_for(self)

    def capability_domain(self) -> str:
        return self.domain.strip() or _domain_for(self)

    def capability_skills(self) -> tuple[str, ...]:
        if self.skills:
            return tuple(dict.fromkeys(str(item).strip() for item in self.skills if str(item).strip()))
        return _skills_for(self)

    def agent_description(self) -> str:
        parts = [self.description.strip()]
        if self.use_when:
            parts.append(f"【必须/适合调用】{self.use_when.strip()}")
        if self.do_not_use_when:
            parts.append(f"【不要调用】{self.do_not_use_when.strip()}")
        if self.validation_rules:
            parts.append("【参数规则】" + "；".join(rule.strip() for rule in self.validation_rules if rule.strip()))
        if not self.read_only:
            if self.risk_level in {"high", "critical", "destructive"}:
                parts.append("【安全规则】这是有副作用的高风险操作；只能先生成确认卡片，未经用户明确确认绝不能执行。")
            else:
                parts.append("【安全规则】这是写操作，调用前必须确保用户意图明确。")
        return "\n".join(part for part in parts if part)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "tags": list(self.tags),
            "read_only": self.read_only,
            "risk_level": self.risk_level,
            "transport": self.transport,
            "endpoint_template": self.endpoint_template,
            "implementation": self.implementation,
            "version": self.version,
            "status": self.status,
            "use_when": self.use_when,
            "do_not_use_when": self.do_not_use_when,
            "validation_rules": list(self.validation_rules),
            "max_llm_output_chars": self.max_llm_output_chars,
            "agent_exposed": self.agent_exposed,
            "atomic_id": self.capability_id(),
            "kind": self.capability_kind(),
            "domain": self.capability_domain(),
            "skills": list(self.capability_skills()),
            "agent_description": self.agent_description(),
            "invokable": self.handler is not None,
            "agent_available": self.handler is not None or self.transport == "stream",
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
        }


@dataclass(frozen=True)
class SkillDefinition:
    id: str
    name: str
    description: str
    domains: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "domains": list(self.domains),
        }


_SKILL_DEFINITIONS: tuple[SkillDefinition, ...] = (
    SkillDefinition("auto", "自动识别", "根据本轮目标动态装载最小原子能力集", ()),
    SkillDefinition("logs", "日志分析", "日志定位、时间线、语义、证据与案例分析", ("context", "environment", "log", "timeline", "event", "semantic", "case", "data", "navigation", "topology")),
    SkillDefinition("deployment", "环境部署", "环境检查、部署参数、预览、确认和执行", ("context", "environment", "deployment", "navigation")),
    SkillDefinition("environment", "环境状态", "环境拓扑、在线状态、版本与资源检查", ("context", "environment", "navigation", "topology")),
    SkillDefinition("atlog", "自动化用例", "ATLog、event/debug、断言、证据与根因分析", ("context", "atlog", "event", "log", "timeline", "case", "data", "semantic", "environment", "navigation", "topology")),
    SkillDefinition("data", "数据提取", "从真实日志生成/复用数据采集能力并可视化", ("context", "data", "log", "timeline", "semantic", "environment", "navigation")),
)
_SKILL_MAP = {item.id: item for item in _SKILL_DEFINITIONS}

_CATEGORY_DOMAIN = {
    "基础能力": "context",
    "环境": "environment",
    "环境操作": "environment",
    "日志发现": "log",
    "日志检索": "log",
    "日志分析": "log",
    "日志语义": "semantic",
    "日志策略": "log_skill",
    "语义分析": "semantic",
    "事件关联": "timeline",
    "拓扑关联": "topology",
    "知识检索": "case",
    "自动化用例": "atlog",
    "数据提取": "data",
    "部署": "deployment",
    "部署操作": "deployment",
    "界面操作": "navigation",
}

_DOMAIN_SKILLS: dict[str, tuple[str, ...]] = {
    "context": ("logs", "deployment", "environment", "atlog", "data"),
    "environment": ("logs", "deployment", "environment", "atlog", "data"),
    "log": ("logs", "atlog", "data"),
    "log_skill": ("logs", "atlog"),
    "timeline": ("logs", "atlog", "data"),
    "event": ("logs", "atlog"),
    "semantic": ("logs", "atlog", "data"),
    "data": ("logs", "atlog", "data"),
    "case": ("logs", "atlog"),
    "topology": ("logs", "environment", "atlog"),
    "atlog": ("atlog",),
    "deployment": ("deployment",),
    "navigation": ("logs", "deployment", "environment", "atlog", "data"),
}

_DOMAIN_OVERRIDES = {
    "get_current_time": "context",
    "query_atlog_event": "event",
    "resolve_environment_event": "event",
    "query_atlog_logs": "atlog",
    "analyze_atlog_case": "atlog",
    "build_timeline": "timeline",
    "find_event_clusters": "timeline",
    "extract_process_events": "timeline",
    "get_related_modules": "topology",
    "match_cases": "case",
    "list_log_semantic_rules": "semantic",
    "set_log_semantic_labels": "semantic",
    "create_log_semantic_rule": "semantic",
    "bulk_generate_log_rules": "semantic",
    "ask_user_choice": "context",
    "create_log_anomaly_rule": "semantic",
    "list_data_extraction_rules": "data",
    "create_data_extraction_capability": "data",
    "run_data_extraction": "data",
    "open_log_rule_settings": "semantic",
    "open_workspace_page": "navigation",
    "open_environment_page": "environment",
    "open_log_locator": "log",
    "list_log_query_skills": "log_skill",
    "match_log_query_skills": "log_skill",
    "create_log_query_skill": "log_skill",
    "query_log_query_skill_step": "log_skill",
}

_SKILL_OVERRIDES: dict[str, tuple[str, ...]] = {
    "open_environment_page": ("logs", "deployment", "environment", "data", "atlog"),
    "open_log_locator": ("logs", "data", "atlog"),
    "list_log_query_skills": ("logs", "atlog"),
    "match_log_query_skills": ("logs", "atlog"),
    "create_log_query_skill": ("logs", "atlog"),
    "query_log_query_skill_step": ("logs", "atlog"),
    "open_log_rule_settings": ("logs", "data"),
    "refresh_environment": ("environment", "deployment"),
    "get_environment_runtime_status": ("environment", "deployment"),
    "query_environment_version": ("environment", "deployment"),
}

_ATOMIC_ID_OVERRIDES = {
    "get_current_time": "context.now",
    "find_environment": "environment.resolve",
    "list_environments": "environment.list",
    "get_environment_info": "environment.get",
    "get_environment_runtime_status": "environment.status",
    "query_environment_version": "environment.version",
    "refresh_environment": "environment.refresh",
    "resolve_environment_component": "log.resolve_component",
    "query_environment_logs": "log.read_evidence",
    "list_log_query_skills": "log_skill.list",
    "match_log_query_skills": "log_skill.match",
    "create_log_query_skill": "log_skill.create",
    "query_log_query_skill_step": "log_skill.query_step",
    "get_log_catalog": "log.catalog",
    "get_log_time_range": "log.time_range",
    "search_errors": "log.search_errors",
    "search_keyword": "log.search_keyword",
    "get_log_context": "log.read_context",
    "build_timeline": "timeline.build",
    "find_event_clusters": "timeline.find_clusters",
    "extract_process_events": "timeline.process_events",
    "get_related_modules": "topology.related_modules",
    "match_cases": "case.match",
    "query_atlog_event": "event.query",
    "resolve_environment_event": "event.resolve_definition",
    "query_atlog_logs": "atlog.read_logs",
    "analyze_atlog_case": "atlog.analyze_case",
    "list_log_semantic_rules": "semantic.list_rules",
    "set_log_semantic_labels": "semantic.set_labels",
    "create_log_semantic_rule": "semantic.create_rule",
    "create_log_anomaly_rule": "semantic.create_anomaly_rule",
    "list_data_extraction_rules": "data.list_capabilities",
    "create_data_extraction_capability": "data.create_capability",
    "run_data_extraction": "data.extract",
    "open_log_rule_settings": "navigation.open_log_rules",
    "open_workspace_page": "navigation.open_page",
    "open_environment_page": "environment.open",
    "open_log_locator": "log.open",
    "get_deployment_defaults": "deployment.defaults",
    "preview_deployment": "deployment.preview",
    "list_environment_deployments": "deployment.list",
    "get_latest_deployment": "deployment.latest",
    "get_deployment_detail": "deployment.detail",
    "get_active_deployments": "deployment.active",
    "start_environment_deployment": "deployment.start",
    "stop_environment_deployment": "deployment.stop",
    "retry_environment_deployment_step": "deployment.retry_step",
    "ensure_deployment_ssh_trust": "deployment.ensure_ssh_trust",
    "sync_deployment_time": "deployment.sync_time",
}


def _domain_for(tool: ToolDefinition) -> str:
    if tool.id in _DOMAIN_OVERRIDES:
        return _DOMAIN_OVERRIDES[tool.id]
    if "deployment" in tool.id:
        return "deployment"
    return _CATEGORY_DOMAIN.get(tool.category, "general")


def _kind_for(tool: ToolDefinition) -> str:
    if tool.id == "get_current_time":
        return "context"
    if not tool.read_only:
        return "mutation"
    if tool.id.startswith("open_"):
        return "navigation"
    if tool.id.startswith(("set_", "run_", "create_", "control_")):
        return "action"
    return "query"


def _skills_for(tool: ToolDefinition) -> tuple[str, ...]:
    if tool.id in _SKILL_OVERRIDES:
        return _SKILL_OVERRIDES[tool.id]
    return _DOMAIN_SKILLS.get(tool.capability_domain(), ())


def _atomic_id_for(tool_id: str, domain: str) -> str:
    if tool_id in _ATOMIC_ID_OVERRIDES:
        return _ATOMIC_ID_OVERRIDES[tool_id]
    value = str(tool_id or "").strip()
    prefixes = ("get_", "list_", "find_", "query_", "resolve_", "build_", "search_", "recognize_", "extract_", "analyze_", "match_")
    verb = value
    for prefix in prefixes:
        if value.startswith(prefix):
            verb = value[len(prefix):]
            break
    return f"{domain}.{verb}" if domain else value


def list_skills() -> list[dict[str, Any]]:
    return [item.as_dict() for item in _SKILL_DEFINITIONS]


def get_skill(skill_id: str) -> SkillDefinition | None:
    return _SKILL_MAP.get(str(skill_id or "").strip().casefold())


def normalize_skill_id(skill_id: str | None) -> str:
    value = str(skill_id or "auto").strip().casefold()
    return value if value in _SKILL_MAP else "auto"


def _object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


_ENV_ID = {"type": "integer", "minimum": 1, "description": "TraceLens 环境 ID"}
_FM_TARGETS = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "subsystem": {"type": "string"},
            "fm": {"type": "string"},
            "kind": {"type": "string", "enum": ["normal", "executor"]},
        },
        "required": ["subsystem", "fm"],
    },
}

_LOG_ENTRIES = {
    "type": "array",
    "description": "结构化日志对象数组；支持 timestamp/timestamp_ns、level/severity、message/raw、module/component、source_file、line_number 等字段。",
    "items": {"type": "object"},
    "maxItems": 50000,
}

TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        id="get_current_time",
        name="获取当前时间",
        description="读取 TraceLens 服务运行时的真实当前日期、时间和时区。涉及‘现在/当前/今天/刚才/最近几分钟/今晚’等相对时间时，使用此工具而不是模型自身时间。",
        category="基础能力",
        handler=services.get_current_time,
        input_schema=_object_schema({
            "timezone": {"type": "string", "default": "", "description": "可选 IANA 时区，例如 Asia/Shanghai；省略时使用服务配置时区。"},
        }),
        tags=("assistant", "runtime", "time"),
        use_when="用户的问题依赖真实当前时间，或者需要把相对时间转换成绝对查询时间窗时。",
        do_not_use_when="用户已经给出完整绝对时间且无需知道现在几点时，不必额外调用。",
        validation_rules=("不得使用模型训练时间或系统提示中的静态日期替代本工具结果",),
        max_llm_output_chars=800,
        implementation="apps.tooling.services.get_current_time",
    ),
    ToolDefinition(
        id="get_environment_info",
        name="获取环境信息",
        description="读取指定环境的上位机、下位机、DHH、版本与环境状态等基础信息。",
        category="环境",
        handler=services.get_environment_info,
        input_schema=_object_schema({"environment_id": _ENV_ID}, ["environment_id"]),
        tags=("environment", "topology", "version"),
        max_llm_output_chars=5000,
        implementation="apps.tooling.services.get_environment_info",
    ),
    ToolDefinition(
        id="get_log_catalog",
        name="获取日志目录",
        description="扫描或读取指定环境当前可用的日志子系统、模块、日志类型与文件目录。",
        category="日志发现",
        handler=services.get_log_catalog,
        input_schema=_object_schema(
            {
                "environment_id": _ENV_ID,
                "source_categories": {"type": "array", "items": {"type": "string"}, "default": []},
                "refresh": {"type": "boolean", "default": False, "description": "是否强制重扫远端目录"},
            },
            ["environment_id"],
        ),
        tags=("catalog", "discovery", "logs"),
        implementation="apps.tooling.services.get_log_catalog",
        agent_exposed=False,
    ),
    ToolDefinition(
        id="resolve_environment_event",
        name="解析环境事件码归属",
        description="根据当前环境的 DisplayCode（优先）、CodeString 或 Code 查询环境刷新时缓存的事件配置，确定事件所属子系统、组件代码仓、配置文件和已确认根因目标，不进行远端扫描。",
        category="事件关联",
        handler=services.resolve_environment_event,
        input_schema=_object_schema(
            {
                "environment_id": {"type": ["integer", "null"], "minimum": 1},
                "display_code": {"type": "string", "default": ""},
                "code": {"type": "string", "default": ""},
                "code_string": {"type": "string", "default": ""},
            },
            [],
        ),
        tags=("event", "display-code", "component", "subsystem", "database"),
        use_when="已经从真实 event.log 证据中拿到 DisplayCode、CodeString 或 Code，需要解析其所属子系统/组件时使用。",
        do_not_use_when="不要把自动化用例编号、用例名称、模块名、普通错误文本当成 DisplayCode/CodeString/Code；没有真实事件码时不要调用。",
        implementation="apps.tooling.services.resolve_environment_event",
    ),
    ToolDefinition(
        id="resolve_log_components",
        name="解析事件组件到日志模块",
        description="优先查询“子系统模块表”中 Event组件配置的目标模块；未命中时再把 event.log 组件名与数据库子系统/模块配置、当前 ATLog debug 目录做交叉映射，返回真实 subsystem/module 候选。",
        category="日志发现",
        handler=services.resolve_log_components,
        input_schema=_object_schema(
            {
                "event_components": {"type": "array", "items": {"type": "string"}, "default": []},
                "event_display_codes": {"type": "array", "items": {"type": "string"}, "default": []},
                "environment_id": {"type": ["integer", "null"], "minimum": 1},
                "event_text": {"type": "string", "default": ""},
                "available_targets": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "subsystem": {"type": "string"},
                            "module": {"type": "string"},
                        },
                        "required": ["subsystem", "module"],
                    },
                    "default": [],
                },
            },
            ["event_components", "available_targets"],
        ),
        tags=("component", "module", "mapping", "database"),
        implementation="apps.tooling.services.resolve_log_components",
        agent_exposed=False,
    ),
    ToolDefinition(
        id="build_log_plan",
        name="生成日志检索计划",
        description="根据时间范围、日志类型和模块选择解析实际需要读取的远端日志文件，不读取正文。",
        category="日志检索",
        handler=services.build_log_plan_tool,
        input_schema=_object_schema(
            {
                "environment_id": _ENV_ID,
                "start_time": {"type": "string", "format": "date-time"},
                "end_time": {"type": "string", "format": "date-time"},
                "source_categories": {"type": "array", "items": {"type": "string"}, "default": []},
                "subsystems": {"type": "array", "items": {"type": "string"}, "default": []},
                "fms": {"type": "array", "items": {"type": "string"}, "default": []},
                "fm_targets": _FM_TARGETS,
                "keyword": {"type": "string", "default": ""},
            },
            ["environment_id", "start_time", "end_time"],
        ),
        tags=("plan", "time-range", "module"),
        implementation="apps.tooling.services.build_log_plan_tool",
        agent_exposed=False,
    ),
    ToolDefinition(
        id="query_logs",
        name="查询日志",
        description="按环境、时间范围、日志类型和模块流式检索日志正文；当前 UI 日志定位使用的核心读取能力。",
        category="日志检索",
        handler=None,
        # Stream-transport UI endpoint: the agent has no way to invoke it.
        agent_exposed=False,
        input_schema=_object_schema(
            {
                "environment_id": _ENV_ID,
                "start_time": {"type": "string", "format": "date-time"},
                "end_time": {"type": "string", "format": "date-time"},
                "source_categories": {"type": "array", "items": {"type": "string"}, "default": []},
                "fm_targets": _FM_TARGETS,
                "keyword": {"type": "string", "default": ""},
            },
            ["environment_id", "start_time", "end_time"],
        ),
        tags=("stream", "query", "logs"),
        transport="stream",
        endpoint_template="/api/environment-logs/{environment_id}/stream/",
        implementation="apps.logsources.services.remote_logs.stream_log_window",
    ),
    ToolDefinition(
        id="get_search_progress",
        name="获取检索进度",
        description="读取一次日志检索任务的当前阶段、完成比例、命中文件与结果数量。",
        category="日志检索",
        handler=services.get_search_progress,
        input_schema=_object_schema(
            {
                "environment_id": _ENV_ID,
                "operation_id": {"type": "string", "minLength": 1},
            },
            ["environment_id", "operation_id"],
        ),
        tags=("progress", "operation"),
        implementation="apps.tooling.services.get_search_progress",
        agent_exposed=False,
    ),
    ToolDefinition(
        id="recognize_semantic_source",
        name="识别单条日志源码语义",
        description="根据日志中的源码文件、行号、函数和模块上下文解析对应源码语义。",
        category="语义分析",
        handler=services.recognize_semantic_source,
        input_schema=_object_schema(
            {
                "environment_id": _ENV_ID,
                "source_file": {"type": "string"},
                "source_line": {"type": ["integer", "null"], "minimum": 1},
                "function_name": {"type": "string"},
                "fm_targets": {"type": "array", "items": {"type": "object"}, "minItems": 1},
            },
            ["environment_id", "source_file", "fm_targets"],
        ),
        tags=("semantic", "source", "code"),
        implementation="apps.tooling.services.recognize_semantic_source",
        agent_exposed=False,
    ),
    ToolDefinition(
        id="recognize_semantic_sources_batch",
        name="批量识别日志源码语义",
        description="批量解析多条日志记录对应的源码语义，减少重复源码查询。",
        category="语义分析",
        handler=services.recognize_semantic_sources_batch,
        input_schema=_object_schema(
            {
                "environment_id": _ENV_ID,
                "items": {"type": "array", "items": {"type": "object"}, "minItems": 1},
                "fm_targets": {"type": "array", "items": {"type": "object"}, "minItems": 1},
            },
            ["environment_id", "items", "fm_targets"],
        ),
        tags=("semantic", "batch", "source"),
        implementation="apps.tooling.services.recognize_semantic_sources_batch",
        agent_exposed=False,
    ),
    ToolDefinition(
        id="get_log_time_range",
        name="获取日志时间范围",
        description="从结构化日志中计算最早/最晚时间、有效时间戳数量与总跨度，供 Agent 判断下一步检索窗口。",
        category="日志分析",
        handler=services.get_log_time_range,
        input_schema=_object_schema({"entries": _LOG_ENTRIES}, ["entries"]),
        tags=("time-range", "bounds", "local-log"),
        implementation="apps.tooling.services.get_log_time_range",
        agent_exposed=False,
    ),
    ToolDefinition(
        id="search_errors",
        name="检索错误日志",
        description="按 ERROR/FATAL/CRITICAL 等级从已获取日志中确定性筛选异常记录，并返回可继续获取上下文的原始下标。",
        category="日志分析",
        handler=services.search_errors,
        input_schema=_object_schema(
            {
                "entries": _LOG_ENTRIES,
                "levels": {"type": "array", "items": {"type": "string"}, "default": ["ERROR", "FATAL", "CRITICAL"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 5000, "default": 500},
            },
            ["entries"],
        ),
        tags=("error", "filter", "evidence"),
        implementation="apps.tooling.services.search_errors",
        agent_exposed=False,
    ),
    ToolDefinition(
        id="search_keyword",
        name="检索日志关键字",
        description="在日志正文、模块和函数信息中检索关键字，支持大小写与整词匹配。",
        category="日志分析",
        handler=services.search_keyword,
        input_schema=_object_schema(
            {
                "entries": _LOG_ENTRIES,
                "keyword": {"type": "string", "minLength": 1},
                "case_sensitive": {"type": "boolean", "default": False},
                "whole_word": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "minimum": 1, "maximum": 5000, "default": 500},
            },
            ["entries", "keyword"],
        ),
        tags=("keyword", "search", "filter"),
        implementation="apps.tooling.services.search_keyword",
        agent_exposed=False,
    ),
    ToolDefinition(
        id="get_log_context",
        name="获取日志上下文",
        description="围绕指定日志下标获取前后若干行，适合 Agent 在发现 ERROR 后继续检查故障前因与后续连锁异常。",
        category="日志分析",
        handler=services.get_log_context,
        input_schema=_object_schema(
            {
                "entries": _LOG_ENTRIES,
                "anchor_index": {"type": "integer", "minimum": 0},
                "before_lines": {"type": "integer", "minimum": 0, "maximum": 500, "default": 20},
                "after_lines": {"type": "integer", "minimum": 0, "maximum": 500, "default": 20},
            },
            ["entries", "anchor_index"],
        ),
        tags=("context", "evidence", "root-cause"),
        implementation="apps.tooling.services.get_log_context",
        agent_exposed=False,
    ),
    ToolDefinition(
        id="build_timeline",
        name="构建结构化时间线",
        description="把多模块日志按时间排序并归一化为事件时间线，可选按日志等级过滤。",
        category="事件关联",
        handler=services.build_timeline,
        input_schema=_object_schema(
            {
                "entries": _LOG_ENTRIES,
                "levels": {"type": "array", "items": {"type": "string"}, "default": []},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20000, "default": 5000},
            },
            ["entries"],
        ),
        tags=("timeline", "correlation", "multi-module"),
        implementation="apps.tooling.services.build_timeline",
        agent_exposed=False,
    ),
    ToolDefinition(
        id="find_event_clusters",
        name="查找异常事件簇",
        description="按时间间隔聚合短时间内密集出现的异常事件，帮助 Agent 优先聚焦故障爆发窗口。",
        category="事件关联",
        handler=services.find_event_clusters,
        input_schema=_object_schema(
            {
                "entries": _LOG_ENTRIES,
                "levels": {"type": "array", "items": {"type": "string"}, "default": ["ERROR", "FATAL", "CRITICAL"]},
                "max_gap_ms": {"type": "number", "minimum": 0.1, "maximum": 60000, "default": 1000},
                "min_events": {"type": "integer", "minimum": 2, "maximum": 100, "default": 2},
            },
            ["entries"],
        ),
        tags=("cluster", "error", "timeline"),
        implementation="apps.tooling.services.find_event_clusters",
        agent_exposed=False,
    ),
    ToolDefinition(
        id="extract_process_events",
        name="提取进程生命周期事件",
        description="从日志中提取启动、停止、重启、崩溃、Kill 等进程生命周期事件，供根因链分析使用。",
        category="事件关联",
        handler=services.extract_process_events,
        input_schema=_object_schema({"entries": _LOG_ENTRIES}, ["entries"]),
        tags=("process", "restart", "crash", "lifecycle"),
        implementation="apps.tooling.services.extract_process_events",
        agent_exposed=False,
    ),
    ToolDefinition(
        id="get_related_modules",
        name="获取关联模块",
        description="读取日志模块配置中的依赖与被依赖关系，为 Agent 决定下一步跨模块检索方向。",
        category="拓扑关联",
        handler=services.get_related_modules,
        input_schema=_object_schema(
            {
                "module": {"type": "string", "minLength": 1},
                "subsystem": {"type": "string", "default": ""},
            },
            ["module"],
        ),
        tags=("dependency", "module", "topology"),
        implementation="apps.tooling.services.get_related_modules",
        agent_exposed=False,
    ),
    ToolDefinition(
        id="match_cases",
        name="检索历史异常案例",
        description="按已提取的稳定症状/错误文本/模块一次性检索历史异常案例，仅返回相似度达到阈值的 Top 候选，供 Agent 做辅助验证。",
        category="知识检索",
        handler=services.match_cases,
        input_schema=_object_schema(
            {
                "query": {"type": "string", "default": ""},
                "module": {"type": "string", "default": ""},
                "environment_id": {"type": ["integer", "null"], "minimum": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 3},
                "min_score": {"type": "number", "minimum": 0, "maximum": 100, "default": 70},
            },
            [],
        ),
        tags=("case", "knowledge", "root-cause"),
        use_when="已经从失败断言、event 或目标组件日志提取出稳定症状/错误文本/模块后，用这些事实检索历史案例做辅助验证。",
        do_not_use_when="不要只拿测试用例编号做第一步检索；历史案例不能替代当前用例报告和运行日志证据。",
        implementation="apps.tooling.services.match_cases",
    ),
    ToolDefinition(
        id="analyze_atlog_case",
        name="分析 ATLog 自动化用例",
        description="输入一条 ATLog 用例报告根目录 URL，按 summary_report.xml → pytest XML → xytest.log 的确定性顺序提取通过/失败、断言、代码位置、调用链和默认 event.log 时间窗。",
        category="自动化用例",
        handler=atlog_services.analyze_case_tool,
        input_schema=_object_schema(
            {
                "url": {"type": "string", "minLength": 1, "description": "ATLog 用例报告根目录 URL"},
            },
            ["url"],
        ),
        tags=("atlog", "pytest", "case", "failure"),
        use_when="当前页面没有完整失败报告事实，或需要重新读取 summary_report/pytest HTML/XML/xytest 来补齐断言、失败位置和时间窗时使用；URL 可由 Core Kernel 从当前用例上下文自动注入。",
        do_not_use_when="当前页面已经提供足够的 report_facts 时，不要仅为了重复确认同一断言而再次读取。",
        implementation="apps.atlog.services.analyze_case",
    ),
    ToolDefinition(
        id="query_atlog_logs",
        name="定向查询 ATLog debug 日志",
        description="按用例 URL、失败时间窗和精确 subsystem/module 目标读取 full_logs/log/debug 下的日志；可直接按 TraceLens 异常规则过滤，只返回规则命中的关键日志节点。",
        category="自动化用例",
        handler=diagnostic_retrieval.query_debug,
        input_schema=_object_schema(
            {
                "url": {"type": "string", "minLength": 1},
                "start_time": {"type": "string"},
                "end_time": {"type": "string"},
                "targets": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "subsystem": {"type": "string"},
                            "module": {"type": "string"},
                        },
                        "required": ["subsystem", "module"],
                    },
                    "default": [],
                },
                "keyword": {"type": "string", "default": ""},
                "anomaly_rules": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "keyword": {"type": "string"},
                            "case_sensitive": {"type": "boolean", "default": False},
                            "whole_word": {"type": "boolean", "default": False},
                            "enabled": {"type": "boolean", "default": True},
                        },
                        "required": ["keyword"],
                    },
                    "default": [],
                },
                "include_event": {"type": "boolean", "default": True},
                "max_lines": {"type": "integer", "minimum": 100, "maximum": 12000, "default": 5000},
            },
            ["url", "targets"],
        ),
        tags=("atlog", "debug", "targeted", "time-range"),
        use_when="当前页面运行证据不足，且已经从 event/组件映射/用例语义得到明确 subsystem/module 目标后，定向补读取对应 debug/executor 证据。",
        do_not_use_when="不要在没有目标模块时读取整份日志；页面已提供 runtime_evidence 且足以回答时不要重复查询。",
        implementation="apps.atlog.services.query_case_logs",
    ),
    ToolDefinition(
        id="query_atlog_event",
        name="查询 ATLog event.log",
        description="按用例 URL 和失败时间窗读取 event.log，支持组件、事件级别和关键字过滤；大文件优先使用 HTTP Range 定位时间窗口。",
        category="自动化用例",
        handler=diagnostic_retrieval.query_events,
        input_schema=_object_schema(
            {
                "url": {"type": "string", "minLength": 1},
                "start_time": {"type": "string"},
                "end_time": {"type": "string"},
                "modules": {"type": "array", "items": {"type": "string"}, "default": []},
                "levels": {"type": "array", "items": {"type": "string"}, "default": []},
                "keyword": {"type": "string", "default": ""},
                "max_lines": {"type": "integer", "minimum": 100, "maximum": 50000, "default": 8000},
            },
            ["url"],
        ),
        tags=("atlog", "event", "time-range", "root-cause"),
        use_when="页面尚未提供 event_evidence，或需要围绕失败时间窗补充 event.log 触发点、组件名和真实 DisplayCode 时使用。",
        do_not_use_when="页面已经有足够 event_evidence 时不要重复读取；不要用用例编号代替 event DisplayCode。",
        implementation="apps.atlog.services.query_event_log",
    ),


    ToolDefinition(
        id="list_environments",
        name="列出环境",
        description="按名称、说明或上位机地址列出 TraceLens 环境，供助手解析用户所指环境。",
        category="环境操作",
        handler=services.list_environments,
        input_schema=_object_schema({"query": {"type": "string", "default": ""}, "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}}),
        tags=("assistant", "environment", "search"),
        use_when="用户询问哪些/哪套环境满足某个条件，或需要先取得环境集合再逐个比较状态/版本时调用。",
        do_not_use_when="用户已经明确指定单个 environment_id，且问题只针对该环境。",
        max_llm_output_chars=9000,
        implementation="apps.tooling.services.list_environments",
        agent_exposed=True,
    ),
    ToolDefinition(
        id="find_environment",
        name="查找环境",
        description="按环境名、上位机 IP 或关键字定位环境；歧义时返回候选，不擅自选择。",
        use_when="用户用环境名称、IP、简称指代环境但当前没有可靠 environment_id 时，优先调用本工具解析。",
        do_not_use_when="上下文中已经有明确且仍有效的 environment_id，且用户没有切换环境。",
        validation_rules=("query 必须是用户明确给出的环境名称/IP/关键字", "返回多个候选时必须停止并让用户确认，不能自行挑选"),
        max_llm_output_chars=5000,
        category="环境操作",
        handler=services.find_environment,
        input_schema=_object_schema({"query": {"type": "string", "minLength": 1}}, ["query"]),
        tags=("assistant", "environment", "resolve"),
        implementation="apps.tooling.services.find_environment",
    ),
    ToolDefinition(
        id="get_environment_runtime_status",
        name="获取环境运行状态",
        description="读取指定环境上下位机、DHH 与模拟器运行状态，可选择强制刷新。",
        category="环境操作",
        handler=services.get_environment_runtime_status,
        input_schema=_object_schema({"environment_id": _ENV_ID, "refresh": {"type": "boolean", "default": False}}, ["environment_id"]),
        tags=("assistant", "runtime", "environment"),
        max_llm_output_chars=6000,
        implementation="apps.tooling.services.get_environment_runtime_status",
    ),
    ToolDefinition(
        id="refresh_environment",
        name="刷新环境拓扑",
        description="重新读取环境 stations.xml 并刷新上下位机拓扑。",
        category="环境操作",
        handler=services.refresh_environment,
        input_schema=_object_schema({"environment_id": _ENV_ID}, ["environment_id"]),
        tags=("assistant", "environment", "discovery"),
        read_only=False,
        risk_level="low_write",
        implementation="apps.tooling.services.refresh_environment",
    ),
    ToolDefinition(
        id="query_environment_version",
        name="查询环境版本",
        description="实时查询指定环境上下位机软件版本并返回版本一致性信息。",
        category="环境操作",
        handler=services.query_environment_version,
        input_schema=_object_schema({"environment_id": _ENV_ID}, ["environment_id"]),
        tags=("assistant", "environment", "version"),
        max_llm_output_chars=5000,
        implementation="apps.tooling.services.query_environment_version",
    ),
    ToolDefinition(
        id="get_deployment_defaults",
        name="获取部署默认参数",
        description="读取当前环境部署默认参数、GPB 列表、模式、安装端口和 DISPLAY。",
        category="部署",
        handler=services.get_deployment_defaults,
        input_schema=_object_schema({"environment_id": _ENV_ID}, ["environment_id"]),
        tags=("assistant", "deployment", "parameters"),
        max_llm_output_chars=6000,
        implementation="apps.tooling.services.get_deployment_defaults",
    ),
    ToolDefinition(
        id="preview_deployment",
        name="预览部署",
        description="根据环境和用户指定的部署参数生成部署命令预览，不启动部署。",
        use_when="用户明确只想查看部署命令/参数预览，或需要在不创建确认卡的情况下做 dry-run 校验。",
        do_not_use_when="用户已经明确要求实际部署；start_environment_deployment 在生成确认卡前会自动完成同一套预览校验。",
        validation_rules=("environment_id 必须明确", "只覆盖用户明确指定的参数，其余沿用环境默认值", "预览本身不产生部署副作用"),
        max_llm_output_chars=9000,
        category="部署",
        handler=services.preview_deployment,
        input_schema=_object_schema({
            "environment_id": _ENV_ID,
            "parameters": {"type": "object", "description": "部署参数覆盖项", "additionalProperties": True},
            "target_version": {"type": "string"},
            "simulation_mode": {"type": "string", "enum": ["sim0_sil", "sim2", "sim0_real"]},
            "include_sdk": {"type": "boolean"},
            "precheck_stop_lower": {"type": "boolean"},
            "gpb_ips": {"type": "array", "items": {"type": "string"}},
            "include_dhh": {"type": "boolean"},
            "refresh_defaults": {"type": "boolean", "default": False},
        }, ["environment_id"]),
        tags=("assistant", "deployment", "preview"),
        implementation="apps.tooling.services.preview_deployment",
    ),
    ToolDefinition(
        id="list_environment_deployments",
        name="查看部署历史",
        description="列出指定环境最近的部署任务。",
        category="部署",
        handler=services.list_environment_deployments,
        input_schema=_object_schema({"environment_id": _ENV_ID, "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}}, ["environment_id"]),
        tags=("assistant", "deployment", "history"),
        max_llm_output_chars=7000,
        implementation="apps.tooling.services.list_environment_deployments",
    ),
    ToolDefinition(
        id="get_latest_deployment",
        name="获取最近部署",
        description="读取指定环境最近一条部署任务状态。",
        category="部署",
        handler=services.get_latest_deployment,
        input_schema=_object_schema({"environment_id": _ENV_ID}, ["environment_id"]),
        tags=("assistant", "deployment", "status"),
        implementation="apps.tooling.services.get_latest_deployment",
    ),
    ToolDefinition(
        id="get_deployment_detail",
        name="获取部署详情",
        description="读取部署任务步骤、状态和指定步骤日志。",
        category="部署",
        handler=services.get_deployment_detail,
        input_schema=_object_schema({"environment_id": _ENV_ID, "deployment_id": {"type": "integer", "minimum": 1}, "step_key": {"type": "string", "default": ""}}, ["environment_id", "deployment_id"]),
        tags=("assistant", "deployment", "detail", "logs"),
        max_llm_output_chars=9000,
        implementation="apps.tooling.services.get_deployment_detail",
    ),
    ToolDefinition(
        id="get_active_deployments",
        name="获取进行中部署",
        description="读取当前等待、执行中或停止中的部署任务。",
        category="部署",
        handler=services.get_active_deployments,
        input_schema=_object_schema({"environment_id": {"type": ["integer", "null"], "minimum": 1}}),
        tags=("assistant", "deployment", "active"),
        max_llm_output_chars=6000,
        implementation="apps.tooling.services.get_active_deployments",
    ),
    ToolDefinition(
        id="start_environment_deployment",
        name="启动环境部署",
        description="按环境当前默认值和用户给定覆盖参数准备部署；生成确认卡前自动执行预览校验，用户确认后创建部署任务。",
        use_when="用户明确要求实际开始/重新部署。直接传 environment_id 和用户明确覆盖项即可，不需要先调用默认参数/预览 Tool。",
        do_not_use_when="用户只是咨询、查看部署参数/命令，或环境、版本、模式仍有歧义。",
        validation_rules=("必须有明确 environment_id", "生成确认卡前由后端自动预览校验", "未经人工确认不得执行", "确认后只能执行确认卡片中同一组参数"),
        max_llm_output_chars=8000,
        category="部署操作",
        handler=services.start_environment_deployment,
        input_schema=_object_schema({
            "environment_id": _ENV_ID,
            "parameters": {"type": "object", "description": "部署参数覆盖项", "additionalProperties": True},
            "target_version": {"type": "string"},
            "simulation_mode": {"type": "string", "enum": ["sim0_sil", "sim2", "sim0_real"]},
            "include_sdk": {"type": "boolean"},
            "precheck_stop_lower": {"type": "boolean"},
            "gpb_ips": {"type": "array", "items": {"type": "string"}},
            "include_dhh": {"type": "boolean"},
            "scheduled_at": {"type": ["string", "null"]},
        }, ["environment_id"]),
        tags=("assistant", "deployment", "write", "confirm"),
        read_only=False,
        risk_level="high",
        implementation="apps.tooling.services.start_environment_deployment",
    ),
    ToolDefinition(
        id="stop_environment_deployment",
        name="停止环境部署",
        description="请求停止指定部署任务。该操作必须经用户确认。",
        use_when="用户明确要求停止某个正在等待/执行/停止中的部署任务。",
        do_not_use_when="deployment_id 不明确，或用户只是询问部署状态。",
        validation_rules=("environment_id 和 deployment_id 必须明确", "未经人工确认不得执行"),
        max_llm_output_chars=6000,
        category="部署操作",
        handler=services.stop_environment_deployment,
        input_schema=_object_schema({"environment_id": _ENV_ID, "deployment_id": {"type": "integer", "minimum": 1}}, ["environment_id", "deployment_id"]),
        tags=("assistant", "deployment", "write", "confirm"),
        read_only=False,
        risk_level="high",
        implementation="apps.tooling.services.stop_environment_deployment",
    ),
    ToolDefinition(
        id="retry_environment_deployment_step",
        name="重试部署步骤",
        description="从指定部署步骤发起复制重试。该操作必须经用户确认。",
        category="部署操作",
        handler=services.retry_environment_deployment_step,
        input_schema=_object_schema({"environment_id": _ENV_ID, "deployment_id": {"type": "integer", "minimum": 1}, "step_key": {"type": "string", "minLength": 1}}, ["environment_id", "deployment_id", "step_key"]),
        tags=("assistant", "deployment", "retry", "confirm"),
        read_only=False,
        risk_level="high",
        implementation="apps.tooling.services.retry_environment_deployment_step",
    ),
    ToolDefinition(
        id="ensure_deployment_ssh_trust",
        name="修复部署 SSH 互信",
        description="为选定 GPB/DHH 修复部署所需 SSH 互信。该操作会修改远端授权配置，必须确认。",
        category="部署操作",
        handler=services.ensure_deployment_ssh_trust,
        input_schema=_object_schema({"environment_id": _ENV_ID, "gpb_ips": {"type": "array", "items": {"type": "string"}}, "include_dhh": {"type": "boolean", "default": False}, "dhh_ip": {"type": "string", "default": ""}, "dhh_user": {"type": "string", "default": "root"}}, ["environment_id", "gpb_ips"]),
        tags=("assistant", "deployment", "ssh", "confirm"),
        read_only=False,
        risk_level="high",
        implementation="apps.tooling.services.ensure_deployment_ssh_trust",
    ),
    ToolDefinition(
        id="sync_deployment_time",
        name="同步部署目标时间",
        description="同步 GPB/DHH 时间到上位机。该操作会修改远端系统时间，必须确认。",
        category="部署操作",
        handler=services.sync_deployment_time,
        input_schema=_object_schema({"environment_id": _ENV_ID, "gpb_ips": {"type": "array", "items": {"type": "string"}}, "include_dhh": {"type": "boolean", "default": False}, "dhh_ip": {"type": "string", "default": ""}, "dhh_user": {"type": "string", "default": "root"}}, ["environment_id", "gpb_ips"]),
        tags=("assistant", "deployment", "time", "confirm"),
        read_only=False,
        risk_level="high",
        implementation="apps.tooling.services.sync_deployment_time",
    ),
    ToolDefinition(
        id="resolve_environment_component",
        name="精确解析组件日志目标",
        description="按用户给出的组件名查询 TraceLens 子系统模块表中的 Event组件、目标模块、查询优先级和日志规则。唯一精确匹配可直接使用；多目标或相似候选只返回给用户选择，不自动决定。",
        use_when="用户明确说出组件名，并需要知道它属于哪个子系统/模块，或准备查询该组件日志时。",
        do_not_use_when="用户没有给出组件名。相似候选只能用于展示选择，禁止直接替代为查询目标。查询日志时也可把 component_name 交给 AI 定向读取环境日志，由后者内部执行相同的人选门控。",
        validation_rules=("component_name 必须来自用户输入", "只有唯一 exact_match=true 才能自动继续", "多目标或模糊候选必须 selection_required=true 并让用户选择，禁止擅自选择"),
        max_llm_output_chars=2000,
        category="日志发现",
        handler=services.resolve_environment_component,
        input_schema=_object_schema({
            "component_name": {"type": "string", "minLength": 1, "maxLength": 128},
            "environment_id": _ENV_ID,
        }, ["component_name"]),
        tags=("assistant", "component", "exact", "database"),
        implementation="apps.tooling.services.resolve_environment_component",
    ),
    ToolDefinition(
        id="query_environment_logs",
        name="AI 定向读取环境日志",
        description="按环境、时间和组件定向读取日志证据。若 component_name 唯一精确匹配则自动生成 fm_targets；若存在多个日志目标或只能模糊命中，则只返回候选让用户选择。时间范围仅用于定位日志，不再按分钟限制；后端按页面函数折叠规则聚合并做极限压缩，再按文本大小预算投喂 AI。未指定日志类型时默认只查 debug。",
        use_when="已经明确环境、时间窗和组件名/模块目标，需要直接读取日志证据时。常见组件日志查询应优先直接调用本工具；时间窗大小不作为拒绝条件，按函数折叠后的文本预算控制投喂量。",
        do_not_use_when="环境或时间窗不明确；组件既没有 component_name 也没有 fm_targets。不要为了日志查询先重复调用生成日志检索计划。",
        validation_rules=("未传 source_categories 时强制默认 debug", "component_name 只有唯一精确匹配时才自动生成 fm_targets；多目标/模糊候选必须等待用户选择", "查报错传 errors_only=true，只返回错误证据", "查关键字必须传 keyword，只返回匹配证据", "不按分钟限制日志查询；按页面函数名/START-END边界折叠", "AI 输入只使用函数折叠摘要，并由扫描字节预算和最终上下文字数预算限制"),
        max_llm_output_chars=14000,
        category="日志检索",
        handler=services.query_environment_logs,
        input_schema=_object_schema({
            "environment_id": _ENV_ID,
            "start_time": {"type": "string", "format": "date-time"},
            "end_time": {"type": "string", "format": "date-time"},
            "source_categories": {"type": "array", "items": {"type": "string"}, "default": ["debug"]},
            "component_name": {"type": "string", "default": "", "description": "用户给出的组件名；提供后自动精确解析为 fm_targets。"},
            "subsystems": {"type": "array", "items": {"type": "string"}, "default": []},
            "fms": {"type": "array", "items": {"type": "string"}, "default": []},
            "fm_targets": _FM_TARGETS,
            "keyword": {"type": "string", "default": ""},
            "errors_only": {"type": "boolean", "default": False},
            "max_context_chars": {"type": "integer", "minimum": 2000, "maximum": 30000, "default": 9000, "description": "函数折叠后送入 Agent 的最大文本字符数"},
            "max_scan_bytes": {"type": "integer", "minimum": 262144, "maximum": 64000000, "default": 12000000, "description": "单次最多扫描的原始日志字节数；限制按文本大小而不是时间"},
        }, ["environment_id", "start_time", "end_time"]),
        tags=("assistant", "logs", "evidence", "function-fold", "size-bounded"),
        implementation="apps.tooling.services.query_environment_logs",
    ),
    ToolDefinition(
        id="list_log_query_skills",
        name="读取日志查询 Skill",
        description="读取按子系统维护的补充日志查询策略。Skill 只用于 AI 诊断补证据，不替代普通日志定位的固定快速路径。",
        category="日志策略",
        handler=services.list_log_query_skills,
        input_schema=_object_schema({
            "subsystem": {"type": "string", "default": "", "description": "子系统标识/显示名；建议明确传入以避免跨子系统。"},
            "query": {"type": "string", "default": ""},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 40},
        }),
        tags=("assistant", "logs", "skill", "subsystem"),
        use_when="需要了解某个子系统有哪些补充日志/下层日志检索策略，或准备基于现有 Skill 继续诊断时。",
        do_not_use_when="普通日志定位已经能直接给出充分证据时不要调用；不要跨子系统泛查全部 Skill。",
        validation_rules=("优先传 subsystem", "Skill 仅作为补充检索策略，不改变人工日志查询固定路径"),
        max_llm_output_chars=9000,
        implementation="apps.tooling.services.list_log_query_skills",
    ),
    ToolDefinition(
        id="match_log_query_skills",
        name="匹配子系统日志查询 Skill",
        description="根据当前子系统、模块和已有日志证据匹配可继续下钻的日志查询 Skill。严格按子系统隔离。",
        category="日志策略",
        handler=services.match_log_query_skills,
        input_schema=_object_schema({
            "subsystem": {"type": "string", "minLength": 1},
            "module": {"type": "string", "default": ""},
            "evidence_text": {"type": "string", "default": "", "maxLength": 12000},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 6},
        }, ["subsystem"]),
        tags=("assistant", "logs", "skill", "diagnosis", "subsystem"),
        use_when="标准固定路径没有查到日志，或当前日志只看到上层 timeout/failed 等现象、缺少根因，需要判断该子系统下一步应查哪类下层日志时。",
        do_not_use_when="当前证据已经足够回答；不能用 A 子系统的 Skill 去补查 B 子系统。",
        validation_rules=("subsystem 必填", "模块有明确值时必须传 module", "evidence_text 只放本轮真实日志/工具结果"),
        max_llm_output_chars=10000,
        implementation="apps.tooling.services.match_log_query_skills",
    ),
    ToolDefinition(
        id="create_log_query_skill",
        name="创建日志查询 Skill",
        description="把用户描述的排查经验保存成按子系统隔离的日志查询 Skill。Skill 可声明触发模块/关键字，以及标准模块日志或自定义远端路径的补充检索步骤。",
        category="日志策略",
        handler=services.create_log_query_skill,
        input_schema=_object_schema({
            "skill": {
                "type": "object",
                "properties": {
                    "subsystem": {"type": "string", "description": "必须是已有子系统标识或显示名。"},
                    "subsystem_id": {"type": "integer", "minimum": 1},
                    "name": {"type": "string", "minLength": 1, "maxLength": 128},
                    "enabled": {"type": "boolean", "default": True},
                    "priority": {"type": "integer", "minimum": -1000, "maximum": 1000, "default": 100},
                    "trigger_modules": {"type": "array", "items": {"type": "string"}, "maxItems": 32},
                    "trigger_keywords": {"type": "array", "items": {"type": "string"}, "maxItems": 48},
                    "description": {"type": "string", "maxLength": 4000},
                    "steps": {
                        "type": "array",
                        "maxItems": 8,
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "when": {"type": "string", "enum": ["always", "no_match", "source_empty", "source_not_found", "insufficient_evidence", "keyword_match"], "default": "insufficient_evidence"},
                                "source_type": {"type": "string", "enum": ["standard", "custom_path"], "default": "standard"},
                                "machine_scope": {"type": "string", "enum": ["upper", "lower", "all_lower", "dhh"], "default": "upper"},
                                "source_category": {"type": "string", "default": "debug"},
                                "module": {"type": "string", "default": ""},
                                "path_template": {"type": "string", "default": ""},
                                "file_pattern": {"type": "string", "default": "*.log*"},
                                "keywords": {"type": "array", "items": {"type": "string"}, "maxItems": 24},
                                "time_before_seconds": {"type": "integer", "minimum": 0, "maximum": 600, "default": 5},
                                "time_after_seconds": {"type": "integer", "minimum": 0, "maximum": 600, "default": 5},
                                "note": {"type": "string", "default": ""},
                            },
                            "required": ["name", "source_type", "machine_scope"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["subsystem", "name", "steps"],
                "additionalProperties": False,
            },
        }, ["skill"]),
        tags=("assistant", "logs", "skill", "authoring", "subsystem"),
        use_when="用户明确要求把某个子系统的日志排查经验保存成 Skill，或明确要求新增‘查不到后去哪里查’的规则。",
        do_not_use_when="用户只是咨询排查思路但没有要求保存时，不要擅自创建。",
        validation_rules=("必须绑定一个已有子系统", "自定义路径必须是用户/平台已有规则中明确提供的路径，禁止模型猜路径", "最多 8 个补充步骤"),
        max_llm_output_chars=7000,
        read_only=False,
        risk_level="low_write",
        implementation="apps.tooling.services.create_log_query_skill",
    ),
    ToolDefinition(
        id="query_log_query_skill_step",
        name="执行日志查询 Skill 步骤",
        description="执行已保存 Skill 的某个补充日志步骤。标准步骤复用现有日志查询引擎；自定义路径步骤只读取 Skill 中已保存的受限远端路径。",
        category="日志策略",
        handler=services.query_log_query_skill_step,
        input_schema=_object_schema({
            "environment_id": _ENV_ID,
            "skill_id": {"type": "integer", "minimum": 1},
            "step_index": {"type": "integer", "minimum": 0, "maximum": 7},
            "start_time": {"type": "string", "format": "date-time"},
            "end_time": {"type": "string", "format": "date-time"},
            "module": {"type": "string", "default": ""},
            "max_lines": {"type": "integer", "minimum": 20, "maximum": 500, "default": 160},
        }, ["environment_id", "skill_id", "step_index", "start_time", "end_time"]),
        tags=("assistant", "logs", "skill", "evidence", "bounded"),
        use_when="已经匹配到当前子系统的 Skill，且标准日志没有证据或只有上层现象，需要读取该 Skill 指定的下一层日志时。",
        do_not_use_when="没有先匹配/读取 Skill；不要自行构造 skill_id 或路径。",
        validation_rules=("skill_id 必须来自已保存 Skill", "只执行 Skill 中配置的步骤", "时间仅用于定位证据，不作为拒绝条件", "自定义路径不允许任意命令执行"),
        max_llm_output_chars=14000,
        implementation="apps.tooling.services.query_log_query_skill_step",
    ),
    ToolDefinition(
        id="list_log_semantic_rules",
        name="读取日志语义与标签规则",
        description="读取已配置的日志语义/标签能力目录，只返回规则名称、触发范围和语义/标签摘要，不返回完整模板。",
        category="日志语义",
        handler=services.list_log_semantic_rules,
        input_schema=_object_schema({
            "query": {"type": "string", "default": ""},
            "enabled_only": {"type": "boolean", "default": True},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
        }),
        tags=("assistant", "logs", "semantic", "labels"),
        use_when="用户询问当前有哪些日志语义/标签规则，或希望基于已有规则给当前日志显示语义标签时。",
        do_not_use_when="只需要查询原始日志正文时不必调用。",
        max_llm_output_chars=5000,
        implementation="apps.tooling.services.list_log_semantic_rules",
    ),
    ToolDefinition(
        id="set_log_semantic_labels",
        name="切换日志语义标签",
        description="控制当前日志定位页面是否显示已配置的源码语义、用户语义和自定义标签。",
        category="界面操作",
        handler=services.set_log_semantic_labels,
        input_schema=_object_schema({"enabled": {"type": "boolean", "default": True}}),
        tags=("assistant", "logs", "semantic", "ui"),
        risk_level="ui_navigation",
        max_llm_output_chars=800,
        implementation="apps.tooling.services.set_log_semantic_labels",
    ),
    ToolDefinition(
        id="create_log_semantic_rule",
        name="创建日志语义规则",
        description="根据当前真实日志或函数样例创建语义说明/标签规则。前端会转换成 TraceLens 原生语义规则、校验后保存，并可打开日志规则配置查看。",
        category="日志语义",
        handler=services.create_log_semantic_rule,
        input_schema=_object_schema({
            "rule": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "kind": {"type": "string", "enum": ["keyword", "template"], "default": "keyword"},
                    "scope": {"type": "string", "enum": ["function", "log", "both"], "default": "log"},
                    "keyword": {"type": "string"},
                    "display_template": {"type": "string"},
                    "display_mode": {"type": "string", "enum": ["semantic", "label", "both"], "default": "semantic"},
                    "custom_label_template": {"type": "string"},
                    "custom_label_color": {"type": "string", "default": "#2563eb"},
                    "show_label_on_timeline": {"type": "boolean", "default": True},
                    "supplemental_description": {"type": "string"},
                    "sample_message": {"type": "string"},
                    "parameters": {
                        "type": "array",
                        "maxItems": 24,
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "sample_value": {"type": "string"},
                            },
                            "required": ["label", "sample_value"],
                            "additionalProperties": False,
                        },
                        "default": [],
                    },
                    "enabled": {"type": "boolean", "default": True},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            "open_settings": {"type": "boolean", "default": True},
        }, ["rule"]),
        tags=("assistant", "logs", "semantic", "rule", "authoring", "ui"),
        use_when="用户明确要求根据当前日志创建/新增语义说明、日志标签或函数语义规则时。必须基于当前真实日志/函数样例，不要只打开设置页面。",
        do_not_use_when="用户只想临时显示已有语义时使用 set_log_semantic_labels；没有真实样例且规则内容无法确定时先补证据。",
        validation_rules=("keyword 规则必须给 keyword", "template 规则必须给 sample_message 和真实参数样例", "不得编造日志不存在的参数值"),
        max_llm_output_chars=1800,
        risk_level="low_write",
        implementation="apps.tooling.services.create_log_semantic_rule",
        read_only=False,
    ),
    ToolDefinition(
        id="ask_user_choice",
        name="让用户选择",
        description=(
            "需要用户在**有限选项**里做决定时调用：界面会渲染成固定的选择组件（按钮列表），"
            "用户点一下就把选项内容作为下一条消息发回来。不要用文字列选项让用户手打。"
        ),
        category="交互",
        handler=services.ask_user_choice,
        input_schema=_object_schema({
            "question": {"type": "string", "description": "要问用户的问题（写清楚为什么要选）。"},
            "options": {
                "type": "array",
                "minItems": 2,
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "选项文字（用户点击后原样发回，写得能独立看懂）。"},
                        "detail": {"type": "string", "description": "可选的一句话补充说明。"},
                    },
                    "required": ["label"],
                    "additionalProperties": False,
                },
            },
            "multi": {"type": "boolean", "default": False, "description": "是否允许多选（前端会显示为可多选再提交）。"},
            "allow_other": {"type": "boolean", "default": True, "description": "是否允许用户自己输入其它答案。"},
        }, ["question", "options"]),
        tags=("assistant", "interaction", "ui"),
        use_when=(
            "需要用户从几个固定选项里挑一个时（例如「生成语义还是标签」、环境有歧义要选一个、"
            "参数方案二选一）。**凡是能用选项表达的选择都用它**，别让用户手打。"
        ),
        do_not_use_when="需要用户确认高风险写操作时用确认卡（那是执行前确认，不是选择）；纯开放问题（比如让用户补一段日志）直接问即可。",
        validation_rules=("options 至少 2 个、最多 6 个", "label 要能独立看懂（用户点它等于回答你）"),
        max_llm_output_chars=600,
        risk_level="low_write",
        implementation="apps.tooling.services.ask_user_choice",
        read_only=False,
    ),
    ToolDefinition(
        id="bulk_generate_log_rules",
        name="批量生成日志语义/标签规则",
        description=(
            "把一批真实日志按「函数方法」或「同类特征」分组，一次生成多条语义说明/标签规则候选。"
            "**候选只在对话里列成带勾选框的预览**，用户勾选并点「创建」之后才会真正写入并渲染 —— "
            "所以调用后不要去承诺「已经创建」，也不要逐条复述配置。"
            "用户没说明要语义还是标签时，先用 ask_user_choice 让他点选再调用。"
        ),
        category="日志语义",
        handler=services.bulk_generate_log_rules,
        input_schema=_object_schema({
            "mode": {
                "type": "string",
                "enum": ["semantic", "label", "both"],
                "description": "生成语义说明 / 标签 / 两者都要。用户没说就先用 ask_user_choice 让他点选，不要自己替他决定。",
            },
            "group_by": {
                "type": "string",
                "enum": ["function", "similar"],
                "default": "similar",
                "description": "function=按函数方法批量建（关键字就是函数名）；similar=把同类特征的日志归到一组（正文只有变量不同）。",
            },
            "samples": {
                "type": "array",
                "minItems": 1,
                "maxItems": 400,
                "items": {"type": "string"},
                "description": "参与分析的原始日志行（必须是本轮真实日志证据里的原文，不要自己编）。",
            },
            "existing_keywords": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
                "description": "已知的规则关键字，用来跳过重复候选。",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 24, "default": 12},
            "open_settings": {"type": "boolean", "default": False, "description": "保存后是否跳到「设置 → 日志规则」让用户复核。"},
        }, ["mode", "samples"]),
        tags=("assistant", "logs", "semantic", "rule", "authoring", "batch", "ui"),
        use_when=(
            "用户希望**批量**给日志加语义说明或标签（例如「这批日志你帮我批量加标签」"
            "「按函数方法生成语义规则」「把同类日志整理成规则」）时调用。"
            "**mode 必须来自用户**：用户没说语义还是标签，先用 ask_user_choice 让他在选项里点选，再调用本工具。"
        ),
        do_not_use_when=(
            "只针对单条日志/单个函数建规则用 create_log_semantic_rule；"
            "只是想临时显示已有语义用 set_log_semantic_labels；没有真实日志证据时先取证据。"
        ),
        validation_rules=(
            "samples 必须是本轮真实日志原文",
            "mode 必须来自用户选择，不能替用户决定",
            "不得把时间戳、PID/TID、随机 ID、波动数值当成稳定特征",
        ),
        max_llm_output_chars=2400,
        risk_level="low_write",
        implementation="apps.tooling.services.bulk_generate_log_rules",
        read_only=False,
    ),
    ToolDefinition(
        id="create_log_anomaly_rule",
        name="创建日志异常规则",
        description="根据当前真实异常日志创建一个或多个异常关键字规则，直接复用 TraceLens 日志规则配置中的异常规则能力。",
        category="日志语义",
        handler=services.create_log_anomaly_rule,
        input_schema=_object_schema({
            "keywords": {"type": "array", "items": {"type": "string"}, "maxItems": 32, "default": []},
            "keyword": {"type": "string", "default": ""},
            "case_sensitive": {"type": "boolean", "default": False},
            "whole_word": {"type": "boolean", "default": False},
            "enabled": {"type": "boolean", "default": True},
            "open_settings": {"type": "boolean", "default": True},
        }),
        tags=("assistant", "logs", "anomaly", "rule", "authoring", "ui"),
        use_when="用户明确要求把当前日志里的稳定异常特征沉淀成异常规则，或要求 AI 新增异常关键字规则时。",
        do_not_use_when="不要把时间戳、PID、TID、随机 ID、具体数值等动态字段直接当异常关键字；只分析异常而不要求保存规则时无需调用。",
        validation_rules=("关键字必须来自真实日志证据", "优先选择跨多条同类异常稳定出现的错误码/异常短语/固定语义 token"),
        max_llm_output_chars=1400,
        risk_level="low_write",
        implementation="apps.tooling.services.create_log_anomaly_rule",
        read_only=False,
    ),
    ToolDefinition(
        id="list_data_extraction_rules",
        name="读取数据提取能力",
        description="读取平台已经配置的数据提取器，只返回规则名称、适用模块和可提取字段。用于优先复用现有数据采集能力。",
        category="数据提取",
        handler=services.list_data_extraction_rules,
        input_schema=_object_schema({
            "query": {"type": "string", "default": ""},
            "field_names": {"type": "array", "items": {"type": "string"}, "default": []},
            "enabled_only": {"type": "boolean", "default": True},
            "limit": {"type": "integer", "minimum": 1, "maximum": 60, "default": 30},
        }),
        tags=("assistant", "logs", "data", "extraction"),
        use_when="用户希望从当前日志中提取任意结构化数据时，先检查是否已有适用提取器，避免重复创建规则。",
        do_not_use_when="没有已加载日志且用户尚未要求数据提取；不要让模型自行编造正则替代平台配置规则。",
        validation_rules=("优先按用户目标字段和当前日志范围匹配已有规则", "没有合适规则时再创建新的数据提取能力"),
        max_llm_output_chars=6000,
        implementation="apps.tooling.services.list_data_extraction_rules",
    ),
    ToolDefinition(
        id="create_data_extraction_capability",
        name="创建数据提取能力",
        description="根据当前真实日志样例创建新的数据提取规则候选。前端会先在已加载日志上验证命中，验证通过才保存为可复用能力，并可立即执行提取。",
        category="数据提取",
        handler=services.create_data_extraction_capability,
        input_schema=_object_schema({
            "rule": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "match_keyword": {"type": "string", "default": ""},
                    "case_sensitive": {"type": "boolean", "default": False},
                    "source_categories": {"type": "array", "items": {"type": "string"}, "default": []},
                    "subsystems": {"type": "array", "items": {"type": "string"}, "default": []},
                    "modules": {"type": "array", "items": {"type": "string"}, "default": []},
                    "sample_message": {"type": "string", "minLength": 1},
                    "fields": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 32,
                        "items": {
                            "type": "object",
                            "properties": {
                                "key": {"type": "string"},
                                "name": {"type": "string"},
                                "sample_value": {"type": "string"},
                                "value_type": {"type": "string", "enum": ["number", "integer", "boolean", "string"]},
                                "source_unit": {"type": "string"},
                                "plot_unit": {"type": "string"},
                                "structured_path": {"type": "array", "items": {"type": "string"}},
                                "structured_root_hint": {"type": "string"},
                            },
                            "required": ["key", "name", "sample_value"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["name", "sample_message", "fields"],
                "additionalProperties": False,
            },
            "persist_rule": {"type": "boolean", "default": True},
            "auto_start": {"type": "boolean", "default": True},
            "open_data_page": {"type": "boolean", "default": True},
            "open_rule_settings": {"type": "boolean", "default": False, "description": "只创建/维护提取器时，验证保存后打开日志规则→数据提取页面"},
            "min_matches": {"type": "integer", "minimum": 1, "maximum": 100, "default": 1},
        }, ["rule"]),
        tags=("assistant", "logs", "data", "extraction", "capability", "ui"),
        use_when="用户要求的数据在现有提取器中没有合适能力时；必须基于当前页面可见日志或刚读取到的真实日志证据生成候选规则。",
        do_not_use_when="已有合适提取器时直接 run_data_extraction；没有真实日志样例时不要凭空创建字段或规则。",
        validation_rules=("sample_message 必须来自真实日志", "字段 key 必须优先使用样例里真实出现的参数名/token；name 才是可读保存字段名", "sample_value 必须是该样例里对应的真实值，或提供真实 structured_path", "前端验证命中前不会保存规则"),
        max_llm_output_chars=2200,
        risk_level="low_write",
        implementation="apps.tooling.services.create_data_extraction_capability",
        read_only=False,
    ),
    ToolDefinition(
        id="run_data_extraction",
        name="从当前日志提取数据",
        description="让当前日志定位页面使用已配置的数据提取器对当前已加载日志执行提取，并可在完成后进入数据页面继续可视化。",
        category="数据提取",
        handler=services.run_data_extraction,
        input_schema=_object_schema({
            "rule_ids": {"type": "array", "items": {"type": "string"}, "default": []},
            "field_names": {"type": "array", "items": {"type": "string"}, "default": []},
            "auto_start": {"type": "boolean", "default": True},
            "open_data_page": {"type": "boolean", "default": True},
        }),
        tags=("assistant", "logs", "data", "extraction", "ui"),
        use_when="当前日志已经查询/解析完成，且用户明确要求提取某种结构化数据。",
        do_not_use_when="当前没有已解析日志；若没有匹配提取器，应先根据真实日志样例创建并验证新的数据提取能力。",
        validation_rules=("rule_ids 或 field_names 至少一个非空", "只使用平台已有提取规则"),
        max_llm_output_chars=1200,
        risk_level="low_write",
        implementation="apps.tooling.services.run_data_extraction",
        read_only=False,
    ),
    ToolDefinition(
        id="open_log_rule_settings",
        name="打开日志规则配置",
        description="打开设置中心的日志规则区域，可直接进入语义/标签、异常、数据提取或日志查询 Skill 配置。",
        category="界面操作",
        handler=services.open_log_rule_settings,
        input_schema=_object_schema({"tab": {"type": "string", "enum": ["semantic", "anomaly", "data", "query-skill"], "default": "semantic"}}),
        tags=("assistant", "logs", "settings", "data", "semantic"),
        risk_level="ui_navigation",
        max_llm_output_chars=800,
        implementation="apps.tooling.services.open_log_rule_settings",
    ),
    ToolDefinition(
        id="open_workspace_page",
        name="打开功能页面",
        description="让 TraceLens 前端切换到指定主功能页面，并展示 AI 接管提示。",
        category="界面操作",
        handler=services.open_workspace_page,
        input_schema=_object_schema({"page": {"type": "string", "enum": ["resources", "logs", "atlog", "data", "knowledge", "audit", "platform-settings", "tools"]}}, ["page"]),
        tags=("assistant", "ui", "navigation"),
        risk_level="ui_navigation",
        implementation="apps.tooling.services.open_workspace_page",
    ),
    ToolDefinition(
        id="control_log_view", name="操作当前日志视图",
        description="在用户当前屏幕筛选日志、调整时间窗、启停函数折叠，或按上下文中的真实 entry_id 展开目录并滚动高亮。定位行会清除隐藏该行的筛选。页面回执返回前不得声称操作完成。",
        category="界面操作", handler=workstation.control_log_view, domain="log", skills=("logs", "atlog", "data"),
        input_schema=_object_schema({
            "task_id": {"type": "string"}, "entry_id": {"type": "string"}, "query": {"type": "string"},
            "errors_only": {"type": "boolean"}, "folding_enabled": {"type": "boolean"},
            "components": {"type": "array", "items": {"type": "string"}, "maxItems": 16},
            "start_time": {"type": "string"}, "end_time": {"type": "string"},
        }), risk_level="ui_navigation",
    ),
    ToolDefinition(
        id="plan_log_retrieval", name="制定关联日志检索计划",
        description="从断言失败或 event 真实时间生成三档窗口：narrow ±30秒、standard ±2分钟、wide ±10分钟。先报告→event→映射组件→debug；无证据不得猜组件。首次窄窗，缺上下文逐档扩窗，截断时收窄。返回的 start_time/end_time/targets/max_lines 可直接交给检索工具。",
        category="日志分析", handler=workstation.retrieval_plan, domain="log", skills=("logs", "atlog"),
        input_schema=_object_schema({
            "anchor_time": {"type": "string"}, "tier": {"type": "string", "enum": ["narrow", "standard", "wide"]},
            "reason": {"type": "string"}, "trace_id": {"type": "string"}, "error_code": {"type": "string"},
            "targets": {"type": "array", "maxItems": 4, "items": {"type": "object", "properties": {"subsystem": {"type": "string"}, "module": {"type": "string"}}, "required": ["subsystem", "module"]}},
        }, ["anchor_time", "reason"]),
    ),
    ToolDefinition(
        id="open_environment_page",
        name="打开环境页面",
        description="切换到环境资源并选中指定环境，可同时打开部署窗口。",
        category="界面操作",
        handler=services.open_environment_page,
        input_schema=_object_schema({"environment_id": _ENV_ID, "open_deployment": {"type": "boolean", "default": False}}, ["environment_id"]),
        tags=("assistant", "ui", "environment"),
        risk_level="ui_navigation",
        implementation="apps.tooling.services.open_environment_page",
    ),
    ToolDefinition(
        id="open_log_locator",
        name="打开日志定位",
        description="切换到指定环境的日志定位页面，并可带入时间窗、关键字和模块选择。只给用户点名的 component_name 时，会先按组件名解析成子系统/模块再下发，页面打开即自动选中该组件。",
        use_when="用户要求「打开/查看/切到某个组件（模块、子系统、功能块）的日志」，或需要把页面切到某环境+组件+时间窗的日志视图时。用户只说了组件名就传 component_name，不要让用户自己去页面上再选一次。",
        do_not_use_when="只需要日志结论、不需要改变页面选择时，优先用 query_environment_logs。",
        validation_rules=("component_name 必须来自用户输入", "组件名匹配到多个日志目标时后端会返回候选，必须让用户选择，禁止擅自指定"),
        category="界面操作",
        handler=services.open_log_locator,
        input_schema=_object_schema({
            "environment_id": _ENV_ID,
            "start_time": {"type": "string"},
            "end_time": {"type": "string"},
            "keyword": {"type": "string", "default": ""},
            "source_categories": {"type": "array", "items": {"type": "string"}, "default": []},
            "component_name": {"type": "string", "default": "", "description": "用户点名的组件名；提供后自动解析为子系统/模块并在页面上选中。"},
            "fm_targets": _FM_TARGETS,
        }, ["environment_id"]),
        tags=("assistant", "ui", "logs", "navigation"),
        risk_level="ui_navigation",
        implementation="apps.tooling.services.open_log_locator",
    ),
)

_TOOL_MAP = {tool.id: tool for tool in TOOLS}
_CAPABILITY_MAP = {tool.capability_id(): tool for tool in TOOLS}


def list_tools() -> list[dict[str, Any]]:
    return [tool.as_dict() for tool in TOOLS]


def list_capabilities(skill_id: str | None = None) -> list[dict[str, Any]]:
    normalized = normalize_skill_id(skill_id)
    values = list_tools()
    if normalized == "auto":
        return values
    return [item for item in values if normalized in (item.get("skills") or [])]


def _kernel_registry():
    """The live registry. Imported lazily: apps.tooling.kernel imports this module."""
    try:
        from apps.tooling.kernel.executor import get_default_kernel

        return get_default_kernel().registry
    except Exception:  # noqa: BLE001
        logger.exception("tooling.registry.kernel_unavailable")
        return None


def get_tool(tool_id: str) -> ToolDefinition | None:
    """Resolve a tool by id or atomic id.

    The Core Kernel registry is authoritative: it indexes the legacy ``TOOLS`` tuple
    *and* the dynamically discovered plugins. Consulting only the static ``_TOOL_MAP``
    here is what left discovered plugins unreachable through this accessor.
    """
    key = str(tool_id or "").strip()
    if not key:
        return None
    registry = _kernel_registry()
    if registry is not None:
        definition = registry.get(key)
        if definition is not None:
            return definition
    return _TOOL_MAP.get(key) or _CAPABILITY_MAP.get(key)


def invoke_tool(tool_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    tool = get_tool(tool_id)
    if tool is None:
        raise KeyError(tool_id)
    if tool.handler is not None:
        return tool.handler(payload)
    # Discovered plugins keep their callable on the adapter, not on the frozen
    # ToolDefinition, so the handler shortcut above is not enough on its own.
    registry = _kernel_registry()
    plugin = registry.get_plugin(tool_id) if registry is not None else None
    if plugin is not None and plugin.__class__.__name__ == "FunctionToolPlugin":
        return plugin.invoke(payload)
    raise services.ToolInputError(f"工具 {tool_id} 使用 {tool.transport} 传输，不能通过通用 JSON invoke 调用。")


ToolInputError = services.ToolInputError
