from __future__ import annotations

import logging
import time
import uuid

from apps.common.logging import reset_request_id, set_request_id

logger = logging.getLogger("tracelens.http")


class RequestIdMiddleware:
    """Attach a stable operation id to API responses and structured server logs."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = request.headers.get("X-TraceLens-Request-ID") or uuid.uuid4().hex[:12]
        request.trace_id = request_id
        token = set_request_id(request_id)
        started = time.monotonic()
        logger.info("http.request.start method=%s path=%s query=%s remote=%s", request.method, request.path, request.META.get("QUERY_STRING", ""), request.META.get("REMOTE_ADDR", ""))
        try:
            response = self.get_response(request)
            response["X-TraceLens-Request-ID"] = request_id
            logger.info("http.request.finish method=%s path=%s status=%s elapsed_ms=%d", request.method, request.path, getattr(response, "status_code", "-"), int((time.monotonic() - started) * 1000))
            return response
        except Exception:
            logger.exception("http.request.failed method=%s path=%s elapsed_ms=%d", request.method, request.path, int((time.monotonic() - started) * 1000))
            raise
        finally:
            reset_request_id(token)
