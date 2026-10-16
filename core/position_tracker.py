from __future__ import annotations

import logging
import time
from decimal import Decimal, InvalidOperation

from config.settings import settings
from core.redis_client import get_redis

logger = logging.getLogger(__name__)


class PositionTracker:
    """Local cache of outcome-share positions, reconciled with the API periodically.

    Reads are served from the in-memory cache so that on_tick stays fast.
    A full refresh is triggered automatically when the cache is stale.
    When REDIS_URL is configured, every reconcile is also written to Redis so that
    other processes can observe positions and a restart can warm-start from Redis.
    """

    def __init__(self, http_client: "PredictHttpClient", reconcile_interval_seconds: float = 30.0) -> None:  # noqa: F821
        self._http = http_client
        self._reconcile_interval = reconcile_interval_seconds
        # {(market_id, token_id): shares}
        self._cache: dict[tuple[int, str], Decimal] = {}
        self._last_reconcile: float = 0.0

    async def get_net_shares(self, market_id: int, token_id: str) -> Decimal:
        """Return the cached share balance for an outcome token, refreshing if stale."""
        await self._maybe_reconcile()
        return self._cache.get((int(market_id), str(token_id)), Decimal("0"))

    async def force_reconcile(self) -> None:
        """Unconditionally fetch positions from the API and rebuild the cache."""
        await self._do_reconcile()

    async def load_from_redis(self) -> bool:
        """Seed in-memory cache from Redis (warm-start after restart).

        Returns True when data was found and loaded.  Does nothing and returns
        False when Redis is not configured or the key is missing/empty.
        """
        redis = get_redis()
        if redis is None:
            return False
        try:
            raw: dict[str, str] = await redis.hgetall(settings.redis_positions_key)
            if not raw:
                return False
            cache: dict[tuple[int, str], Decimal] = {}
            for field, value in raw.items():
                parts = field.split(":", 1)
                if len(parts) != 2:
                    continue
                mid_str, tid = parts
                cache[(int(mid_str), tid)] = _parse_amount(value)
            self._cache = cache
            logger.info(
                "Positions loaded from Redis: key=%s tokens=%d",
                settings.redis_positions_key,
                len(cache),
            )
            return True
        except Exception as exc:
            logger.warning("Redis position load failed: %s", exc)
            return False

    async def _maybe_reconcile(self) -> None:
        if time.monotonic() - self._last_reconcile >= self._reconcile_interval:
            await self._do_reconcile()

    async def _do_reconcile(self) -> None:
        try:
            rows = await self._http.get_all_positions()
            new_cache: dict[tuple[int, str], Decimal] = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                market_id = row.get("marketId")
                if market_id is None and isinstance(row.get("market"), dict):
                    market_id = row["market"].get("id")
                outcome = row.get("outcome")
                if not isinstance(outcome, dict):
                    continue
                token_id = outcome.get("onChainId")
                if market_id is None or not token_id:
                    continue
                new_cache[(int(market_id), str(token_id))] = _parse_amount(row.get("amount"))
            self._cache = new_cache
            self._last_reconcile = time.monotonic()
            logger.debug("Positions reconciled: %d tokens", len(self._cache))
            await self._sync_to_redis(new_cache)
        except Exception as exc:
            logger.warning("Position reconcile failed, using stale cache: %s", exc)

    async def _sync_to_redis(self, cache: dict[tuple[int, str], Decimal]) -> None:
        redis = get_redis()
        if redis is None:
            return
        try:
            key = settings.redis_positions_key
            if cache:
                mapping = {f"{mid}:{tid}": str(shares) for (mid, tid), shares in cache.items()}
                await redis.hset(key, mapping=mapping)
            else:
                await redis.delete(key)
            await redis.expire(key, settings.redis_positions_ttl)
            logger.debug("Positions synced to Redis: key=%s fields=%d", key, len(cache))
        except Exception as exc:
            logger.warning("Redis position sync failed: %s", exc)


def _parse_amount(raw: object) -> Decimal:
    if raw is None:
        return Decimal("0")
    text = str(raw).strip()
    if not text:
        return Decimal("0")
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        return Decimal("0")
    # API returns bigint wei strings without a decimal point.
    if "." not in text:
        return amount / Decimal(10**18)
    return amount
