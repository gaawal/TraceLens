from __future__ import annotations

import re
from typing import Any, Iterable


def normalize_anomaly_rules(values: Any) -> list[dict[str, Any]]:
    """Normalize TraceLens UI anomaly rules for backend matching.

    The UI historically stores camelCase fields in localStorage; API callers may
    also use snake_case.  Only enabled, non-empty rules are returned.
    """
    if not isinstance(values, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, bool, bool]] = set()
    for index, item in enumerate(values):
        if not isinstance(item, dict):
            continue
        keyword = str(item.get("keyword") or "").strip()
        if not keyword or item.get("enabled", True) is False:
            continue
        case_sensitive = bool(item.get("case_sensitive", item.get("caseSensitive", False)))
        whole_word = bool(item.get("whole_word", item.get("wholeWord", False)))
        key = (keyword, case_sensitive, whole_word)
        if key in seen:
            continue
        seen.add(key)
        result.append({
            "id": str(item.get("id") or f"anomaly-rule-{index}"),
            "keyword": keyword,
            "case_sensitive": case_sensitive,
            "whole_word": whole_word,
            "enabled": True,
        })
    return result


def anomaly_rule_identity(values: Any) -> list[dict[str, Any]]:
    rules = normalize_anomaly_rules(values)
    return sorted(
        [
            {
                "keyword": rule["keyword"],
                "case_sensitive": bool(rule["case_sensitive"]),
                "whole_word": bool(rule["whole_word"]),
            }
            for rule in rules
        ],
        key=lambda item: (str(item["keyword"]).casefold(), bool(item["case_sensitive"]), bool(item["whole_word"])),
    )




def compile_anomaly_rules(values: Any) -> list[tuple[dict[str, Any], re.Pattern[str]]]:
    compiled: list[tuple[dict[str, Any], re.Pattern[str]]] = []
    for rule in normalize_anomaly_rules(values):
        keyword = str(rule.get("keyword") or "")
        flags = 0 if bool(rule.get("case_sensitive")) else re.IGNORECASE
        pattern = re.escape(keyword)
        if bool(rule.get("whole_word")):
            pattern = rf"(?<!\w){pattern}(?!\w)"
        compiled.append((rule, re.compile(pattern, flags)))
    return compiled


def matching_compiled_anomaly_rules(text: str, compiled: Iterable[tuple[dict[str, Any], re.Pattern[str]]]) -> list[dict[str, Any]]:
    value = str(text or "")
    return [rule for rule, matcher in compiled if matcher.search(value)]

def rule_matches_text(text: str, rule: dict[str, Any]) -> bool:
    keyword = str(rule.get("keyword") or "").strip()
    if not keyword or rule.get("enabled", True) is False:
        return False
    flags = 0 if bool(rule.get("case_sensitive")) else re.IGNORECASE
    pattern = re.escape(keyword)
    if bool(rule.get("whole_word")):
        pattern = rf"(?<!\w){pattern}(?!\w)"
    return re.search(pattern, str(text or ""), flags) is not None


def matching_anomaly_rules(text: str, values: Any) -> list[dict[str, Any]]:
    rules = normalize_anomaly_rules(values)
    return [rule for rule in rules if rule_matches_text(text, rule)]


def matches_any_anomaly_rule(text: str, values: Any) -> bool:
    return bool(matching_anomaly_rules(text, values))


def matched_rule_keywords(text: str, values: Any) -> list[str]:
    return [str(rule["keyword"]) for rule in matching_anomaly_rules(text, values)]
