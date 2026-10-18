import time
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, Mock, patch

from config.settings import Settings
from core.http_client import PredictHttpClient
from core.order_manager import OrderManager
from core.position_tracker import PositionTracker
from core.types import MarketOutcome, MarketSummary, OrderbookSnapshot
from strategy.inventory_market_maker import InventoryMarketMakerStrategy


class InventoryStrategyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = Settings(_env_file=None, DRY_RUN=False, MM_MIN_LEVEL_SIZE=0,
                                 MM_QUOTE_SIZE=8, MM_MIN_ORDER_VALUE_USDT=.1)
        patcher = patch("strategy.inventory_market_maker.settings", self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.market = MarketSummary(1, "", "", "OPEN", "", 0, False, False,
            [MarketOutcome("Down", 2, "no"), MarketOutcome("Up", 1, "yes")])
        self.data = Mock(get_market=Mock(return_value=self.market), get_orderbook=AsyncMock())
        self.positions = Mock(force_reconcile=AsyncMock(return_value=True),
                              get_net_shares=AsyncMock(side_effect=lambda mid, tid: self.holdings[tid]))
        self.holdings = {"yes": 16, "no": 8}
        self.orders = Mock(cancel_by_hashes=AsyncMock(return_value=True))
        self.strategy = InventoryMarketMakerStrategy(self.data, [1], self.positions, self.orders)

    async def quote(self, bid=.59, ask=.61, age=0):
        self.strategy._last_quote_ts = 0
        self.data.get_orderbook.return_value = OrderbookSnapshot(
            1, [(ask, 100)], [(bid, 100)], int((time.time() - age) * 1000))
        return await self.strategy.on_tick()

    async def test_quotes_net_inventory_and_limits_to_owned_shares(self):
        intents = await self.quote()
        self.assertEqual([i.side for i in intents], ["SELL"])
        self.assertTrue(all(i.token_id == "yes" and "net_shares=8.000000" in i.reason for i in intents))
        self.assertTrue(all(i.quantity == 8 for i in intents))
        self.assertGreater(intents[0].price_per_share, .59)

    async def test_auto_keeps_old_exposure_after_reversal(self):
        intents = await self.quote(.39, .41)
        self.assertTrue(intents)
        self.assertTrue(all(i.token_id == "yes" for i in intents))
        self.assertIn("SELL", [i.side for i in intents])

    async def test_complement_book_and_opposite_inventory(self):
        self.holdings = {"yes": 8, "no": 16}
        intents = await self.quote(.29, .31)
        self.assertTrue(all(i.token_id == "no" for i in intents))
        self.assertTrue(all(.65 < i.price_per_share < .75 for i in intents))

    async def test_inventory_bound_blocks_buy_before_overshoot(self):
        self.holdings = {"yes": 45, "no": 0}
        self.assertEqual([i.side for i in await self.quote()], ["SELL"])

    async def test_no_short_sales_or_min_notional_inflation(self):
        self.holdings = {"yes": 1, "no": 0}
        intents = await self.quote()
        self.assertEqual([i.side for i in intents], ["SELL"])
        self.assertEqual(intents[0].quantity, 1)
        self.settings.mm_min_order_value_usdt = 100
        self.assertEqual(await self.quote(), [])

    async def test_flat_required_before_buy_and_fixed_direction_can_exit(self):
        self.settings.mm_outcome = 'YES'
        for holdings in ({'yes': 8, 'no': 8}, {'yes': 0, 'no': 8}, {'yes': 1, 'no': 0}):
            self.holdings = holdings
            self.assertEqual([i.side for i in await self.quote()], ['SELL'])
        self.holdings = {'yes': 0, 'no': 0}
        self.assertEqual([i.side for i in await self.quote()], ['BUY'])
        self.holdings = {'yes': .000001, 'no': 0}
        self.assertEqual(await self.quote(), [])

    async def test_cancel_before_position_read_and_failure_retry(self):
        self.strategy.record_quote("old")
        sequence = []
        async def cancel(hashes, **kwargs):
            sequence.append(("cancel", list(hashes)))
            return False
        self.orders.cancel_by_hashes.side_effect = cancel
        self.assertEqual(await self.quote(), [])
        self.positions.force_reconcile.assert_not_awaited()
        self.assertEqual(self.strategy._quote_hashes, ["old"])
        async def succeed(hashes, **kwargs):
            sequence.append(("cancel", list(hashes)))
            return True
        self.orders.cancel_by_hashes.side_effect = succeed
        self.assertTrue(await self.quote())
        self.assertEqual(sequence, [("cancel", ["old"]), ("cancel", ["old"])])

    async def test_reconcile_failure_suppresses_quotes(self):
        self.positions.force_reconcile.return_value = False
        self.assertEqual(await self.quote(), [])
        self.data.get_orderbook.assert_not_awaited()

    async def test_bad_or_stale_market_cancels_then_skips(self):
        for bid, ask, age in [(.5, .5, 0), (.61, .59, 0), (float("nan"), .6, 0), (.59, .61, 20)]:
            self.strategy.record_quote("old")
            self.assertEqual(await self.quote(bid, ask, age), [])
            self.assertEqual(self.strategy._quote_hashes, [])

    async def test_closed_market_and_end_time(self):
        self.market.trading_status = "CLOSED"
        self.assertEqual(await self.quote(), [])
        self.market.trading_status = "OPEN"
        self.settings.mm_market_end_times = {1: datetime.now(timezone.utc)}
        self.assertEqual(await self.quote(), [])

    async def test_refresh_gate_and_dry_run(self):
        self.settings.dry_run = True
        intents = await self.quote()
        self.assertEqual([i.side for i in intents], ["BUY"])
        self.positions.force_reconcile.assert_not_awaited()
        self.positions.get_net_shares.assert_not_awaited()
        self.assertEqual(await self.strategy.on_tick(), [])

    async def test_as_can_be_selected(self):
        self.settings.mm_inventory_model = "as"
        self.assertTrue(all("model=as" in i.reason for i in await self.quote()))

    async def test_stop_confirms_cancellation(self):
        self.strategy.record_quote("old")
        captured = []
        async def cancel(hashes, **kwargs):
            captured.extend(hashes)
            self.assertTrue(kwargs["verify"])
            return True
        self.orders.cancel_by_hashes.side_effect = cancel
        await self.strategy.on_stop()
        self.assertEqual(captured, ["old"])
        self.assertEqual(self.strategy._quote_hashes, [])


class InventoryPlumbingTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_position_snapshot_keeps_old_cache_and_reports_failure(self):
        http = Mock(get_all_positions=AsyncMock())
        tracker = PositionTracker(http)
        tracker._cache = {(1, "yes"): Decimal(10)}
        for row in [None, {}, {"marketId": 1, "outcome": {"onChainId": "yes"}, "amount": "NaN"},
                    {"marketId": 1, "outcome": {"onChainId": "yes"}, "amount": "garbage"}]:
            http.get_all_positions.return_value = [row]
            self.assertFalse(await tracker.force_reconcile())
            self.assertEqual(tracker._cache[(1, "yes")], 10)

    async def test_cancellation_requires_confirmed_absence(self):
        cfg = Settings(_env_file=None, DRY_RUN=False)
        http = Mock(remove_orders_by_hash=AsyncMock(return_value={"success": True}),
                    get_open_order_hashes=AsyncMock(return_value={"old"}))
        manager = OrderManager(http, Mock(), Mock())
        with patch("core.order_manager.settings", cfg):
            self.assertFalse(await manager.cancel_by_hashes(["old"], verify=True))
            http.get_open_order_hashes.return_value = set()
            self.assertTrue(await manager.cancel_by_hashes(["old"], verify=True))
            http.remove_orders_by_hash.return_value = {"success": False}
            self.assertFalse(await manager.cancel_by_hashes(["old"], verify=True))

    async def test_open_hashes_traverse_pages(self):
        http = PredictHttpClient.__new__(PredictHttpClient)
        http.get_orders = AsyncMock(side_effect=[
            {"data": [{"status": "OPEN", "order": {"hash": "a"}}], "cursor": "next"},
            {"data": [{"status": "OPEN", "order": {"hash": "b"}}]}])
        self.assertEqual(await http.get_open_order_hashes(), {"a", "b"})

    def test_factory_registers_strategy_and_dates_require_timezone(self):
        from bot import PredictBot
        bot = PredictBot.__new__(PredictBot)
        bot._market_data, bot._position_tracker, bot._order_manager = Mock(), Mock(), Mock()
        cfg = Settings(_env_file=None, STRATEGY_MODE="inventory_market_maker")
        with patch("bot.settings", cfg):
            self.assertIsInstance(bot._build_strategies([1])[0], InventoryMarketMakerStrategy)
        with self.assertRaises(ValueError):
            Settings(_env_file=None, MM_MARKET_END_TIMES={1: "2026-10-19T00:00:00"})


if __name__ == "__main__":
    unittest.main()
