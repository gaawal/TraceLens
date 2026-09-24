from __future__ import annotations

import copy
import json
from typing import Any

from django.utils import timezone

from apps.atlog.models import AtLogCaseAnalysisSnapshot
from apps.atlog.services import normalize_base_url

MAX_QUERY_ROWS = 5000
MAX_EVENT_RAW_CHARS = 800_000
MAX_THINKING_CHARS = 24_000


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _url(value: str) -> str:
    return normalize_base_url(str(value or ""))


def _bounded_query_result(result: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    payload = _json_safe(result)
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    original_count = len(rows)
    if original_count > MAX_QUERY_ROWS:
        payload["rows"] = rows[-MAX_QUERY_ROWS:]
        payload["snapshot_rows_truncated"] = True
        payload["snapshot_original_row_count"] = original_count
    raw = str(payload.get("event_raw_text") or "")
    if len(raw) > MAX_EVENT_RAW_CHARS:
        payload["event_raw_text"] = raw[-MAX_EVENT_RAW_CHARS:]
        payload["snapshot_event_text_truncated"] = True
    return payload


def _public(snapshot: AtLogCaseAnalysisSnapshot | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    return {
        "case_url": snapshot.case_url,
        "case_id": snapshot.case_id,
        "case_name": snapshot.case_name,
        "case_status": snapshot.case_status,
        "updated_at": snapshot.updated_at.isoformat() if snapshot.updated_at else "",
        "workspace": {
            "state": copy.deepcopy(snapshot.workspace_state or {}),
            "result": copy.deepcopy(snapshot.query_result_snapshot or {}),
        },
        "ai": {
            "result": copy.deepcopy(snapshot.ai_result_snapshot) if snapshot.ai_result_snapshot else None,
            "thinking_text": snapshot.ai_thinking_text or "",
            "token_usage": copy.deepcopy(snapshot.ai_token_usage or {}),
            "job_id": snapshot.ai_job_id or "",
            "completed_at": snapshot.ai_completed_at.isoformat() if snapshot.ai_completed_at else "",
            "revision": int(snapshot.ai_revision or 0),
        },
    }


def get_case_snapshot(url: str) -> dict[str, Any] | None:
    base = _url(url)
    return _public(AtLogCaseAnalysisSnapshot.objects.filter(case_url=base).first())


def save_analysis_snapshot(url: str, analysis: dict[str, Any]) -> dict[str, Any]:
    base = _url(url)
    snapshot, _ = AtLogCaseAnalysisSnapshot.objects.get_or_create(case_url=base)
    snapshot.case_id = str(analysis.get("case_id") or snapshot.case_id or "")[:255]
    snapshot.case_name = str(analysis.get("case_name") or snapshot.case_name or "")[:255]
    snapshot.case_status = str(analysis.get("status") or snapshot.case_status or "")[:32]
    # Do not recursively persist previously attached saved_state.
    clean = {key: value for key, value in analysis.items() if key != "saved_state"}
    snapshot.analysis_snapshot = _json_safe(clean)
    snapshot.save(update_fields=["case_id", "case_name", "case_status", "analysis_snapshot", "updated_at"])
    return _public(snapshot) or {}


def save_workspace_snapshot(url: str, state: dict[str, Any] | None, result: dict[str, Any] | None) -> dict[str, Any]:
    base = _url(url)
    snapshot, _ = AtLogCaseAnalysisSnapshot.objects.get_or_create(case_url=base)
    snapshot.workspace_state = _json_safe(state or {})
    snapshot.query_result_snapshot = _bounded_query_result(result)
    snapshot.save(update_fields=["workspace_state", "query_result_snapshot", "updated_at"])
    return _public(snapshot) or {}


def _ai_selected_target_keys(result: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    targets = result.get("selected_targets") if isinstance(result.get("selected_targets"), list) else []
    for item in targets:
        if not isinstance(item, dict):
            continue
        subsystem = str(item.get("subsystem") or "").strip()
        module = str(item.get("module") or "").strip()
        if not subsystem or not module:
            continue
        # Keep the same target-key representation as RemoteLogQueryPanel.targetKey().
        key = f"{subsystem}\x00{module}\x00normal"
        if key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return keys


def save_ai_snapshot(
    url: str,
    result: dict[str, Any],
    *,
    thinking_text: str = "",
    token_usage: dict[str, Any] | None = None,
    job_id: str = "",
) -> dict[str, Any]:
    base = _url(url)
    snapshot, _ = AtLogCaseAnalysisSnapshot.objects.get_or_create(case_url=base)
    snapshot.case_id = str(result.get("case_id") or snapshot.case_id or "")[:255]
    snapshot.case_name = str(result.get("case_name") or snapshot.case_name or "")[:255]
    snapshot.ai_result_snapshot = _json_safe({key: value for key, value in result.items() if not str(key).startswith("_")})
    selected_target_keys = _ai_selected_target_keys(result)
    if selected_target_keys:
        workspace_state = dict(snapshot.workspace_state or {})
        workspace_state["selectedTargets"] = selected_target_keys
        workspace_state["selectedTargetSource"] = "ai"
        snapshot.workspace_state = _json_safe(workspace_state)
    snapshot.ai_thinking_text = str(thinking_text or "")[-MAX_THINKING_CHARS:]
    snapshot.ai_token_usage = _json_safe(token_usage or result.get("token_usage") or {})
    snapshot.ai_job_id = str(job_id or "")[:64]
    snapshot.ai_completed_at = timezone.now()
    snapshot.ai_revision = int(snapshot.ai_revision or 0) + 1
    snapshot.save(update_fields=[
        "case_id", "case_name", "ai_result_snapshot", "workspace_state", "ai_thinking_text", "ai_token_usage",
        "ai_job_id", "ai_completed_at", "ai_revision", "updated_at",
    ])
    return _public(snapshot) or {}
