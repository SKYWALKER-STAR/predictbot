from __future__ import annotations

import logging
import time

from config.settings import settings
from core.market_data import MarketDataService
from core.types import TradeIntent
from strategy.base import BaseStrategy

logger = logging.getLogger(__name__)


class WsSpreadStrategy(BaseStrategy):
    name = "ws_spread"

    def __init__(self, market_data: MarketDataService, market_ids: list[int]) -> None:
        self._market_data = market_data
        self._market_ids = market_ids
        self._last_trade_ts = 0.0

    async def on_start(self) -> None:
        logger.info("[%s] enabled for markets=%s", self.name, self._market_ids)

    async def on_tick(self) -> list[TradeIntent]:
        intents: list[TradeIntent] = []

        now = time.time()
        if now - self._last_trade_ts < settings.strategy_cooldown_seconds:
            return intents

        for market_id in self._market_ids:
            market = self._market_data.get_market(market_id)
            if market is None or market.trading_status != "OPEN":
                continue

            book = await self._market_data.get_orderbook(market_id, prefer_ws=True)
            if not book.best_ask or not book.best_bid:
                continue

            ask_price, ask_qty = book.best_ask
            bid_price, bid_qty = book.best_bid
            spread = ask_price - bid_price
            if spread < settings.strategy_min_spread:
                continue

            depth_sum = ask_qty + bid_qty
            if depth_sum <= 0:
                continue
            imbalance = (bid_qty - ask_qty) / depth_sum
            if abs(imbalance) < settings.strategy_min_imbalance:
                continue

            side = "BUY" if imbalance > 0 else "SELL"
            # Market orderbook prices belong to indexSet=1, regardless of its name.
            matches = [o for o in market.outcomes if o.index_set == 1]
            if len(matches) != 1 or not matches[0].on_chain_id:
                logger.warning("[%s] missing or ambiguous book outcome: market=%s", self.name, market_id)
                continue
            selected = matches[0]
            ref_price = bid_price if side == "BUY" else ask_price
            edge = min(spread / 4.0, 0.01)
            px = round(ref_price + edge if side == "BUY" else ref_price - edge, 4)
            px = min(max(px, 0.001), 0.999)

            intents.append(
                TradeIntent(
                    market_id=market_id,
                    outcome=selected.name,
                    token_id=selected.on_chain_id,
                    side=side,
                    strategy="LIMIT",
                    quantity=settings.strategy_base_order_size,
                    price_per_share=px,
                    reason=f"spread={spread:.4f};imbalance={imbalance:.4f}",
                )
            )

        if intents:
            self._last_trade_ts = now
        return intents

    async def on_stop(self) -> None:
        logger.info("[%s] stopped", self.name)
