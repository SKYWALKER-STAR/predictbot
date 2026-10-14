from __future__ import annotations

import logging
import math
import time

from config.settings import settings
from core.market_data import MarketDataService
from core.position_tracker import PositionTracker
from core.types import MarketOutcome, TradeIntent
from strategy.base import BaseStrategy

logger = logging.getLogger(__name__)


def _resolve_token_id(outcomes: list[MarketOutcome], target_outcome: str) -> str:
    target = target_outcome.strip().lower()
    for outcome in outcomes:
        if outcome.name.strip().lower() == target:
            return outcome.on_chain_id
    return outcomes[0].on_chain_id if outcomes else ""


class BasicMarketMakerStrategy(BaseStrategy):
    """Two-sided quote strategy with inventory-aware price skew.

    Skew logic:
    - net_shares / mm_max_position_shares gives an inventory_ratio in [-1, 1].
    - Both bid and ask are shifted down by (inventory_ratio * mm_inventory_skew_bps),
      so a long inventory makes selling more attractive and buying less so.
    - BUY orders are suppressed once net_shares >= mm_max_position_shares.
    """

    name = "basic_market_maker"

    def __init__(
        self,
        market_data: MarketDataService,
        market_ids: list[int],
        position_tracker: PositionTracker,
    ) -> None:
        self._market_data = market_data
        self._market_ids = market_ids
        self._positions = position_tracker
        self._last_quote_ts = 0.0

    async def on_start(self) -> None:
        await self._positions.load_from_redis()
        await self._positions.force_reconcile()
        logger.info(
            "[%s] enabled markets=%s quote_size=%s half_spread_bps=%s refresh_seconds=%s "
            "max_position_shares=%s inventory_skew_bps=%s outcome=%s min_probability=%s",
            self.name,
            self._market_ids,
            settings.mm_quote_size,
            settings.mm_half_spread_bps,
            settings.mm_refresh_seconds,
            settings.mm_max_position_shares,
            settings.mm_inventory_skew_bps,
            settings.mm_outcome,
            settings.mm_min_probability,
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

            yes_mid = (best_bid + best_ask) / 2.0

            # Select which outcome to quote.
            # AUTO: always quote on the high-probability side (mid > 0.50) so both
            # BID and ASK land in a liquid part of the book.  When neither side
            # clears mm_min_probability the market is too uncertain to quote.
            mm_outcome_cfg = settings.mm_outcome.strip().upper()
            if mm_outcome_cfg == "AUTO":
                high_prob = max(yes_mid, 1.0 - yes_mid)
                if high_prob < settings.mm_min_probability:
                    logger.info(
                        "[%s] market=%s skipped: high_prob=%.3f < mm_min_probability=%.2f",
                        self.name, market_id, high_prob, settings.mm_min_probability,
                    )
                    continue
                if yes_mid >= 0.50:
                    outcome = "YES"
                    mid = yes_mid
                else:
                    # Quote NO; prices are the mirror of the YES book.
                    outcome = "NO"
                    mid = 1.0 - yes_mid
            else:
                outcome = mm_outcome_cfg
                mid = yes_mid

            token_id = _resolve_token_id(market.outcomes, outcome)

            net_shares = float(await self._positions.get_net_shares(market_id, token_id))
            max_shares = settings.mm_max_position_shares
            inventory_ratio = max(-1.0, min(1.0, net_shares / max_shares))
            skew = inventory_ratio * settings.mm_inventory_skew_bps / 10_000.0

            half = settings.mm_half_spread_bps / 10_000.0
            # Predict currently accepts up to 2 decimal places for price.
            bid_px = math.floor(max(0.01, min(0.99, mid - half - skew)) * 100) / 100.0
            ask_px = math.ceil(max(0.01, min(0.99, mid + half - skew)) * 100) / 100.0

            if bid_px >= ask_px:
                continue

            reason = (
                f"mm outcome={outcome};mid={mid:.4f};spread_bps={spread_bps:.1f};"
                f"half_bps={settings.mm_half_spread_bps:.1f};"
                f"net_shares={net_shares:.2f};skew_bps={skew * 10_000:.1f}"
            )
            min_qty_bid = settings.mm_min_order_value_usdt / bid_px if bid_px > 0 else settings.mm_quote_size
            min_qty_ask = settings.mm_min_order_value_usdt / ask_px if ask_px > 0 else settings.mm_quote_size
            buy_qty = round(max(settings.mm_quote_size, min_qty_bid), 6)
            sell_qty = round(max(settings.mm_quote_size, min_qty_ask), 6)

            if net_shares < max_shares:
                intents.append(TradeIntent(
                    market_id=market_id,
                    outcome=outcome,
                    side="BUY",
                    strategy="LIMIT",
                    quantity=buy_qty,
                    price_per_share=bid_px,
                    reason=reason,
                ))

            intents.append(TradeIntent(
                market_id=market_id,
                outcome=outcome,
                side="SELL",
                strategy="LIMIT",
                quantity=sell_qty,
                price_per_share=ask_px,
                reason=reason,
            ))

        if intents:
            self._last_quote_ts = now
        return intents

    async def on_stop(self) -> None:
        logger.info("[%s] stopped", self.name)
