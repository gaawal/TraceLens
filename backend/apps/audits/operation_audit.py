"""把一次 HTTP 请求翻译成一条**功能级**操作审计。

分工：
- ``features.py`` 决定"这个接口算不算用户功能、叫什么名字"；
- 本模块决定"这条记录里写什么"（操作记录文字、目标、脱敏后的参数、结果）；
- ``middleware.py`` 负责在请求结束时调用它，并且**绝不因为审计失败而影响业务响应**。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from apps.audits.features import FeatureAudit

#: 请求头：前端用它区分"用户点出来的"和"页面自己在轮的"。
#: ``auto`` = 自动刷新/轮询/流式，不记录；``ai`` = AI 代操作，记录但标记来源。
ACTION_HEADER = "X-TraceLens-Action"
ACTION_AUTO = "auto"
ACTION_AI = "ai"

#: 只有这些路径前缀才进入功能审计（后台/静态/文档不算）。
AUDITED_PATH_PREFIX = "/api/"

#: 落库前一律打码的字段名（大小写不敏感、按子串匹配）。
SENSITIVE_KEY_PARTS = (
    "password", "passwd", "secret", "token", "credential", "private_key",
    "access_key", "api_key", "apikey", "authorization", "cookie", "session",
)

#: 请求参数里**值得进操作记录**的字段（顺序即优先级）。
SUMMARY_FIELDS: tuple[tuple[str, str], ...] = (
    ("environment_name", "环境"),
    ("case_name", "用例"),
    ("task_name", "任务"),
    ("name", "名称"),
    ("report_name", "报告"),
    ("host", "主机"),
    ("keyword", "关键字"),
    ("query", "查询"),
    ("fm", "模块"),
    ("subsystem", "子系统"),
    ("version", "版本"),
    ("software_version", "版本"),
    ("target", "目标"),
    ("path", "路径"),
    ("url", "地址"),
    ("file", "文件"),
    ("filename", "文件"),
)

PROCESS_ACTION_LABELS = {"start": "启动", "stop": "停止", "restart": "重启"}

#: 请求体最多留这么多字符，避免把大 payload（日志正文、规则快照）整份塞进审计。
MAX_PAYLOAD_CHARS = 4000

#: 单次请求最多保留多少个参数键；超出的丢掉并在 payload 里标记。
MAX_PAYLOAD_KEYS = 40

#: 错误信息截断长度。
MAX_ERROR_CHARS = 500

#: 只在视图解析之前"嗅探"这么小的 JSON 请求体，用来写操作记录。
#: 大文件上传（Excel / 日志包）不抓，免得占内存、也别打断 DRF 的流式解析。
BODY_SNIFF_LIMIT = 64 * 1024

JSON_MEDIA_TYPES = ("application/json",)


@dataclass
class OperationContext:
    """一条待落库的操作审计（还没写库，方便单测）。"""

    feature: FeatureAudit
    trigger: str = "user"
    target_kind: str = ""
    target_name: str = ""
    environment_id: int | None = None
    environment_name: str = ""
    summary: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


def _is_sensitive(key: str) -> bool:
    lowered = str(key or "").lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def sanitize_payload(payload: Any, *, _depth: int = 0) -> Any:
    """递归脱敏 + 截断：密码/密钥一律 ``***``，超长字符串截断。"""
    if _depth > 4:
        return "…"
    if isinstance(payload, dict):
        result: dict[str, Any] = {}
        for index, (key, value) in enumerate(payload.items()):
            if index >= MAX_PAYLOAD_KEYS:
                result["…"] = f"另有 {len(payload) - MAX_PAYLOAD_KEYS} 个参数"
                break
            text_key = str(key)
            if _is_sensitive(text_key):
                result[text_key] = "***"
            else:
                result[text_key] = sanitize_payload(value, _depth=_depth + 1)
        return result
    if isinstance(payload, (list, tuple)):
        items = [sanitize_payload(item, _depth=_depth + 1) for item in list(payload)[:20]]
        if len(payload) > 20:
            items.append(f"…另有 {len(payload) - 20} 项")
        return items
    if isinstance(payload, str):
        return payload if len(payload) <= 300 else f"{payload[:300]}…（共 {len(payload)} 字）"
    if isinstance(payload, (int, float, bool)) or payload is None:
        return payload
    return str(payload)[:300]


def capture_request_body(request) -> None:
    """在视图读取请求体**之前**把小的 JSON body 抓下来，供操作记录使用。

    中间件拿不到 DRF 的 ``request.data``（视图里的 request 才是 DRF Request），而响应阶段
    再读 ``request.body`` 会因为流已经被消费而抛 ``RawPostDataException``；所以在请求阶段
    先读一次并缓存（``request._body``），DRF 之后照常解析 —— 它在 ``_read_started`` 时会退回
    ``io.BytesIO(self.body)``。
    """
    body: Any = None
    try:
        content_type = str(getattr(request, "content_type", "") or "").split(";")[0].strip().lower()
        if content_type in JSON_MEDIA_TYPES:
            try:
                length = int(request.META.get("CONTENT_LENGTH") or 0)
            except (TypeError, ValueError):
                length = 0
            if 0 < length <= BODY_SNIFF_LIMIT:
                parsed = json.loads(request.body.decode("utf-8", "ignore") or "{}")
                body = parsed if isinstance(parsed, dict) else {"_body": parsed}
    except Exception:  # pragma: no cover - 抓不到就只留 query 参数
        body = None
    request._audit_json_body = body


def request_payload(request) -> dict[str, Any]:
    """合并 query + 请求体，得到一个可读的参数快照（已脱敏）。"""
    merged: dict[str, Any] = {}
    try:
        for key in request.GET.keys():
            values = request.GET.getlist(key)
            merged[key] = values[0] if len(values) == 1 else values
    except Exception:  # pragma: no cover - 防御性
        pass

    body = getattr(request, "_audit_json_body", None)
    if isinstance(body, dict):
        merged.update(body)
    cleaned = sanitize_payload(merged)
    if isinstance(cleaned, dict) and len(json.dumps(cleaned, ensure_ascii=False, default=str)) > MAX_PAYLOAD_CHARS:
        # 还是太长就只留键名，正文进不了审计表。
        cleaned = {key: "…" for key in cleaned.keys()}
    return cleaned if isinstance(cleaned, dict) else {}


def _first_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        if isinstance(value, dict):
            value = value.get("name") or value.get("id") or ""
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _environment_id_from(request, payload: dict[str, Any]) -> int | None:
    for key in ("environment_id", "environment"):
        value = payload.get(key)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        if isinstance(value, dict):
            value = value.get("id")
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        return number
    kwargs = getattr(getattr(request, "resolver_match", None), "kwargs", None) or {}
    raw_pk = str(kwargs.get("pk") or "").strip()
    if raw_pk.isdigit():
        # 详情类路由的 pk 只有 environment / environment-log 系列才是环境 id。
        url_name = str(getattr(getattr(request, "resolver_match", None), "url_name", "") or "")
        if url_name.startswith(("environment-", "environment-log-")):
            return int(raw_pk)
    return None


def resolve_environment(environment_id: int | None, lookup: Callable[[int], tuple[int, str] | None] | None) -> tuple[int | None, str]:
    """把环境 id 换成名字快照。``lookup`` 可注入，方便单测不碰数据库。"""
    if environment_id is None:
        return None, ""
    if lookup is None:
        return environment_id, ""
    found = lookup(environment_id)
    if not found:
        return environment_id, ""
    return found[0], found[1]


def build_summary(feature: FeatureAudit, payload: dict[str, Any], target: str) -> str:
    """操作记录：一句话说清"对谁做了什么"。"""
    name = feature.name
    pieces: list[str] = []

    # 进程启停要把 start/stop 翻出来，否则「启动/停止环境进程」看不出到底干了哪件。
    action = str(payload.get("action") or "").strip().lower()
    if "环境进程" in name and action:
        prefix = PROCESS_ACTION_LABELS.get(action, action)
        name = f"{prefix}环境进程"

    if target:
        pieces.append(target)

    for key, label in SUMMARY_FIELDS:
        if key in {"environment_name", "name"} and target:
            # 名称已经当目标显示过了，不重复。
            value = _first_text(payload, key)
            if value and value == target:
                continue
        value = _first_text(payload, key)
        if not value or value == target:
            continue
        pieces.append(f"{label} {value}")
        if len(pieces) >= 3:
            break

    start = _first_text(payload, "start_time")
    end = _first_text(payload, "end_time")
    if start and end and len(pieces) < 3:
        pieces.append(f"{start} ~ {end}")

    return " · ".join([name, *pieces])[:255]


def build_operation_context(
    request,
    feature: FeatureAudit,
    *,
    environment_lookup: Callable[[int], tuple[int, str] | None] | None = None,
) -> OperationContext:
    """从请求里抽出「操作记录 / 目标 / 参数」等，得到待落库的上下文。"""
    payload = request_payload(request)
    trigger = ACTION_AI if str(request.META.get(f"HTTP_{ACTION_HEADER.upper().replace('-', '_')}", "")).strip().lower() == ACTION_AI else "user"

    environment_id = _environment_id_from(request, payload)
    environment_id, environment_name = resolve_environment(environment_id, environment_lookup)

    target_kind = ""
    target_name = ""
    if environment_name:
        target_kind, target_name = "环境", environment_name
    else:
        for key, label in SUMMARY_FIELDS:
            if key == "environment_name":
                continue
            value = _first_text(payload, key)
            if value:
                target_kind, target_name = label, value
                break

    return OperationContext(
        feature=feature,
        trigger=trigger,
        target_kind=target_kind,
        target_name=target_name,
        environment_id=environment_id,
        environment_name=environment_name,
        summary=build_summary(feature, payload, target_name or environment_name),
        payload=payload,
    )


def is_auto_request(request) -> bool:
    """前端标记的"自动刷新/轮询"——这类请求不是用户操作，不记录。"""
    value = str(request.META.get(f"HTTP_{ACTION_HEADER.upper().replace('-', '_')}", "")).strip().lower()
    return value == ACTION_AUTO


def extract_error_message(response) -> str:
    """失败时把响应体里的 message/detail 抽出来，方便审计里直接看原因。"""
    status = int(getattr(response, "status_code", 0) or 0)
    if status < 400:
        return ""
    data = getattr(response, "data", None)
    text = ""
    if isinstance(data, dict):
        for key in ("message", "detail", "error"):
            value = data.get(key)
            if value:
                text = str(value)
                break
        if not text:
            text = json.dumps(sanitize_payload(data), ensure_ascii=False, default=str)
    elif data is not None:
        text = str(data)
    if not text and hasattr(response, "render"):
        try:
            text = response.content.decode("utf-8", "ignore")
        except Exception:  # pragma: no cover - 防御性
            text = ""
    return text[:MAX_ERROR_CHARS]


def outcome_for(response) -> str:
    status = int(getattr(response, "status_code", 0) or 0)
    return "success" if status and status < 400 else "failed"
