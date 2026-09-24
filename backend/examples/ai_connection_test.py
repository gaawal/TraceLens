"""Minimal GLM/OpenAI-compatible connectivity test.

Run from backend with the same environment used by TraceLens:
    python examples/ai_connection_test.py

This bypasses Django views and LangGraph and sends only model + messages.
"""
from __future__ import annotations

import os

import httpx
from openai import OpenAI


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


base_url = os.getenv("TRACELENS_AI_BASE_URL", "").strip().rstrip("/")
api_key = os.getenv("TRACELENS_AI_API_KEY", "").strip()
model = os.getenv("TRACELENS_AI_MODEL", "GLM-4.7-XS").strip()
timeout = float(os.getenv("TRACELENS_AI_TIMEOUT", "600"))
proxy = os.getenv("TRACELENS_AI_PROXY", "").strip()
use_env_proxy = _env_bool("TRACELENS_AI_USE_ENV_PROXY", False)

if not base_url or not api_key:
    raise SystemExit("Please set TRACELENS_AI_BASE_URL and TRACELENS_AI_API_KEY in the current shell/environment first.")

http_kwargs = {"trust_env": use_env_proxy, "timeout": timeout}
if proxy:
    http_kwargs["proxy"] = proxy

with httpx.Client(**http_kwargs) as http_client:
    client = OpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=timeout,
        http_client=http_client,
    )
    print(f"base_url={base_url}")
    print(f"model={model}")
    print(f"timeout={timeout:g}s")
    print(f"proxy={proxy or '<direct>'}")
    print(f"use_env_proxy={use_env_proxy}")
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "只回复 OK"}],
    )
    print(response.choices[0].message.content)
