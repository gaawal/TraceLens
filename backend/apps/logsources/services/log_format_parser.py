from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping


CORE_FIELDS = (
    "timestamp", "level", "component", "process_id", "thread_id", "source",
    "mode", "rpc", "message",
)
RUN_EVENT_FIELDS = (
    "event_category", "event_level", "current_event_code", "linked_event_codes",
    "current_err_iid", "linked_err_iids", "display_code", "linked_display_codes",
    "event_type",
)
STANDARD_FIELDS = CORE_FIELDS + RUN_EVENT_FIELDS
TIMESTAMP_FORMATS = {
    "auto",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S.%f",
    "%Y/%m/%d %H:%M:%S",
    "unix_ms",
    "unix_ns",
}
MAX_PATTERN_LENGTH = 10000
MAX_TEST_LINES = 200
MAX_TEST_LINE_LENGTH = 65536

DEBUG_PATTERN = r"^\[(?P<timestamp>[^\]]+)]\s+\[(?P<level>[^\]]+)]\s+\[(?P<component>[^\]]+)]\s+\[(?P<process_id>[^\]]+)]\s+\[(?P<thread_id>[^\]]+)]\s+\[(?P<source>[^\]]+)]\s+\[(?P<mode>[^\]]+)]\s+\[(?P<rpc>[^\]]+)]\s*(?P<message>.*)$"
# 执行器有两种已确认布局：
#   101 外部：... [source] [101] [rpc] message
#   100 内部：... [source] [context] [rpc] [100] message
# mode 用前瞻从前 3 个结构字段中定位，rpc 固定取第二个结构字段；消费段再按两种布局切到真正正文。
EXECUTOR_PATTERN = r"^\[(?P<timestamp>[^\]]+)]\s+\[(?P<level>[^\]]+)]\s+\[(?P<component>[^\]]+)]\s+\[(?P<process_id>[^\]]+)]\s+\[(?P<thread_id>[^\]]+)]\s+\[(?P<source>[^\]]+)]\s+(?=(?:\[[^\]]+]\s+){0,2}\[(?P<mode>100|101)](?:\s|$))(?=\[[^\]]+]\s+\[(?P<rpc>[^\]]*:[^\]]*:[^\]]*)])(?:\[[^\]]+]\s+\[[^\]]+]\s+\[100]|\[(?:100|101)]\s+\[[^\]]+])\s*(?P<message>.*)$"
RUN_PATTERN = r"^\[(?P<timestamp>[^\]]+)]\s+\[(?P<component>[^\]]+)]\s+\[(?P<process_id>[^\]]+)]\s+\[(?P<source>[^\]]+)]\s+\[(?P<event_category>[^\]]+)]\s+\[(?P<event_level>[^\]]+)]\s+\[(?P<current_event_code>[^\]]*)]\s+\[(?P<linked_event_codes>[^\]]*)]\s+\[(?P<current_err_iid>[^\]]*)]\s+\[(?P<linked_err_iids>[^\]]*)]\s+\[(?P<display_code>[^\]]*)]\s+\[(?P<linked_display_codes>[^\]]*)]\s+\[(?P<event_type>[^\]]+)]\s*(?P<message>.*)$"
DEBUG_FIELD_MAP = {field: field for field in CORE_FIELDS}
RUN_FIELD_MAP = {
    "timestamp": "timestamp", "level": "event_level", "component": "component", "process_id": "process_id",
    "source": "source", "mode": "event_type", "message": "message", "event_category": "event_category",
    "event_level": "event_level", "current_event_code": "current_event_code", "linked_event_codes": "linked_event_codes",
    "current_err_iid": "current_err_iid", "linked_err_iids": "linked_err_iids", "display_code": "display_code",
    "linked_display_codes": "linked_display_codes", "event_type": "event_type",
}
BUILTIN_RULE_DEFAULTS = {
    ("debug", "标准调试日志"): {
        "priority": 100, "file_pattern": "*.log*", "pattern": DEBUG_PATTERN, "field_map": DEBUG_FIELD_MAP,
        "timestamp_format": "auto", "ignore_case": False, "enabled": True, "description": "TraceLens 默认八字段调试日志格式。",
    },
    ("executor", "标准执行器日志"): {
        "priority": 100, "file_pattern": "*.log*", "pattern": EXECUTOR_PATTERN, "field_map": DEBUG_FIELD_MAP,
        "timestamp_format": "auto", "ignore_case": False, "enabled": True, "description": "执行器日志：兼容 100 内部（context/rpc/mode）与 101 外部（mode/rpc）字段布局。",
    },
    ("run", "运行事件日志"): {
        "priority": 120, "file_pattern": "event.log,*.log*", "pattern": RUN_PATTERN, "field_map": RUN_FIELD_MAP,
        "timestamp_format": "auto", "ignore_case": False, "enabled": True, "description": "运行 event 日志结构化事件格式。",
    },
}


class LogFormatRuleError(ValueError):
    pass


@dataclass(frozen=True)
class RuleTestLine:
    line_number: int
    raw: str
    matched: bool
    groups: dict[str, str]
    fields: dict[str, str]
    timestamp_valid: bool | None
    message: str


def python_pattern_to_client(pattern: str) -> str:
    """Translate the portable named-group subset from Python re to JS RegExp."""
    if "(?P=" in pattern:
        raise LogFormatRuleError("暂不支持 Python 命名反向引用 (?P=name)，请改用普通捕获逻辑。")
    if "(?aiLmsux-" in pattern:
        raise LogFormatRuleError("暂不支持作用域内联 flags，请使用“忽略大小写”开关。")
    return re.sub(r"\(\?P<([A-Za-z_]\w*)>", r"(?<\1>", pattern)


def _compile(pattern: str, ignore_case: bool = False) -> re.Pattern[str]:
    text = str(pattern or "")
    if not text.strip():
        raise LogFormatRuleError("正则表达式不能为空。")
    if len(text) > MAX_PATTERN_LENGTH:
        raise LogFormatRuleError(f"正则表达式最多 {MAX_PATTERN_LENGTH} 个字符。")
    try:
        return re.compile(text, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        position = f"，位置 {exc.pos}" if getattr(exc, "pos", None) is not None else ""
        raise LogFormatRuleError(f"正则语法错误：{exc.msg}{position}") from exc


def normalize_field_map(value: Mapping[str, Any] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for field, group in dict(value or {}).items():
        field_name = str(field).strip()
        group_name = str(group).strip()
        if not group_name:
            continue
        if field_name not in STANDARD_FIELDS:
            raise LogFormatRuleError(f"未知标准字段：{field_name}")
        if not re.fullmatch(r"[A-Za-z_]\w*", group_name):
            raise LogFormatRuleError(f"捕获组名称无效：{group_name}")
        result[field_name] = group_name
    return result


def validate_rule_config(config: Mapping[str, Any]) -> dict[str, Any]:
    pattern = str(config.get("pattern") or "")
    ignore_case = bool(config.get("ignore_case", False))
    compiled = _compile(pattern, ignore_case)
    client_pattern = python_pattern_to_client(pattern)
    field_map = normalize_field_map(config.get("field_map"))
    groups = set(compiled.groupindex)
    missing_groups = sorted({group for group in field_map.values() if group not in groups})
    if missing_groups:
        raise LogFormatRuleError(f"字段映射引用了不存在的命名捕获组：{', '.join(missing_groups)}")
    timestamp_format = str(config.get("timestamp_format") or "auto").strip()
    if timestamp_format not in TIMESTAMP_FORMATS:
        raise LogFormatRuleError("不支持的时间格式。")
    if "message" not in field_map:
        raise LogFormatRuleError("至少需要映射标准字段 message。")
    return {
        "compiled": compiled,
        "client_pattern": client_pattern,
        "field_map": field_map,
        "group_names": sorted(groups),
        "timestamp_format": timestamp_format,
    }


def timestamp_is_valid(value: str, timestamp_format: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        if timestamp_format == "unix_ms":
            float(text)
            return True
        if timestamp_format == "unix_ns":
            int(text)
            return True
        if timestamp_format != "auto":
            datetime.strptime(text, timestamp_format)
            return True
        candidates = (
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%Y/%m/%d %H:%M:%S.%f",
            "%Y/%m/%d %H:%M:%S",
        )
        return any(_try_datetime(text, fmt) for fmt in candidates)
    except (TypeError, ValueError, OverflowError):
        return False


def _try_datetime(value: str, fmt: str) -> bool:
    try:
        datetime.strptime(value, fmt)
        return True
    except ValueError:
        return False


def _mapped_fields(match: re.Match[str], field_map: Mapping[str, str]) -> dict[str, str]:
    groups = match.groupdict()
    return {
        field: str(groups.get(group) or "")
        for field, group in field_map.items()
    }


def test_rule_config(config: Mapping[str, Any], text: str) -> dict[str, Any]:
    validated = validate_rule_config(config)
    compiled: re.Pattern[str] = validated["compiled"]
    field_map: dict[str, str] = validated["field_map"]
    timestamp_format: str = validated["timestamp_format"]
    raw_lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    non_empty = [(index + 1, line) for index, line in enumerate(raw_lines) if line.strip()]
    if not non_empty:
        raise LogFormatRuleError("请粘贴至少一行测试日志。")
    if len(non_empty) > MAX_TEST_LINES:
        raise LogFormatRuleError(f"一次最多测试 {MAX_TEST_LINES} 行日志。")

    results: list[RuleTestLine] = []
    matched_count = 0
    timestamp_valid_count = 0
    for line_number, raw in non_empty:
        if len(raw) > MAX_TEST_LINE_LENGTH:
            results.append(RuleTestLine(line_number, raw[:1000], False, {}, {}, None, "日志行过长，已跳过"))
            continue
        match = compiled.search(raw)
        if not match:
            results.append(RuleTestLine(line_number, raw, False, {}, {}, None, "未命中"))
            continue
        matched_count += 1
        groups = {key: str(value or "") for key, value in match.groupdict().items()}
        fields = _mapped_fields(match, field_map)
        timestamp_value = fields.get("timestamp")
        timestamp_valid = timestamp_is_valid(timestamp_value, timestamp_format) if "timestamp" in field_map else None
        if timestamp_valid is True:
            timestamp_valid_count += 1
        results.append(RuleTestLine(line_number, raw, True, groups, fields, timestamp_valid, "匹配成功"))

    return {
        "line_count": len(non_empty),
        "matched_count": matched_count,
        "unmatched_count": len(non_empty) - matched_count,
        "match_rate": round((matched_count / len(non_empty)) * 100, 2) if non_empty else 0,
        "timestamp_valid_count": timestamp_valid_count,
        "client_pattern": validated["client_pattern"],
        "group_names": validated["group_names"],
        "results": [item.__dict__ for item in results],
    }


def rule_applies_to_file(file_pattern: str, file_name: str) -> bool:
    patterns = [item.strip() for item in str(file_pattern or "").replace(";", ",").split(",") if item.strip()]
    if not patterns:
        return True
    return any(fnmatch.fnmatch(file_name, pattern) for pattern in patterns)
