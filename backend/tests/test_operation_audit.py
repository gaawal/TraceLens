"""功能级操作审计：功能映射、脱敏、操作记录、中间件判定。

这些用例全部是**纯函数级**的（不碰数据库）：本仓库没装 pytest-django，带 DB 的用例会报
settings 未配置。真实链路（请求 → 落库 → 列表接口）用浏览器脚本
`/tmp/tl-verify/audit-operations.cjs` 验证。
"""

from __future__ import annotations

from types import SimpleNamespace

from apps.audits import features as feature_map
from apps.audits import operation_audit
from apps.audits.middleware import OperationAuditMiddleware


# --- 功能映射 ---------------------------------------------------------------

def test_every_api_endpoint_is_either_audited_or_explicitly_skipped():
    """**每个接口都要有结论**：要么能映射到功能名，要么在 SKIP 里写明为什么不记。

    这条是"用户希望能审计到每个接口"的兜底：以后新增接口忘了登记，会在这里被拦下来。
    """
    from config.urls import router

    unknown: list[str] = []
    for url in router.urls:
        if "format" in str(url.pattern):
            continue
        name = url.name or ""
        if not name or name.startswith("operation-audit"):
            continue
        actions = getattr(url.callback, "actions", None) or {}
        for method in actions or {"GET": None}:
            verb = method.upper()
            if verb in feature_map.SKIP_METHODS:
                continue
            if feature_map.resolve_feature(name, verb) is None and name not in feature_map.SKIP_FEATURES:
                unknown.append(f"{name}:{verb}")
    assert unknown == [], f"这些接口既没有功能名也不在 SKIP 里：{sorted(set(unknown))}"


def test_user_facing_examples_get_readable_feature_names():
    """用户点名的几个功能名必须一眼看懂（界面不显示 URL）。"""
    cases = {
        ("environment-deployments", "POST"): "部署环境",
        ("environment-process-control", "POST"): "启动/停止环境进程",
        ("environment-runtime-status", "GET"): "查询环境运行状态",
        ("environment-list", "GET"): "查询环境列表",
        ("environment-detail", "GET"): "查询环境详情",
        ("tool-assistant-chat-stream", "POST"): "AI 对话",
        ("environment-log-stream", "POST"): "日志定位（检索）",
        ("data-extraction-list", "GET"): "查询提取记录",
    }
    for (url_name, method), expected in cases.items():
        feature = feature_map.resolve_feature(url_name, method)
        assert feature is not None, f"{url_name} 应该被审计到"
        assert feature.name == expected, f"{url_name}:{method} → {feature.name}，期望 {expected}"
        assert feature.group, "功能必须归属一个分组"


def test_system_traffic_and_polling_are_not_audited():
    """系统自己跑的（流式/轮询/审计自身/自动回执）都不允许落库。"""
    for url_name in (
        "log-audit-list",
        "operation-audit-list",
        "environment-log-progress",
        "atlog-analysis-ai-diagnose-status",
        "atlog-analysis-logs-status",
        "deployment-events",
        "watch-events",
        "task-events",
        "tool-assistant-ui-receipt",
        "log-audit-client-result",
        "health-check",
    ):
        assert url_name in feature_map.SKIP_FEATURES, f"{url_name} 应该在 SKIP 名单里"
        assert feature_map.resolve_feature(url_name, "POST") is None
        assert feature_map.resolve_feature(url_name, "GET") is None


def test_unregistered_custom_action_still_gets_a_chinese_name():
    """没登记过的自定义动作也要有像样的中文名，不能掉成空或英文路径。"""
    feature = feature_map.resolve_feature("environment-some-new-action", "POST")
    assert feature is not None
    assert feature.group == "环境资源"
    assert feature.name.startswith("环境")


# --- 脱敏与操作记录 ---------------------------------------------------------

def test_sanitize_payload_masks_secrets_and_truncates():
    cleaned = operation_audit.sanitize_payload(
        {
            "password": "tracelens",
            "auth_type": "password",
            "nested": {"api_key": "sk-123", "keep": "ok"},
            "long_text": "x" * 500,
            "items": list(range(30)),
        }
    )
    assert cleaned["password"] == "***"
    assert cleaned["nested"]["api_key"] == "***"
    assert cleaned["nested"]["keep"] == "ok"
    assert cleaned["auth_type"] == "password"  # 只有敏感**字段名**才打码
    assert cleaned["long_text"].endswith("（共 500 字）")
    assert len(cleaned["items"]) == 21 and "另有 10 项" in cleaned["items"][-1]


def _request(*, path="/api/environments/2/process-control/", method="POST", url_name="environment-process-control", kwargs=None, body=None, action_header="", query=""):
    match = SimpleNamespace(url_name=url_name, kwargs=kwargs or {"pk": "2"}, route="api/environments/{pk}/process-control/")
    meta = {"CONTENT_LENGTH": "0"}
    if action_header:
        meta["HTTP_X_TRACELENS_ACTION"] = action_header
    return SimpleNamespace(
        path=path,
        method=method,
        META=meta,
        GET=_QueryDict(query),
        resolver_match=match,
        content_type="application/json",
        headers={},
        _audit_json_body=body,
    )


class _QueryDict:
    def __init__(self, query: str = ""):
        self._items = [item.split("=", 1) for item in query.split("&") if "=" in item]

    def keys(self):
        return [key for key, _ in self._items]

    def getlist(self, key):
        return [value for item_key, value in self._items if item_key == key]


def test_process_control_summary_says_start_or_stop_with_environment_name():
    """操作记录要能看出"启动还是停止、对哪套环境"。"""
    request = _request(body={"action": "stop"})
    context = operation_audit.build_operation_context(
        request,
        feature_map.resolve_feature("environment-process-control", "POST"),
        environment_lookup=lambda pk: (pk, "SIM-EUV-01"),
    )
    assert context.environment_name == "SIM-EUV-01"
    assert context.target_name == "SIM-EUV-01"
    assert context.summary.startswith("停止环境进程")
    assert "SIM-EUV-01" in context.summary


def test_deploy_summary_and_payload_are_sanitized():
    request = _request(
        path="/api/environments/2/deployments/",
        url_name="environment-deployments",
        body={"password": "secret", "version": "SPM-V2026.09.23"},
    )
    context = operation_audit.build_operation_context(
        request,
        feature_map.resolve_feature("environment-deployments", "POST"),
        environment_lookup=lambda pk: (pk, "SIM-EUV-01"),
    )
    assert context.summary.startswith("部署环境")
    assert context.payload["password"] == "***"
    assert context.payload["version"] == "SPM-V2026.09.23"


def test_ai_trigger_is_recorded_but_marked():
    """AI 代操作也要留痕，但要能区分出来（发起方 = AI 代操作）。"""
    request = _request(url_name="tool-assistant-chat-stream", action_header="ai")
    context = operation_audit.build_operation_context(
        request, feature_map.resolve_feature("tool-assistant-chat-stream", "POST")
    )
    assert context.trigger == "ai"
    plain = _request(url_name="tool-assistant-chat-stream")
    assert operation_audit.build_operation_context(
        plain, feature_map.resolve_feature("tool-assistant-chat-stream", "POST")
    ).trigger == "user"


def test_error_message_is_taken_from_response_body():
    response = SimpleNamespace(status_code=400, data={"message": "action 只能是 stop 或 start。"})
    assert operation_audit.extract_error_message(response) == "action 只能是 stop 或 start。"
    assert operation_audit.outcome_for(response) == "failed"
    ok = SimpleNamespace(status_code=201, data={"id": 1})
    assert operation_audit.extract_error_message(ok) == ""
    assert operation_audit.outcome_for(ok) == "success"


# --- 中间件判定 -------------------------------------------------------------

def _middleware():
    return OperationAuditMiddleware(lambda request: SimpleNamespace(status_code=200))


def test_middleware_skips_auto_refresh_and_non_api_paths():
    middleware = _middleware()
    assert middleware._candidate(_request(action_header="auto")) is None, "自动刷新不该记"
    assert middleware._candidate(_request(path="/admin/login/", url_name="admin:login")) is None
    assert middleware._candidate(_request(path="/api/health/", url_name="health-check")) is None
    assert middleware._candidate(_request(path="/api/log-audits/", method="GET", url_name="log-audit-list")) is None
    feature = middleware._candidate(_request())
    assert feature is not None and feature.name == "启动/停止环境进程"


def test_middleware_only_sniffs_bodies_for_auditable_requests():
    """粗筛（请求阶段，URL 还没解析）：非 /api/、自动刷新、OPTIONS/HEAD 不抓请求体。"""
    middleware = _middleware()
    assert middleware._maybe_audited(_request()) is True
    assert middleware._maybe_audited(_request(action_header="auto")) is False
    assert middleware._maybe_audited(_request(path="/admin/login/", url_name="admin:login")) is False
    assert middleware._maybe_audited(_request(method="OPTIONS")) is False
    # SKIP 名单要等响应阶段拿到 url_name 才判（那时才决定记不记、也就不需要 body 了）。
    assert middleware._candidate(_request(path="/api/health/", method="GET", url_name="health-check")) is None
