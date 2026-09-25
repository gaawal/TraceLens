from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DEBUG = os.getenv("DJANGO_DEBUG", "true").lower() in {"1", "true", "yes", "on"}
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "tracelens-dev-only-change-me")
ALLOWED_HOSTS = [
    item.strip()
    for item in os.getenv("DJANGO_ALLOWED_HOSTS", "*").split(",")
    if item.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "drf_spectacular",
    "drf_spectacular_sidecar",
    "apps.common.apps.CommonConfig",
    "apps.tooling.apps.ToolingConfig",
    "apps.atlog.apps.AtLogConfig",
    "apps.machines.apps.MachinesConfig",
    "apps.environments.apps.EnvironmentsConfig",
    "apps.logsources.apps.LogSourcesConfig",
    "apps.reports.apps.ReportsConfig",
    "apps.audits.apps.AuditsConfig",
    "apps.knowledge.apps.KnowledgeConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "apps.common.middleware.RequestIdMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

sqlite_path = Path(os.getenv("SQLITE_PATH", "db.sqlite3"))
if not sqlite_path.is_absolute():
    sqlite_path = BASE_DIR / sqlite_path
sqlite_path.parent.mkdir(parents=True, exist_ok=True)

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": sqlite_path,
        "OPTIONS": {"timeout": 30},
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

CORS_ALLOW_ALL_ORIGINS = os.getenv("DJANGO_CORS_ALLOW_ALL_ORIGINS", "true").lower() in {"1", "true", "yes", "on"}
CORS_ALLOWED_ORIGINS = [
    item.strip()
    for item in os.getenv(
        "DJANGO_CORS_ALLOWED_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if item.strip()
]
CORS_ALLOW_CREDENTIALS = False
# Log streaming uses TraceLens-specific request/response headers.  django-cors-headers
# does not include custom headers in its default allow-list, so browsers would fail
# the OPTIONS preflight even when CORS_ALLOW_ALL_ORIGINS is enabled.
try:
    from corsheaders.defaults import default_headers
except Exception:  # pragma: no cover - keeps settings importable for static tooling
    default_headers = ()
CORS_ALLOW_HEADERS = (*default_headers, "x-tracelens-operation-id", "x-tracelens-request-id")
CORS_EXPOSE_HEADERS = [
    "X-TraceLens-Operation-ID",
    "X-TraceLens-Request-ID",
    "X-TraceLens-Artifact-Count",
]
CSRF_TRUSTED_ORIGINS = [
    item.strip()
    for item in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if item.strip()
]

REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_PAGINATION_CLASS": "apps.common.pagination.StandardResultsSetPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "EXCEPTION_HANDLER": "apps.common.exceptions.api_exception_handler",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "TraceLens Backend API",
    "DESCRIPTION": "TraceLens 环境资源、上下位机拓扑和日志源配置 API",
    "VERSION": "0.11.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SWAGGER_UI_DIST": "SIDECAR",
    "SWAGGER_UI_FAVICON_HREF": "SIDECAR",
    "REDOC_DIST": "SIDECAR",
    "COMPONENT_SPLIT_REQUEST": True,
}

credential_key = os.getenv("TRACELENS_CREDENTIAL_KEY", "").strip()
if not credential_key:
    # 仅用于本地开发。正式环境应显式提供独立 Fernet key。
    digest = hashlib.sha256(SECRET_KEY.encode("utf-8")).digest()
    credential_key = base64.urlsafe_b64encode(digest).decode("ascii")
TRACELENS_CREDENTIAL_KEY = credential_key

TRACELENS_SSH_CONNECT_TIMEOUT = int(os.getenv("TRACELENS_SSH_CONNECT_TIMEOUT", "10"))
TRACELENS_SSH_AUTO_ADD_HOST_KEY = os.getenv("TRACELENS_SSH_AUTO_ADD_HOST_KEY", "true").lower() in {"1", "true", "yes", "on"}
TRACELENS_KNOWN_HOSTS_FILE = os.getenv("TRACELENS_KNOWN_HOSTS_FILE", "").strip()


TRACELENS_SSH_KEEPALIVE = int(os.getenv("TRACELENS_SSH_KEEPALIVE", "30"))
TRACELENS_SSH_SESSION_IDLE_TIMEOUT = int(os.getenv("TRACELENS_SSH_SESSION_IDLE_TIMEOUT", "120"))
TRACELENS_SSH_SESSION_REAP_INTERVAL = int(os.getenv("TRACELENS_SSH_SESSION_REAP_INTERVAL", "15"))
TRACELENS_SSH_SESSION_MAX = int(os.getenv("TRACELENS_SSH_SESSION_MAX", "32"))
TRACELENS_DEPLOYMENT_SSH_IDLE_OUTPUT_TIMEOUT = int(os.getenv("TRACELENS_DEPLOYMENT_SSH_IDLE_OUTPUT_TIMEOUT", "300"))

# Redis is a rebuildable acceleration layer for remote log file indexes and
# compressed log-content window chunks. Searches fall back to SSH when Redis is
# disabled or temporarily unavailable.
TRACELENS_REDIS_ENABLED = os.getenv("TRACELENS_REDIS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
# Either configure TRACELENS_REDIS_URL, or use HOST/PORT/DB/USERNAME/PASSWORD.
# Raw host:port values are accepted and normalized by RedisLogStore.
TRACELENS_REDIS_URL = os.getenv("TRACELENS_REDIS_URL", "").strip()
TRACELENS_REDIS_HOST = os.getenv("TRACELENS_REDIS_HOST", "127.0.0.1").strip()
TRACELENS_REDIS_PORT = int(os.getenv("TRACELENS_REDIS_PORT", "6379"))
TRACELENS_REDIS_DB = int(os.getenv("TRACELENS_REDIS_DB", "0"))
TRACELENS_REDIS_USERNAME = os.getenv("TRACELENS_REDIS_USERNAME", "").strip()
TRACELENS_REDIS_PASSWORD = os.getenv("TRACELENS_REDIS_PASSWORD", "")
TRACELENS_REDIS_CONNECT_TIMEOUT = float(os.getenv("TRACELENS_REDIS_CONNECT_TIMEOUT", "1.0"))
TRACELENS_REDIS_SOCKET_TIMEOUT = float(os.getenv("TRACELENS_REDIS_SOCKET_TIMEOUT", "2.0"))
TRACELENS_REDIS_RETRY_SECONDS = int(os.getenv("TRACELENS_REDIS_RETRY_SECONDS", "30"))
TRACELENS_DEPLOYMENT_ACTIVE_TTL = int(os.getenv("TRACELENS_DEPLOYMENT_ACTIVE_TTL", str(24 * 3600)))
TRACELENS_DEPLOYMENT_ACTIVE_INDEX_TTL = int(os.getenv("TRACELENS_DEPLOYMENT_ACTIVE_INDEX_TTL", "3600"))
TRACELENS_LOG_INDEX_TTL = int(os.getenv("TRACELENS_LOG_INDEX_TTL", str(30 * 24 * 3600)))
TRACELENS_LOG_INDEX_HEAD_BYTES = int(os.getenv("TRACELENS_LOG_INDEX_HEAD_BYTES", str(128 * 1024)))
TRACELENS_LOG_INDEX_TAIL_BYTES = int(os.getenv("TRACELENS_LOG_INDEX_TAIL_BYTES", str(256 * 1024)))
TRACELENS_LOG_CONTENT_CACHE_TTL = int(os.getenv("TRACELENS_LOG_CONTENT_CACHE_TTL", "7200"))
TRACELENS_LOG_CONTENT_CACHE_MAX_BYTES = int(os.getenv("TRACELENS_LOG_CONTENT_CACHE_MAX_BYTES", str(16 * 1024 * 1024)))
TRACELENS_SOURCE_CACHE_TTL = int(os.getenv("TRACELENS_SOURCE_CACHE_TTL", "3600"))

LOG_DIR = Path(os.getenv("TRACELENS_LOG_DIR", BASE_DIR / "runtime"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "request_id": {"()": "apps.common.logging.RequestIdFilter"},
    },
    "formatters": {
        "tracelens": {
            "format": "{asctime} {levelname} [{request_id}] {name} - {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "tracelens",
            "filters": ["request_id"],
        },
        "file": {
            "class": "apps.common.logging_handlers.CopyTruncateRotatingFileHandler",
            "filename": str(LOG_DIR / "tracelens.log"),
            "maxBytes": 20 * 1024 * 1024,
            "backupCount": 5,
            "encoding": "utf-8",
            "formatter": "tracelens",
            "filters": ["request_id"],
        },
    },
    "root": {"handlers": ["console", "file"], "level": os.getenv("TRACELENS_LOG_LEVEL", "INFO")},
    "loggers": {
        # HTTP access lines are intentionally quiet by default. TraceLens terminals
        # should show business/process progress rather than every GET/POST request.
        "django.server": {"handlers": ["console", "file"], "level": "WARNING", "propagate": False},
        "django.request": {"handlers": ["console", "file"], "level": "WARNING", "propagate": False},
        "uvicorn.access": {"handlers": [], "level": "WARNING", "propagate": False},
        "watchfiles.main": {"handlers": [], "level": "ERROR", "propagate": False},
        "tracelens.http": {"handlers": ["console", "file"], "level": "WARNING", "propagate": False},
        "tracelens": {"handlers": ["console", "file"], "level": os.getenv("TRACELENS_LOG_LEVEL", "INFO"), "propagate": False},
    },
}

# CPD report snapshot: reusable across environments that bind to the same host+username.
TRACELENS_CPD_SNAPSHOT_TTL = int(os.getenv("TRACELENS_CPD_SNAPSHOT_TTL", str(30 * 24 * 60 * 60)))
TRACELENS_CPD_SCAN_WORKERS = int(os.getenv("TRACELENS_CPD_SCAN_WORKERS", "4"))
TRACELENS_CPD_PARSE_WORKERS = int(os.getenv("TRACELENS_CPD_PARSE_WORKERS", "6"))
TRACELENS_RUNTIME_STATUS_TTL = int(os.getenv("TRACELENS_RUNTIME_STATUS_TTL", "60"))
TRACELENS_LOWER_TIME_SYNC_TOLERANCE_SECONDS = int(os.getenv("TRACELENS_LOWER_TIME_SYNC_TOLERANCE_SECONDS", "60"))

# OpenAI-compatible LLM used by the ATLog LangGraph diagnosis agent.
# Values mirror the provider block used by OpenCode/@ai-sdk/openai-compatible.
TRACELENS_AI_PROVIDER = os.getenv("TRACELENS_AI_PROVIDER", "my-llm").strip()
# AI 日志证据（异常锚点上下文）的长度上限：没超过就直送原文，超过才压缩。
# 页面里也可以在 TracePilot 的模型菜单里覆盖这一档，这里给服务端默认值。
TRACELENS_AI_EVIDENCE_MAX_CHARS = int(os.getenv("TRACELENS_AI_EVIDENCE_MAX_CHARS", "40000") or 40000)
TRACELENS_AI_MODEL = os.getenv("TRACELENS_AI_MODEL", "GLM-4.7-XS").strip()
TRACELENS_AI_BASE_URL = os.getenv("TRACELENS_AI_BASE_URL", "").strip().rstrip("/")
TRACELENS_AI_API_KEY = os.getenv("TRACELENS_AI_API_KEY", "").strip()
TRACELENS_AI_TIMEOUT = float(os.getenv("TRACELENS_AI_TIMEOUT", "600"))
# Adaptive retry for overloaded/private LLM gateways. SDK retries are disabled;
# TraceLens progressively spaces retries and shares the cooldown across Agent nodes.
TRACELENS_AI_RETRY_MAX_ATTEMPTS = int(os.getenv("TRACELENS_AI_RETRY_MAX_ATTEMPTS", "7"))
TRACELENS_AI_RETRY_BASE_DELAY = float(os.getenv("TRACELENS_AI_RETRY_BASE_DELAY", "3"))
TRACELENS_AI_RETRY_MAX_DELAY = float(os.getenv("TRACELENS_AI_RETRY_MAX_DELAY", "90"))
TRACELENS_AI_RETRY_JITTER = float(os.getenv("TRACELENS_AI_RETRY_JITTER", "0.2"))
# Optional explicit proxy for the LLM only. Empty means no explicit proxy.
TRACELENS_AI_PROXY = os.getenv("TRACELENS_AI_PROXY", "").strip()
# Internal model gateways should normally bypass HTTP_PROXY/HTTPS_PROXY inherited by the backend.
TRACELENS_AI_USE_ENV_PROXY = os.getenv("TRACELENS_AI_USE_ENV_PROXY", "false").strip().lower() in {"1", "true", "yes", "on"}
TRACELENS_AI_MAX_LOG_ROWS = int(os.getenv("TRACELENS_AI_MAX_LOG_ROWS", "220"))
TRACELENS_AI_MAX_CLIENT_ROWS = int(os.getenv("TRACELENS_AI_MAX_CLIENT_ROWS", "260"))
TRACELENS_AI_MAX_CONTEXT_CHARS = int(os.getenv("TRACELENS_AI_MAX_CONTEXT_CHARS", "120000"))
TRACELENS_AI_MAX_EVIDENCE_NODES = int(os.getenv("TRACELENS_AI_MAX_EVIDENCE_NODES", "24"))
TRACELENS_AI_MAX_EVIDENCE_CHARS = int(os.getenv("TRACELENS_AI_MAX_EVIDENCE_CHARS", "18000"))
TRACELENS_ATLOG_AI_CACHE_TTL = int(os.getenv("TRACELENS_ATLOG_AI_CACHE_TTL", "604800"))
