from __future__ import annotations

from django.http import HttpRequest, HttpResponseNotAllowed, StreamingHttpResponse

from apps.environments.services.deployment_events import DeploymentEventBus


async def deployment_event_stream(request: HttpRequest):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    response = StreamingHttpResponse(
        DeploymentEventBus.event_stream(),
        content_type="text/event-stream; charset=utf-8",
    )
    response["Cache-Control"] = "no-cache, no-transform"
    response["X-Accel-Buffering"] = "no"
    response["Connection"] = "keep-alive"
    return response
