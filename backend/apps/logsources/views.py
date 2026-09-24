from __future__ import annotations

import json
import logging
import time

from django.db import transaction
from django.db.models import Count, Q
from django.http import StreamingHttpResponse
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.environments.models import Environment
from apps.audits.models import AuditResult
from apps.audits.services import cancel_running_log_search_audits, create_log_search_audit, finish_log_search_audit, record_log_search_artifacts, record_log_search_cache_hit
from apps.logsources.models import EventCodeDefinition, LogFmDefinition, LogFormatParserRule, LogModuleKind, LogQuerySkill, LogSourceRule, LogSubsystemDefinition
from apps.logsources.serializers import (
    LogFmDefinitionSerializer,
    LogFormatParserRuleSerializer,
    LogFormatRuleDraftSerializer,
    LogQuerySkillSerializer,
    LogSourceRuleSerializer,
    LogSubsystemDefinitionSerializer,
    LogWindowRequestSerializer,
    LiveLogRequestSerializer,
    SemanticSourceRequestSerializer,
    SemanticSourceBatchRequestSerializer,
)
from apps.logsources.services.remote_logs import build_live_log_plan, scan_environment_logs, stream_live_log_events
from apps.logsources.services.unified_search import build_environment_search_plan, stream_environment_search_window
from apps.logsources.services.semantic_source import SemanticSourceError, recognize_semantic_batch, recognize_semantic_description
from apps.logsources.services.search_progress import LogSearchCancelled, LogSearchProgressStore, request_log_search_cancel
from apps.logsources.services.log_scene import LogSceneStore
from apps.logsources.services.search_result_cache import LogSearchResultCache
from apps.logsources.services.url_import import UrlImportError, filename_from_url, inspect_urls, stream_log_url
from apps.logsources.services.log_format_parser import BUILTIN_RULE_DEFAULTS, LogFormatRuleError, test_rule_config
from apps.tooling import services as tool_services
from apps.common.streaming import iter_sync_stream_in_thread

logger = logging.getLogger("tracelens.log_api")
process_logger = logging.getLogger("tracelens.process.live")


def _bulk_model_action(*, queryset, ids, action_name: str) -> dict:
    normalized = sorted({int(value) for value in ids if str(value).isdigit()})
    existing_ids = list(queryset.filter(id__in=normalized).values_list("id", flat=True))
    existing_set = set(existing_ids)
    skipped = [item for item in normalized if item not in existing_set]
    with transaction.atomic():
        if action_name == "delete":
            queryset.filter(id__in=existing_ids).delete()
        elif action_name in {"enable", "disable"}:
            queryset.filter(id__in=existing_ids).update(enabled=action_name == "enable")
        else:
            raise ValueError("不支持的批量操作。")
    return {"action": action_name, "requested": len(normalized), "affected": len(existing_ids), "deleted": len(existing_ids) if action_name == "delete" else 0, "updated": len(existing_ids) if action_name != "delete" else 0, "skipped_ids": skipped}


class LogFormatParserRuleViewSet(viewsets.ModelViewSet):
    serializer_class = LogFormatParserRuleSerializer

    def get_queryset(self):
        queryset = LogFormatParserRule.objects.all()
        category = self.request.query_params.get("category", "").strip().lower()
        enabled = self.request.query_params.get("enabled", "").strip().lower()
        if category:
            queryset = queryset.filter(category=category)
        if enabled in {"1", "true", "yes"}:
            queryset = queryset.filter(enabled=True)
        elif enabled in {"0", "false", "no"}:
            queryset = queryset.filter(enabled=False)
        return queryset.order_by("category", "-priority", "id")

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.built_in:
            return Response({"message": "系统内置解析规则不能删除，可停用或恢复默认。"}, status=status.HTTP_400_BAD_REQUEST)
        return super().destroy(request, *args, **kwargs)

    @action(detail=False, methods=["get"], url_path="runtime")
    def runtime(self, request):
        queryset = self.get_queryset()
        return Response(LogFormatParserRuleSerializer(queryset, many=True).data)

    @action(detail=False, methods=["post"], url_path="test-draft")
    def test_draft(self, request):
        serializer = LogFormatRuleDraftSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = dict(serializer.validated_data)
        text = payload.pop("text")
        try:
            return Response(test_rule_config(payload, text))
        except LogFormatRuleError as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="test")
    def test(self, request, pk=None):
        instance = self.get_object()
        text = str(request.data.get("text", ""))
        try:
            return Response(test_rule_config({
                "pattern": instance.pattern,
                "ignore_case": instance.ignore_case,
                "field_map": instance.field_map,
                "timestamp_format": instance.timestamp_format,
            }, text))
        except LogFormatRuleError as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="restore-default")
    def restore_default(self, request, pk=None):
        instance = self.get_object()
        if not instance.built_in:
            return Response({"message": "只有系统内置解析规则可以恢复默认。"}, status=status.HTTP_400_BAD_REQUEST)
        defaults = BUILTIN_RULE_DEFAULTS.get((instance.category, instance.name))
        if defaults is None:
            return Response({"message": "未找到该内置规则的默认模板。"}, status=status.HTTP_404_NOT_FOUND)
        for field, value in defaults.items():
            setattr(instance, field, value)
        instance.save()
        logger.info("log_format_rule.restore_default id=%s category=%s name=%s", instance.id, instance.category, instance.name)
        return Response(self.get_serializer(instance).data)


class LogSourceRuleViewSet(viewsets.ModelViewSet):
    serializer_class = LogSourceRuleSerializer
    queryset = LogSourceRule.objects.select_related("machine").all()

    @action(detail=False, methods=["post"], url_path="bulk")
    def bulk(self, request):
        try:
            payload = _bulk_model_action(queryset=LogSourceRule.objects.all(), ids=request.data.get("ids", []), action_name=request.data.get("action", ""))
        except (TypeError, ValueError) as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        logger.info("log_source_rule.bulk action=%s requested=%s affected=%s", payload["action"], payload["requested"], payload["affected"] )
        return Response(payload)


class LogQuerySkillViewSet(viewsets.ModelViewSet):
    serializer_class = LogQuerySkillSerializer

    def get_queryset(self):
        queryset = LogQuerySkill.objects.select_related("subsystem")
        subsystem = self.request.query_params.get("subsystem", "").strip()
        query = self.request.query_params.get("q", "").strip()
        enabled = self.request.query_params.get("enabled", "").strip().lower()
        if subsystem:
            if subsystem.isdigit():
                queryset = queryset.filter(subsystem_id=int(subsystem))
            else:
                queryset = queryset.filter(Q(subsystem__name__iexact=subsystem) | Q(subsystem__display_name__iexact=subsystem))
        if query:
            queryset = queryset.filter(
                Q(name__icontains=query)
                | Q(description__icontains=query)
                | Q(subsystem__name__icontains=query)
                | Q(subsystem__display_name__icontains=query)
            )
        if enabled in {"1", "true", "yes"}:
            queryset = queryset.filter(enabled=True)
        elif enabled in {"0", "false", "no"}:
            queryset = queryset.filter(enabled=False)
        return queryset.order_by("subsystem__sort_order", "subsystem__name", "-priority", "name")

    @action(detail=False, methods=["post"], url_path="bulk")
    def bulk(self, request):
        try:
            payload = _bulk_model_action(
                queryset=LogQuerySkill.objects.all(),
                ids=request.data.get("ids", []),
                action_name=request.data.get("action", ""),
            )
        except (TypeError, ValueError) as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        logger.info(
            "log_query_skill.bulk action=%s requested=%s affected=%s",
            payload["action"], payload["requested"], payload["affected"],
        )
        return Response(payload)


class LogSubsystemDefinitionViewSet(viewsets.ModelViewSet):
    serializer_class = LogSubsystemDefinitionSerializer

    def get_queryset(self):
        queryset = LogSubsystemDefinition.objects.prefetch_related("fms", "fms__target_modules", "fms__target_modules__subsystem").annotate(fm_count=Count("fms"))
        query = self.request.query_params.get("q", "").strip()
        enabled = self.request.query_params.get("enabled", "").strip().lower()
        if query:
            queryset = queryset.filter(
                Q(name__icontains=query)
                | Q(display_name__icontains=query)
                | Q(description__icontains=query)
                | Q(fms__name__icontains=query)
                | Q(fms__display_name__icontains=query)
            ).distinct()
        if enabled in {"1", "true", "yes"}:
            queryset = queryset.filter(enabled=True)
        elif enabled in {"0", "false", "no"}:
            queryset = queryset.filter(enabled=False)
        return queryset.order_by("sort_order", "name")

    @action(detail=False, methods=["get"], url_path="tree")
    def tree(self, request):
        include_disabled = request.query_params.get("include_disabled", "").strip().lower() in {"1", "true", "yes"}
        queryset = self.get_queryset()
        if not include_disabled:
            queryset = queryset.filter(enabled=True).prefetch_related(
                "fms"
            )
        payload = []
        for subsystem in queryset:
            fms = subsystem.fms.all().order_by("sort_order", "name")
            if not include_disabled:
                fms = fms.filter(enabled=True)
            payload.append({
                "id": subsystem.id,
                "name": subsystem.name,
                "display_name": subsystem.display_name,
                "effective_name": subsystem.display_name or subsystem.name,
                "enabled": subsystem.enabled,
                "sort_order": subsystem.sort_order,
                "description": subsystem.description,
                "last_discovered_at": subsystem.last_discovered_at,
                "fms": LogFmDefinitionSerializer(fms, many=True).data,
            })
        return Response(payload)

    @action(detail=False, methods=["post"], url_path="bulk")
    def bulk(self, request):
        try:
            payload = _bulk_model_action(queryset=LogSubsystemDefinition.objects.all(), ids=request.data.get("ids", []), action_name=request.data.get("action", ""))
        except (TypeError, ValueError) as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        logger.info("log_subsystem.bulk action=%s requested=%s affected=%s skipped=%s", payload["action"], payload["requested"], payload["affected"], payload["skipped_ids"] )
        return Response(payload)


class LogFmDefinitionViewSet(viewsets.ModelViewSet):
    serializer_class = LogFmDefinitionSerializer

    def get_queryset(self):
        queryset = LogFmDefinition.objects.select_related("subsystem").prefetch_related("target_modules", "target_modules__subsystem")
        subsystem = self.request.query_params.get("subsystem", "").strip()
        query = self.request.query_params.get("q", "").strip()
        enabled = self.request.query_params.get("enabled", "").strip().lower()
        if subsystem:
            queryset = queryset.filter(subsystem_id=subsystem)
        if query:
            queryset = queryset.filter(
                Q(name__icontains=query)
                | Q(display_name__icontains=query)
                | Q(description__icontains=query)
                | Q(subsystem__name__icontains=query)
            )
        if enabled in {"1", "true", "yes"}:
            queryset = queryset.filter(enabled=True)
        elif enabled in {"0", "false", "no"}:
            queryset = queryset.filter(enabled=False)
        return queryset.order_by("subsystem__sort_order", "subsystem__name", "sort_order", "name")

    @action(detail=False, methods=["post"], url_path="bulk")
    def bulk(self, request):
        action_name = str(request.data.get("action", "")).strip()
        if action_name == "set_target_modules":
            try:
                ids = sorted({int(value) for value in request.data.get("ids", []) if str(value).isdigit()})
                target_module_ids = sorted({int(value) for value in (request.data.get("target_module_ids", []) or []) if str(value).isdigit()})
            except (TypeError, ValueError):
                return Response({"message": "模块或目标模块参数无效。"}, status=status.HTTP_400_BAD_REQUEST)
            modules = list(LogFmDefinition.objects.filter(id__in=ids).order_by("id"))
            existing_ids = {item.id for item in modules}
            skipped_ids = [item for item in ids if item not in existing_ids]
            target_qs = LogFmDefinition.objects.filter(id__in=target_module_ids).order_by("query_priority", "subsystem__name", "name")
            with transaction.atomic():
                for module in modules:
                    module.target_modules.set(target_qs)
            payload = {
                "action": action_name,
                "requested": len(ids),
                "affected": len(modules),
                "updated": len(modules),
                "skipped_ids": skipped_ids,
            }
            logger.info(
                "log_fm.bulk_targets requested=%s affected=%s targets=%s skipped=%s",
                len(ids), len(modules), target_module_ids, skipped_ids,
            )
            return Response(payload)
        try:
            payload = _bulk_model_action(queryset=LogFmDefinition.objects.all(), ids=request.data.get("ids", []), action_name=action_name)
        except (TypeError, ValueError) as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        logger.info("log_fm.bulk action=%s requested=%s affected=%s skipped=%s", payload["action"], payload["requested"], payload["affected"], payload["skipped_ids"] )
        return Response(payload)


class LogUrlImportViewSet(viewsets.ViewSet):
    """Proxy Nginx-hosted logs into the local import workflow without browser CORS requirements."""

    def create(self, request):
        raw_urls = request.data.get("urls", []) if isinstance(request.data, dict) else []
        if isinstance(raw_urls, str):
            raw_urls = [item.strip() for item in raw_urls.splitlines() if item.strip()]
        if not isinstance(raw_urls, list) or not raw_urls:
            return Response({"message": "请至少输入一个日志链接。"}, status=status.HTTP_400_BAD_REQUEST)
        if len(raw_urls) > 50:
            return Response({"message": "一次最多检查 50 个日志链接。"}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"results": inspect_urls([str(item) for item in raw_urls])})

    @action(detail=False, methods=["post"], url_path="content")
    def content(self, request):
        payload = request.data if isinstance(request.data, dict) else {}
        url = str(payload.get("url", "")).strip()
        member = str(payload.get("member", "")).strip()
        try:
            filename = filename_from_url(url)
            download_name = member.rsplit("/", 1)[-1] if member else filename
            iterator = stream_log_url(url, member)
        except UrlImportError as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        response = StreamingHttpResponse(iterator, content_type="text/plain; charset=utf-8")
        response["Content-Disposition"] = f'inline; filename="{download_name.replace(chr(34), "")}"'
        response["Cache-Control"] = "no-store"
        return response


class LogSceneViewSet(viewsets.ViewSet):
    """Short URL scene snapshots; payload is UI state, not log content."""

    def create(self, request):
        payload = request.data if isinstance(request.data, dict) else {}
        scene_id = LogSceneStore.create(dict(payload))
        return Response({"id": scene_id})

    def retrieve(self, request, pk=None):
        payload = LogSceneStore.get(str(pk or ""))
        if payload is None:
            return Response({"message": "日志现场链接已失效。"}, status=status.HTTP_404_NOT_FOUND)
        return Response({"id": str(pk), "scene": payload})

    def update(self, request, pk=None):
        scene_id = str(pk or "").strip()
        if not scene_id:
            return Response({"message": "现场 ID 不能为空。"}, status=status.HTTP_400_BAD_REQUEST)
        payload = request.data if isinstance(request.data, dict) else {}
        LogSceneStore.put(scene_id, dict(payload))
        return Response({"id": scene_id})

    partial_update = update


class EnvironmentLogViewSet(viewsets.ViewSet):
    def _environment(self, pk):
        return Environment.objects.select_related("upper_machine").prefetch_related("machine_relations__target_machine").get(pk=pk)

    @action(detail=True, methods=["get"], url_path="subsystems")
    def subsystems(self, request, pk=None):
        categories = list(filter(None, request.query_params.getlist("source_category")))
        refresh = request.query_params.get("refresh", "").strip().lower() in {"1", "true", "yes"}
        logger.info("log.catalog.api.tool_service environment=%s categories=%s refresh=%s", pk, categories, refresh)
        try:
            return Response(tool_services.get_log_catalog({
                "environment_id": pk,
                "source_categories": categories,
                "refresh": refresh,
            }))
        except tool_services.ToolInputError as exc:
            code = status.HTTP_404_NOT_FOUND if "不存在" in str(exc) else status.HTTP_400_BAD_REQUEST
            return Response({"message": str(exc)}, status=code)
        except Exception as exc:
            logger.exception("log.catalog.api.failed environment=%s", pk)
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

    @action(detail=True, methods=["post"], url_path="semantic-source")
    def semantic_source(self, request, pk=None):
        try:
            payload = tool_services.recognize_semantic_source({"environment_id": pk, **dict(request.data)})
            logger.info("semantic.source.api.tool_service environment=%s found=%s", pk, payload.get("found"))
            return Response(payload)
        except tool_services.ToolInputError as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except SemanticSourceError as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("semantic.source.api.failed environment=%s", pk)
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

    @action(detail=True, methods=["post"], url_path="semantic-source-batch")
    def semantic_source_batch(self, request, pk=None):
        try:
            payload = tool_services.recognize_semantic_sources_batch({"environment_id": pk, **dict(request.data)})
            logger.info("semantic.source.batch.tool_service environment=%s matched=%d misses=%d", pk, len(payload.get("results", [])), len(payload.get("misses", [])))
            return Response(payload)
        except tool_services.ToolInputError as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("semantic.source.batch.failed environment=%s", pk)
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

    @action(detail=True, methods=["post"], url_path="plan")
    def plan(self, request, pk=None):
        try:
            payload = tool_services.build_log_plan_tool({
                "environment_id": pk,
                "operation_id": getattr(request, "trace_id", ""),
                **dict(request.data),
            })
            logger.info("log.plan.api.tool_service environment=%s artifacts=%s", pk, payload.get("count"))
            return Response(payload)
        except tool_services.ToolInputError as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("log.plan.api.failed environment=%s", pk)
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

    @action(detail=True, methods=["get"], url_path="progress")
    def progress(self, request, pk=None):
        try:
            return Response(tool_services.get_search_progress({
                "environment_id": pk,
                "operation_id": request.query_params.get("operation_id", ""),
            }))
        except tool_services.ToolInputError as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None):
        environment = self._environment(pk)
        operation_id = str(request.data.get("operation_id", "")).strip()
        if not operation_id:
            return Response({"message": "缺少 operation_id。"}, status=status.HTTP_400_BAD_REQUEST)
        progress = request_log_search_cancel(operation_id)
        try:
            cancel_running_log_search_audits(
                operation_id=operation_id,
                environment_id=environment.id,
                result_count=max(0, int(progress.get("result_count") or 0)),
                artifact_count=max(0, int(progress.get("selected_files") or progress.get("artifact_total") or 0)),
            )
        except Exception:
            # Audit persistence must never block the user's stop action.
            logger.exception("log.audit.cancel_failed environment=%s operation=%s", environment.id, operation_id)
        return Response(progress)

    @action(detail=True, methods=["post"], url_path="live")
    def live(self, request, pk=None):
        serializer = LiveLogRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        environment = self._environment(pk)
        live_targets = list(serializer.validated_data.get("fm_targets", []))
        unique_live_targets = {
            (
                str(item.get("subsystem", "")).strip().casefold(),
                str(item.get("fm", "")).strip().casefold(),
                str(item.get("kind", LogModuleKind.NORMAL)).strip() or LogModuleKind.NORMAL,
            )
            for item in live_targets
            if str(item.get("subsystem", "")).strip() and str(item.get("fm", "")).strip()
        }
        if len(live_targets) != 1 or len(unique_live_targets) != 1:
            return Response(
                {"message": "实时日志监听仅支持单模块，请只选择一个模块后再开启监听。"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        live_target = live_targets[0]
        try:
            plan = build_live_log_plan(
                environment,
                [live_target["subsystem"]],
                [live_target["fm"]],
                serializer.validated_data.get("source_categories", []),
                fm_targets=[live_target],
            )
        except Exception as exc:
            logger.exception("log.live.api.plan_failed environment=%s", environment.id)
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        if not plan:
            return Response(
                {"message": "当前选择的日志范围没有可实时监听的 current 日志文件。"},
                status=status.HTTP_404_NOT_FOUND,
            )

        def encode_event(payload: dict):
            event_type = str(payload.get("type") or "message")
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            yield f"event: {event_type}\ndata: {data}\n\n".encode("utf-8")

        def event_stream():
            live_target_label = f"{live_target['subsystem']}/{live_target['fm']}"
            process_logger.info(
                "[LIVE] subscribe environment=%s target=%s files=%s",
                environment.id, live_target_label, len(plan),
            )
            try:
                for payload in stream_live_log_events(environment, plan):
                    yield from encode_event(payload)
            except GeneratorExit:
                process_logger.info(
                    "[LIVE] disconnect environment=%s target=%s reason=client_closed",
                    environment.id, live_target_label,
                )
                raise
            except Exception as exc:
                process_logger.exception(
                    "[LIVE] failed environment=%s target=%s error=%s",
                    environment.id, live_target_label, exc,
                )
                yield from encode_event({"type": "error", "message": str(exc)})
            finally:
                process_logger.info(
                    "[LIVE] stop environment=%s target=%s",
                    environment.id, live_target_label,
                )

        response = StreamingHttpResponse(
            # `event_stream` is a blocking generator (it tails remote files over SSH).
            # Handing a *sync* iterator to StreamingHttpResponse makes Django materialize
            # it with `sync_to_async(list)(...)`; for an endless live stream that means the
            # browser receives nothing at all. The bridge keeps the producer on its own
            # thread and forwards each frame as it appears.
            iter_sync_stream_in_thread(event_stream(), thread_name=f"live-log-{environment.id}"),
            content_type="text/event-stream; charset=utf-8",
        )
        response["Cache-Control"] = "no-cache, no-transform"
        response["X-Accel-Buffering"] = "no"
        response["Connection"] = "keep-alive"
        response["Access-Control-Expose-Headers"] = "X-Accel-Buffering"
        return response

    @action(detail=True, methods=["post"], url_path="stream")
    def stream(self, request, pk=None):
        serializer = LogWindowRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        environment = self._environment(pk)
        start = serializer.naive(serializer.validated_data["start_time"])
        end = serializer.naive(serializer.validated_data["end_time"])
        raw_text = bool(serializer.validated_data.get("raw_text", False))
        operation_id = request.headers.get("X-TraceLens-Operation-ID") or getattr(request, "trace_id", "-")
        logger.info("log.stream.api.start environment=%s operation=%s start=%s end=%s targets=%d keyword=%s", environment.id, operation_id, start, end, len(serializer.validated_data.get("fm_targets", [])), bool(serializer.validated_data.get("keyword")))
        audit = None
        try:
            audit = create_log_search_audit(
                request=request,
                environment=environment,
                operation_id=operation_id,
                payload=dict(request.data),
                start_time=serializer.validated_data["start_time"],
                end_time=serializer.validated_data["end_time"],
            )
        except Exception:
            # 审计异常不能反向阻断用户日志检索，但必须留后端日志便于修复。
            logger.exception("log.audit.create_failed environment=%s operation=%s", environment.id, operation_id)
        LogSearchProgressStore.start(operation_id, environment_id=environment.id)

        # Exact repeated requests (especially reopened shared-scene URLs) can be
        # restored from the final-result cache without SSH or file enumeration.
        request_payload = dict(serializer.validated_data)
        for key in ("start_time", "end_time"):
            value = request_payload.get(key)
            if hasattr(value, "isoformat"):
                request_payload[key] = value.isoformat()
        cache_identity = LogSearchResultCache.identity(environment.id, request_payload)
        cache_allowed = LogSearchResultCache.allow_final_cache(serializer.validated_data["end_time"])
        cached_result = LogSearchResultCache.get(environment.id, request_payload) if cache_allowed else None
        if not cache_allowed:
            logger.info(
                "log.result_cache.bypass_recent environment=%s operation=%s cache=%s end=%s guard_seconds=%s",
                environment.id, operation_id, cache_identity[:12], end, LogSearchResultCache.recent_guard_seconds(),
            )
        if cached_result is not None:
            cached_path, cached_meta = cached_result
            logger.info(
                "log.result_cache.hit environment=%s operation=%s cache=%s bytes=%s",
                environment.id, operation_id, cache_identity[:12], cached_path.stat().st_size,
            )
            result_count = max(0, int(cached_meta.get("result_count") or 0))
            try:
                record_log_search_cache_hit(
                    audit,
                    artifact_count=max(0, int(cached_meta.get("artifact_count") or 0)),
                    result_count=result_count,
                    output_bytes=int(cached_path.stat().st_size),
                )
            except Exception:
                logger.exception("log.audit.cache_diagnostic_failed environment=%s operation=%s", environment.id, operation_id)
            LogSearchProgressStore.patch(
                operation_id,
                stage="complete", percent=100, done=True,
                artifact_total=max(0, int(cached_meta.get("artifact_count") or 0)),
                artifact_done=max(0, int(cached_meta.get("artifact_count") or 0)),
                result_count=result_count,
                output_bytes=int(cached_path.stat().st_size),
                message="已从结果缓存恢复日志",
            )

            def cached_stream():
                output_bytes = 0
                try:
                    for chunk in LogSearchResultCache.iter_file(cached_path):
                        output_bytes += len(chunk)
                        yield chunk
                    finish_log_search_audit(
                        audit,
                        result=AuditResult.SUCCESS if result_count else AuditResult.NO_RESULT,
                        artifact_count=max(0, int(cached_meta.get("artifact_count") or 0)),
                        result_count=result_count,
                        output_bytes=output_bytes,
                    )
                except GeneratorExit:
                    finish_log_search_audit(audit, result=AuditResult.CANCELLED, error_message="客户端停止读取缓存日志。", result_count=result_count, output_bytes=output_bytes)
                    raise

            # A sync iterator would be materialized by Django (`sync_to_async(list)`),
            # so the browser saw nothing until the whole search finished and the entire
            # result had to be held in memory. The bridge streams each chunk as it is read.
            response = StreamingHttpResponse(
                iter_sync_stream_in_thread(cached_stream(), thread_name=f"log-stream-{environment.id}"),
                content_type="text/plain; charset=utf-8",
            )
            response["Content-Disposition"] = f'attachment; filename="environment-{environment.id}-logs.log"'
            response["X-TraceLens-Artifact-Count"] = str(max(0, int(cached_meta.get("artifact_count") or 0)))
            response["X-TraceLens-Operation-ID"] = operation_id
            response["X-TraceLens-Result-Cache"] = "HIT"
            response["Access-Control-Expose-Headers"] = "X-TraceLens-Artifact-Count, X-TraceLens-Operation-ID, X-TraceLens-Result-Cache"
            return response

        logger.info(
            "log.result_cache.miss environment=%s operation=%s cache=%s normalized=%s",
            environment.id, operation_id, cache_identity[:12], LogSearchResultCache.normalize_request(request_payload),
        )
        try:
            plan = build_environment_search_plan(
                environment,
                start,
                end,
                serializer.validated_data["subsystems"],
                serializer.validated_data["fms"],
                serializer.validated_data["source_categories"],
                fm_targets=serializer.validated_data.get("fm_targets", []),
                operation_id=operation_id,
            )
        except LogSearchCancelled:
            LogSearchProgressStore.cancelled(operation_id)
            finish_log_search_audit(audit, result=AuditResult.CANCELLED, error_message="日志检索已停止。")
            return Response({"message": "日志检索已停止。", "operation_id": operation_id}, status=499)
        except Exception as exc:
            LogSearchProgressStore.fail(operation_id, str(exc))
            finish_log_search_audit(audit, result=AuditResult.FAILED, error_message=str(exc))
            logger.exception("log.stream.api.plan_failed environment=%s operation=%s", environment.id, operation_id)
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        try:
            record_log_search_artifacts(audit, plan)
        except Exception:
            logger.exception("log.audit.files_failed environment=%s operation=%s", environment.id, operation_id)

        def audited_stream():
            output_bytes = 0
            _, cache_data_path, cache_meta_path, cache_part_path = LogSearchResultCache.open_writer(environment.id, request_payload, operation_id)
            cache_handle = None
            cache_enabled = cache_allowed
            try:
                if cache_enabled:
                    try:
                        cache_handle = cache_part_path.open("wb")
                    except OSError:
                        cache_enabled = False
                for chunk in stream_environment_search_window(environment, plan, start, end, operation_id, include_internal_markers=not raw_text):
                    output_bytes += len(chunk)
                    if cache_enabled and cache_handle is not None:
                        if output_bytes <= LogSearchResultCache.max_bytes():
                            cache_handle.write(chunk)
                        else:
                            cache_enabled = False
                            cache_handle.close()
                            cache_handle = None
                            cache_part_path.unlink(missing_ok=True)
                    yield chunk
                if cache_handle is not None:
                    cache_handle.close()
                    cache_handle = None
                progress = LogSearchProgressStore.get(operation_id) or {}
                stage = str(progress.get("stage") or "")
                message = str(progress.get("message") or "")
                result_count = max(0, int(progress.get("result_count") or 0))
                # Empty results are deliberately not cached. A directory/catalog
                # refresh or delayed log rotation may make the exact same query
                # valid shortly afterwards, and a cached zero would hide it.
                if cache_enabled and result_count > 0:
                    cache_stored = LogSearchResultCache.commit(
                        data_path=cache_data_path, meta_path=cache_meta_path, part_path=cache_part_path,
                        metadata={
                            "created_at": time.time(),
                            "environment_id": environment.id,
                            "artifact_count": len(plan),
                            "result_count": result_count,
                            "output_bytes": output_bytes,
                            "request": LogSearchResultCache.normalize_request(request_payload),
                        },
                    )
                    logger.info(
                        "log.result_cache.store environment=%s operation=%s cache=%s stored=%s bytes=%s",
                        environment.id, operation_id, cache_identity[:12], cache_stored, output_bytes,
                    )
                elif cache_enabled:
                    cache_part_path.unlink(missing_ok=True)
                    logger.info(
                        "log.result_cache.skip_empty environment=%s operation=%s cache=%s",
                        environment.id, operation_id, cache_identity[:12],
                    )
                if stage == "cancelled":
                    finish_log_search_audit(audit, result=AuditResult.CANCELLED, error_message=message or "日志检索已停止。", artifact_count=len(plan), result_count=result_count, output_bytes=output_bytes)
                elif stage == "error":
                    finish_log_search_audit(audit, result=AuditResult.FAILED, error_message=message or "日志检索失败。", artifact_count=len(plan), result_count=result_count, output_bytes=output_bytes)
                elif result_count == 0:
                    finish_log_search_audit(audit, result=AuditResult.NO_RESULT, artifact_count=len(plan), result_count=0, output_bytes=output_bytes)
                else:
                    finish_log_search_audit(audit, result=AuditResult.SUCCESS, artifact_count=len(plan), result_count=result_count, output_bytes=output_bytes)
            except GeneratorExit:
                if cache_handle is not None:
                    cache_handle.close()
                cache_part_path.unlink(missing_ok=True)
                finish_log_search_audit(audit, result=AuditResult.FAILED, error_message="客户端在日志流完成前断开连接。", artifact_count=len(plan), result_count=max(0, int((LogSearchProgressStore.get(operation_id) or {}).get("result_count") or 0)), output_bytes=output_bytes)
                raise
            except Exception as exc:
                if cache_handle is not None:
                    cache_handle.close()
                cache_part_path.unlink(missing_ok=True)
                finish_log_search_audit(audit, result=AuditResult.FAILED, error_message=str(exc), artifact_count=len(plan), result_count=max(0, int((LogSearchProgressStore.get(operation_id) or {}).get("result_count") or 0)), output_bytes=output_bytes)
                raise

        response = StreamingHttpResponse(
            iter_sync_stream_in_thread(audited_stream(), thread_name=f"log-stream-{environment.id}"),
            content_type="text/plain; charset=utf-8",
        )
        response["Content-Disposition"] = f'attachment; filename="environment-{environment.id}-logs.log"'
        response["X-TraceLens-Artifact-Count"] = str(len(plan))
        response["X-TraceLens-Operation-ID"] = operation_id
        response["X-TraceLens-Result-Cache"] = "MISS" if cache_allowed else "BYPASS-RECENT"
        response["Access-Control-Expose-Headers"] = "X-TraceLens-Artifact-Count, X-TraceLens-Operation-ID, X-TraceLens-Result-Cache"
        return response
