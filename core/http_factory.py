from __future__ import annotations

import os
from typing import Any

import httpx

from config.settings import settings


def build_async_client(*, timeout_seconds: float, base_url: str | None = None) -> httpx.AsyncClient:
    """Create an AsyncClient with normalized proxy handling.

    Priority:
    1) PREDICT_PROXY_URL from settings
    2) common proxy env vars (HTTPS_PROXY, ALL_PROXY, HTTP_PROXY)
    """

    proxy = _resolve_proxy_url()

    kwargs: dict[str, Any] = {
        "timeout": timeout_seconds,
        # We resolve proxy config ourselves to avoid httpx rejecting invalid
        # env schemes such as "socks://...".
        "trust_env": False,
    }
    if base_url is not None:
        kwargs["base_url"] = base_url
    if proxy:
        kwargs["proxy"] = proxy

    return httpx.AsyncClient(**kwargs)


def _resolve_proxy_url() -> str | None:
    configured = settings.predict_proxy_url.strip()
    if configured:
        return _normalize_proxy_url(configured)

    for key in (
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
        "HTTP_PROXY",
        "http_proxy",
    ):
        value = os.getenv(key, "").strip()
        if value:
            return _normalize_proxy_url(value)
    return None


def _normalize_proxy_url(url: str) -> str:
    normalized = url.strip()
    # httpx expects socks5:// or socks5h://, not socks://
    if normalized.lower().startswith("socks://"):
        normalized = "socks5://" + normalized[len("socks://") :]
    return normalized
