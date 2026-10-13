from __future__ import annotations

import logging
import math
import time

from config.settings import settings
from core.market_data import MarketDataService
from core.types import TradeIntent
from strategy.base import BaseStrategy

logger = logging.getLogger(__name__)


class BasicMarketMakerStrategy(BaseStrategy):
    """Simple two-sided quote strategy for activity-oriented market making."""

    name = "basic_market_maker"

    def __init__(self, market_data: MarketDataService, market_ids: list[int]) -> None:
        self._market_data = market_data
        self._market_ids = market_ids
        self._last_quote_ts = 0.0

    async def on_start(self) -> None:
        logger.info(
            "[%s] enabled markets=%s quote_size=%s half_spread_bps=%s refresh_seconds=%s",
            self.name,
            self._market_ids,
            settings.mm_quote_size,
            settings.mm_half_spread_bps,
            settings.mm_refresh_seconds,
        )

    async def on_tick(self) -> list[TradeIntent]:
        now = time.time()
        if now - self._last_quote_ts < settings.mm_refresh_seconds:
            return []

        intents: list[TradeIntent] = []
        for market_id in self._market_ids:
            market = self._market_data.get_market(market_id)
            logger.info("[%s] market=%s", self.name, market)
            if market is None or market.trading_status != "OPEN":
                logger.info("[%s] market not open or unavailable: %s", self.name, market)
                continue

            book = await self._market_data.get_orderbook(market_id, prefer_ws=True)
            logger.info("[%s] orderbook=%s", self.name, book)
            if not book.best_bid or not book.best_ask:
                continue

            best_bid, bid_size = book.best_bid
            best_ask, ask_size = book.best_ask
            if bid_size < settings.mm_min_level_size or ask_size < settings.mm_min_level_size:
                continue

            spread = best_ask - best_bid
            spread_bps = spread * 10_000
            if spread_bps < settings.mm_min_spread_bps:
                continue

            mid = (best_bid + best_ask) / 2.0
            half = settings.mm_half_spread_bps / 10_000.0
            raw_bid = max(0.01, min(0.99, mid - half))
            raw_ask = max(0.01, min(0.99, mid + half))
            # Predict currently accepts up to 2 decimal places for price.
            bid_px = math.floor(raw_bid * 100) / 100.0
            ask_px = math.ceil(raw_ask * 100) / 100.0

            if bid_px >= ask_px:
                continue

            outcome = settings.mm_outcome.strip().upper() or "YES"
            reason = f"mm mid={mid:.4f};spread_bps={spread_bps:.1f};half_bps={settings.mm_half_spread_bps:.1f}"
            min_qty_bid = settings.mm_min_order_value_usdt / bid_px if bid_px > 0 else settings.mm_quote_size
            min_qty_ask = settings.mm_min_order_value_usdt / ask_px if ask_px > 0 else settings.mm_quote_size
            buy_qty = round(max(settings.mm_quote_size, min_qty_bid), 6)
            sell_qty = round(max(settings.mm_quote_size, min_qty_ask), 6)

            intents.append(
                TradeIntent(
                    market_id=market_id,
                    outcome=outcome,
                    side="BUY",
                    strategy="LIMIT",
                    quantity=buy_qty,
                    price_per_share=bid_px,
                    reason=reason,
                )
            )
            intents.append(
                TradeIntent(
                    market_id=market_id,
                    outcome=outcome,
                    side="SELL",
                    strategy="LIMIT",
                    quantity=sell_qty,
                    price_per_share=ask_px,
                    reason=reason,
                )
            )

        if intents:
            self._last_quote_ts = now
        return intents

    async def on_stop(self) -> None:
        logger.info("[%s] stopped", self.name)
