"""一键自动配置：把一条真实日志样例变成可用的数据提取器 / 语义规则配置。

设计约束（和「整理成案例」同一条原则）
--------------------------------------
按钮是**确定性触发**，只有「识别」这一步用模型。识别结果必须能被真实样例验证：
模型给出的字段值、参数值都要在样例原文里**逐字出现**，否则丢弃并写进 warnings。
模型看不见的东西就是不存在 —— 编一个样例里没有的参数名，规则保存后必然一条都提不出来，
而这种错误用户在配置界面里很难发现。

另外，单位换算、字典路径这类东西不交给模型凭空生成：
- 字典字段直接用前端已有的 `detectStructuredDataCandidates` 结果做交叉校验；
- 单位只在样例里真的出现单位后缀时才配置，且换算关系必须能由模型给出的等价式自洽推出。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from apps.tooling.llm import get_llm_client

logger = logging.getLogger("tracelens.tooling.rule_autoconfig")

MAX_SAMPLE_CHARS = 6000
MAX_FIELDS = 24
_JSON_BLOCK = re.compile(r"\{[\s\S]*\}")

VALUE_TYPES = ("number", "integer", "boolean", "string")
OUTPUT_FORMATS = ("table", "text")
DISPLAY_SCOPES = ("function", "log", "both")
DISPLAY_MODES = ("semantic", "label", "both")

_COMMON_RULES = """通用规则：
1. 只能使用样例里**真实出现**的文本。样例里没有的字段名、参数值、单位一律不要写。
2. 只输出一个 JSON 对象，不要解释文字、不要 markdown 代码块。
3. 拿不准就留空，并把原因写进 warnings；不要用猜测把字段填满。
4. `sampleValue` 必须是样例原文中的一个连续子串（逐字复制，不要改写、不要加引号）。
"""

_DATA_EXTRACTOR_PROMPT = """你是 TraceLens 的数据提取器配置助手。

用户给你一段真实日志样例（可能多行）。你要判断这段日志里有哪些**可周期采集的参数**，
并给出可直接保存的提取器配置。

""" + _COMMON_RULES + """
字段规则：
- `name` 是数据集名称（简短、可检索，说明采的是什么参数）。
- `matchKeyword` 可选：样例里能唯一标识这类日志的稳定短语（不要用会变化的时间/数值）。
- `key` 是日志里出现的参数名原文；`name` 是保存到数据表的字段名（英文/下划线优先）。
- 只挑**数值型或布尔型**的稳定参数；纯文本段落不要当字段。
- `valueType` 只能是 number / integer / boolean / string。
- 数值带单位后缀时（例如 12.5 ms、3.4 mm），填 `sourceUnit`（如 ms、mm）；
  只有样例里确实出现了多个等价单位时，才填 `unitConversions`
  （形如 {"leftValue":1000,"leftUnit":"us","rightValue":1,"rightUnit":"ms"}，必须等价）。
- `outputFormat`：多个字段用 table，单个文本指标用 text。

输出字段：
{"name": "数据集名称", "description": "一句话说明", "matchKeyword": "稳定短语或空串",
 "caseSensitive": false, "subsystems": [], "modules": [], "sourceCategories": [],
 "outputFormat": "table",
 "fields": [{"key": "日志里的参数名", "name": "保存字段名", "valueType": "number",
             "sampleValue": "样例里的参数值原文", "sourceUnit": "", "plotUnit": "",
             "unitConversions": [], "why": "为什么这是可采集参数"}],
 "confidence": "high|medium|low", "warnings": ["样例里没有找到的东西"]}"""

_SEMANTIC_FOCUS_HINTS = {
    "semantic": "本次只关心语义说明：displayTemplate 必须给出；parameters 按 template 类型照常给。customLabelTemplate 留空。",
    "label_with_parameters": (
        "本次只关心自定义标签，而且这条规则已经有一套参数："
        "customLabelTemplate 里的 {占位符} 只能引用 `已有参数名` 里列出的名字，"
        "绝对不要发明新的参数名（发明出来的占位符不会被替换，规则会直接判为无效）。"
    ),
    "label": (
        "本次只关心自定义标签：customLabelTemplate 必须给出（简短，可含 {参数名}），"
        "customLabelColor 必须给一个 #RRGGBB，displayMode 必须包含 label。"
        "displayTemplate 可以留空。"
    ),
}

_SEMANTIC_RULE_PROMPT = """你是 TraceLens 的日志语义规则配置助手。

用户给你一条真实日志（可能多行），以及这条规则要用的类型。你要给出一条可直接保存的
语义规则配置：让日志里晦涩的函数名/正文，变成一句人能直接读懂的中文说明。

""" + _COMMON_RULES + """
按类型：
- 类型 `keyword`（关键字替换）：`keyword` 填日志的函数名或正文里出现的稳定关键字；
  `displayTemplate` 是一句中文说明。不要编造 keyword 里没有的函数。
- 类型 `template`（智能参数模板）：先用 `parameters` 列出正文里**会变化**的部分
  （每个参数的 `sampleValue` 必须是样例里的原文子串，`label` 是中文参数名），
  再在 `displayTemplate` 里用 `{参数名}` 引用它们。固定部分保持原样。
- `supplementalDescription` 可选：异常含义、处置建议或排查策略（没有就留空）。
- `customLabelTemplate` 可选：日志行右侧的短标签，可含 `{参数名}`；没有合适内容就留空。
- `customLabelColor`：形如 #2563eb，只在真的适合做标签时给。
- `scope` 只能是 function / log / both；`displayMode` 只能是 semantic / label / both。

输出字段：
{"name": "规则名称", "keyword": "稳定关键字或空串", "sampleMessage": "用于匹配的正文（样例原文）",
 "parameters": [{"label": "中文参数名", "sampleValue": "样例原文子串"}],
 "displayTemplate": "中文语义说明，可含 {参数名}", "supplementalDescription": "",
 "customLabelTemplate": "", "customLabelColor": "", "scope": "both", "displayMode": "semantic",
 "confidence": "high|medium|low", "warnings": ["不确定的地方"]}"""


def _parse_json_object(raw: str) -> dict[str, Any] | None:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    match = _JSON_BLOCK.search(text)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _chat_json(system_prompt: str, payload: dict[str, Any]) -> dict[str, Any]:
    client = get_llm_client()
    response = client.chat([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)[:MAX_SAMPLE_CHARS + 4000]},
    ])
    try:
        raw = str(response.choices[0].message.content or "")
    except Exception:  # noqa: BLE001
        raw = ""
    parsed = _parse_json_object(raw)
    if parsed is None:
        logger.warning("rule_autoconfig.parse_failed raw=%s", raw[:400])
        raise ValueError("模型没有返回可解析的配置，请重试或把样例补得更完整一些。")
    return parsed


def _text_list(value: Any, limit: int = 12) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text[:128])
    return result[:limit]


def _in_sample(sample: str, value: str) -> bool:
    """样例里是否逐字出现过这个片段。

    这是「AI 识别」和「AI 编造」的分界线：模型很擅长把参数名写得像模像样，
    但只有真的出现在原文里的值才提取得出来。
    """
    return bool(value) and value in sample


def _clamp_confidence(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in {"high", "medium", "low"} else "medium"


def _normalize_unit_conversions(raw: Any, sample: str) -> list[dict[str, Any]]:
    """只保留能在样例里找到依据、且左右真的等价的换算规则。

    单位换算配错会让绘图数值整体差一个量级，比不配更危险：宁可少配。
    """
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        try:
            left_value = float(item.get("leftValue"))
            right_value = float(item.get("rightValue"))
        except (TypeError, ValueError):
            continue
        left_unit = str(item.get("leftUnit") or "").strip()[:24]
        right_unit = str(item.get("rightUnit") or "").strip()[:24]
        if left_value <= 0 or right_value <= 0 or not left_unit or not right_unit or left_unit == right_unit:
            continue
        if not (_in_sample(sample, left_unit) or _in_sample(sample, right_unit)):
            continue
        result.append({
            "id": f"ai-unit-{index}-{left_unit}-{right_unit}",
            "leftValue": left_value,
            "leftUnit": left_unit,
            "rightValue": right_value,
            "rightUnit": right_unit,
        })
        if len(result) >= 6:
            break
    return result


_DATA_FOCUS_HINTS = {
    "fields": "本次只关心字段定义：把 fields 填准，作用范围可以留空。",
    "units": (
        "本次只关心单位：对每个字段给出 sourceUnit（日志里的单位后缀）；"
        "只有当样例里确实出现了两个等价单位时才给 unitConversions，否则留空。"
        "plotUnit 只在明显更适合统一到另一个单位时才填。"
    ),
    "scope": "本次只关心作用范围：尽量根据日志内容判断 subsystems / modules / sourceCategories；fields 可以是空数组。",
}


def autoconfigure_data_extractor(
    sample: str,
    *,
    hints: dict[str, Any] | None = None,
    structured_candidates: list[dict[str, Any]] | None = None,
    focus: str = "all",
) -> dict[str, Any]:
    """一条真实日志样例 → 可直接填入「新增数据提取器」抽屉的配置。

    `focus` 让不同按钮的承诺落到实处：配置框里的按钮只填它负责的那部分，
    不去动用户已经调好的其它字段。
    """
    text = str(sample or "").strip()[:MAX_SAMPLE_CHARS]
    if not text:
        raise ValueError("请先粘贴或从当前日志带入样例，AI 才能识别要采集什么。")
    hints = hints or {}
    candidates = [item for item in (structured_candidates or []) if isinstance(item, dict)]
    candidate_paths = {str(item.get("displayPath") or item.get("path") or "").strip() for item in candidates}

    focus_note = _DATA_FOCUS_HINTS.get(str(focus or "all").strip().lower(), "")
    payload = {
        "任务": "识别这段日志里可周期采集的参数，输出数据提取器配置。" + (f" {focus_note}" if focus_note else ""),
        "样例日志": text,
        "样例来源": {
            "子系统": _text_list(hints.get("subsystems")),
            "FM/模块": _text_list(hints.get("modules")),
            "日志类型": _text_list(hints.get("sourceCategories")),
            "函数名": str(hints.get("functionName") or "")[:200],
        },
        "已识别到的字典字段（可直接引用 displayPath）": [
            {"displayPath": str(item.get("displayPath") or ""), "sampleValue": str(item.get("sampleValue") or "")[:80]}
            for item in candidates[:60]
        ],
    }
    fields_raw = _chat_json(_DATA_EXTRACTOR_PROMPT, payload)

    warnings = _text_list(fields_raw.get("warnings"))
    fields: list[dict[str, Any]] = []
    dropped: list[str] = []
    seen_names: set[str] = set()
    for index, item in enumerate(fields_raw.get("fields") or []):
        if not isinstance(item, dict) or len(fields) >= MAX_FIELDS:
            continue
        key = str(item.get("key") or "").strip()[:128]
        name = str(item.get("name") or "").strip()[:64]
        sample_value = str(item.get("sampleValue") or "").strip()[:128]
        path = item.get("structuredPath") if isinstance(item.get("structuredPath"), list) else []
        display_path = ".".join(str(part) for part in path)
        if not key or not name:
            continue
        # 结构化字典字段：样例里不一定有字面 key，允许用「已识别到的字典路径」作为依据。
        structured_ok = bool(display_path) and display_path in candidate_paths
        if not structured_ok and not _in_sample(text, sample_value or key):
            dropped.append(f"{key}（样例里找不到 {sample_value or key}）")
            continue
        if name in seen_names:
            dropped.append(f"{key}（保存字段名 {name} 重复）")
            continue
        seen_names.add(name)
        value_type = str(item.get("valueType") or "").strip().lower()
        fields.append({
            "id": f"ai-field-{index}-{re.sub(r'[^a-zA-Z0-9_]+', '_', name) or index}",
            "key": key,
            "name": name,
            "sampleValue": sample_value,
            "valueType": value_type if value_type in VALUE_TYPES else "string",
            "sourceUnit": str(item.get("sourceUnit") or "").strip()[:24],
            "plotUnit": str(item.get("plotUnit") or "").strip()[:24],
            "unitConversionEnabled": False,
            "unitConversions": _normalize_unit_conversions(item.get("unitConversions"), text),
            "structuredPath": [str(part) for part in path][:8],
            "why": str(item.get("why") or "").strip()[:200],
        })
        if fields[-1]["unitConversions"]:
            fields[-1]["unitConversionEnabled"] = True
    if not fields and str(focus or "").strip().lower() != "scope":
        raise ValueError("没能从这段样例里识别出可采集的参数；请确认样例里包含参数名和参数值。")

    output_format = str(fields_raw.get("outputFormat") or "").strip().lower()
    result = {
        "ok": True,
        "target": "data_extractor",
        "name": str(fields_raw.get("name") or "").strip()[:128],
        "description": str(fields_raw.get("description") or "").strip()[:255],
        "matchKeyword": str(fields_raw.get("matchKeyword") or "").strip()[:255],
        "caseSensitive": bool(fields_raw.get("caseSensitive", False)),
        "subsystems": _text_list(fields_raw.get("subsystems")) or _text_list(hints.get("subsystems")),
        "modules": _text_list(fields_raw.get("modules")) or _text_list(hints.get("modules")),
        "sourceCategories": _text_list(fields_raw.get("sourceCategories")) or _text_list(hints.get("sourceCategories")),
        "outputFormat": output_format if output_format in OUTPUT_FORMATS else "table",
        "fields": fields,
        "confidence": _clamp_confidence(fields_raw.get("confidence")),
    }
    if dropped:
        warnings.append("已丢弃样例里找不到依据的字段：" + "、".join(dropped[:6]))
    result["warnings"] = warnings[:8]
    return result


_BATCH_RULE_PROMPT = """你在给 TraceLens 的日志规则批量起名字。

输入是一批**已经分好组**的日志（每组代表一类日志：要么是同一个函数方法，要么是同一类特征
——正文里只有变量不同）。请为每组写：

- `semantic`：一句中文语义说明，告诉工程师这段日志在干什么（可含 {参数名}，只能用给出的参数名）。
- `label`：日志行右侧的短标签，最多 10 个字，能一眼分类（例如「回零位」「版本校验」）。
- `color`：适合这个语义的十六进制颜色（例如异常 #dc2626、流程开始 #10b981、建模 #8b5cf6）。
- `scope`：function（整个函数折叠成一条语义）/ log（逐行日志）/ both。
- `supplemental`：可选，异常含义或排查建议；没有就留空。
- `name`：规则名称（简短中文，可与 label 相同）。

通用规则：
1. 只根据给出的日志内容写，不要编造日志里没有的业务含义。
2. 只输出一个 JSON 对象，不要解释文字、不要 markdown 代码块。
3. 每个输入组都要有一条输出，`index` 必须与输入组的下标一致。
4. 拿不准就把 `semantic` 写得保守一些（例如"回零位流程开始"），不要瞎猜专有名词。

输出格式：
{"rules": [{"index": 0, "name": "回零位开始", "semantic": "回零位流程开始", "label": "回零位",
            "color": "#10b981", "scope": "both", "supplemental": ""}], "warnings": ["不确定的地方"]}"""


def autoconfigure_rule_batch(
    groups: list[dict[str, Any]],
    *,
    mode: str = "semantic",
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """一批分组 → 每组一条中文语义/标签建议。

    返回 ``({分组下标: 建议}, warnings)``。**模型不可用时返回空建议**，调用方会退回
    确定性兜底（函数名 + 同类特征），所以批量生成不会因为模型抽风而整批失败。
    """
    warnings: list[str] = []
    payload_groups = []
    for index, group in enumerate(groups):
        payload_groups.append({
            "index": index,
            "函数方法": str(group.get("function_name") or ""),
            "同类特征": str(group.get("label") or ""),
            "组件": str(group.get("component") or ""),
            "出现次数": int(group.get("count") or 1),
            "参数": [f"{key}={value}" for key, value in (group.get("parameters") or [])],
            "样例日志": [str(item)[:400] for item in (group.get("members") or [])][:3],
        })
    if not payload_groups:
        return {}, warnings
    wanted_mode = "标签" if str(mode) == "label" else "语义说明" if str(mode) == "semantic" else "语义说明和标签"
    payload = {
        "任务": f"为下面每个分组生成{wanted_mode}。",
        "目标模式": str(mode),
        "分组数": len(payload_groups),
        "分组": payload_groups,
    }
    try:
        raw = _chat_json(_BATCH_RULE_PROMPT, payload)
    except Exception as exc:  # noqa: BLE001 - 模型不可用/返回不可解析都要退回兜底
        logger.warning("rule_autoconfig.batch_llm_failed error=%s", exc)
        return {}, [f"AI 没能给出语义建议（{exc}），已用函数名/同类特征兜底。"]
    warnings.extend(_text_list(raw.get("warnings"))[:6])
    suggestions: dict[str, dict[str, Any]] = {}
    for item in raw.get("rules") or []:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if index < 0 or index >= len(payload_groups):
            continue
        color = str(item.get("color") or "").strip()
        scope = str(item.get("scope") or "").strip().lower()
        suggestions[str(index)] = {
            "name": str(item.get("name") or "").strip()[:120],
            "semantic": str(item.get("semantic") or "").strip()[:600],
            "label": str(item.get("label") or "").strip()[:60],
            "color": color if re.fullmatch(r"#[0-9a-fA-F]{6}", color) else "",
            "scope": scope if scope in DISPLAY_SCOPES else "",
            "supplemental": str(item.get("supplemental") or "").strip()[:600],
        }
    if len(suggestions) < len(payload_groups):
        warnings.append(f"有 {len(payload_groups) - len(suggestions)} 组没有拿到 AI 语义，已用兜底文案。")
    return suggestions, warnings


def autoconfigure_semantic_rule(
    sample: str,
    *,
    kind: str = "keyword",
    hints: dict[str, Any] | None = None,
    focus: str = "all",
) -> dict[str, Any]:
    """一条真实日志 → 可直接填入「新增语义规则」抽屉的配置。

    `focus` 同上：语义说明框和自定义标签框各有自己的按钮，各自只填自己那块。
    """
    text = str(sample or "").strip()[:MAX_SAMPLE_CHARS]
    if not text:
        raise ValueError("请先粘贴样例日志，AI 才能识别这条日志该显示成什么语义。")
    rule_kind = "template" if str(kind or "").strip().lower() == "template" else "keyword"
    hints = hints or {}

    rule_focus = str(focus or "all").strip().lower()
    existing_labels = _text_list(hints.get("parameterLabels"), limit=20)
    focus_note = _SEMANTIC_FOCUS_HINTS.get(rule_focus, "")
    if rule_focus == "label" and existing_labels:
        focus_note = _SEMANTIC_FOCUS_HINTS["label_with_parameters"]
    payload = {
        "任务": "把这条日志里晦涩的函数名/正文，改写成一句人能直接读懂的中文语义说明。"
                + (f" {focus_note}" if focus_note else ""),
        "规则类型": rule_kind,
        "样例日志": text,
        "模板正文（template 类型时以它为准）": str(hints.get("sampleMessage") or "")[:MAX_SAMPLE_CHARS] or None,
        "样例来源": {
            "子系统": _text_list(hints.get("subsystems")),
            "FM/模块": _text_list(hints.get("modules")),
            "函数名": str(hints.get("functionName") or "")[:200],
        },
        "已有参数名（只能引用这些）": existing_labels or None,
    }
    raw = _chat_json(_SEMANTIC_RULE_PROMPT, payload)

    warnings = _text_list(raw.get("warnings"))
    # 匹配正文的权威顺序：浏览器已经算好的正文 > 模型给出的（必须是原文子串）> 样例最长行。
    # 浏览器那份是确定性的（它知道「第 8 个字段之后是正文」这个格式约定），模型只能猜。
    hinted_message = str(hints.get("sampleMessage") or "").strip()[:MAX_SAMPLE_CHARS]
    model_message = str(raw.get("sampleMessage") or "").strip()[:MAX_SAMPLE_CHARS]
    if hinted_message and _in_sample(text, hinted_message):
        sample_message = hinted_message
    elif model_message and _in_sample(text, model_message):
        sample_message = model_message
    else:
        sample_message = max(text.splitlines() or [""], key=len).strip()
        if model_message:
            warnings.append("模型给出的匹配正文与样例不一致，已回退为样例中最长的一行。")

    parameters: list[dict[str, Any]] = []
    dropped: list[str] = []
    seen_labels: set[str] = set()
    for index, item in enumerate(raw.get("parameters") or []):
        if not isinstance(item, dict) or len(parameters) >= 12:
            continue
        label = str(item.get("label") or "").strip()[:64]
        value = str(item.get("sampleValue") or "").strip()[:255]
        if not label or not value:
            continue
        if not _in_sample(sample_message, value):
            dropped.append(f"{label}={value}")
            continue
        if label in seen_labels:
            dropped.append(f"{label}（参数名重复）")
            continue
        seen_labels.add(label)
        parameters.append({"id": f"ai-param-{index}-{label}", "label": label, "sampleValue": value})
    if dropped:
        warnings.append("已丢弃正文里找不到的参数：" + "、".join(dropped[:6]))

    # 模板里引用的参数必须真的存在，否则保存后日志显示成字面量 {参数名}。
    display_template = str(raw.get("displayTemplate") or "").strip()[:2000]
    used = set(re.findall(r"\{([^{}]+)\}", display_template))
    known = {item["label"] for item in parameters} | set(existing_labels)
    if used - known:
        warnings.append("语义说明里引用了不存在的参数，已改用剩余说明文本：" + "、".join(sorted(used - known)[:4]))
        display_template = re.sub(r"\{[^{}]+\}", "", display_template).strip()

    scope = str(raw.get("scope") or "").strip().lower()
    display_mode = str(raw.get("displayMode") or "").strip().lower()
    color = str(raw.get("customLabelColor") or "").strip()[:32]
    if color and not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        color = ""
    custom_label = str(raw.get("customLabelTemplate") or "").strip()[:500]
    # 标签里出现不存在的参数名会写进规则却永远替换不了，保存校验也会直接拦下来
    # （界面提示「自定义标签中的 {X} 没有对应参数」）。这里先把它们清掉并说明。
    known_labels = {item["label"] for item in parameters} | set(existing_labels)
    label_used = set(re.findall(r"\{([^{}]+)\}", custom_label))
    if label_used - known_labels:
        unknown = sorted(label_used - known_labels)
        warnings.append("标签里引用了不存在的参数，已去掉占位符：" + "、".join(unknown[:4]))
        custom_label = re.sub(r"\{[^{}]+\}", "", custom_label).strip()
    if rule_focus == "label":
        # 按钮承诺的是「生成标签」，那就必须真的给一个；给不出来要明说，不能静默空着。
        if not custom_label:
            warnings.append("这次没能从样例里提炼出合适的标签文本，可以手动填写或用语义说明框的按钮。")
        if not color:
            color = "#2563eb"
        display_mode = "label" if (display_mode or "semantic") == "semantic" else display_mode
        if display_mode == "semantic":
            display_mode = "both"
    elif custom_label and display_mode == "semantic":
        # 给了标签却说「只显示语义」是自相矛盾的：标签只有 displayMode 含 label 才会被渲染，
        # 留着这个组合会让界面上的「自定义标签」配置框根本不出现。
        display_mode = "both"
    elif not custom_label and display_mode in {"label", "both"}:
        display_mode = "semantic"
    if rule_focus == "semantic" and display_mode in {"label", "both"} and not custom_label:
        display_mode = "semantic"

    return {
        "ok": True,
        "target": "semantic_rule",
        "kind": rule_kind,
        "name": str(raw.get("name") or "").strip()[:128],
        "keyword": str(raw.get("keyword") or "").strip()[:255] if rule_kind == "keyword" else "",
        "sampleMessage": sample_message,
        "parameters": parameters,
        "displayTemplate": display_template,
        "supplementalDescription": str(raw.get("supplementalDescription") or "").strip()[:2000],
        "customLabelTemplate": custom_label,
        "customLabelColor": color,
        "scope": scope if scope in DISPLAY_SCOPES else "both",
        "displayMode": display_mode if display_mode in DISPLAY_MODES else "semantic",
        "confidence": _clamp_confidence(raw.get("confidence")),
        "warnings": warnings[:8],
    }
