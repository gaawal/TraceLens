from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
import time
from typing import Any
from uuid import uuid4

from django.conf import settings

from apps.atlog.ai_agent import AiDiagnosisError, diagnose_case_with_ai
from apps.atlog.anomaly_rules import anomaly_rule_identity, normalize_anomaly_rules
from apps.atlog.services import analyze_case
from apps.atlog.snapshots import get_case_snapshot, save_ai_snapshot
from apps.logsources.services.redis_store import RedisLogStore
from apps.logsources.models import LogFmDefinition

logger = logging.getLogger("tracelens.atlog.ai.jobs")
process_logger = logging.getLogger("tracelens.process.ai")

_JOB_TTL_SECONDS = 2 * 60 * 60
_MAX_JOBS = 120
_LOCK = threading.RLock()
_EVENT_CONDITION = threading.Condition(_LOCK)
_JOBS: dict[str, dict[str, Any]] = {}


def _diagnosis_cache_identity(url: str, anomaly_rules: list[dict[str, Any]] | None = None, case_context: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """Build a stable key for one concrete failed-case scene, not just one URL."""
    analysis = analyze_case(url)
    module_revision = LogFmDefinition.objects.order_by("-updated_at").values_list("updated_at", flat=True).first()
    identity = {
        "schema": "atlog-ai-v3-anomaly-rules",
        "base_url": str(analysis.get("base_url") or url).rstrip("/"),
        "case_id": str(analysis.get("case_id") or ""),
        "case_name": str(analysis.get("case_name") or ""),
        "failure_time": str(analysis.get("failure_time") or ""),
        "event_start_time": str(analysis.get("event_start_time") or analysis.get("start_time") or ""),
        "event_end_time": str(analysis.get("event_end_time") or analysis.get("end_time") or ""),
        "assertion": str(analysis.get("assertion_summary") or analysis.get("assertion") or "")[:1200],
        "reason_category": str(analysis.get("reason_category") or ""),
        "reason_detail": str(analysis.get("reason_detail") or "")[:1200],
        # Component dictionary edits change what the Agent is allowed to read, so
        # they must invalidate older AI result caches automatically.
        "log_module_revision": module_revision.isoformat() if module_revision else "",
        # User-managed anomaly rules define which log lines are allowed to become AI evidence.
        # Changing those rules must invalidate an older AI result automatically.
        "anomaly_rules": anomaly_rule_identity(anomaly_rules or []),
        "case_context": {
            "case_id": str((case_context or {}).get("case_id") or "")[:255],
            "case_name": str((case_context or {}).get("case_name") or "")[:255],
            "case_description": str((case_context or {}).get("case_description") or "")[:4000],
        },
    }
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"tracelens:{RedisLogStore.CACHE_SCHEMA}:atlog-ai:result:{digest}", identity


def _thinking_snapshot_line(event: dict[str, Any]) -> str:
    agent = str(event.get("agent") or "Diagnosis Agent")
    kind = str(event.get("kind") or "reason")
    label = {"tool": "工具", "action": "动作", "reason": "依据"}.get(kind, "依据")
    text = str(event.get("text") or "").strip()
    return f"[{agent} · {label}]\n{text}" if text else ""


def _cleanup_locked(now: float | None = None) -> None:
    now = now or time.time()
    expired = [
        job_id for job_id, job in _JOBS.items()
        if now - float(job.get("updated_at") or job.get("created_at") or now) > _JOB_TTL_SECONDS
    ]
    for job_id in expired:
        _JOBS.pop(job_id, None)
    if len(_JOBS) <= _MAX_JOBS:
        return
    ordered = sorted(_JOBS.items(), key=lambda pair: float(pair[1].get("updated_at") or 0))
    for job_id, _ in ordered[: max(0, len(_JOBS) - _MAX_JOBS)]:
        _JOBS.pop(job_id, None)


def _append_event(job_id: str, event: dict[str, Any]) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        events = job.setdefault("events", [])
        event_copy = copy.deepcopy(event)
        event_copy["seq"] = int(job.get("next_seq") or 1)
        job["next_seq"] = event_copy["seq"] + 1
        events.append(event_copy)
        if len(events) > 300:
            del events[:-300]
        if event_copy.get("type") == "stage":
            stage = event_copy.get("stage") if isinstance(event_copy.get("stage"), dict) else {}
            job["current_stage"] = stage
        elif event_copy.get("type") == "token_usage":
            usage = event_copy.get("usage") if isinstance(event_copy.get("usage"), dict) else {}
            current = dict(job.get("token_usage") or {})
            for key in ("input_tokens", "output_tokens", "total_tokens"):
                current[key] = int(current.get(key) or 0) + int(usage.get(key) or 0)
            job["token_usage"] = current
        elif event_copy.get("type") == "report_preview":
            job["report_preview"] = str(event_copy.get("text") or "")[:12000]
        elif event_copy.get("type") == "thinking":
            line = _thinking_snapshot_line(event_copy)
            if line:
                previous = str(job.get("thinking_text") or "")
                job["thinking_text"] = ((previous + "\n\n" if previous else "") + line)[-24000:]
        job["updated_at"] = time.time()
        _EVENT_CONDITION.notify_all()


def _public_result(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not result:
        return None
    return {key: copy.deepcopy(value) for key, value in result.items() if not key.startswith("_")}


def _worker(job_id: str, url: str, messages: list[dict[str, Any]], anomaly_rules: list[dict[str, Any]], case_context: dict[str, Any], cache_key: str) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job["status"] = "running"
        job["updated_at"] = time.time()
    process_logger.info("[AI-JOB] start job=%s url=%s", job_id, url[:240])
    try:
        result = diagnose_case_with_ai(url, current_messages=messages, anomaly_rules=anomaly_rules, case_context=case_context, progress_callback=lambda event: _append_event(job_id, event))
        with _LOCK:
            job = _JOBS.get(job_id)
            if not job:
                return
            job["status"] = "completed"
            job["result"] = result
            job["token_usage"] = copy.deepcopy(result.get("token_usage") or job.get("token_usage") or {})
            job["current_stage"] = {
                "name": "done", "label": "诊断完成", "status": "completed",
                "agent": "Diagnosis Workflow", "action": "END", "detail": "根因报告已生成。",
            }
            job["updated_at"] = time.time()
            _append_event(job_id, {"type": "diagnosis", "status": "completed", "detail": "诊断完成。", "timestamp": time.time()})
            cache_payload = {
                "cached_at": time.time(),
                "result": copy.deepcopy(result),
                "thinking_text": str(job.get("thinking_text") or "")[-24000:],
            }
        ttl = max(60, int(getattr(settings, "TRACELENS_ATLOG_AI_CACHE_TTL", 604800)))
        cache_ok = RedisLogStore.set_json(cache_key, cache_payload, ttl)
        save_ai_snapshot(
            url,
            _public_result(result) or {},
            thinking_text=str(cache_payload.get("thinking_text") or ""),
            token_usage=result.get("token_usage") if isinstance(result.get("token_usage"), dict) else {},
            job_id=job_id,
        )
        process_logger.info("[AI-JOB] finish job=%s redis_cache=%s ttl=%s db_snapshot=true", job_id, cache_ok, ttl)
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        logger.exception("atlog.ai.job_failed job=%s", job_id)
        with _LOCK:
            job = _JOBS.get(job_id)
            if job:
                job["status"] = "error"
                job["error"] = message
                job["updated_at"] = time.time()
                _append_event(job_id, {"type": "diagnosis", "status": "error", "detail": message, "timestamp": time.time()})
        process_logger.error("[AI-JOB] failed job=%s error=%s", job_id, message[:500])


def _bootstrap_worker(
    job_id: str,
    url: str,
    messages: list[dict[str, Any]],
    anomaly_rules: list[dict[str, Any]],
    case_context: dict[str, Any],
    *,
    force: bool,
) -> None:
    """Prepare cache identity/cache hit inside the worker, not the HTTP request.

    The old start endpoint parsed the report synchronously before returning job_id,
    so the browser could not open SSE until a potentially expensive preparation
    step had already finished.  Creating the job first makes progress observable
    from the very first preparation stage.
    """
    _append_event(job_id, {
        "type": "stage",
        "stage": {
            "name": "prepare", "label": "准备诊断上下文", "status": "running",
            "agent": "Diagnosis Workflow", "action": "cache_identity + report facts",
            "detail": "正在校验当前用例现场并检查可复用诊断结果。",
        },
        "timestamp": time.time(),
    })
    try:
        cache_key, identity = _diagnosis_cache_identity(url, anomaly_rules, case_context)
        db_saved = None if force else get_case_snapshot(url)
        db_ai = db_saved.get("ai") if isinstance(db_saved, dict) and isinstance(db_saved.get("ai"), dict) else {}
        db_result = db_ai.get("result") if isinstance(db_ai.get("result"), dict) and db_ai.get("result") else None
        cached = None if force or db_result else RedisLogStore.get_json(cache_key)
        cached_result = db_result or (cached.get("result") if isinstance(cached, dict) and isinstance(cached.get("result"), dict) else None)
        persisted_thinking = str(db_ai.get("thinking_text") or "") if db_result else str((cached or {}).get("thinking_text") or "")
        persisted_source = "database" if db_result else ("redis" if cached_result else "")
        with _LOCK:
            job = _JOBS.get(job_id)
            if not job:
                return
            job["cache_key"] = cache_key
            job["cache_identity"] = identity
            job["cache_hit"] = bool(cached_result)
            job["cache_source"] = persisted_source
            if cached_result:
                job["status"] = "completed"
                job["result"] = copy.deepcopy(cached_result)
                job["token_usage"] = copy.deepcopy(cached_result.get("token_usage") or job.get("token_usage") or {})
                job["thinking_text"] = persisted_thinking[-24000:]
                job["current_stage"] = {
                    "name": "cache",
                    "label": "数据库快照命中" if db_result else "Redis 缓存命中",
                    "status": "completed",
                    "agent": "Diagnosis Snapshot" if db_result else "Diagnosis Cache",
                    "action": "Database URL snapshot" if db_result else "Redis result cache",
                    "detail": "已直接恢复最近一次用例诊断结果。",
                }
                job["updated_at"] = time.time()
        if cached_result:
            if db_result:
                text = "数据库已命中该用例 URL 最近一次 AI 诊断结果。本次直接恢复上一次结论；只有点击‘重新 AI 诊断’才会重新调用模型并覆盖该用例的最新诊断快照。"
                detail = "命中数据库用例诊断快照。"
                agent = "Diagnosis Snapshot"
            else:
                text = "Redis 已命中同一用例、同一失败时间窗和断言现场的 AI 诊断结果。本次直接复用历史结论，不再调用大模型；如现场已变化可点击‘重新 AI 诊断’强制刷新。"
                detail = "命中 Redis AI 诊断缓存。"
                agent = "Diagnosis Cache"
                save_ai_snapshot(url, _public_result(cached_result) or {}, thinking_text=persisted_thinking, token_usage=cached_result.get("token_usage") if isinstance(cached_result.get("token_usage"), dict) else {}, job_id=job_id)
            _append_event(job_id, {
                "type": "thinking", "stage_name": "cache", "agent": agent, "kind": "reason",
                "text": text, "timestamp": time.time(),
            })
            _append_event(job_id, {"type": "diagnosis", "status": "completed", "detail": detail, "timestamp": time.time()})
            process_logger.info("[AI-JOB] cache.hit source=%s job=%s case=%s", persisted_source, job_id, identity.get("case_id") or identity.get("case_name") or "-")
            return

        _append_event(job_id, {
            "type": "stage",
            "stage": {
                "name": "prepare", "label": "准备诊断上下文", "status": "completed",
                "agent": "Diagnosis Workflow", "action": "cache_identity + report facts",
                "detail": "现场校验完成，未命中可直接复用的诊断结果。",
            },
            "timestamp": time.time(),
        })
        _worker(job_id, url, messages, anomaly_rules, case_context, cache_key)
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        logger.exception("atlog.ai.bootstrap_failed job=%s", job_id)
        with _LOCK:
            job = _JOBS.get(job_id)
            if job:
                job["status"] = "error"
                job["error"] = message
                job["updated_at"] = time.time()
        _append_event(job_id, {"type": "diagnosis", "status": "error", "detail": message, "timestamp": time.time()})


def start_diagnosis_job(
    url: str,
    messages: list[dict[str, Any]] | None = None,
    anomaly_rules: list[dict[str, Any]] | None = None,
    case_context: dict[str, Any] | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    base_url = str(url or "").strip()
    if not base_url:
        raise AiDiagnosisError("用例日志 URL 不能为空。")
    normalized_rules = normalize_anomaly_rules(anomaly_rules or [])
    now = time.time()
    job_id = uuid4().hex
    with _LOCK:
        _cleanup_locked(now)
        _JOBS[job_id] = {
            "job_id": job_id,
            "url": base_url,
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "events": [],
            "next_seq": 1,
            "token_usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "current_stage": {
                "name": "queued", "label": "等待启动", "status": "running",
                "agent": "Diagnosis Workflow", "action": "排队",
                "detail": "AI 诊断任务已创建，正在启动后台工作流。",
            },
            "result": None,
            "error": "",
            "report_preview": "",
            "thinking_text": "",
            "cache_hit": False,
            "cache_source": "",
            "cache_key": "",
            "cache_identity": {},
            "anomaly_rules": copy.deepcopy(normalized_rules),
        }
    _append_event(job_id, {
        "type": "thinking", "stage_name": "queued", "agent": "Diagnosis Workflow", "kind": "action",
        "text": f"AI 诊断任务已创建。当前启用异常规则 {len(normalized_rules)} 条；正在建立实时诊断事件流。",
        "timestamp": time.time(),
    })
    thread = threading.Thread(
        target=_bootstrap_worker,
        args=(job_id, base_url, list(messages or []), normalized_rules, dict(case_context or {})),
        kwargs={"force": force},
        name=f"atlog-ai-{job_id[:8]}",
        daemon=True,
    )
    thread.start()
    # Return immediately. Report parsing/cache identity now happens in the worker,
    # allowing the browser to establish SSE before the first expensive step.
    return get_diagnosis_job(job_id)


def get_diagnosis_job(job_id: str, *, after_seq: int = 0, include_internal: bool = False) -> dict[str, Any]:
    with _LOCK:
        _cleanup_locked()
        job = _JOBS.get(str(job_id or "").strip())
        if not job:
            raise KeyError("AI 诊断任务不存在或已过期。")
        events = [copy.deepcopy(item) for item in (job.get("events") or []) if int(item.get("seq") or 0) > int(after_seq or 0)]
        result = copy.deepcopy(job.get("result")) if include_internal else _public_result(job.get("result"))
        return {
            "job_id": job["job_id"],
            "url": job["url"],
            "status": job["status"],
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "current_stage": copy.deepcopy(job.get("current_stage") or {}),
            "token_usage": copy.deepcopy(job.get("token_usage") or {}),
            "report_preview": str(job.get("report_preview") or ""),
            "thinking_text": str(job.get("thinking_text") or ""),
            "cache_hit": bool(job.get("cache_hit")),
            "cache_source": str(job.get("cache_source") or ""),
            "events": events,
            "last_seq": int((job.get("next_seq") or 1) - 1),
            "result": result,
            "error": str(job.get("error") or ""),
        }


def wait_for_diagnosis_job(job_id: str, *, after_seq: int = 0, timeout: float = 12.0) -> dict[str, Any]:
    """Block until a new diagnosis event/status is available, then return a snapshot.

    Used by the SSE endpoint so the browser receives push updates without polling.
    """
    key = str(job_id or "").strip()
    deadline = time.monotonic() + max(0.2, float(timeout or 12.0))
    with _EVENT_CONDITION:
        while True:
            _cleanup_locked()
            job = _JOBS.get(key)
            if not job:
                raise KeyError("AI 诊断任务不存在或已过期。")
            last_seq = int((job.get("next_seq") or 1) - 1)
            status = str(job.get("status") or "")
            if last_seq > int(after_seq or 0) or status in {"completed", "error"}:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _EVENT_CONDITION.wait(timeout=remaining)
    return get_diagnosis_job(key, after_seq=after_seq)
