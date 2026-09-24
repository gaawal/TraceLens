"""Turn a finished analysis into an importable case draft.

Triggered by an explicit user action, never by hoping the model remembers to call a tool.
The conversation is the evidence: this makes one focused model call to *extract* the
conclusion into fields, then reuses ``draft_diagnosis_case`` to render and validate it — so
the Markdown the user reads and the fields the case editor receives can never disagree.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from apps.tooling.llm import get_llm_client

logger = logging.getLogger("tracelens.tooling.case_draft")

MAX_CONVERSATION_CHARS = 24000
_JSON_BLOCK = re.compile(r"\{[\s\S]*\}")

_SYSTEM_PROMPT = """你是 TraceLens 的诊断结论整理器。

用户会给你一段已经完成的日志分析对话。你的唯一任务是把其中的**结论**抽取成结构化字段。

规则：
1. 只抽取对话里**已经出现**的结论，不要新增推断、不要补全你没看到的东西。
2. `evidence` 只能填对话里出现过的**真实日志原文或关键日志行**，不要编造、不要改写。
3. 根因不确定时 `root_cause` 留空，并把缺什么写进 `open_questions`。
4. `components` 填对话里提到的子系统/模块，例如 `cpfr/cpdisp`。
5. `confidence` 在 high/medium/low 中选一个，反映证据强度而不是语气。
6. 只输出一个 JSON 对象，不要输出解释文字、不要用 markdown 代码块。

输出字段：
{"name": "简短可检索的案例名", "symptom": "故障现象", "root_cause": "已确认根因或空串",
 "solution": "处理建议或空串", "category": "分类或空串", "confidence": "high|medium|low",
 "tags": ["标签"], "components": ["子系统/模块"], "evidence": ["真实日志行"],
 "open_questions": ["还缺什么证据"]}"""


def _conversation_text(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        content = str(item.get("content") or "").strip()
        if not content or role not in {"user", "assistant"}:
            continue
        parts.append(f"### {role}\n{content}")
    text = "\n\n".join(parts)
    # Keep the tail: the conclusion lives at the end of a diagnosis conversation.
    return text[-MAX_CONVERSATION_CHARS:]


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


def build_case_draft(
    messages: list[dict[str, Any]],
    *,
    context: dict[str, Any] | None = None,
    extra_evidence: list[str] | None = None,
) -> dict[str, Any]:
    """Extract a case draft from a finished conversation.

    Returns the same shape as the ``draft_diagnosis_case`` tool so the UI has exactly one
    card contract to render.
    """
    from apps.tooling.plugins.analysis import draft_diagnosis_case

    conversation = _conversation_text(messages)
    if not conversation.strip():
        return {"ok": False, "error": "当前对话还没有可整理的内容。"}

    context = context or {}
    context_lines = [
        f"- 页面：{context.get('page_label') or context.get('page') or '-'}",
        f"- 环境：{context.get('environment_name') or context.get('environment_id') or '-'}",
    ]

    client = get_llm_client()
    response = client.chat([
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": "运行上下文：\n" + "\n".join(context_lines) + "\n\n分析对话：\n" + conversation},
    ])
    try:
        raw = str(response.choices[0].message.content or "")
    except Exception:  # noqa: BLE001
        raw = ""

    fields = _parse_json_object(raw)
    if fields is None:
        logger.warning("case_draft.parse_failed raw=%s", raw[:400])
        return {"ok": False, "error": "模型没有返回可解析的结构化结论，请重试或让结论更明确一些。"}

    # Evidence the user explicitly supplied (e.g. quoted from the log view) always wins:
    # it is real log text, whereas the model may only paraphrase.
    evidence = [str(item) for item in (fields.get("evidence") or []) if str(item).strip()]
    for item in (extra_evidence or []):
        text = str(item or "").strip()
        if text and text not in evidence:
            evidence.insert(0, text)

    result = draft_diagnosis_case({
        "name": fields.get("name"),
        "symptom": fields.get("symptom"),
        "root_cause": fields.get("root_cause"),
        "solution": fields.get("solution"),
        "category": fields.get("category"),
        "confidence": fields.get("confidence"),
        "tags": fields.get("tags"),
        "components": fields.get("components"),
        "evidence": evidence,
        "open_questions": fields.get("open_questions"),
    })
    result["source"] = "conversation"
    return result
