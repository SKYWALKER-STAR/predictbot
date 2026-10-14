from __future__ import annotations

import logging
from typing import Optional

from config.settings import settings

logger = logging.getLogger(__name__)

_client: Optional["redis.asyncio.Redis"] = None  # type: ignore[name-defined]


def get_redis() -> Optional["redis.asyncio.Redis"]:  # type: ignore[name-defined]
    """Return a shared async Redis client, or None if REDIS_URL is not configured."""
    if not settings.redis_url:
        return None
    global _client
    if _client is None:
        import redis.asyncio as aioredis
        _client = aioredis.from_url(settings.redis_url, decode_responses=True)
        logger.info("Redis client initialised: url=%s", settings.redis_url)
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
        logger.debug("Redis client closed")
