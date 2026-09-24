from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid

from django.http import StreamingHttpResponse
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.atlog.ai_agent import AiDiagnosisError, diagnose_case_with_ai
from apps.atlog.ai_jobs import get_diagnosis_job, start_diagnosis_job, wait_for_diagnosis_job
from apps.atlog.knowledge_bridge import accept_ai_diagnosis, match_case_knowledge
from apps.atlog.snapshots import get_case_snapshot, save_ai_snapshot, save_analysis_snapshot, save_workspace_snapshot
from apps.atlog.services import (
    AtLogError,
    analyze_case,
    analyze_report_file,
    list_case_directory,
    query_case_logs,
    query_event_log,
    read_case_file,
)
from apps.logsources.services.search_progress import LogSearchCancelled, LogSearchProgressStore, request_log_search_cancel

logger = logging.getLogger("tracelens.atlog")


class AtLogAnalysisViewSet(viewsets.ViewSet):
    """Stateless ATLog report analyzer. Data stays on the nginx report host."""

    def list(self, request):
        return Response({
            "name": "TraceLens ATLog 自动化用例分析",
            "workflow": ["summary_report.xml", "result/pytest-*.xml", "xytest.log", "event.log", "full_logs/"],
            "report_file_analysis": "/api/atlog-analysis/analyze-report/",
            "read_only": True,
        })

    @action(detail=False, methods=["post"], url_path="excel-import-debug")
    def excel_import_debug(self, request):
        payload = request.data if isinstance(request.data, dict) else {}
        file_name = str(payload.get("file_name") or "<unknown>")[:255]
        file_size = payload.get("file_size")
        selected_sheet = str(payload.get("selected_sheet") or "")[:255]
        sheets = payload.get("sheets") if isinstance(payload.get("sheets"), list) else []
        header_row = payload.get("header_row")
        indexes = payload.get("indexes") if isinstance(payload.get("indexes"), dict) else {}
        headers = payload.get("headers") if isinstance(payload.get("headers"), dict) else {}
        accepted_count = int(payload.get("accepted_count") or 0)
        rejected_counts = payload.get("rejected_counts") if isinstance(payload.get("rejected_counts"), dict) else {}
        error = str(payload.get("error") or "").strip()[:2000]
        logger.info(
            "[ATLog Excel Import] file=%s size=%s sheets=%s selected_sheet=%s header_row=%s indexes=%s headers=%s",
            file_name, file_size, sheets[:20], selected_sheet, header_row, indexes, headers,
        )
        header_scan = payload.get("header_scan") if isinstance(payload.get("header_scan"), list) else []
        for item in header_scan[:12]:
            if not isinstance(item, dict):
                continue
            logger.info(
                "[ATLog Excel Import] header_scan file=%s row=%s values=%s url_like_columns=%s",
                file_name, item.get("row_number"), item.get("values"), item.get("url_like_columns"),
            )
        samples = payload.get("row_samples") if isinstance(payload.get("row_samples"), list) else []
        for item in samples[:40]:
            if not isinstance(item, dict):
                continue
            level = logging.INFO if str(item.get("reason") or "") in {"accepted", "duplicate"} else logging.WARNING
            logger.log(
                level,
                "[ATLog Excel Import] row file=%s row=%s column=%s source=%s raw=%r extracted=%r normalized=%r reason=%s case_id=%r",
                file_name, item.get("row_number"), item.get("column"), item.get("source"),
                str(item.get("raw") or "")[:500], str(item.get("extracted") or "")[:500],
                str(item.get("normalized") or "")[:500], str(item.get("reason") or "")[:120],
                str(item.get("case_id") or "")[:255],
            )
        summary_level = logging.WARNING if error or accepted_count == 0 else logging.INFO
        logger.log(
            summary_level,
            "[ATLog Excel Import] summary file=%s accepted=%s rejected=%s error=%r",
            file_name, accepted_count, rejected_counts, error,
        )
        return Response({"ok": True})

    @action(detail=False, methods=["post"], url_path="analyze")
    def analyze(self, request):
        payload = request.data if isinstance(request.data, dict) else {}
        try:
            result = analyze_case(str(payload.get("url") or payload.get("base_url") or ""))
            if str(payload.get("case_id") or "").strip():
                result["case_id"] = str(payload.get("case_id") or "").strip()[:255]
            if str(payload.get("case_name") or "").strip():
                result["case_name"] = str(payload.get("case_name") or "").strip()[:255]
            result["case_description"] = str(payload.get("case_description") or result.get("case_description") or "").strip()[:4000]
        except (AtLogError, FileNotFoundError) as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("atlog.analyze.failed")
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        saved_state = save_analysis_snapshot(str(result.get("base_url") or payload.get("url") or ""), result)
        result["saved_state"] = saved_state
        return Response(result)

    @action(detail=False, methods=["post"], url_path="analyze-report")
    def analyze_report(self, request):
        """按 URL 分析单个报告文件（用例报告 / CPD 报告 / 日志），不要求整个用例目录。"""
        payload = request.data if isinstance(request.data, dict) else {}
        try:
            result = analyze_report_file(
                str(payload.get("url") or payload.get("base_url") or ""),
                case_id=str(payload.get("case_id") or ""),
            )
        except (AtLogError, FileNotFoundError, TypeError, ValueError) as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("atlog.analyze_report.failed")
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(result)

    @action(detail=False, methods=["get", "post"], url_path="snapshot")
    def snapshot(self, request):
        payload = request.data if request.method == "POST" and isinstance(request.data, dict) else request.query_params
        try:
            result = get_case_snapshot(str(payload.get("url") or payload.get("base_url") or ""))
        except (AtLogError, FileNotFoundError, TypeError, ValueError) as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result or {})

    @action(detail=False, methods=["post"], url_path="ai-diagnose")
    def ai_diagnose(self, request):
        payload = request.data if isinstance(request.data, dict) else {}
        messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
        anomaly_rules = payload.get("anomaly_rules") if isinstance(payload.get("anomaly_rules"), list) else []
        try:
            result = diagnose_case_with_ai(
                str(payload.get("url") or payload.get("base_url") or ""),
                current_messages=messages,
                anomaly_rules=anomaly_rules,
                case_context=payload.get("case_context") if isinstance(payload.get("case_context"), dict) else {},
            )
        except (AtLogError, AiDiagnosisError, FileNotFoundError, TypeError, ValueError) as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("atlog.ai_diagnose.failed")
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        public = {key: value for key, value in result.items() if not key.startswith("_")}
        save_ai_snapshot(str(public.get("base_url") or payload.get("url") or ""), public, token_usage=public.get("token_usage") or {})
        return Response(public)

    @action(detail=False, methods=["post"], url_path="ai-diagnose-start")
    def ai_diagnose_start(self, request):
        payload = request.data if isinstance(request.data, dict) else {}
        messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
        anomaly_rules = payload.get("anomaly_rules") if isinstance(payload.get("anomaly_rules"), list) else []
        try:
            result = start_diagnosis_job(
                str(payload.get("url") or payload.get("base_url") or ""),
                messages=messages,
                anomaly_rules=anomaly_rules,
                case_context=payload.get("case_context") if isinstance(payload.get("case_context"), dict) else {},
                force=bool(payload.get("force", False)),
            )
        except (AtLogError, AiDiagnosisError, FileNotFoundError, TypeError, ValueError) as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result, status=status.HTTP_202_ACCEPTED)

    @action(detail=False, methods=["get", "post"], url_path="ai-diagnose-status")
    def ai_diagnose_status(self, request):
        payload = request.data if request.method == "POST" and isinstance(request.data, dict) else request.query_params
        job_id = str(payload.get("job_id") or "").strip()
        try:
            after_seq = int(payload.get("after_seq") or 0)
            result = get_diagnosis_job(job_id, after_seq=after_seq)
        except KeyError as exc:
            return Response({"message": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        except (TypeError, ValueError) as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result)

    @action(detail=False, methods=["get"], url_path="ai-diagnose-stream")
    def ai_diagnose_stream(self, request):
        job_id = str(request.query_params.get("job_id") or "").strip()
        try:
            after_seq = int(request.query_params.get("after_seq") or 0)
            # Fail before opening a streaming response for an unknown job.
            get_diagnosis_job(job_id, after_seq=after_seq)
        except KeyError as exc:
            return Response({"message": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        except (TypeError, ValueError) as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        def encode(event_name: str, payload: dict, *, event_id: int | None = None) -> bytes:
            # One diagnosis event per SSE frame.  Do not batch a whole job snapshot:
            # batching makes the browser look frozen until several Agent stages have
            # already completed.  A tiny flush comment also helps conservative reverse
            # proxies forward small progress frames immediately.
            lines: list[str] = []
            if event_id is not None:
                lines.append(f"id: {int(event_id)}")
            lines.append(f"event: {event_name}")
            lines.append("data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str))
            lines.append(": flush " + (" " * 192))
            return ("\n".join(lines) + "\n\n").encode("utf-8")

        def live_job(snapshot: dict, *, include_result: bool = False) -> dict:
            # Keep every SSE frame small.  The event itself carries the incremental
            # detail; this state block only lets the UI update the current stage, token
            # counters and terminal result without requesting another status endpoint.
            return {
                "job_id": snapshot.get("job_id"),
                "url": snapshot.get("url"),
                "status": snapshot.get("status"),
                "created_at": snapshot.get("created_at"),
                "updated_at": snapshot.get("updated_at"),
                "current_stage": snapshot.get("current_stage") or {},
                "token_usage": snapshot.get("token_usage") or {},
                "report_preview": snapshot.get("report_preview") or "",
                "thinking_text": snapshot.get("thinking_text") or "",
                "cache_hit": bool(snapshot.get("cache_hit")),
                "cache_source": snapshot.get("cache_source") or "",
                "events": [],
                "last_seq": snapshot.get("last_seq") or 0,
                "result": snapshot.get("result") if include_result else None,
                "error": snapshot.get("error") or "",
            }

        async def event_stream():
            seq = after_seq
            initial = await asyncio.to_thread(get_diagnosis_job, job_id, after_seq=seq)
            yield ("retry: 1000\n: atlog-ai-stream-open " + (" " * 2048) + "\n\n").encode("utf-8")
            yield encode("state", {"type": "state", "job": live_job(initial, include_result=str(initial.get("status") or "") in {"completed", "error"})})
            if str(initial.get("status") or "") in {"completed", "error"}:
                return
            while True:
                try:
                    snapshot = await asyncio.to_thread(wait_for_diagnosis_job, job_id, after_seq=seq, timeout=8.0)
                except KeyError:
                    yield encode("error", {"type": "error", "message": "AI 诊断任务不存在或已过期。"})
                    break
                events = list(snapshot.get("events") or [])
                for item in events:
                    event_seq = int(item.get("seq") or 0)
                    if event_seq <= seq:
                        continue
                    seq = event_seq
                    event_job = live_job(snapshot)
                    if str(item.get("type") or "") == "stage" and isinstance(item.get("stage"), dict):
                        event_job["current_stage"] = item.get("stage") or {}
                    yield encode(
                        "progress",
                        {"type": "event", "event": item, "job": event_job},
                        event_id=event_seq,
                    )
                    # Give the ASGI server an event-loop turn between frames so several
                    # progress events produced in one fast Agent node do not collapse
                    # into one network write / one React render.
                    await asyncio.sleep(0)
                terminal = str(snapshot.get("status") or "") in {"completed", "error"}
                if terminal:
                    yield encode("state", {"type": "state", "job": live_job(snapshot, include_result=True)}, event_id=int(snapshot.get("last_seq") or seq))
                    break
                if not events:
                    yield b": atlog-ai-heartbeat\n\n"

        response = StreamingHttpResponse(event_stream(), content_type="text/event-stream; charset=utf-8")
        response["Cache-Control"] = "no-cache, no-transform"
        response["X-Accel-Buffering"] = "no"
        response["Connection"] = "keep-alive"
        response["Access-Control-Expose-Headers"] = "X-Accel-Buffering"
        return response

    @action(detail=False, methods=["post"], url_path="knowledge-match")
    def knowledge_match(self, request):
        payload = request.data if isinstance(request.data, dict) else {}
        try:
            result = match_case_knowledge(
                str(payload.get("url") or payload.get("base_url") or ""),
                limit=int(payload.get("limit") or 5),
                anomaly_rules=payload.get("anomaly_rules") if isinstance(payload.get("anomaly_rules"), list) else [],
            )
        except (AtLogError, FileNotFoundError, TypeError, ValueError) as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("atlog.knowledge_match.failed")
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(result)

    @action(detail=False, methods=["post"], url_path="ai-accept")
    def ai_accept(self, request):
        payload = request.data if isinstance(request.data, dict) else {}
        job_id = str(payload.get("job_id") or "").strip()
        try:
            diagnosis_result = None
            if job_id:
                try:
                    job = get_diagnosis_job(job_id, include_internal=True)
                    if job.get("status") == "completed" and isinstance(job.get("result"), dict):
                        diagnosis_result = job["result"]
                except KeyError:
                    diagnosis_result = None
            if diagnosis_result is None:
                saved = get_case_snapshot(str(payload.get("url") or payload.get("base_url") or ""))
                ai = saved.get("ai") if isinstance(saved, dict) and isinstance(saved.get("ai"), dict) else {}
                if isinstance(ai.get("result"), dict) and ai.get("result"):
                    diagnosis_result = ai["result"]
            if diagnosis_result is None:
                raise ValueError("没有可采纳的 AI 诊断结果。")
            target_case_id = payload.get("target_case_id")
            result = accept_ai_diagnosis(diagnosis_result, target_case_id=target_case_id if target_case_id not in (None, "") else None)
        except (TypeError, ValueError) as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("atlog.ai_accept.failed")
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(result, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path="event")
    def event(self, request):
        payload = request.data if isinstance(request.data, dict) else {}
        try:
            result = query_event_log(
                str(payload.get("url") or payload.get("base_url") or ""),
                start_time=str(payload.get("start_time") or ""),
                end_time=str(payload.get("end_time") or ""),
                modules=payload.get("modules") or [],
                levels=payload.get("levels") or [],
                keyword=str(payload.get("keyword") or ""),
                max_lines=int(payload.get("max_lines") or 8000),
            )
        except (AtLogError, FileNotFoundError, TypeError, ValueError) as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("atlog.event.failed")
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(result)


    @action(detail=False, methods=["post"], url_path="logs-start")
    def logs_start(self, request):
        payload = request.data if isinstance(request.data, dict) else {}
        operation_id = str(uuid.uuid4())
        LogSearchProgressStore.start(operation_id, source_type="atlog_url", source_ref=str(payload.get("url") or payload.get("base_url") or ""))

        def worker() -> None:
            try:
                result = query_case_logs(
                    str(payload.get("url") or payload.get("base_url") or ""),
                    start_time=str(payload.get("start_time") or ""),
                    end_time=str(payload.get("end_time") or ""),
                    components=payload.get("components") or [],
                    targets=payload.get("targets") or [],
                    source_categories=payload.get("source_categories") if isinstance(payload.get("source_categories"), list) else None,
                    levels=payload.get("levels") or [],
                    anomaly_rules=payload.get("anomaly_rules") if isinstance(payload.get("anomaly_rules"), list) else [],
                    keyword=str(payload.get("keyword") or ""),
                    max_lines=int(payload.get("max_lines") or 12000),
                    operation_id=operation_id,
                )
                LogSearchProgressStore.raise_if_cancelled(operation_id)
                save_workspace_snapshot(
                    str(result.get("base_url") or payload.get("url") or ""),
                    payload.get("workspace_state") if isinstance(payload.get("workspace_state"), dict) else {
                        "startTime": str(payload.get("start_time") or ""),
                        "endTime": str(payload.get("end_time") or ""),
                        "keyword": str(payload.get("keyword") or ""),
                        "selectedTargets": payload.get("targets") or [],
                        "sourceCategories": payload.get("source_categories") or [],
                        "onlyErrors": bool(payload.get("anomaly_rules")),
                    },
                    result,
                )
                LogSearchProgressStore.patch(
                    operation_id,
                    stage="complete",
                    percent=100,
                    message=f"检索完成，共 {result.get('count', 0)} 条日志",
                    done=True,
                    result=result,
                    matched_files=int(result.get("matched_file_count") or 0),
                    total_files=int(result.get("matched_file_count") or 0) + (1 if (result.get("event_query") or {}).get("requested") else 0),
                    read_files=int((LogSearchProgressStore.get(operation_id) or {}).get("artifact_done") or 0),
                )
            except LogSearchCancelled:
                LogSearchProgressStore.cancelled(operation_id)
            except Exception as exc:
                logger.exception("atlog.logs.async_failed operation=%s", operation_id)
                LogSearchProgressStore.fail(operation_id, str(exc))

        threading.Thread(target=worker, name=f"atlog-search-{operation_id[:8]}", daemon=True).start()
        return Response({"operation_id": operation_id}, status=status.HTTP_202_ACCEPTED)

    @action(detail=False, methods=["get"], url_path="logs-status")
    def logs_status(self, request):
        operation_id = str(request.query_params.get("operation_id") or "").strip()
        if not operation_id:
            return Response({"message": "缺少 operation_id。"}, status=status.HTTP_400_BAD_REQUEST)
        progress = LogSearchProgressStore.get(operation_id)
        if not progress:
            return Response({"message": "日志检索任务不存在或已过期。"}, status=status.HTTP_404_NOT_FOUND)
        return Response(progress)

    @action(detail=False, methods=["post"], url_path="logs-cancel")
    def logs_cancel(self, request):
        payload = request.data if isinstance(request.data, dict) else {}
        operation_id = str(payload.get("operation_id") or "").strip()
        if not operation_id:
            return Response({"message": "缺少 operation_id。"}, status=status.HTTP_400_BAD_REQUEST)
        progress = LogSearchProgressStore.get(operation_id)
        if not progress:
            return Response({"message": "日志检索任务不存在或已过期。"}, status=status.HTTP_404_NOT_FOUND)
        return Response(request_log_search_cancel(operation_id))

    @action(detail=False, methods=["post"], url_path="logs")
    def logs(self, request):
        payload = request.data if isinstance(request.data, dict) else {}
        try:
            result = query_case_logs(
                str(payload.get("url") or payload.get("base_url") or ""),
                start_time=str(payload.get("start_time") or ""),
                end_time=str(payload.get("end_time") or ""),
                components=payload.get("components") or [],
                targets=payload.get("targets") or [],
                source_categories=payload.get("source_categories") if isinstance(payload.get("source_categories"), list) else None,
                levels=payload.get("levels") or [],
                anomaly_rules=payload.get("anomaly_rules") if isinstance(payload.get("anomaly_rules"), list) else [],
                keyword=str(payload.get("keyword") or ""),
                max_lines=int(payload.get("max_lines") or 12000),
            )
            save_workspace_snapshot(
                str(result.get("base_url") or payload.get("url") or ""),
                payload.get("workspace_state") if isinstance(payload.get("workspace_state"), dict) else {
                    "startTime": str(payload.get("start_time") or ""),
                    "endTime": str(payload.get("end_time") or ""),
                    "keyword": str(payload.get("keyword") or ""),
                    "selectedTargets": payload.get("targets") or [],
                    "sourceCategories": payload.get("source_categories") or [],
                    "onlyErrors": bool(payload.get("anomaly_rules")),
                },
                result,
            )
        except (AtLogError, FileNotFoundError, TypeError, ValueError) as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("atlog.logs.failed")
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(result)


    @action(detail=False, methods=["post"], url_path="browse")
    def browse(self, request):
        payload = request.data if isinstance(request.data, dict) else {}
        try:
            result = list_case_directory(
                str(payload.get("url") or payload.get("base_url") or ""),
                str(payload.get("relative_path") or ""),
            )
        except (AtLogError, FileNotFoundError, TypeError, ValueError) as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("atlog.browse.failed")
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(result)

    @action(detail=False, methods=["post"], url_path="read-file")
    def read_file(self, request):
        payload = request.data if isinstance(request.data, dict) else {}
        try:
            result = read_case_file(
                str(payload.get("url") or payload.get("base_url") or ""),
                str(payload.get("relative_path") or ""),
                int(payload.get("max_bytes") or 2 * 1024 * 1024),
            )
        except (AtLogError, FileNotFoundError, TypeError, ValueError) as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("atlog.read_file.failed")
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(result)
