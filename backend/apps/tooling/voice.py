from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx


DEFAULT_LOCAL_STT_URL = 'http://127.0.0.1:8001/v1/audio/transcriptions'


class VoiceServiceError(RuntimeError):
    pass


def _env_bool(name: str, default: bool = False) -> bool:
    value = str(os.getenv(name, '')).strip().lower()
    if not value:
        return default
    return value in {'1', 'true', 'yes', 'on'}


def _stt_url() -> str:
    """Use TraceLens local STT by default; an environment variable may override it."""
    return str(os.getenv('TRACELENS_VOICE_STT_URL', '')).strip() or DEFAULT_LOCAL_STT_URL


def _default_stt_health_url() -> str:
    parts = urlsplit(DEFAULT_LOCAL_STT_URL)
    return urlunsplit((parts.scheme, parts.netloc, '/health', '', ''))


def _default_local_stt_available() -> bool:
    try:
        with httpx.Client(timeout=0.45, trust_env=False) as client:
            response = client.get(_default_stt_health_url())
        return response.status_code < 400
    except httpx.HTTPError:
        return False


def _extract_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload.strip()
    if not isinstance(payload, dict):
        return ''
    for key in ('text', 'transcript', 'result_text'):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ('data', 'result', 'output'):
        nested = payload.get(key)
        if isinstance(nested, dict):
            text = _extract_text(nested)
            if text:
                return text
    return ''


def transcribe_audio(*, file_name: str, content_type: str, content: bytes) -> str:
    """Send one recorded clip to the configured local STT/ASR service.

    Without extra configuration TraceLens connects to the bundled local service at
    ``127.0.0.1:8001``. ``TRACELENS_VOICE_STT_URL`` can still override that endpoint.
    """
    url = _stt_url()
    if not content:
        raise VoiceServiceError('录音内容为空。')

    timeout = max(1.0, float(os.getenv('TRACELENS_VOICE_STT_TIMEOUT', '90') or '90'))
    max_bytes = max(
        1024 * 1024,
        int(os.getenv('TRACELENS_VOICE_STT_MAX_BYTES', str(25 * 1024 * 1024)) or str(25 * 1024 * 1024)),
    )
    if len(content) > max_bytes:
        raise VoiceServiceError(f'录音过大，当前限制 {max_bytes // (1024 * 1024)} MiB。')

    headers: dict[str, str] = {}
    api_key = str(os.getenv('TRACELENS_VOICE_STT_API_KEY', '')).strip()
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'

    file_field = str(os.getenv('TRACELENS_VOICE_STT_FILE_FIELD', 'file')).strip() or 'file'
    form: dict[str, str] = {}
    language = str(os.getenv('TRACELENS_VOICE_STT_LANGUAGE', 'zh')).strip()
    model = str(os.getenv('TRACELENS_VOICE_STT_MODEL', '')).strip()
    if language:
        form['language'] = language
    if model:
        form['model'] = model

    try:
        with httpx.Client(timeout=timeout, trust_env=_env_bool('TRACELENS_VOICE_STT_USE_ENV_PROXY', False)) as client:
            response = client.post(
                url,
                headers=headers,
                data=form,
                files={file_field: (file_name or 'tracepilot-voice.webm', content, content_type or 'application/octet-stream')},
            )
    except httpx.HTTPError as exc:
        if url == DEFAULT_LOCAL_STT_URL:
            raise VoiceServiceError(
                '本地 STT 服务未启动。请进入 backend 目录运行 python start_stt.py。'
            ) from exc
        raise VoiceServiceError(f'本地语音识别服务连接失败：{exc}') from exc

    if response.status_code >= 400:
        detail = response.text.strip()[:500]
        raise VoiceServiceError(f'本地语音识别失败：HTTP {response.status_code}{": " + detail if detail else ""}')

    text = ''
    try:
        text = _extract_text(response.json())
    except ValueError:
        if response.headers.get('content-type', '').lower().startswith('text/'):
            text = response.text.strip()
    if not text:
        raise VoiceServiceError('本地语音识别服务未返回可用文本。')
    return text


def voice_service_capabilities() -> dict[str, bool]:
    """Expose voice availability to the assistant composer."""
    explicit_stt_url = str(os.getenv('TRACELENS_VOICE_STT_URL', '')).strip()
    return {
        # Custom endpoints preserve the previous behavior. For the bundled default
        # endpoint we only enable the microphone when the local service is alive.
        'stt': bool(explicit_stt_url) or _default_local_stt_available(),
        'tts': bool(str(os.getenv('TRACELENS_VOICE_TTS_URL', '')).strip()),
    }
