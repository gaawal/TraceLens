"""Server-side evaluation of the user's semantic rules (``display_rules``).

The authoritative definition of "what does this log line mean" lives in the rule settings
page and is stored in ``ResourceSettings.display_rules``. A headless watcher cannot use the
frontend's implementation, so this module is a faithful Python port of
``frontend/src/rendering/displayRules.ts``:

* ``extract_display_rule_message`` mirrors ``extractDisplayRuleMessage`` — first non-empty
  line, with up to eight leading ``[...]`` fields stripped.
* keyword rules are a **case-sensitive substring** test on that message.
* template rules compile the visual ``patternTokens`` into an anchored regex whose
  parameters are non-greedy ``([\\s\\S]+?)`` captures, with literal text escaped and runs
  of whitespace collapsed to ``\\s+``.

Keeping the two in step matters: if the server matched differently from the UI, a tag
would light up on the timeline but never fire a watch (or the reverse).

One deliberate difference: this module **validates** rules instead of silently dropping
them. The frontend's ``safeParseRules`` discards malformed entries, which is fine for a UI
but would make a watcher quietly observe nothing.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger("tracelens.logsources.watch_rules")

# `^(?:\[[^\]]*]\s*){8}(.*)$` — the eight standard log fields in front of the message body.
_LEADING_FIELDS = re.compile(r"^(?:\[[^\]]*\]\s*){8}(.*)$", re.DOTALL)


def extract_display_rule_message(raw: str) -> str:
    """First non-empty line, minus the up-to-eight leading ``[...]`` fields."""
    text = str(raw or "")
    first = ""
    for line in re.split(r"\r?\n", text):
        if line.strip():
            first = line.strip()
            break
    match = _LEADING_FIELDS.match(first)
    return (match.group(1) if match else first).strip()


def _escape_reg_exp(value: str) -> str:
    return re.escape(value)


def _compile_fixed_text(value: str) -> str:
    """Literal text with whitespace runs collapsed, matching ``compileFixedText``."""
    parts = re.split(r"(\s+)", value)
    return "".join(r"\s+" if re.fullmatch(r"\s+", part) else _escape_reg_exp(part) for part in parts if part)


@dataclass
class CompiledRule:
    rule_id: str
    name: str
    kind: str
    pattern: re.Pattern[str] | None = None
    keyword: str = ""
    # Extraction rules carry a user-facing "区分大小写" switch; semantic rules never did.
    case_sensitive: bool = True
    parameter_ids: list[str] = field(default_factory=list)
    capture_index_by_parameter: dict[str, int] = field(default_factory=dict)
    # Presentation copied from the rule so a hit can be rendered without a second lookup.
    label_template: str = ""
    label_color: str = "#2563eb"
    display_template: str = ""
    display_mode: str = "semantic"
    show_on_timeline: bool = True
    supplemental: str = ""

    def matches(self, raw: str) -> re.Match[str] | None | bool:
        """Return the regex match for template rules, a bool-ish for keyword rules."""
        if self.kind == "keyword":
            message = extract_display_rule_message(raw)
            if self.case_sensitive:
                return self.keyword in message
            return self.keyword.casefold() in message.casefold()
        if self.pattern is None:
            return None
        return self.pattern.search(extract_display_rule_message(raw))


def compile_display_rule(rule: Any) -> tuple[CompiledRule | None, str]:
    """Compile one rule. Returns ``(compiled, "")`` or ``(None, reason)``."""
    if not isinstance(rule, dict):
        return None, "规则不是对象"
    if rule.get("enabled", True) is False:
        return None, "规则已停用"
    rule_id = str(rule.get("id") or "").strip()
    if not rule_id:
        return None, "规则缺少 id"

    display_mode = str(rule.get("displayMode") or "semantic")
    common = {
        "rule_id": rule_id,
        "name": str(rule.get("name") or "").strip(),
        "label_template": str(rule.get("customLabelTemplate") or "").strip(),
        "label_color": str(rule.get("customLabelColor") or "#2563eb"),
        "display_template": str(rule.get("displayTemplate") or ""),
        "display_mode": display_mode,
        "show_on_timeline": bool(rule.get("showLabelOnTimeline", True)),
        "supplemental": str(rule.get("supplementalDescription") or "").strip(),
    }

    kind = str(rule.get("kind") or "keyword")
    if kind == "keyword":
        keyword = str(rule.get("keyword") or "").strip()
        if not keyword:
            return None, "关键字规则缺少 keyword"
        return CompiledRule(kind="keyword", keyword=keyword, **common), ""

    tokens = rule.get("patternTokens")
    if not isinstance(tokens, list) or not tokens:
        return None, "模板规则缺少 patternTokens"

    capture_index: dict[str, int] = {}
    source = "^"
    for token in tokens:
        if not isinstance(token, dict):
            return None, "patternTokens 含非法项"
        if token.get("kind") == "text":
            source += _compile_fixed_text(str(token.get("value") or ""))
            continue
        parameter_id = str(token.get("parameterId") or "").strip()
        if not parameter_id:
            return None, "模板参数缺少 parameterId"
        existing = capture_index.get(parameter_id)
        if existing is not None:
            source += f"\\{existing}"
            continue
        index = len(capture_index) + 1
        capture_index[parameter_id] = index
        # Same non-greedy body capture as the UI: the following literal or the end anchor
        # decides where a parameter stops.
        source += r"([\s\S]+?)"
    source += "$"
    try:
        pattern = re.compile(source)
    except re.error as exc:
        return None, f"模板无法编译：{exc}"

    return CompiledRule(kind="template", pattern=pattern, capture_index_by_parameter=capture_index, **common), ""


def compile_display_rules(rules: Any) -> tuple[list[CompiledRule], list[dict[str, str]]]:
    """Compile every rule; returns ``(compiled, problems)``.

    ``problems`` is surfaced rather than swallowed so a watcher that observes nothing can
    say *why* instead of looking idle.
    """
    compiled: list[CompiledRule] = []
    problems: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, rule in enumerate(rules if isinstance(rules, (list, tuple)) else []):
        result, reason = compile_display_rule(rule)
        if result is None:
            if isinstance(rule, dict) and rule.get("enabled", True) is not False:
                # A rule with no id cannot be named; report its position so the user can
                # still find it in the settings list.
                label = str((rule or {}).get("id") or "").strip() or f"<第 {index + 1} 条规则>"
                problems.append({"rule_id": label, "reason": reason})
            continue
        if result.rule_id in seen:
            problems.append({"rule_id": result.rule_id, "reason": "重复 id"})
            continue
        seen.add(result.rule_id)
        compiled.append(result)
    return compiled, problems


def apply_display_template(template: str, parameter_labels: dict[str, str], values_by_id: dict[str, str]) -> str:
    """Substitute ``{参数名}`` placeholders the way the UI's label templates do."""
    text = str(template or "")
    if not text:
        return ""
    for parameter_id, value in values_by_id.items():
        label = parameter_labels.get(parameter_id, parameter_id)
        text = text.replace("{" + label + "}", value)
    return text


def normalize_signature(message: str) -> str:
    """Collapse volatile numbers so repeated incidents share one signature.

    Uses the same message extraction as rule matching: keeping the ``[LEVEL] [component]``
    prefix would make the same problem in two modules look like two different signatures,
    which defeats both burst counting and de-duplication.
    """
    text = extract_display_rule_message(message)
    text = re.sub(r"\d+", "#", text)
    return text[:240]


def rules_for_watch(display_rules: Iterable[Any], source_rule_id: str) -> list[CompiledRule]:
    """Compile only the rule a watch points at (or all rules when it points at nothing)."""
    wanted = str(source_rule_id or "").strip()
    selected = [
        rule for rule in (display_rules or [])
        if isinstance(rule, dict) and (not wanted or str(rule.get("id") or "") == wanted)
    ]
    compiled, problems = compile_display_rules(selected)
    for problem in problems:
        logger.warning("watch.rule.compile_failed rule_id=%s reason=%s", problem["rule_id"], problem["reason"])
    return compiled


def compile_extraction_rule(rule: Any) -> tuple[CompiledRule | None, str]:
    """Compile one 数据提取器 into the same matcher the worker already runs.

    The extractor's own field definitions stay in the browser — the server only needs to know
    *which log lines* belong to it, so this mirrors exactly one property: ``matchKeyword``
    with the rule's case sensitivity. Subsystem/module scoping is enforced by the watch's
    targets, and the browser still applies field extraction over the captured text.
    """
    if not isinstance(rule, dict):
        return None, "规则不是对象"
    if rule.get("enabled", True) is False:
        return None, "提取器已停用"
    rule_id = str(rule.get("id") or "").strip()
    if not rule_id:
        return None, "提取器缺少 id"
    keyword = str(rule.get("matchKeyword") or "").strip()
    if not keyword:
        return None, "提取器缺少 Match 关键字，实时采集需要它来定位日志行"
    return CompiledRule(
        rule_id=rule_id,
        name=str(rule.get("name") or "").strip() or rule_id,
        kind="keyword",
        keyword=keyword,
        case_sensitive=bool(rule.get("caseSensitive")),
        display_mode="data",
        label_template=str(rule.get("name") or "").strip(),
        # Data collection is not a symptom lane; the timeline stays for semantic rules.
        show_on_timeline=False,
        supplemental=str(rule.get("description") or "").strip(),
    ), ""


def extraction_rules_for_watch(extraction_rules: Iterable[Any], extraction_rule_id: str) -> list[CompiledRule]:
    """The 实时采集 counterpart of :func:`rules_for_watch`."""
    wanted = str(extraction_rule_id or "").strip()
    for rule in (extraction_rules or []):
        if not isinstance(rule, dict):
            continue
        if wanted and str(rule.get("id") or "") != wanted:
            continue
        compiled, reason = compile_extraction_rule(rule)
        if compiled is None:
            logger.warning("watch.extraction_rule.compile_failed rule_id=%s reason=%s", wanted or "-", reason)
            return []
        return [compiled]
    return []
