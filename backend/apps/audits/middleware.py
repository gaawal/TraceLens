"""功能级操作审计中间件。

**只记用户发起的**：不是用户点出来的（SSE / 秒级轮询 / 前端自动回执 / 审计自身）在
``features.SKIP_FEATURES`` 里排除；前端还会给自动刷新打 ``X-TraceLens-Action: auto``，
这里也一并跳过（同一个接口既可能是"用户点了查询"也可能是"页面自己在刷"，
只有前端知道区别）。

三条硬约束：
1. **绝不影响业务**：审计写库失败只记日志，异常一律吞掉。
2. **不记响应体**：只留状态码、耗时和失败原因，避免把日志数据复制进审计表。
3. **不记敏感字段**：密码/密钥在 :mod:`apps.audits.operation_audit` 里统一打码。
"""

from __future__ import annotations

import logging
import time

from apps.audits import features as feature_map
from apps.audits import operation_audit
from apps.audits.models import OperationAudit, OperationOutcome
from apps.audits.services import request_client_ip, request_operator_username

logger = logging.getLogger(__name__)


class OperationAuditMiddleware:
    """在响应返回给用户**之后**落一条操作审计。"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started = time.monotonic()
        # 请求阶段：只做"这条请求有没有可能被记录"的粗筛（URL 还没解析，拿不到 url_name）。
        # 顺手把小的 JSON body 抓下来，等会儿写操作记录用。
        if self._maybe_audited(request):
            operation_audit.capture_request_body(request)
        try:
            response = self.get_response(request)
        except Exception:
            feature = self._candidate(request)
            if feature is not None:
                self._record_failure(request, feature, started)
            raise
        feature = self._candidate(request)
        if feature is not None:
            self._record(request, response, feature, started)
        return response

    # -- 判定 -------------------------------------------------------------
    @staticmethod
    def _maybe_audited(request) -> bool:
        """粗筛：/api/ 下的、非自动刷新、非 OPTIONS/HEAD 的请求才值得继续看。"""
        path = str(getattr(request, "path", "") or "")
        if not path.startswith(operation_audit.AUDITED_PATH_PREFIX):
            return False
        if operation_audit.is_auto_request(request):
            return False
        return str(getattr(request, "method", "GET")).upper() not in feature_map.SKIP_METHODS

    def _candidate(self, request):
        """响应阶段判定：要记就返回 FeatureAudit，否则 None（此时 resolver_match 一定有了）。"""
        if not self._maybe_audited(request):
            return None
        match = getattr(request, "resolver_match", None)
        url_name = str(getattr(match, "url_name", "") or "")
        if not url_name:
            return None
        return feature_map.resolve_feature(url_name, getattr(request, "method", "GET"))

    # -- 落库 -------------------------------------------------------------
    def _record(self, request, response, feature, started: float) -> None:
        try:
            context = operation_audit.build_operation_context(
                request, feature, environment_lookup=self._environment_lookup
            )
            OperationAudit.objects.create(
                operation_id=str(getattr(request, "trace_id", "") or "")[:128],
                operator_username=request_operator_username(request)[:150],
                client_ip=request_client_ip(request)[:64],
                session_id=session_id(request),
                trigger=context.trigger,
                feature_group=feature.group,
                feature_name=feature.name,
                summary=context.summary,
                environment_id=context.environment_id,
                environment_name=context.environment_name[:128],
                target_kind=context.target_kind[:32],
                target_name=context.target_name[:255],
                log_search_audit=self._log_search_audit(request, response),
                http_method=str(getattr(request, "method", ""))[:8],
                path=path_snapshot(request),
                status_code=int(getattr(response, "status_code", 0) or 0),
                outcome=operation_audit.outcome_for(response),
                error_message=operation_audit.extract_error_message(response),
                duration_ms=int((time.monotonic() - started) * 1000),
                request_payload=context.payload,
            )
        except Exception:  # pragma: no cover - 审计绝不能影响业务
            logger.exception("operation.audit.record_failed path=%s", getattr(request, "path", ""))

    def _record_failure(self, request, feature, started: float) -> None:
        """视图抛异常（500）时也要留痕：否则"用户点了但服务崩了"反而查不到。"""
        try:
            context = operation_audit.build_operation_context(
                request, feature, environment_lookup=self._environment_lookup
            )
            OperationAudit.objects.create(
                operation_id=str(getattr(request, "trace_id", "") or "")[:128],
                operator_username=request_operator_username(request)[:150],
                client_ip=request_client_ip(request)[:64],
                session_id=session_id(request),
                trigger=context.trigger,
                feature_group=feature.group,
                feature_name=feature.name,
                summary=context.summary,
                environment_id=context.environment_id,
                environment_name=context.environment_name[:128],
                target_kind=context.target_kind[:32],
                target_name=context.target_name[:255],
                http_method=str(getattr(request, "method", ""))[:8],
                path=path_snapshot(request),
                status_code=500,
                outcome=OperationOutcome.FAILED,
                error_message="服务端异常中断（详见服务端日志）。",
                duration_ms=int((time.monotonic() - started) * 1000),
                request_payload=context.payload,
            )
        except Exception:  # pragma: no cover - 防御性
            logger.exception("operation.audit.failure_record_failed path=%s", getattr(request, "path", ""))

    # -- 小工具 -----------------------------------------------------------
    @staticmethod
    def _environment_lookup(environment_id: int) -> tuple[int, str] | None:
        from apps.environments.models import Environment

        row = Environment.objects.filter(pk=environment_id).values_list("id", "name").first()
        return (int(row[0]), str(row[1])) if row else None

    @staticmethod
    def _log_search_audit(request, response):
        """日志检索把两条审计串起来（功能审计 ↔ 检索详情），其它接口没有这条关联。

        用请求头里的 operation_id：日志检索是流式响应，此时还没有 ``response.data``。
        """
        if str(getattr(getattr(request, "resolver_match", None), "url_name", "")) != "environment-log-stream":
            return None
        operation_id = str(request.headers.get("X-TraceLens-Operation-ID") or "").strip()
        if not operation_id:
            return None
        from apps.audits.models import LogSearchAudit

        return LogSearchAudit.objects.filter(operation_id=operation_id).order_by("-id").first()


def session_id(request) -> str:
    """浏览器会话（前端 ``X-Session-Id``）：没有登录体系时用它区分"同一个人"。"""
    return str(request.headers.get("X-Session-Id") or "").strip()[:64]


def path_snapshot(request) -> str:
    """只留**路由模板**，不留真实参数值（界面上也不显示 URL，这里仅后端留档）。"""
    match = getattr(request, "resolver_match", None)
    route = str(getattr(match, "route", "") or "")
    if route:
        return f"/{route.lstrip('/')}"[:512]
    return str(getattr(request, "path", ""))[:512]
