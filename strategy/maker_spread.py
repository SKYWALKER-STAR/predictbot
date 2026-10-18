from __future__ import annotations

import logging
import time
import math
from decimal import Decimal, ROUND_FLOOR

from config.settings import settings
from core.market_data import MarketDataService
from core.types import TradeIntent
from strategy.base import BaseStrategy

logger = logging.getLogger(__name__)


class WsSpreadStrategy(BaseStrategy):
    name = "ws_spread"

    def __init__(self, market_data: MarketDataService, market_ids: list[int], position_tracker) -> None:
        self._positions = position_tracker
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

        if not settings.dry_run and not await self._positions.force_reconcile():
            return []
        for market_id in self._market_ids:
            market = self._market_data.get_market(market_id)
            if market is None or market.trading_status != "OPEN":
                continue

            book = await self._market_data.get_orderbook(market_id, prefer_ws=True)
            if not book.best_ask or not book.best_bid:
                continue

            ask_price, ask_qty = book.best_ask
            bid_price, bid_qty = book.best_bid
            outcomes = {o.index_set: o for o in market.outcomes}
            if len(market.outcomes) != 2 or set(outcomes) != {1, 2}:
                continue
            holdings = {i: float(await self._positions.get_net_shares(market_id, o.on_chain_id))
                        for i, o in outcomes.items()}
            if not all(math.isfinite(v) and v >= 0 for v in holdings.values()):
                continue
            has_inventory = any(v > 0 for v in holdings.values())
            index = max(holdings, key=holdings.get) if has_inventory else 1
            if index == 2:
                bid_price, ask_price, bid_qty, ask_qty = 1 - ask_price, 1 - bid_price, ask_qty, bid_qty
            spread = ask_price - bid_price
            if not has_inventory and spread < settings.strategy_min_spread:
                continue

            depth_sum = ask_qty + bid_qty
            if depth_sum <= 0:
                continue
            imbalance = (bid_qty - ask_qty) / depth_sum
            if not has_inventory and abs(imbalance) < settings.strategy_min_imbalance:
                continue

            side = "SELL" if has_inventory else ("BUY" if imbalance > 0 else "SELL")
            if not has_inventory and side == "SELL":
                continue
            selected = outcomes[index]
            ref_price = bid_price if side == "BUY" else ask_price
            edge = min(spread / 4.0, 0.01)
            px = round(ref_price + edge if side == "BUY" else ref_price - edge, 4)
            px = min(max(px, 0.001), 0.999)

            quantity = settings.strategy_base_order_size
            if has_inventory:
                quantity = float(Decimal(str(min(quantity, holdings[index]))).quantize(
                    Decimal("0.000001"), rounding=ROUND_FLOOR))
            if quantity <= 0 or quantity * px < settings.mm_min_order_value_usdt:
                continue
            intents.append(
                TradeIntent(
                    market_id=market_id,
                    outcome=selected.name,
                    token_id=selected.on_chain_id,
                    side=side,
                    strategy="LIMIT",
                    quantity=quantity,
                    price_per_share=px,
                    reason=f"spread={spread:.4f};imbalance={imbalance:.4f}",
                )
            )

        if intents:
            self._last_trade_ts = now
        return intents

    async def on_stop(self) -> None:
        logger.info("[%s] stopped", self.name)
