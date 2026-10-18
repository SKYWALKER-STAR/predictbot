"""Paper-based inventory market maker, using the existing execution gateway."""
from __future__ import annotations

import logging
import math
import time
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from config.settings import settings
from core.types import TradeIntent
from strategy.inventory_model import PriceVariance, inventory_quotes
from strategy.market_maker import BasicMarketMakerStrategy

logger = logging.getLogger(__name__)


def _tick(price: float, *, up: bool) -> float:
    return float(Decimal(str(price)).quantize(Decimal("0.01"),
                 rounding=ROUND_CEILING if up else ROUND_FLOOR))


class InventoryMarketMakerStrategy(BasicMarketMakerStrategy):
    name = "inventory_market_maker"

    def __init__(self, market_data, market_ids, position_tracker, order_manager) -> None:
        super().__init__(market_data, market_ids, position_tracker, order_manager)
        if settings.mm_quote_size > settings.mm_max_position_shares:
            raise ValueError("MM_QUOTE_SIZE must not exceed MM_MAX_POSITION_SHARES")
        if settings.mm_inventory_model == "glft" and math.floor(
            settings.mm_max_position_shares / settings.mm_quote_size
        ) > 200:
            raise ValueError("GLFT requires MM_MAX_POSITION_SHARES / MM_QUOTE_SIZE < 201")
        self._variance: dict[int, PriceVariance] = {}

    async def on_start(self) -> None:
        logger.info("[%s] markets=%s model=%s gamma=%s k=%s A=%s sigma_floor=%s "
                    "volatility_window_seconds=%s min_samples=%s horizon_seconds=%s "
                    "lot_shares=%s max_inventory_shares=%s refresh_seconds=%s dry_run=%s",
                    self.name, self._market_ids, settings.mm_inventory_model,
                    settings.mm_risk_aversion, settings.mm_order_decay_k,
                    settings.mm_arrival_rate_a, settings.mm_initial_sigma,
                    settings.mm_volatility_window_seconds, settings.mm_volatility_min_samples,
                    settings.mm_model_horizon_seconds, settings.mm_quote_size,
                    settings.mm_max_position_shares, settings.mm_refresh_seconds, settings.dry_run)
        if settings.dry_run:
            logger.info("[%s] dry-run uses zero simulated inventory; no fills are simulated", self.name)
            return
        await self._positions.load_from_redis()
        if not await self._positions.force_reconcile():
            logger.warning("[%s] inventory unavailable; quotes remain disabled until reconciled", self.name)

    async def on_tick(self) -> list[TradeIntent]:
        now = time.monotonic()
        if now - self._last_quote_ts < settings.mm_refresh_seconds:
            logger.debug("[%s] refresh deferred: remaining_seconds=%.3f", self.name,
                         settings.mm_refresh_seconds - (now - self._last_quote_ts))
            return []
        logger.debug("[%s] refresh started: previous_quotes=%d markets=%d",
                     self.name, len(self._quote_hashes), len(self._market_ids))
        # Cancellation failure retains hashes and blocks all replacements.
        if not await self._orders.cancel_by_hashes(self._quote_hashes, verify=True):
            logger.error("[%s] cancellation failed; no replacement quotes", self.name)
            return []
        self._quote_hashes.clear()
        logger.debug("[%s] previous quotes cleared; reconciling inventory dry_run=%s",
                     self.name, settings.dry_run)
        self._last_quote_ts = now
        # Reconcile AFTER cancelling: submissions are never treated as fills.
        # A failed API read must not turn stale/unknown holdings into zero inventory.
        if not settings.dry_run and not await self._positions.force_reconcile():
            logger.warning("[%s] failed to reconcile inventory; no quotes", self.name)
            return []
        intents: list[TradeIntent] = []
        for market_id in self._market_ids:
            try:
                intents.extend(await self._quote_market(market_id))
            except Exception:
                logger.exception("[%s] cannot quote market=%s", self.name, market_id)
        logger.debug("[%s] refresh completed: intents=%d elapsed_ms=%.1f", self.name,
                     len(intents), (time.monotonic() - now) * 1000)
        return intents

    async def on_stop(self) -> None:
        logger.debug("[%s] stopping: quotes_to_cancel=%d", self.name, len(self._quote_hashes))
        if await self._orders.cancel_by_hashes(self._quote_hashes, verify=True):
            self._quote_hashes.clear()
        else:
            logger.error("[%s] could not confirm quote cancellation on stop", self.name)

    def _skip(self, market_id: int, reason: str, *args: object) -> list[TradeIntent]:
        logger.debug("[%s] market=%s skipped: " + reason, self.name, market_id, *args)
        return []

    async def _quote_market(self, market_id: int) -> list[TradeIntent]:
        market = self._market_data.get_market(market_id)
        if market is None or market.trading_status != "OPEN":
            return self._skip(market_id, "market unavailable or not OPEN; status=%s",
                              market.trading_status if market else None)
        outcomes = {o.index_set: o for o in market.outcomes}
        if len(market.outcomes) != 2 or set(outcomes) != {1, 2}:
            return self._skip(market_id, "unsupported outcomes; index_sets=%s count=%d",
                              sorted(outcomes), len(market.outcomes))
        if any(not o.on_chain_id for o in outcomes.values()) or outcomes[1].on_chain_id == outcomes[2].on_chain_id:
            return self._skip(market_id, "missing or duplicate outcome token IDs")
        book = await self._market_data.get_orderbook(market_id, prefer_ws=True)
        if book.market_id != market_id or not book.best_bid or not book.best_ask:
            return self._skip(market_id, "missing book side or wrong market; book_market=%s",
                              book.market_id)
        bid, bid_size = book.best_bid
        ask, ask_size = book.best_ask
        if not all(math.isfinite(x) for x in (bid, ask, bid_size, ask_size)):
            return self._skip(market_id, "non-finite book; bid=%s ask=%s bid_size=%s ask_size=%s",
                              bid, ask, bid_size, ask_size)
        if not 0 < bid < ask < 1 or min(bid_size, ask_size) < settings.mm_min_level_size:
            return self._skip(market_id, "invalid price/depth; bid=%.6f ask=%.6f "
                              "bid_size=%.6f ask_size=%.6f min_level_size=%.6f",
                              bid, ask, bid_size, ask_size, settings.mm_min_level_size)
        # An old last-update timestamp alone may mean a quiet book, but without
        # transport freshness evidence we conservatively decline that snapshot.
        stamp = book.update_timestamp_ms / 1000
        age = time.time() - stamp
        if stamp <= 0 or age < -1 or age > settings.mm_max_book_age_seconds:
            return self._skip(market_id, "invalid/stale book timestamp; timestamp_ms=%s "
                              "age_seconds=%.3f max_age_seconds=%.3f",
                              book.update_timestamp_ms, age, settings.mm_max_book_age_seconds)
        logger.debug("[%s] market=%s book bid=%.6f ask=%.6f bid_size=%.6f ask_size=%.6f age_seconds=%.3f",
                     self.name, market_id, bid, ask, bid_size, ask_size, age)
        horizon = settings.mm_model_horizon_seconds
        end = settings.mm_market_end_times.get(market_id)
        if end is not None:
            remaining = end.timestamp() - time.time()
            if remaining <= settings.mm_stop_before_end_seconds:
                return self._skip(market_id, "end-time cutoff; remaining_seconds=%.3f cutoff_seconds=%.3f",
                                  remaining, settings.mm_stop_before_end_seconds)
            horizon = min(horizon, remaining)
        yes_mid = (bid + ask) / 2
        variance = self._variance.setdefault(market_id, PriceVariance(
            settings.mm_volatility_window_seconds, settings.mm_volatility_min_samples,
            settings.mm_initial_sigma))
        sigma = variance.observe(stamp, yes_mid)
        logger.debug("[%s] market=%s volatility sigma=%.8f floor=%.8f samples=%d "
                     "min_samples=%d window_seconds=%.1f sample_span_seconds=%.3f horizon_seconds=%.3f",
                     self.name, market_id, sigma, settings.mm_initial_sigma, len(variance.samples),
                     settings.mm_volatility_min_samples, settings.mm_volatility_window_seconds,
                     variance.samples[-1][0] - variance.samples[0][0], horizon)
        yes = 0.0 if settings.dry_run else float(await self._positions.get_net_shares(market_id, outcomes[1].on_chain_id))
        no = 0.0 if settings.dry_run else float(await self._positions.get_net_shares(market_id, outcomes[2].on_chain_id))
        if not all(math.isfinite(x) and x >= 0 for x in (yes, no)):
            return self._skip(market_id, "invalid inventory; index1_shares=%s index2_shares=%s", yes, no)
        cfg = settings.mm_outcome.strip().upper()
        if cfg == "AUTO":
            # Keep quoting the exposed outcome when the favored side switches,
            # so an old position still has an exit quote after a reversal.
            index = (1 if yes > no else 2) if yes != no else (1 if yes_mid >= .5 else 2)
            selected = outcomes[index]
        elif cfg in {"YES", "NO"}:
            selected = outcomes[1 if cfg == "YES" else 2]
        else:
            matches = [o for o in outcomes.values() if o.name.strip().upper() == cfg]
            if len(matches) != 1:
                return self._skip(market_id, "outcome selection is missing/ambiguous; configured=%s matches=%d",
                                  cfg, len(matches))
            selected = matches[0]
        if selected.index_set == 1:
            mid, own, other, best_bid, best_ask = yes_mid, yes, no, bid, ask
        else:
            mid, own, other, best_bid, best_ask = 1 - yes_mid, no, yes, 1 - ask, 1 - bid
        net = own - other
        lot = settings.mm_quote_size
        logger.debug("[%s] market=%s selection=%s outcome=%s index_set=%s mid=%.6f "
                     "own_shares=%.6f complementary_shares=%.6f net_shares=%.6f inventory_lots=%.6f",
                     self.name, market_id, cfg, selected.name, selected.index_set, mid, own, other,
                     net, net / lot)
        quotes = inventory_quotes(model=settings.mm_inventory_model, mid=mid, inventory=net,
            lot_size=lot, max_inventory=settings.mm_max_position_shares,
            gamma=settings.mm_risk_aversion, sigma=sigma, k=settings.mm_order_decay_k,
            arrival_rate=settings.mm_arrival_rate_a, horizon=horizon)
        logger.debug("[%s] market=%s model=%s raw_bid=%s raw_ask=%s "
                     "effective_inventory_limit=%.6f", self.name, market_id,
                     settings.mm_inventory_model, quotes.bid, quotes.ask,
                     math.floor(settings.mm_max_position_shares / lot) * lot
                     if settings.mm_inventory_model == "glft" else settings.mm_max_position_shares)
        reason = (f"inventory model={settings.mm_inventory_model};mid={mid:.6f};"
                  f"net_shares={net:.6f};sigma={sigma:.6f};tau={horizon:.3f}")
        intents = []
        # Decline out-of-domain model prices instead of clamping an uneconomic
        # bid upwards or an uneconomic ask downwards at the binary boundaries.
        for side, raw in (("SELL", quotes.ask), ("BUY", quotes.bid)):
            if raw is None or not math.isfinite(raw) or not 0.01 <= raw <= 0.99:
                logger.debug("[%s] market=%s side=%s skipped: %s raw_price=%s", self.name,
                             market_id, side, "inventory boundary" if raw is None else
                             "invalid/out-of-range model price", raw)
                continue
            price = _tick(raw, up=side == "SELL")
            if side == "BUY":
                price = min(price, _tick(best_ask, up=True) - .01)
            else:
                price = max(price, _tick(best_bid, up=False) + .01)
            price = round(price, 2)
            if not .01 <= price <= .99:
                logger.debug("[%s] market=%s side=%s skipped: passive price out of range; "
                             "raw_price=%.8f final_price=%.2f", self.name, market_id, side, raw, price)
                continue
            # Fixed lot size preserves the model's execution unit. Do not
            # inflate orders to satisfy min notional or silently oversell.
            if side == "SELL" and own + 1e-9 < lot:
                logger.debug("[%s] market=%s side=SELL skipped: insufficient shares; own=%.6f lot=%.6f",
                             self.name, market_id, own, lot)
                continue
            if lot * price < settings.mm_min_order_value_usdt or lot * price > settings.max_order_usdt:
                logger.debug("[%s] market=%s side=%s skipped: notional=%.6f min=%.6f max=%.6f",
                             self.name, market_id, side, lot * price,
                             settings.mm_min_order_value_usdt, settings.max_order_usdt)
                continue
            logger.debug("[%s] market=%s side=%s intent generated: raw_price=%.8f "
                         "final_price=%.2f quantity=%.6f notional=%.6f best_bid=%.6f best_ask=%.6f",
                         self.name, market_id, side, raw, price, lot, lot * price, best_bid, best_ask)
            intents.append(TradeIntent(market_id, selected.name, side, "LIMIT", lot,
                                       price, selected.on_chain_id, reason))
        return intents
