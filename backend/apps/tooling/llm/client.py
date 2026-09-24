from __future__ import annotations

import asyncio
import json
import logging
import random
import threading
import time
import urllib.parse
import contextvars
from typing import Any, Dict, List, Optional

import httpx
from django.conf import settings
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, OpenAI

logger = logging.getLogger("tracelens.tooling.llm")

# AI 会话链路标识：frontend -> backend -> LLM Gateway
_llm_session_id: contextvars.ContextVar[str] = contextvars.ContextVar("tracelens_llm_session_id", default="")

def set_llm_session_id(session_id: str | None) -> None:
    _llm_session_id.set(str(session_id or "").strip())


def get_llm_session_id() -> str:
    return _llm_session_id.get()


# Per-run model / reasoning-effort selection made in the composer's model picker.
# These travel as contextvars rather than as extra parameters because the agent graph,
# the ATLog skill runtime and the router all issue their own LLM calls; threading a
# "which brain" argument through every layer would touch every orchestration node.
# A worker thread sets them once at the start of a run and every call in that run sees
# the same choice. Empty means "use the configured default".
_llm_model_override: contextvars.ContextVar[str] = contextvars.ContextVar("tracelens_llm_model", default="")
_llm_effort_override: contextvars.ContextVar[str] = contextvars.ContextVar("tracelens_llm_effort", default="")

# Values accepted by the gateway's ``reasoning_effort`` field. Validated here because an
# unknown value comes back as an opaque HTTP 422 rather than a useful message.
REASONING_EFFORTS: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "ultra", "max")


def set_llm_overrides(model: str | None = None, effort: str | None = None) -> None:
    """Pin the model and reasoning effort for every LLM call on this run."""
    _llm_model_override.set(str(model or "").strip()[:120])
    _llm_effort_override.set(str(effort or "").strip().casefold()[:32])


def get_llm_model_override() -> str:
    return _llm_model_override.get()


def get_llm_effort_override() -> str:
    effort = _llm_effort_override.get()
    return effort if effort in REASONING_EFFORTS else ""


class LLMClientError(RuntimeError):
    """Readable transport error raised by the OpenAI-compatible client."""


PLACEHOLDER_VALUES = {"", "xxxx", "xxx", "replace-me", "replace_with_api_key", "changeme"}


RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}

_SENSITIVE_KEYS = ("api_key", "apikey", "authorization", "token", "secret", "password", "cookie", "credential")

def _sanitize_log_value(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "<max-depth>"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:80]:
            name = str(key)
            result[name] = "***" if any(part in name.casefold() for part in _SENSITIVE_KEYS) else _sanitize_log_value(item, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize_log_value(item, depth + 1) for item in list(value)[:30]]
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="replace")
        except Exception:
            return f"<bytes:{len(value)}>"
    if isinstance(value, str):
        return value[:5000] + (f"...<+{len(value)-5000} chars>" if len(value) > 5000 else "")
    return value

def _json_log(value: Any, limit: int = 16000) -> str:
    try:
        text = json.dumps(_sanitize_log_value(value), ensure_ascii=False, default=str, sort_keys=True)
    except Exception:
        text = str(value)
    return text[:limit] + (f"...<+{len(text)-limit} chars>" if len(text) > limit else "")

def _message_debug(messages: List[Dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, item in enumerate(messages[-8:]):
        content = str(item.get("content") or "")
        result.append({
            "index": max(0, len(messages) - 8) + index,
            "role": str(item.get("role") or ""),
            "content_chars": len(content),
            "content_preview": content[:1800],
            "tool_call_id": str(item.get("tool_call_id") or "")[:200],
        })
    return result

def _tool_debug(tools: Optional[List[Dict[str, Any]]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in list(tools or [])[:40]:
        fn = item.get("function") if isinstance(item, dict) and isinstance(item.get("function"), dict) else {}
        result.append({
            "name": str(fn.get("name") or ""),
            "parameters": fn.get("parameters") or {},
        })
    return result

def _request_debug(exc: BaseException) -> dict[str, Any]:
    request = getattr(exc, "request", None)
    response = getattr(exc, "response", None)
    info: dict[str, Any] = {}
    if request is not None:
        info["method"] = str(getattr(request, "method", "") or "")
        info["url"] = str(getattr(request, "url", "") or "")
        try:
            content = getattr(request, "content", b"")
            if content:
                decoded = content.decode("utf-8", errors="replace") if isinstance(content, (bytes, bytearray)) else str(content)
                try:
                    info["body"] = _sanitize_log_value(json.loads(decoded))
                except Exception:
                    info["body"] = decoded[:16000]
        except Exception:
            pass
    if response is not None:
        try:
            info["status"] = int(getattr(response, "status_code", 0) or 0)
        except Exception:
            pass
        try:
            info["response_headers"] = {
                str(k): ("***" if any(part in str(k).casefold() for part in _SENSITIVE_KEYS) else str(v))
                for k, v in dict(getattr(response, "headers", {}) or {}).items()
            }
        except Exception:
            pass
    return info



def _retry_after_seconds(exc: BaseException) -> float | None:
    if not isinstance(exc, APIStatusError):
        return None
    try:
        raw = str(exc.response.headers.get("retry-after") or "").strip()
        if not raw:
            return None
        value = float(raw)
        return max(0.0, value)
    except Exception:  # noqa: BLE001
        return None


def _is_retryable_error(exc: BaseException) -> bool:
    if isinstance(exc, APIStatusError):
        return int(exc.status_code or 0) in RETRYABLE_STATUS_CODES
    return isinstance(exc, (APITimeoutError, APIConnectionError, httpx.TransportError))


class _AdaptiveRetryGate:
    """Process-local adaptive throttle shared by all calls on one LLM client.

    Private gateways often return a burst of 429/5xx responses when overloaded.
    The OpenAI SDK's default retry window is intentionally short, which can make
    multiple TracePilot graph nodes retry almost immediately and amplify the
    overload.  This gate owns the retry cadence instead: every retryable failure
    pushes the next allowed request farther out (exponential backoff + jitter),
    and all concurrent callers observe the same cooldown.
    """

    def __init__(self, *, base_delay: float, max_delay: float, jitter_ratio: float) -> None:
        self.base_delay = max(0.25, float(base_delay))
        self.max_delay = max(self.base_delay, float(max_delay))
        self.jitter_ratio = min(max(float(jitter_ratio), 0.0), 0.5)
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._next_allowed_at = 0.0

    def wait_seconds(self) -> float:
        with self._lock:
            return max(0.0, self._next_allowed_at - time.monotonic())

    def note_failure(self, exc: BaseException) -> tuple[int, float]:
        retry_after = _retry_after_seconds(exc)
        with self._lock:
            self._consecutive_failures += 1
            failure_count = self._consecutive_failures
            exponential = min(self.max_delay, self.base_delay * (2 ** max(0, failure_count - 1)))
            if self.jitter_ratio:
                exponential *= 1.0 + random.uniform(0.0, self.jitter_ratio)
            delay = max(float(retry_after or 0.0), min(self.max_delay, exponential))
            # Never shorten a cooldown already established by another concurrent call.
            self._next_allowed_at = max(self._next_allowed_at, time.monotonic() + delay)
            return failure_count, max(0.0, self._next_allowed_at - time.monotonic())

    def note_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._next_allowed_at = 0.0



def _setting(name: str, default: Any = "") -> Any:
    return getattr(settings, name, default)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _last_user_message(messages: List[Dict[str, Any]]) -> str:
    for item in reversed(messages):
        if str(item.get("role") or "") == "user":
            return str(item.get("content") or "")[:30_000]
    return ""


def _response_summary(response: Any) -> str:
    try:
        choice = response.choices[0]
        message = choice.message
        content = str(getattr(message, "content", "") or "")
        tool_calls = getattr(message, "tool_calls", None) or []
        if tool_calls:
            calls = []
            for call in tool_calls:
                function = getattr(call, "function", None)
                calls.append({
                    "name": str(getattr(function, "name", "") or ""),
                    "arguments": str(getattr(function, "arguments", "") or "")[:10_000],
                })
            return f"content={content[:30_000]} tool_calls={calls}"
        return content[:30_000]
    except Exception:  # noqa: BLE001
        return str(response)[:30_000]


def _normalize_openai_base_url(value: Any) -> str:
    """Normalize OpenAI-compatible gateway URLs to an SDK base URL.

    TraceLens historically accepted both a base URL (``.../v1``) and a full
    chat-completions endpoint (``.../v1/chat/completions``).  The OpenAI SDK
    expects the former and appends ``/chat/completions`` itself.  Passing the
    full endpoint therefore produces paths such as
    ``.../v1/chat/completions/chat/completions`` and an nginx 404.

    Keep custom prefixes (for example ``/openai/v1``), strip only the known
    endpoint suffix, and add ``/v1`` only when the configured URL points at the
    host root.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urllib.parse.urlsplit(raw)
    except Exception:  # noqa: BLE001
        return raw.rstrip("/")
    if not parsed.scheme or not parsed.netloc:
        return raw.rstrip("/")

    path = (parsed.path or "").rstrip("/")
    lower = path.lower()
    suffix = "/chat/completions"
    if lower.endswith(suffix):
        path = path[: -len(suffix)].rstrip("/")
    if not path:
        path = "/v1"

    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")


def _validated_config() -> tuple[str, str, str, float, str, bool]:
    base_url = _normalize_openai_base_url(_setting("TRACELENS_AI_BASE_URL", ""))
    api_key = str(_setting("TRACELENS_AI_API_KEY", "") or "").strip()
    model = str(_setting("TRACELENS_AI_MODEL", "GLM-4.7-XS") or "").strip()
    timeout = float(_setting("TRACELENS_AI_TIMEOUT", 600))
    proxy = str(_setting("TRACELENS_AI_PROXY", "") or "").strip()
    use_env_proxy = _as_bool(_setting("TRACELENS_AI_USE_ENV_PROXY", False), False)

    missing: list[str] = []
    if base_url.lower() in PLACEHOLDER_VALUES or "xxxx" in base_url.lower():
        missing.append("TRACELENS_AI_BASE_URL")
    if api_key.lower() in PLACEHOLDER_VALUES:
        missing.append("TRACELENS_AI_API_KEY")
    if not model:
        missing.append("TRACELENS_AI_MODEL")
    if missing:
        raise LLMClientError(
            "AI 诊断未完成配置：请在现有运行环境中填写 " + ", ".join(missing) + "。"
        )
    if timeout <= 0:
        timeout = 600.0
    return base_url, api_key, model, timeout, proxy, use_env_proxy


def _sync_http_client(proxy: str, use_env_proxy: bool, timeout: float) -> httpx.Client:
    kwargs: dict[str, Any] = {
        "trust_env": use_env_proxy,
        "timeout": timeout,
    }
    if proxy:
        kwargs["proxy"] = proxy
    return httpx.Client(**kwargs)


def _async_http_client(proxy: str, use_env_proxy: bool, timeout: float) -> httpx.AsyncClient:
    kwargs: dict[str, Any] = {
        "trust_env": use_env_proxy,
        "timeout": timeout,
    }
    if proxy:
        kwargs["proxy"] = proxy
    return httpx.AsyncClient(**kwargs)


class LLMClient:
    """Shared OpenAI-compatible transport used by TraceLens AI capabilities.

    Keep the request shape deliberately minimal and equivalent to the known-good
    OpenCode/Python usage: ``model`` + ``messages``.  LangGraph owns orchestration;
    this class owns only LLM transport.
    """

    def __init__(self) -> None:
        raw_base_url = str(_setting("TRACELENS_AI_BASE_URL", "") or "").strip()
        base_url, api_key, model, timeout, proxy, use_env_proxy = _validated_config()
        self.base_url = base_url
        self.chat_completions_endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self.model = model
        self.timeout = timeout
        logger.info(
            "tooling.llm.client_config raw_base_url=%s normalized_base_url=%s endpoint=%s model=%s timeout=%s proxy=%s env_proxy=%s api_key_configured=%s",
            raw_base_url, base_url, self.chat_completions_endpoint, model, timeout, bool(proxy), use_env_proxy, bool(api_key),
        )
        self.proxy = proxy
        self.use_env_proxy = use_env_proxy
        # Gateway capability flags learned from a 400 (see _degrade_tool_choice).
        # Cached on the client so a rejected request shape is never retried blindly.
        self._thinking_disabled = False
        self._forced_tool_choice_unsupported = False
        self.retry_max_attempts = max(1, int(_setting("TRACELENS_AI_RETRY_MAX_ATTEMPTS", 7)))
        self.retry_base_delay = max(0.25, float(_setting("TRACELENS_AI_RETRY_BASE_DELAY", 3.0)))
        self.retry_max_delay = max(self.retry_base_delay, float(_setting("TRACELENS_AI_RETRY_MAX_DELAY", 90.0)))
        self.retry_jitter_ratio = min(max(float(_setting("TRACELENS_AI_RETRY_JITTER", 0.2)), 0.0), 0.5)
        self._retry_gate = _AdaptiveRetryGate(
            base_delay=self.retry_base_delay,
            max_delay=self.retry_max_delay,
            jitter_ratio=self.retry_jitter_ratio,
        )

        self._sync_http = _sync_http_client(proxy, use_env_proxy, timeout)
        self._async_http = _async_http_client(proxy, use_env_proxy, timeout)

        # This intentionally mirrors the user's known-good code path.
        self.client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            max_retries=0,  # TraceLens owns adaptive retry/backoff; avoid SDK's short retry loop.
            http_client=self._async_http,
        )
        self.sync_client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            max_retries=0,  # TraceLens owns adaptive retry/backoff; avoid SDK's short retry loop.
            http_client=self._sync_http,
        )

    def _wait_for_slot_sync(self) -> None:
        delay = self._retry_gate.wait_seconds()
        if delay > 0:
            logger.warning("tooling.llm.throttle_wait delay=%.2fs", delay)
            time.sleep(delay)

    async def _wait_for_slot_async(self) -> None:
        delay = self._retry_gate.wait_seconds()
        if delay > 0:
            logger.warning("tooling.llm.throttle_wait_async delay=%.2fs", delay)
            await asyncio.sleep(delay)

    def _note_retryable_failure(self, exc: BaseException, *, mode: str, attempt: int) -> float:
        failure_count, delay = self._retry_gate.note_failure(exc)
        status = int(exc.status_code or 0) if isinstance(exc, APIStatusError) else 0
        logger.warning(
            "tooling.llm.backoff mode=%s attempt=%s/%s consecutive=%s status=%s next_delay=%.2fs error=%s",
            mode, attempt, self.retry_max_attempts, failure_count, status or "transport", delay, str(exc)[:500],
        )
        return delay

    def note_stream_failure(self, exc: BaseException) -> float:
        """Register a failure that happened while consuming an already-open stream.

        The next call (stream retry or sync fallback) will honor the shared cooldown.
        """
        if not _is_retryable_error(exc) and not isinstance(exc, (httpx.RemoteProtocolError, httpx.ReadError)):
            return 0.0
        failure_count, delay = self._retry_gate.note_failure(exc)
        logger.warning(
            "tooling.llm.backoff mode=stream-consume consecutive=%s next_delay=%.2fs error=%s",
            failure_count, delay, str(exc)[:500],
        )
        return delay

    @staticmethod
    def _set_thinking_disabled(kwargs: Dict[str, Any]) -> None:
        """Send ``thinking: {"type": "disabled"}`` without breaking the SDK signature.

        The OpenAI Python SDK validates keyword arguments client-side, so
        provider-specific body fields must travel in ``extra_body``.
        """
        extra = kwargs.get("extra_body")
        if not isinstance(extra, dict):
            extra = {}
            kwargs["extra_body"] = extra
        extra["thinking"] = {"type": "disabled"}

    def _request_kwargs(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
    ) -> Dict[str, Any]:
        # The per-run picker (if any) wins over the configured default for this call only.
        kwargs: Dict[str, Any] = {
            "model": get_llm_model_override() or model,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = "auto" if self._forced_tool_choice_unsupported else tool_choice
        effort = get_llm_effort_override()
        if effort:
            kwargs["reasoning_effort"] = effort
        if self._thinking_disabled:
            self._set_thinking_disabled(kwargs)
        session_id = getattr(self, "session_id", "") or get_llm_session_id()
        if session_id:
            # OpenAI-compatible SDK 的正确方式是 extra_headers。
            # 它会合并到最终 HTTP 请求 Header，而不是请求 JSON body。
            kwargs["extra_headers"] = {"X-Session-Id": session_id}
        return kwargs

    def _effective_model(self) -> str:
        """The model this client will actually request (per-run picker wins)."""
        return get_llm_model_override() or self.model

    @staticmethod
    def _rejects_tool_choice(exc: BaseException) -> bool:
        """True when a gateway 400s specifically because of the tool_choice mode.

        Reasoning/"thinking" models commonly reject ``tool_choice: "required"`` and
        forced-function choices while still supporting ``"auto"``.
        """
        if not isinstance(exc, APIStatusError) or int(exc.status_code or 0) != 400:
            return False
        text = str(getattr(exc, "message", "") or "")
        try:
            text += " " + str(exc.response.text or "")
        except Exception:  # noqa: BLE001
            pass
        text = text.casefold()
        return "tool_choice" in text or "tool choice" in text

    def _degrade_tool_choice(self, kwargs: Dict[str, Any], exc: BaseException) -> bool:
        """Adapt the request instead of failing the whole turn.

        Order matters: leaving thinking mode keeps the forced route (the strongest
        signal for "which tools does this turn need"); only if the gateway still
        objects do we fall back to ``tool_choice="auto"``. The chosen degradation is
        remembered on the client so later calls never pay the failed round-trip again.
        """
        if not self._rejects_tool_choice(exc) or "tool_choice" not in kwargs:
            return False
        if not self._thinking_disabled:
            self._thinking_disabled = True
            self._set_thinking_disabled(kwargs)
            logger.warning(
                "tooling.llm.tool_choice_compat action=disable_thinking model=%s reason=%s",
                self.model, str(exc)[:300],
            )
            return True
        if not self._forced_tool_choice_unsupported:
            self._forced_tool_choice_unsupported = True
            kwargs["tool_choice"] = "auto"
            logger.warning(
                "tooling.llm.tool_choice_compat action=tool_choice_auto model=%s tool_choice=%s reason=%s",
                self.model, str(kwargs.get("tool_choice"))[:120], str(exc)[:300],
            )
            return True
        return False

    def _raise_readable(self, exc: Exception) -> None:
        logger.error(
            "tooling.llm.failure_details model=%s base_url=%s endpoint=%s request=%s",
            self.model, self.base_url, self.chat_completions_endpoint, _json_log(_request_debug(exc), 20000),
        )
        if isinstance(exc, APIStatusError):
            body = ""
            try:
                body = str(exc.response.text or "").strip()
            except Exception:  # noqa: BLE001
                body = ""
            suffix = f"，响应={body[:800]}" if body else ""
            endpoint_hint = ""
            if int(exc.status_code or 0) == 404:
                endpoint_hint = (
                    f"；实际请求端点={self.chat_completions_endpoint}；model={self.model}。"
                    "该错误发生在 LLM 网关请求阶段，不是 ATLog 用例 URL 请求。"
                    "请结合 tooling.llm.request_payload / tooling.llm.failure_details 日志确认网关路径、模型名和请求体。"
                )
            raise LLMClientError(
                f"LLM 网关返回 HTTP {exc.status_code}：{exc.message}{endpoint_hint}{suffix}"
            ) from exc
        if isinstance(exc, APITimeoutError):
            raise LLMClientError(
                f"LLM 请求超时（当前 timeout={self.timeout:g}s）。"
            ) from exc
        if isinstance(exc, APIConnectionError):
            proxy_text = self.proxy if self.proxy else "直连"
            raise LLMClientError(
                f"无法连接 LLM 网关 {self.base_url}（代理={proxy_text}，读取系统代理={self.use_env_proxy}）：{exc}"
            ) from exc
        raise LLMClientError(f"LLM 调用失败：{exc}") from exc

    def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
    ) -> Any:
        """Return a streaming completion with adaptive gateway backoff.

        SDK retries are disabled. 429/5xx/transport failures are handled here so
        every LangGraph node shares one progressively longer cooldown instead of
        retrying the private gateway independently.
        """
        kwargs = self._request_kwargs(self.model, messages, tools, tool_choice)
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}
        logger.info("tooling.llm.prompt mode=stream user=%s", _last_user_message(messages))
        logger.info(
            "tooling.llm.request mode=stream model=%s messages=%s tools=%s base_url=%s endpoint=%s proxy=%s env_proxy=%s reasoning_effort=%s",
            self._effective_model(),
            len(messages),
            len(tools or []),
            self.base_url,
            self.chat_completions_endpoint,
            bool(self.proxy),
            self.use_env_proxy,
            get_llm_effort_override() or "-",
        )
        logger.info(
            "tooling.llm.request_payload mode=stream model=%s endpoint=%s tool_choice=%s messages=%s tool_specs=%s",
            self._effective_model(), self.chat_completions_endpoint, str(tool_choice),
            _json_log(_message_debug(messages), 18000),
            _json_log(_tool_debug(tools), 18000),
        )

        attempt = 1
        while attempt <= self.retry_max_attempts:
            self._wait_for_slot_sync()
            try:
                logger.info("tooling.llm.session_header session_id=%s", get_llm_session_id())
                response = self.sync_client.chat.completions.create(**kwargs)
                self._retry_gate.note_success()
                logger.info(
                    "tooling.llm.stream_opened model=%s response_type=%s attempt=%s",
                    self._effective_model(),
                    type(response).__name__,
                    attempt,
                )
                return response
            except APIStatusError as exc:
                # Some OpenAI-compatible private gateways do not implement
                # stream_options. Retry the exact same request without that optional
                # field; this compatibility retry is not treated as overload.
                if exc.status_code in {400, 404, 422} and "stream_options" in kwargs:
                    logger.info(
                        "tooling.llm.stream_usage_unsupported status=%s retry=minimal",
                        exc.status_code,
                    )
                    kwargs.pop("stream_options", None)
                    continue
                # Reasoning gateways reject forced tool_choice; adapt instead of failing.
                if self._degrade_tool_choice(kwargs, exc):
                    continue
                if _is_retryable_error(exc):
                    self._note_retryable_failure(exc, mode="stream", attempt=attempt)
                    if attempt < self.retry_max_attempts:
                        attempt += 1
                        continue
                logger.exception("tooling.llm.request_failed mode=stream model=%s", self.model)
                self._raise_readable(exc)
            except Exception as exc:  # noqa: BLE001
                if _is_retryable_error(exc):
                    self._note_retryable_failure(exc, mode="stream", attempt=attempt)
                    if attempt < self.retry_max_attempts:
                        attempt += 1
                        continue
                logger.exception("tooling.llm.request_failed mode=stream model=%s", self.model)
                self._raise_readable(exc)

        raise LLMClientError("LLM 网关重试次数已耗尽。")

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
    ) -> Any:
        kwargs = self._request_kwargs(self.model, messages, tools, tool_choice)
        logger.info(
            "tooling.llm.request mode=sync model=%s messages=%s tools=%s base_url=%s endpoint=%s proxy=%s env_proxy=%s reasoning_effort=%s",
            self._effective_model(),
            len(messages),
            len(tools or []),
            self.base_url,
            self.chat_completions_endpoint,
            bool(self.proxy),
            self.use_env_proxy,
            get_llm_effort_override() or "-",
        )
        logger.info(
            "tooling.llm.request_payload mode=sync model=%s endpoint=%s tool_choice=%s messages=%s tool_specs=%s",
            self._effective_model(), self.chat_completions_endpoint, str(tool_choice),
            _json_log(_message_debug(messages), 18000),
            _json_log(_tool_debug(tools), 18000),
        )
        for attempt in range(1, self.retry_max_attempts + 1):
            self._wait_for_slot_sync()
            try:
                logger.info("tooling.llm.session_header session_id=%s", get_llm_session_id())
                response = self.sync_client.chat.completions.create(**kwargs)
                self._retry_gate.note_success()
                return response
            except Exception as exc:  # noqa: BLE001
                if self._degrade_tool_choice(kwargs, exc):
                    continue
                if _is_retryable_error(exc):
                    self._note_retryable_failure(exc, mode="sync", attempt=attempt)
                    if attempt < self.retry_max_attempts:
                        continue
                logger.exception("tooling.llm.request_failed mode=sync model=%s", self.model)
                self._raise_readable(exc)
        raise LLMClientError("LLM 网关重试次数已耗尽。")

    async def achat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
    ) -> Any:
        kwargs = self._request_kwargs(self.model, messages, tools, tool_choice)
        logger.info(
            "tooling.llm.request mode=async model=%s messages=%s tools=%s base_url=%s endpoint=%s proxy=%s env_proxy=%s reasoning_effort=%s",
            self._effective_model(),
            len(messages),
            len(tools or []),
            self.base_url,
            self.chat_completions_endpoint,
            bool(self.proxy),
            self.use_env_proxy,
            get_llm_effort_override() or "-",
        )
        logger.info("tooling.llm.prompt mode=async user=%s", _last_user_message(messages))
        logger.info(
            "tooling.llm.request_payload mode=async model=%s endpoint=%s tool_choice=%s messages=%s tool_specs=%s",
            self._effective_model(), self.chat_completions_endpoint, str(tool_choice),
            _json_log(_message_debug(messages), 18000),
            _json_log(_tool_debug(tools), 18000),
        )
        for attempt in range(1, self.retry_max_attempts + 1):
            await self._wait_for_slot_async()
            try:
                logger.info("tooling.llm.session_header session_id=%s", get_llm_session_id())
                response = await self.client.chat.completions.create(**kwargs)
                self._retry_gate.note_success()
                logger.info(
                    "tooling.llm.response mode=async model=%s response=%s",
                    self.model,
                    _response_summary(response),
                )
                return response
            except Exception as exc:  # noqa: BLE001
                if self._degrade_tool_choice(kwargs, exc):
                    continue
                if _is_retryable_error(exc):
                    self._note_retryable_failure(exc, mode="async", attempt=attempt)
                    if attempt < self.retry_max_attempts:
                        continue
                logger.exception("tooling.llm.request_failed mode=async model=%s", self.model)
                self._raise_readable(exc)
        raise LLMClientError("LLM 网关重试次数已耗尽。")

    def close(self) -> None:
        self._sync_http.close()

    async def aclose(self) -> None:
        await self._async_http.aclose()


_client: LLMClient | None = None
_client_config: tuple[str, str, str, float, str, bool] | None = None


def get_llm_client(session_id: str | None = None) -> LLMClient:
    """Return a shared client and rebuild it when runtime AI settings change."""
    global _client, _client_config
    config = _validated_config()
    if _client is None or _client_config != config:
        old = _client
        _client = LLMClient()
        _client_config = config
        if old is not None:
            try:
                old.close()
            except Exception:  # noqa: BLE001
                logger.debug("Failed to close previous sync LLM client", exc_info=True)
    if session_id is not None:
        # Share transport pools, never mutable session identity. Bound copy follows the graph state.
        import copy
        bound = copy.copy(_client)
        bound.session_id = str(session_id)
        return bound
    return _client


# --------------------------------------------------------------------------- model catalog
# Cached gateway model list for the composer's model picker. The picker must never be
# the reason a chat is blocked, so every failure degrades to "just the configured model".
_MODEL_CATALOG_TTL = 600.0
_model_catalog_cache: dict[str, Any] = {"at": 0.0, "payload": None}


def _normalize_gateway_model(raw: Any, fallback: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    model_id = str(raw.get("id") or "").strip()
    if not model_id:
        return None
    efforts: list[str] = []
    default_effort = ""
    effort_spec = raw.get("effort")
    if isinstance(effort_spec, dict):
        levels = effort_spec.get("supported_levels")
        if isinstance(levels, list):
            efforts = [str(level).strip().casefold() for level in levels if str(level).strip()]
            efforts = [level for level in efforts if level in REASONING_EFFORTS]
        default_effort = str(effort_spec.get("default_level") or "").strip().casefold()
    if default_effort not in efforts:
        default_effort = efforts[0] if efforts else ""
    return {
        "id": model_id,
        "name": str(raw.get("name") or "").strip() or model_id,
        "context_window": int(raw.get("context_window") or 0) or None,
        "efforts": efforts,
        "default_effort": default_effort,
        "is_default": model_id == fallback["id"],
    }


def list_ai_models(force: bool = False) -> dict[str, Any]:
    """Return ``{current, models, source}`` for the model picker.

    ``source`` is ``"gateway"`` when the live ``/models`` call succeeded and
    ``"configured"`` when we fell back to the single configured model.
    """
    base_url, _api_key, model, timeout, proxy, use_env_proxy = _validated_config()
    current_effort = get_llm_effort_override()
    cached = _model_catalog_cache.get("payload")
    fresh = cached is not None and (time.monotonic() - float(_model_catalog_cache.get("at") or 0.0)) < _MODEL_CATALOG_TTL
    if cached is not None and fresh and not force:
        payload = dict(cached)
        payload["current"] = {"model": get_llm_model_override() or model, "effort": current_effort}
        return payload

    fallback_model = {"id": model, "name": model, "context_window": None,
                      "efforts": list(REASONING_EFFORTS), "default_effort": "high", "is_default": True}
    models: list[dict[str, Any]] = []
    source = "configured"
    try:
        with httpx.Client(trust_env=use_env_proxy, timeout=min(10.0, timeout), proxy=proxy or None) as http_client:
            response = http_client.get(
                f"{base_url}/models",
                headers={"Authorization": f"Bearer {_api_key}"},
            )
            response.raise_for_status()
            body = response.json()
        for raw in (body.get("data") if isinstance(body, dict) else None) or []:
            normalized = _normalize_gateway_model(raw, fallback_model)
            if normalized:
                models.append(normalized)
        if models:
            source = "gateway"
    except Exception as exc:  # noqa: BLE001
        logger.warning("tooling.llm.model_catalog_unavailable error=%s", str(exc)[:400])

    if not any(item["id"] == fallback_model["id"] for item in models):
        models.insert(0, fallback_model)

    payload = {
        "models": models,
        "source": source,
        "default_model": model,
        "current": {"model": get_llm_model_override() or model, "effort": current_effort},
    }
    _model_catalog_cache["at"] = time.monotonic()
    _model_catalog_cache["payload"] = {k: v for k, v in payload.items() if k != "current"}
    return payload


def resolve_model_choice(model: Any, effort: Any) -> tuple[str, str]:
    """Validate a picker selection against the catalog; unknown values fall back safely."""
    available = list_ai_models()
    ids = {item["id"] for item in available["models"]}
    chosen_model = str(model or "").strip()
    if chosen_model not in ids:
        chosen_model = ""
    chosen_effort = str(effort or "").strip().casefold()
    if chosen_effort not in REASONING_EFFORTS:
        chosen_effort = ""
    if not chosen_model:
        chosen_effort = ""
    return chosen_model, chosen_effort
