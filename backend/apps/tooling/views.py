from __future__ import annotations

from .session_context import save_session_context

import asyncio
import json
import logging
import threading

from django.http import StreamingHttpResponse
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from apps.tooling.kernel import get_default_kernel
from apps.tooling.plugins.catalog import plugin_discovery_failures
from apps.tooling.registry import ToolInputError, list_skills
from apps.tooling.assistant import AssistantError, cancel_run as assistant_cancel_run, guide_run as assistant_guide_run, chat as assistant_chat, chat_stream as assistant_chat_stream, confirm as assistant_confirm
from apps.tooling.llm.client import LLMClientError, list_ai_models, resolve_model_choice, set_llm_overrides, set_llm_session_id
from apps.tooling.voice import VoiceServiceError, transcribe_audio, voice_service_capabilities

logger = logging.getLogger("tracelens.tools")



def _request_trace_context(request, *, source: str) -> dict[str, str]:
    user = getattr(request, "user", None)
    operator = ""
    try:
        if user is not None and getattr(user, "is_authenticated", False):
            operator = str(user.get_username() or "").strip()
    except Exception:
        operator = ""
    forwarded = str(request.META.get("HTTP_X_FORWARDED_FOR") or "").strip()
    client_ip = (forwarded.split(",", 1)[0].strip() if forwarded else str(request.META.get("REMOTE_ADDR") or "").strip())
    return {"operator": operator or ("TracePilot" if source == "assistant" else "页面用户"), "client_ip": client_ip, "source": source}


def _assistant_request_context(request, raw) -> dict:
    context = dict(raw) if isinstance(raw, dict) else {}
    context["_trace_context"] = _request_trace_context(request, source="assistant")
    return context

def _kernel():
    return get_default_kernel()


def _list_tools():
    return _kernel().registry.as_dicts()



class ToolViewSet(viewsets.ViewSet):
    @action(detail=False, methods=["post"], url_path="assistant-ui-receipt")
    def assistant_ui_receipt(self, request):
        from apps.tooling.assistant_runtime.state import RUN_STATE
        payload = request.data if isinstance(request.data, dict) else {}
        receipt = payload.get("receipt")
        if not isinstance(receipt, dict) or receipt.get("status") not in {"success", "failed"}:
            return Response({"message": "无效的页面回执"}, status=400)
        # IDs are random, issued only on this run's stream; stale/duplicate receipts fail closed.
        accepted = RUN_STATE.acknowledge_ui(str(payload.get("run_id") or ""), str(payload.get("action_id") or ""), {
            "status": receipt["status"], "detail": str(receipt.get("detail") or "")[:1000],
            "context": receipt.get("context") if isinstance(receipt.get("context"), dict) else {},
        })
        from apps.tooling.models import AgentUiReceipt
        from django.utils import timezone
        persisted = AgentUiReceipt.objects.filter(task_id=str(payload.get("run_id") or ""), action_id=str(payload.get("action_id") or ""), receipt__isnull=True, expires_at__gt=timezone.now(), task__status='running').update(receipt=receipt)
        return Response({"accepted": bool(accepted or persisted)}, status=200 if accepted or persisted else 409)

    def list(self, request):
        tools = _list_tools()
        kind_counts: dict[str, int] = {}
        domain_counts: dict[str, int] = {}
        for item in tools:
            kind = str(item.get("kind") or "unknown")
            domain = str(item.get("domain") or "general")
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
        return Response({
            "count": len(tools),
            "capability_count": len({item.get("atomic_id") for item in tools if item.get("atomic_id")}),
            "agent_ready_count": sum(1 for item in tools if item.get("agent_available")),
            "read_only_count": sum(1 for item in tools if item.get("read_only")),
            "kind_counts": kind_counts,
            "domain_counts": domain_counts,
            "skills": list_skills(),
            # Non-empty means a plugin module failed to import and its capability is
            # missing from the agent's toolset. Surfaced so a broken plugin is visible
            # instead of looking like a capability that was never written.
            "plugin_failures": plugin_discovery_failures(),
            "tools": tools,
        })

    def retrieve(self, request, pk=None):
        tool = _kernel().registry.get(pk or "")
        if tool is None:
            return Response({"message": "工具不存在。"}, status=status.HTTP_404_NOT_FOUND)
        return Response(tool.as_dict())


    @action(detail=False, methods=["post"], url_path="rule-autoconfig")
    def rule_autoconfig(self, request):
        """一键自动配置：把一条真实日志样例识别成数据提取器 / 语义规则配置。

        Triggered by an explicit button in each rule drawer. The model only *proposes*;
        every proposed field/parameter is verified against the sample text before it is
        returned, so the button can never fill a rule that extracts nothing.
        """
        from apps.tooling.rule_autoconfig import (
            autoconfigure_data_extractor,
            autoconfigure_semantic_rule,
        )

        payload = request.data if isinstance(request.data, dict) else {}
        target = str(payload.get("target") or "").strip().lower()
        sample = str(payload.get("sample") or "")
        hints = payload.get("hints") if isinstance(payload.get("hints"), dict) else {}
        try:
            if target == "data_extractor":
                candidates = payload.get("structured_candidates") if isinstance(payload.get("structured_candidates"), list) else []
                result = autoconfigure_data_extractor(
                    sample, hints=hints, structured_candidates=candidates, focus=str(payload.get("focus") or "all")
                )
            elif target == "semantic_rule":
                result = autoconfigure_semantic_rule(
                    sample, kind=str(payload.get("kind") or "keyword"), hints=hints, focus=str(payload.get("focus") or "all")
                )
            else:
                return Response({"message": "target 必须是 data_extractor 或 semantic_rule。"}, status=400)
        except ValueError as exc:
            # 样例不足 / 模型没给出可解析结果：这是用户能自己修的输入问题，不是网关故障。
            return Response({"message": str(exc)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except LLMClientError as exc:
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        except Exception as exc:  # noqa: BLE001
            logger.exception("tools.rule_autoconfig.failed target=%s", target)
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(result)

    @action(detail=False, methods=["get"], url_path="assistant-voice-capabilities")
    def assistant_voice_capabilities(self, request):
        return Response(voice_service_capabilities())

    @action(detail=False, methods=["post"], url_path="case-draft")
    def case_draft(self, request):
        """Turn the current conversation into an importable case draft.

        Driven by an explicit button, not by hoping the model calls a tool at the end of an
        analysis: the trigger is deterministic, only the extraction uses a model.
        """
        from apps.tooling.case_draft import build_case_draft

        payload = request.data if isinstance(request.data, dict) else {}
        messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
        if not messages:
            return Response({"message": "缺少 messages（用于整理的分析对话）。"}, status=400)
        try:
            result = build_case_draft(
                messages,
                context=payload.get("context") if isinstance(payload.get("context"), dict) else {},
                extra_evidence=payload.get("evidence") if isinstance(payload.get("evidence"), list) else [],
            )
        except LLMClientError as exc:
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        except Exception as exc:  # noqa: BLE001
            logger.exception("assistant.case_draft.failed")
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        if not result.get("ok"):
            return Response({"message": result.get("error") or "整理失败。"}, status=422)
        return Response(result)

    @action(detail=False, methods=["get"], url_path="ai-models")
    def ai_models(self, request):
        """Model + reasoning-effort options for the composer's picker.

        Never fails the UI: an unreachable gateway degrades to the configured model.
        """
        try:
            return Response(list_ai_models(force=str(request.query_params.get("refresh") or "") in {"1", "true"}))
        except Exception:  # noqa: BLE001
            logger.exception("assistant.ai_models.failed")
            return Response({"models": [], "source": "unavailable", "default_model": "", "current": {"model": "", "effort": ""}})

    @action(
        detail=False,
        methods=["post"],
        url_path="assistant-transcribe",
        parser_classes=[MultiPartParser, FormParser],
    )
    def assistant_transcribe(self, request):
        upload = request.FILES.get("file")
        if upload is None:
            return Response({"message": "缺少录音文件。"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            text = transcribe_audio(
                file_name=str(getattr(upload, "name", "") or "tracepilot-voice.webm"),
                content_type=str(getattr(upload, "content_type", "") or "application/octet-stream"),
                content=upload.read(),
            )
        except VoiceServiceError as exc:
            return Response({"message": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except Exception as exc:  # noqa: BLE001
            logger.exception("assistant.voice.transcribe.failed")
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        logger.info("assistant.voice.transcribe.success chars=%s", len(text))
        return Response({"text": text})

    @action(detail=False, methods=["post"], url_path="assistant-chat-stream")
    def assistant_chat_stream(self, request):
        payload = request.data if isinstance(request.data, dict) else {}
        from apps.tooling.task_runtime import start_task, event_stream
        payload = {**payload, "session_id": request.headers.get("X-Session-Id", "") or payload.get("session_id", "")}
        payload["model"], payload["effort"] = resolve_model_choice(payload.get("model"), payload.get("effort"))
        try:
            task = start_task(payload, _assistant_request_context(request, payload.get("context")))
        except ValueError as exc:
            return Response({"message":str(exc)}, status=400)
        response = StreamingHttpResponse(event_stream(task.task_id, task.session_id), content_type="text/event-stream; charset=utf-8")
        response["Cache-Control"] = "no-cache, no-transform"
        response["X-Accel-Buffering"] = "no"
        return response

    @action(detail=False, methods=["post"], url_path="assistant-cancel")
    def assistant_cancel(self, request):
        payload = request.data if isinstance(request.data, dict) else {}
        run_id = str(payload.get("run_id") or "").strip()
        if not run_id:
            return Response({"message": "缺少 run_id。"}, status=status.HTTP_400_BAD_REQUEST)
        from apps.tooling.models import AgentTask
        AgentTask.objects.filter(task_id=run_id,status='running').update(status='cancelling')
        active = assistant_cancel_run(run_id)
        logger.info("assistant.cancel.request run=%s active=%s", run_id, active)
        return Response({"cancelled": True, "active": active, "run_id": run_id})

    @action(detail=False, methods=["post"], url_path="assistant-guide")
    def assistant_guide(self, request):
        payload = request.data if isinstance(request.data, dict) else {}
        run_id = str(payload.get("run_id") or "").strip()
        message = str(payload.get("message") or "").strip()
        if not run_id or not message:
            return Response({"message": "缺少 run_id 或引导内容。"}, status=status.HTTP_400_BAD_REQUEST)
        accepted = assistant_guide_run(run_id, message)
        if not accepted:
            return Response({"message": "当前分析已结束，无法追加引导。"}, status=status.HTTP_409_CONFLICT)
        logger.info("assistant.guide.request run=%s message=%s", run_id, message[:500])
        return Response({"accepted": True, "run_id": run_id})

    @action(detail=False, methods=["post"], url_path="assistant-chat")
    def assistant_chat(self, request):
        payload = request.data if isinstance(request.data, dict) else {}
        session_id = request.headers.get("X-Session-Id", "") or str(payload.get("session_id") or "")
        set_llm_session_id(session_id)
        set_llm_overrides(*resolve_model_choice(payload.get("model"), payload.get("effort")))
        save_session_context(session_id, payload.get("context"), payload.get("history"))
        try:
            result = assistant_chat(
                message=str(payload.get("message") or ""),
                history=payload.get("history") if isinstance(payload.get("history"), list) else [],
                context=_assistant_request_context(request, payload.get("context")),
                skill_id=str(payload.get("skill_id") or "auto"),
                memory=str(payload.get("memory") or ""),
            )
        except (AssistantError, ToolInputError) as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("assistant.chat.failed")
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(result)

    @action(detail=False, methods=["post"], url_path="assistant-confirm")
    def assistant_confirm(self, request):
        payload = request.data if isinstance(request.data, dict) else {}
        try:
            result = assistant_confirm(
                str(payload.get("confirmation_token") or ""),
                instruction=str(payload.get("instruction") or ""),
                context=_assistant_request_context(request, payload.get("context")),
            )
        except (AssistantError, ToolInputError) as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("assistant.confirm.failed")
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(result)

    @action(detail=True, methods=["post"], url_path="invoke")
    def invoke(self, request, pk=None):
        payload = request.data if isinstance(request.data, dict) else {}
        try:
            result = _kernel().dispatch({"tool_id": pk or "", "arguments": payload, "context": {}}).get("data")
        except KeyError:
            return Response({"message": "工具不存在。"}, status=status.HTTP_404_NOT_FOUND)
        except ToolInputError as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("tool.invoke.failed tool=%s", pk)
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({"tool_id": pk, "success": True, "data": result})
