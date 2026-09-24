"""HTTP surface for log watches.

Read-mostly: watches are created from the rule settings page, and the live stream is served
over SSE so the browser can append hits to the timeline as they happen.
"""
from __future__ import annotations

import logging
import time

from django.http import StreamingHttpResponse
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.environments.models import ResourceSettings
from apps.logsources.models import LogWatch, LogWatchHit, LogWatchLevel, LogWatchTriggerKind
from apps.logsources.serializers import LogWatchSerializer, LogWatchWriteSerializer
from apps.logsources.services.watch_events import WatchEventBus
from apps.logsources.services.watch_rules import compile_display_rules

logger = logging.getLogger("tracelens.logsources.watch_views")


class LogWatchViewSet(viewsets.ModelViewSet):
    queryset = LogWatch.objects.select_related("environment").all()
    serializer_class = LogWatchSerializer

    def get_serializer_class(self):
        if self.action in {"create", "update", "partial_update"}:
            return LogWatchWriteSerializer
        return LogWatchSerializer

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        environment_id = str(request.query_params.get("environment_id") or "").strip()
        if environment_id:
            queryset = queryset.filter(environment_id=int(environment_id))
        enabled = str(request.query_params.get("enabled") or "").strip().lower()
        if enabled in {"1", "true", "yes"}:
            queryset = queryset.filter(enabled=True)
        rows = self.get_serializer(queryset, many=True).data
        return Response({
            "count": len(rows),
            "watches": rows,
            # Surface why a watch is idle instead of leaving the user staring at nothing.
            "worker": {
                "redis_available": WatchEventBus.enabled(),
                "lease_holders": {
                    str(item["id"]): WatchEventBus.lease_holder(item["id"])
                    for item in rows if item.get("enabled")
                },
            },
        })

    @action(detail=True, methods=["get"], url_path="hits")
    def hits(self, request, pk=None):
        """Durable hit history, or a resume from a client-supplied sequence."""
        watch = self.get_object()
        since_seq = int(str(request.query_params.get("since") or "0") or 0)
        limit = max(1, min(500, int(str(request.query_params.get("limit") or "100") or 100)))
        queryset = LogWatchHit.objects.filter(watch=watch, seq__gt=since_seq).order_by("seq")[:limit]
        # Same two fields the live SSE hit carries, so a restored/replayed hit is classified
        # by the same rule as a fresh one.
        display_mode = "data" if watch.extraction_rule_id else "semantic"
        show_on_timeline = not watch.extraction_rule_id
        rows = [
            {
                "id": hit.pk,
                "seq": hit.seq,
                "display_mode": display_mode,
                "show_on_timeline": show_on_timeline,
                "matched_at": hit.matched_at.isoformat() if hit.matched_at else "",
                "signature": hit.signature,
                "level": hit.level,
                "machine": hit.machine,
                "subsystem": hit.subsystem,
                "fm": hit.fm,
                "source_path": hit.source_path,
                "line_text": hit.line_text,
                "window_lines": hit.window_lines,
                "extracted": hit.extracted,
            }
            for hit in queryset
        ]
        return Response({
            "watch_id": watch.pk,
            "since": since_seq,
            "count": len(rows),
            "max_seq": rows[-1]["seq"] if rows else since_seq,
            "hits": rows,
        })

    @action(detail=True, methods=["get"], url_path="replay")
    def replay(self, request, pk=None):
        """Buffered hits from the Redis ring, for a browser that just reconnected.

        The database holds every hit; the ring only covers the recent window cheaply and
        lets a reconnecting client catch up without re-reading the whole history.
        """
        watch = self.get_object()
        since_seq = int(str(request.query_params.get("since") or "0") or 0)
        return Response({
            "watch_id": watch.pk,
            "since": since_seq,
            "transport": "redis-ring",
            "hits": WatchEventBus.recent_hits(watch.pk, since_seq),
        })

    @action(detail=False, methods=["post"], url_path="sync-capture")
    def sync_capture(self, request):
        """Make the live watches match the rules the user opted into.

        Thin on purpose: the decision lives in ``services.watch_capture`` so the assistant's
        own tools cannot produce a different server state for the same request. ``kind``
        selects 语义规则 (timeline ribbon) or 数据提取器 (collector panel); they never share a watch.
        """
        from apps.environments.models import Environment
        from apps.logsources.services.watch_capture import SOURCES, sync_watches

        payload = request.data if isinstance(request.data, dict) else {}
        kind = str(payload.get("kind") or "extraction").strip()
        if kind not in SOURCES:
            return Response({"message": f"未知的监听类型 {kind}。"}, status=400)
        environment_id = payload.get("environment_id")
        if not environment_id:
            return Response({"message": "缺少 environment_id。"}, status=400)
        environment = Environment.objects.filter(pk=environment_id).first()
        if environment is None:
            return Response({"message": f"环境 {environment_id} 不存在。"}, status=404)

        outcome = sync_watches(
            environment,
            kind=kind,
            rule_ids=payload.get("rule_ids") or [],
            targets=payload.get("targets") or [],
            source_categories=payload.get("source_categories") or [],
            enable=payload.get("enable", True) is not False,
            target_rows=payload.get("target_rows") or 0,
        )
        rows = LogWatchSerializer(outcome["watches"], many=True).data
        return Response({
            "kind": kind,
            "environment_id": environment.id,
            "enabled_rule_ids": outcome["enabled_rule_ids"],
            "stopped_rule_ids": outcome["stopped_rule_ids"],
            "problems": outcome["problems"],
            "targets": [
                item for item in (payload.get("targets") or [])
                if isinstance(item, dict)
            ],
            "watches": rows,
        })

    @action(detail=False, methods=["get"], url_path="rule-health")
    def rule_health(self, request):
        """Which tagged rules the server can actually evaluate.

        The frontend silently drops malformed rules; a watcher must say so out loud, or a
        typo in a rule looks identical to "nothing is happening".
        """
        from apps.environments.models import ResourceSettings

        settings_obj = ResourceSettings.get_solo()
        rules = settings_obj.display_rules or []
        compiled, problems = compile_display_rules(rules)
        return Response({
            "total_rules": len([item for item in rules if isinstance(item, dict)]),
            "compiled": len(compiled),
            "watchable": [
                {
                    "id": rule.rule_id,
                    "name": rule.name,
                    "kind": rule.kind,
                    "label": rule.label_template or rule.name,
                    "color": rule.label_color,
                    "show_on_timeline": rule.show_on_timeline,
                }
                for rule in compiled
            ],
            "problems": problems,
        })


async def watch_event_stream(request):
    """SSE: every watch hit, fanned out from Redis pub/sub."""
    from urllib.parse import parse_qs

    try:
        since = int((parse_qs(request.META.get("QUERY_STRING", "")).get("since") or ["0"])[0])
    except (TypeError, ValueError):
        since = 0
    response = StreamingHttpResponse(
        WatchEventBus.event_stream(since),
        content_type="text/event-stream; charset=utf-8",
    )
    response["Cache-Control"] = "no-cache, no-transform"
    response["X-Accel-Buffering"] = "no"
    response["Connection"] = "keep-alive"
    return response
