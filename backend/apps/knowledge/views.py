from __future__ import annotations

import logging

from django.db.models import Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.common.pagination import StandardResultsSetPagination
from apps.knowledge.models import AbnormalCase
from apps.knowledge.serializers import AbnormalCaseSerializer

logger = logging.getLogger("tracelens.knowledge")


class AbnormalCaseViewSet(viewsets.ModelViewSet):
    serializer_class = AbnormalCaseSerializer
    pagination_class = StandardResultsSetPagination

    @action(detail=False, methods=["post"], url_path="match")
    def match(self, request):
        """Rank stored cases against ad-hoc log evidence.

        Lets the analysis flow ask "have I seen this before?" straight from a log window,
        without first requiring an ATLog case URL.
        """
        from apps.atlog.knowledge_bridge import match_cases_from_log_context

        payload = request.data if isinstance(request.data, dict) else {}
        text = str(payload.get("text") or "").strip()
        if not text:
            return Response({"message": "缺少 text（用于比对的日志证据）。"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            result = match_cases_from_log_context(
                text[:60000],
                component=str(payload.get("component") or "")[:128],
                subsystem=str(payload.get("subsystem") or "")[:128],
                category=str(payload.get("category") or "")[:128],
                limit=int(payload.get("limit") or 5),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("knowledge.match.failed")
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(result)

    def get_queryset(self):
        qs = AbnormalCase.objects.all()
        params = self.request.query_params
        enabled = str(params.get("enabled") or "").strip().lower()
        if enabled in {"1", "true", "yes"}:
            qs = qs.filter(enabled=True)
        elif enabled in {"0", "false", "no"}:
            qs = qs.filter(enabled=False)
        environment_id = str(params.get("environment_id") or "").strip()
        if environment_id.isdigit():
            qs = qs.filter(Q(environment_id=int(environment_id)) | Q(environment__isnull=True))
        category = str(params.get("category") or "").strip()
        if category:
            qs = qs.filter(category=category)
        module = str(params.get("module") or "").strip().lower()
        if module:
            # SQLite JSON contains lookup is not consistently available; keep the API portable.
            ids = []
            for item in qs.only("id", "evidences").iterator():
                if any(str(e.get("module") or e.get("component") or "").strip().lower() == module for e in (item.evidences or []) if isinstance(e, dict)):
                    ids.append(item.id)
            qs = qs.filter(id__in=ids)
        query = str(params.get("query") or "").strip()
        if query:
            needle = query.lower()
            evidence_ids: list[int] = []
            for item in qs.only("id", "evidences").iterator():
                for evidence in (item.evidences or []):
                    if not isinstance(evidence, dict):
                        continue
                    parts = [
                        evidence.get("module"), evidence.get("component"), evidence.get("subsystem"),
                        evidence.get("function_name"), evidence.get("template"), evidence.get("raw"),
                    ]
                    parts.extend(evidence.get("tokens") or [])
                    for code in evidence.get("error_codes") or []:
                        if isinstance(code, dict):
                            parts.extend([code.get("key"), code.get("value")])
                    for rule in evidence.get("anomaly_rules") or []:
                        if isinstance(rule, dict):
                            parts.append(rule.get("keyword"))
                    if needle in " ".join(str(part or "") for part in parts).lower():
                        evidence_ids.append(item.id)
                        break
            qs = qs.filter(
                Q(name__icontains=query)
                | Q(category__icontains=query)
                | Q(symptom__icontains=query)
                | Q(root_cause__icontains=query)
                | Q(solution__icontains=query)
                | Q(description__icontains=query)
                | Q(id__in=evidence_ids)
            )
        return qs

    @action(detail=True, methods=["post"], url_path="mark-match")
    def mark_match(self, request, pk=None):
        case = self.get_object()
        case.mark_matched()
        return Response(self.get_serializer(case).data, status=status.HTTP_200_OK)
