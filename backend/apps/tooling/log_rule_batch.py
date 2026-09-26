"""把**一批日志**整理成"可以批量建规则"的分组与候选配置。

用户要的是：AI 能按函数方法批量建规则，或者把**同类特征**的日志挑出来，一次给出多条
语义规则 / 标签规则，保存后立刻渲染。这里负责其中**确定性**的部分：

- :func:`parse_sample`  —— 从原始日志行（或模型给的结构化对象）里取出函数名、正文、组件、级别；
- :func:`message_signature` —— 把正文里的**变量**（`k=v` 的值、数字、十六进制、路径）抹掉，
  得到"同类特征"的签名：`step=*`/`wafer=*` 这种，同形状的日志归到一组；
- :func:`group_samples` —— 按函数名（`group_by="function"`）或按签名（`group_by="similar"`）分组；
- :func:`build_candidates` —— 每组产出一条**符合前端 DisplayRule 形状**的候选配置。

模型只负责"把这些分组写成中文语义/标签"（见 ``rule_autoconfig.autoconfigure_rule_batch``）；
模型不可用或返回不合法时，这里给出保守的兜底配置，绝不因为模型失败而整批放弃。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

#: 一条日志行里 `[file:Func:line]` 之后的正文；前面固定的 8 个方括号字段与日志格式约定一致。
_ENVELOPE_RE = re.compile(r"^(?:\[[^\]]*\]\s*){8}(?P<body>.*)$")
_FUNCTION_FIELD_RE = re.compile(r"\[[^\]]*?[:/](?P<func>[A-Za-z_~][\w:<>~.\-]*):\d+\]")
_CALL_PREFIX_RE = re.compile(r"^\s*\[?[A-Za-z_~][\w:<>~.\-]*\]?\s*\(?\s*\)?\s*(?:[<>]\s*\(\s*\)\s*)?")
_KEY_VALUE_RE = re.compile(r"\b(?P<key>[A-Za-z_][\w.]*)\s*=\s*(?P<value>\"[^\"]*\"|'[^']*'|[^\s,;]+)")
_LONG_HEX_RE = re.compile(r"\b(?:0x)?[0-9a-fA-F]{6,}\b")
_NUMBER_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])")
_PATH_RE = re.compile(r"(?:[A-Za-z]:\\[^\s]+|/(?:[\w.\-]+/){1,}[\w.\-]*)")
_WS_RE = re.compile(r"\s+")

#: 生成候选时的上限，避免一次任务把规则库灌满。
MAX_GROUPS = 24
MAX_PARAMETERS = 8


@dataclass
class Sample:
    """一条参与分组的日志。"""

    line: str
    function_name: str = ""
    message: str = ""
    component: str = ""
    level: str = ""
    count: int = 1

    @property
    def display(self) -> str:
        return self.line or self.message


@dataclass
class SampleGroup:
    """一组"同类特征"的日志。"""

    key: str
    label: str
    function_name: str
    component: str
    message: str
    line: str
    parameters: list[tuple[str, str]] = field(default_factory=list)
    #: 组内日志条数（由累加得到，初始 0，避免"1 条也显示 2"）。
    count: int = 0
    members: list[str] = field(default_factory=list)


def parse_sample(item: Any) -> Sample | None:
    """原始日志行 / 结构化对象 → :class:`Sample`。认不出来的直接丢弃。"""
    if isinstance(item, dict):
        line = str(item.get("line") or item.get("raw") or "").strip()
        message = str(item.get("message") or item.get("text") or "").strip()
        source = line or message
        if not source:
            return None
        parsed = parse_sample(source)
        if parsed is None:
            return None
        if message and not line:
            parsed.message = message
        parsed.function_name = str(item.get("function_name") or item.get("function") or parsed.function_name).strip()
        parsed.component = str(item.get("component") or parsed.component).strip()
        parsed.level = str(item.get("level") or parsed.level).strip()
        try:
            parsed.count = max(1, int(item.get("count") or parsed.count or 1))
        except (TypeError, ValueError):
            parsed.count = 1
        return parsed

    if not isinstance(item, str):
        return None
    text = item.strip()
    if not text:
        return None
    match = _ENVELOPE_RE.match(text)
    body = (match.group("body") if match else text).strip()
    function_field = _FUNCTION_FIELD_RE.search(text)
    function_name = function_field.group("func") if function_field else ""
    if not function_name:
        call = re.match(r"^\s*(?P<func>[A-Za-z_~][\w:<>~.\-]*)\s*\(\s*\)", body)
        function_name = call.group("func") if call else ""
    if function_name:
        function_name = f"{function_name}()"
    message = strip_call_prefix(body)
    if not message and not function_name:
        return None
    # 必须"看得出是日志"：有函数名、有八字段信封、或者至少有一个 k=v。
    # 否则模型随手传一句自然语言也会被当成一组日志，生成一堆没有意义的规则。
    if not function_name and match is None and not _KEY_VALUE_RE.search(body):
        return None
    return Sample(line=text, function_name=function_name, message=message or body)


def strip_call_prefix(body: str) -> str:
    """去掉正文开头的函数名与调用边界符，只留信息部分（和前端展示同一条规则）。"""
    text = str(body or "").strip()
    text = re.sub(r"^\[?[A-Za-z_~][\w:<>~.\-]*\]?\s*\(\s*\)\s*", "", text)
    text = re.sub(r"^[<>]\s*\(\s*\)\s*", "", text)
    return text.strip()


def message_signature(message: str) -> str:
    """把变量抹成占位符，得到"同类特征"签名。

    `enter stage homing stage start step=1/6 wafer=W01 dur=12.5ms`
    → `enter stage homing stage start step=* wafer=* dur=*`
    """
    text = strip_call_prefix(message)
    text = _KEY_VALUE_RE.sub(lambda match: f"{match.group('key')}=*", text)
    text = _PATH_RE.sub("/<path>", text)
    text = _LONG_HEX_RE.sub("<hex>", text)
    text = _NUMBER_RE.sub("<n>", text)
    return _WS_RE.sub(" ", text).strip()


def extract_parameters(message: str, limit: int = MAX_PARAMETERS) -> list[tuple[str, str]]:
    """正文里的 `k=v` → 参数样例（模板规则靠它把变量位置标出来）。"""
    parameters: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in _KEY_VALUE_RE.finditer(strip_call_prefix(message)):
        key = match.group("key").strip()
        value = match.group("value").strip().strip("\"'")
        if not key or not value or key in seen:
            continue
        seen.add(key)
        parameters.append((key, value))
        if len(parameters) >= limit:
            break
    return parameters


def group_samples(samples: Iterable[Sample], *, group_by: str = "similar") -> list[SampleGroup]:
    """按函数名或按"同类特征"签名分组；组内按出现次数排序。"""
    mode = "function" if str(group_by or "").strip().lower() == "function" else "similar"
    groups: dict[str, SampleGroup] = {}
    for sample in samples:
        if mode == "function":
            key = sample.function_name or message_signature(sample.message) or sample.display
            label = sample.function_name or key
        else:
            key = message_signature(sample.message) or sample.function_name or sample.display
            label = key
        group = groups.get(key)
        if group is None:
            group = SampleGroup(
                key=key,
                label=label,
                function_name=sample.function_name,
                component=sample.component,
                message=sample.message,
                line=sample.display,
                parameters=extract_parameters(sample.message),
            )
            groups[key] = group
        group.count += max(1, sample.count)
        if len(group.members) < 6:
            group.members.append(sample.display)
        # 参数要取**整组**的并集：组里每行可能带不同的 key（dof/point/code/cause/trace…）。
        # 只用第一行的参数，模型引用到别的 key 时就会因"占位符没有对应参数"被前端校验直接拦下。
        existing = {key for key, _ in group.parameters}
        for key, value in extract_parameters(sample.message):
            if key in existing or len(group.parameters) >= MAX_PARAMETERS:
                continue
            existing.add(key)
            group.parameters.append((key, value))
        if not group.function_name and sample.function_name:
            group.function_name = sample.function_name
        if not group.component and sample.component:
            group.component = sample.component
    ordered = sorted(groups.values(), key=lambda item: (-item.count, item.label))
    return ordered[:MAX_GROUPS]


def sanitize_placeholders(text: str, labels: Iterable[str]) -> tuple[str, list[str]]:
    """把模板里**没有对应参数**的 `{x}` 去掉，返回 (清理后的文本, 被去掉的名字)。

    前端的 `validateDisplayRule` 会直接拒绝「语义/标签里引用了不存在的参数」，所以模型多写一个
    `{trace}` 就会让整条候选被跳过（实测 10 条里 9 条因此被拦）。这里先清理并留痕，规则才能真正用上。"""
    known = {str(label).strip() for label in labels if str(label).strip()}
    dropped: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        if name in known:
            return match.group(0)
        if name:
            dropped.append(name)
        return ""

    # 先处理 `key={未知参数}` 这种成对写法：把 `key=` 一起去掉，别留下 `code=` 这种半截。
    def replace_paired(match: "re.Match[str]") -> str:
        name = match.group(1).strip()
        if name in known:
            return match.group(0)
        if name:
            dropped.append(name)
        return ""

    paired = re.sub(r"[A-Za-z_][\w.]*\s*[=:]\s*\{([^{}]+)\}", replace_paired, str(text or ""))
    cleaned = re.sub(r"\{([^{}]+)\}", replace, paired)
    # 去掉占位符后容易留下 ", ," "，，" 这种残渣。
    cleaned = re.sub(r"([，,、;；])\s*(?=[，,、;；])", "", cleaned)
    cleaned = re.sub(r"\s+([，,、;；。])", r"\1", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    cleaned = cleaned.strip("，,、;；")
    return cleaned, dropped


def candidate_spec(
    group: SampleGroup,
    *,
    mode: str,
    name: str = "",
    semantic: str = "",
    label: str = "",
    color: str = "",
    scope: str = "",
    supplemental: str = "",
) -> dict[str, Any]:
    """一组日志 → 一条前端 DisplayRule 形状的候选配置。

    按函数名分组时用 **keyword 规则**（关键字就是函数名，最稳）；
    按同类特征分组时用 **template 规则**（用参数把变量位置标出来，形状一致就命中）。
    """
    display_mode = str(mode or "semantic").strip().lower()
    if display_mode not in {"semantic", "label", "both"}:
        display_mode = "semantic"
    by_function = bool(group.function_name) and group.label == group.function_name
    keyword = group.function_name if by_function else ""
    kind = "keyword" if keyword else "template"

    fallback_name = group.function_name or _shorten(group.label or group.message, 40)
    resolved_name = (name or fallback_name).strip()[:120] or "AI 批量规则"
    semantic_text = (semantic or "").strip()[:600] or _fallback_semantic(group)
    label_text = (label or "").strip()[:60] or _fallback_label(group)
    if display_mode == "semantic":
        label_text = ""
    if display_mode == "label" and not semantic_text:
        semantic_text = _fallback_semantic(group)
    if display_mode == "label":
        semantic_text = semantic_text if semantic else ""
        if not label_text:
            display_mode = "both" if semantic_text else "semantic"

    parameters = [
        {"label": key, "sample_value": value}
        for key, value in group.parameters
        if value and (value in group.message or any(value in member for member in group.members))
    ][:MAX_PARAMETERS]
    parameter_labels = [item["label"] for item in parameters]
    semantic_text, dropped_semantic = sanitize_placeholders(semantic_text, parameter_labels)
    label_text, dropped_label = sanitize_placeholders(label_text, parameter_labels)
    if kind == "template" and not parameters:
        # 模板规则必须有参数样例，否则保存校验会直接拦下；没有 k=v 就退回关键字规则。
        kind = "keyword"
        keyword = _stable_keyword(group)

    return {
        "id": f"ai-batch-{abs(hash(group.key)) % 10**8}",
        "name": resolved_name,
        "enabled": True,
        "kind": kind,
        "scope": scope if scope in {"function", "log", "both"} else ("function" if by_function else "log"),
        "keyword": keyword,
        "sample_message": group.message or group.line,
        "parameters": parameters,
        "display_template": semantic_text,
        "display_mode": display_mode,
        "custom_label_template": label_text,
        "custom_label_color": color if re.fullmatch(r"#[0-9a-fA-F]{6}", color or "") else _fallback_color(group),
        "show_label_on_timeline": True,
        "supplemental_description": supplemental.strip()[:600],
        "group": {
            "label": group.label,
            "count": group.count,
            "function_name": group.function_name,
            "component": group.component,
            "members": group.members[:4],
        },
        # 模型写了但样例里没有对应参数的占位符（已从文案里去掉，供上层提示用户）。
        "dropped_placeholders": sorted({*dropped_semantic, *dropped_label})[:8],
    }


def build_candidates(
    groups: Iterable[SampleGroup],
    *,
    mode: str,
    suggestions: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """分组 + （可选的）模型建议 → 候选配置列表。"""
    hints = suggestions or {}
    candidates: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        hint = hints.get(group.key) or hints.get(str(index)) or {}
        candidates.append(
            candidate_spec(
                group,
                mode=mode,
                name=str(hint.get("name") or ""),
                semantic=str(hint.get("semantic") or ""),
                label=str(hint.get("label") or ""),
                color=str(hint.get("color") or ""),
                scope=str(hint.get("scope") or ""),
                supplemental=str(hint.get("supplemental") or ""),
            )
        )
    return candidates


def dedupe_candidates(candidates: list[dict[str, Any]], existing_keywords: Iterable[str] = ()) -> tuple[list[dict[str, Any]], int]:
    """去掉重复（同关键字/同模板正文）以及与已有规则冲突的候选，返回 (保留的, 丢弃数)。"""
    seen = {str(item or "").strip().lower() for item in existing_keywords if str(item or "").strip()}
    kept: list[dict[str, Any]] = []
    dropped = 0
    for candidate in candidates:
        signature = f"{candidate.get('kind')}|{candidate.get('keyword')}|{candidate.get('sample_message')}".lower()
        keyword = str(candidate.get("keyword") or "").strip().lower()
        if (keyword and keyword in seen) or signature in seen:
            dropped += 1
            continue
        seen.add(signature)
        if keyword:
            seen.add(keyword)
        kept.append(candidate)
    return kept, dropped


def _shorten(text: str, limit: int) -> str:
    value = _WS_RE.sub(" ", str(text or "")).strip()
    return value if len(value) <= limit else f"{value[: limit - 1]}…"


def _fallback_semantic(group: SampleGroup) -> str:
    """模型不可用时的语义说明：直接用"函数名 + 同类特征"讲清楚这段日志是什么。"""
    parts = [item for item in (group.function_name, group.component) if item]
    text = " · ".join(parts) if parts else _shorten(group.line, 60)
    if group.label and group.label != group.function_name:
        text = f"{text}：{_shorten(group.label, 80)}" if text else _shorten(group.label, 80)
    return text.strip() or "AI 批量规则（待补充语义说明）"


def _fallback_label(group: SampleGroup) -> str:
    text = group.function_name or group.label or group.message
    return _shorten(text, 10)


def _fallback_color(group: SampleGroup) -> str:
    """按签名关键词挑一个语义色，避免一批规则全是同一个蓝。"""
    palette = ["#2563eb", "#7c3aed", "#0891b2", "#16a34a", "#d97706", "#db2777", "#4f46e5", "#0ea5e9"]
    return palette[abs(hash(group.key)) % len(palette)]


def _stable_keyword(group: SampleGroup) -> str:
    """从正文里挑一段稳定的字面量当关键字（去掉变量之后最长的一段）。"""
    text = message_signature(group.message) or group.label
    chunks = [chunk.strip() for chunk in re.split(r"[=*]+|,|;", text) if len(chunk.strip()) >= 4]
    if not chunks:
        return _shorten(group.function_name or group.line, 40)
    return _shorten(max(chunks, key=len), 60)
