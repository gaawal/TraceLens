"""一条「举证」到底算不算有效、能不能参与指纹匹配 —— 只在这里判定。

背景（一次真实事故）
-------------------
`draft_diagnosis_case` 把模型引用的日志行写成了 ``{"raw": ..., "component": "", "timestamp": ""}``，
而序列化器要求每条举证都带 ``template``，于是「AI 整理成案例 → 保存」这条链路
**永远**返回 400「第 1 条举证缺少原始日志或指纹模板。」。
同一个模板归一化逻辑当时散在三个地方（前端 ``normalizeAbnormalMessage``、
`atlog/knowledge_bridge._normalize_template`、以及序列化器的校验），
谁都不知道别人在要求什么。

同时那份校验还有个更根本的错误假设：它要求案例里**至少有一条命中异常规则的运行日志**。
这在自动化用例场景下是错的 —— 用例报告执行过程本身的报错（断言失败、超时、pytest/xytest
报错）根本不是日志行，没有异常规则可言，但它是最值得沉淀的案例。

于是本模块把判定拆成两件事：
1. **有效性**：举证必须有内容（``raw`` 或 ``message``）。指纹模板缺失时**推导**，不再拒绝。
2. **可匹配性**：只有带异常规则的运行日志才能进指纹比对；报告证据和自由文本用例片段
   仍然是合法举证，只是标记为不可指纹匹配（用文本相似度参与检索）。
   这是「质量信号」，不是「保存门槛」。
"""
from __future__ import annotations

import re
from typing import Any

# 举证类型。前端的中文标签：运行日志 / 用例报告 / 用例片段。
EVIDENCE_KIND_RUNTIME_LOG = "runtime_log"
EVIDENCE_KIND_CASE_REPORT = "case_report"
EVIDENCE_KIND_CASE_FRAGMENT = "case_fragment"

EVIDENCE_KIND_LABELS = {
    EVIDENCE_KIND_RUNTIME_LOG: "运行日志",
    EVIDENCE_KIND_CASE_REPORT: "用例报告",
    EVIDENCE_KIND_CASE_FRAGMENT: "用例片段",
}

# 来自 pytest / xytest / summary_report 这类用例产物的举证，允许没有异常规则。
REPORT_SOURCE_CATEGORIES = {"case_report", "pytest", "xytest", "case-metadata", "case_metadata"}

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}\b")
_HEX_RE = re.compile(r"\b0x[0-9a-fA-F]{4,}\b")
_NUMBER_RE = re.compile(r"(?<![\w.])[-+]?(?:\d+\.\d+|\d+)(?![\w.])")

_TOKEN_RE = re.compile(r"[0-9A-Za-z_:.+\-/\u4e00-\u9fff]{2,}")
# 与前端 fingerprintTokens 的停用词口径保持一致：动态字段名不承载故障语义。
_TOKEN_STOP = {
    "the", "and", "with", "from", "this", "that", "true", "false",
    "pid", "tid", "traceid", "trace_id", "spanid", "span_id", "timestamp",
}


def normalize_template(text: Any) -> str:
    """把一行原始日志归一化成指纹模板（时间/地址/数值泛化）。

    这是「同一条日志的不同次出现」能被认成同一条的依据，所以缺失时应当推导，
    而不是像旧校验那样直接判举证无效。
    """
    value = str(text or "").strip()
    if not value:
        return ""
    value = _IPV4_RE.sub("<IP>", value)
    value = _UUID_RE.sub("<UUID>", value)
    value = _HEX_RE.sub("<HEX>", value)
    value = _NUMBER_RE.sub("<N>", value)
    return re.sub(r"\s+", " ", value).strip()


def fingerprint_tokens(template: Any, *, limit: int = 160) -> list[str]:
    """模板 → token 集合。模板为空时返回空列表（调用方据此判定不可指纹匹配）。"""
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in _TOKEN_RE.findall(str(template or "").lower()):
        token = raw.strip("._:/-+")
        if len(token) < 2 or token in _TOKEN_STOP:
            continue
        if token.isdigit():
            continue
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)
        if len(tokens) >= limit:
            break
    return tokens


def source_category_of(item: dict[str, Any]) -> str:
    return str(item.get("source_category") or item.get("source_kind") or "").strip().lower()


def is_report_source(item: dict[str, Any]) -> bool:
    return source_category_of(item) in REPORT_SOURCE_CATEGORIES


def anomaly_rules_of(item: dict[str, Any]) -> list[dict[str, Any]]:
    rules = item.get("anomaly_rules")
    return [rule for rule in rules if isinstance(rule, dict)] if isinstance(rules, list) else []


def resolve_evidence_kind(item: dict[str, Any]) -> str:
    """判定举证类型。显式声明永远优先，其余按来源和字段特征保守推断。

    推断刻意保守：只有真的带着「异常规则」或日志管线字段（level / source_file / timestamp）
    才敢认定是运行日志；否则宁可当成自由文本用例片段，也不要把一段说明文字
    伪装成一条能参与指纹比对的日志。
    """
    explicit = str(item.get("evidence_kind") or "").strip().lower()
    if explicit in EVIDENCE_KIND_LABELS:
        return explicit
    if is_report_source(item):
        return EVIDENCE_KIND_CASE_REPORT
    if anomaly_rules_of(item):
        return EVIDENCE_KIND_RUNTIME_LOG
    if any(str(item.get(key) or "").strip() for key in ("level", "source_file", "timestamp", "subsystem", "module", "function_name")):
        return EVIDENCE_KIND_RUNTIME_LOG
    if str(item.get("template") or "").strip() and isinstance(item.get("tokens"), list) and item.get("tokens"):
        return EVIDENCE_KIND_RUNTIME_LOG
    return EVIDENCE_KIND_CASE_FRAGMENT


def evidence_text(item: dict[str, Any]) -> str:
    """举证的正文：raw 优先，其次 message。两者都空才算没有内容。"""
    return str(item.get("raw") or item.get("message") or "").strip()


def is_matchable(item: dict[str, Any]) -> bool:
    """能否参与指纹比对。

    只有「运行日志 + 命中异常规则」才算。报告证据和用例片段照常入库、
    照常用文本相似度检索，只是不参与指纹投票 —— 所以这是给界面看的质量信号，
    不是拒绝保存的理由。
    """
    kind = str(item.get("evidence_kind") or "").strip().lower() or resolve_evidence_kind(item)
    return kind == EVIDENCE_KIND_RUNTIME_LOG and bool(anomaly_rules_of(item))


def normalize_evidence_item(item: dict[str, Any]) -> dict[str, Any]:
    """补齐一条举证缺少的指纹字段，并写上判定结果。

    只做补齐，不做否决：调用方负责「内容为空」这一条真正的错误。
    """
    text = evidence_text(item)
    message = str(item.get("message") or "").strip() or text
    template = str(item.get("template") or "").strip()
    if not template:
        # 缺失的模板不是错误，而是可以推导出来的东西。
        template = normalize_template(message or text)
    tokens = item.get("tokens")
    if not isinstance(tokens, list) or not tokens:
        tokens = fingerprint_tokens(template)
    kind = resolve_evidence_kind({**item, "template": template, "tokens": tokens})
    normalized = {**item}
    normalized["raw"] = str(item.get("raw") or message or text).strip()
    normalized["message"] = message
    normalized["template"] = template
    normalized["tokens"] = tokens
    normalized["evidence_kind"] = kind
    normalized["evidence_kind_label"] = EVIDENCE_KIND_LABELS[kind]
    normalized["source_category"] = str(item.get("source_category") or item.get("source_kind") or "").strip()
    normalized["anomaly_rules"] = anomaly_rules_of(item)
    normalized["matchable"] = is_matchable(normalized)
    return normalized
