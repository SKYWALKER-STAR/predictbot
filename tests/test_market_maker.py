import unittest
from unittest.mock import AsyncMock, Mock, patch

from config.settings import Settings
from core.types import MarketOutcome, MarketSummary, OrderbookSnapshot
from strategy.market_maker import BasicMarketMakerStrategy
from bot import PredictBot


class MarketMakerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = Settings(_env_file=None, MM_OUTCOME='AUTO', MM_MIN_PROBABILITY=0.55,
                                 MM_MIN_LEVEL_SIZE=0, MM_MIN_SPREAD_BPS=0)
        self.patch = patch('strategy.market_maker.settings', self.settings)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.market = MarketSummary(1, '', '', 'OPEN', '', 0, False, False,
                                   [MarketOutcome('KC', 2, 'kc'), MarketOutcome('C9', 1, 'c9')])
        self.data = Mock()
        self.data.get_market.return_value = self.market
        self.data.get_orderbook = AsyncMock()
        self.positions = Mock(get_net_shares=AsyncMock(return_value=0))
        self.orders = Mock(cancel_by_hashes=AsyncMock(return_value=True))
        self.strategy = BasicMarketMakerStrategy(self.data, [1], self.positions, self.orders)

    async def quote(self, bid, ask):
        self.strategy._last_quote_ts = 0
        self.data.get_orderbook.return_value = OrderbookSnapshot(1, [(ask, 100)], [(bid, 100)], 0)
        return await self.strategy.on_tick()

    async def test_auto_selects_actual_complement_even_with_reordered_outcomes(self):
        intents = await self.quote(.30, .31)
        self.assertEqual([i.outcome for i in intents], ['KC', 'KC'])
        self.assertTrue(all(.68 <= i.price_per_share <= .71 for i in intents))
        self.positions.get_net_shares.assert_awaited_once_with(1, 'kc')
        self.assertEqual(PredictBot._resolve_token_id(self.market, intents[0]), 'kc')
        intents[0].token_id = 'unknown'
        self.assertEqual(PredictBot._resolve_token_id(self.market, intents[0]), '')

    async def test_dynamic_names_ids_and_metadata_refresh(self):
        for name, token in [('Team B', '123'), ('下跌', '456'), ('Some long outcome name', '789')]:
            self.market.outcomes[0].name = name
            self.market.outcomes[0].on_chain_id = token
            intents = await self.quote(.30, .31)
            self.assertEqual([i.token_id for i in intents], [token, token])
            self.assertEqual(PredictBot._resolve_token_id(self.market, intents[0]), token)
            self.assertEqual(intents[0].outcome, name)
        self.market.outcomes[0].name = 'Renamed'
        self.assertEqual(PredictBot._resolve_token_id(self.market, intents[0]), '789')
        self.assertEqual(intents[0].outcome, 'Renamed')
        self.market.outcomes[0].on_chain_id = 'new-token'
        self.assertEqual(PredictBot._resolve_token_id(self.market, intents[0]), '')

    async def test_duplicate_names_do_not_change_token_selection(self):
        for outcome in self.market.outcomes:
            outcome.name = 'Same name'
        intents = await self.quote(.30, .31)
        self.assertEqual(PredictBot._resolve_token_id(self.market, intents[0]), 'kc')
        intents[0].market_id = 999
        self.assertEqual(PredictBot._resolve_token_id(self.market, intents[0]), '')

    async def test_auto_first_direction(self):
        intents = await self.quote(.69, .70)
        self.assertEqual([i.outcome for i in intents], ['C9', 'C9'])

    async def test_threshold_and_uncertain_market(self):
        for bid, ask in [(.54, .56), (.44, .46), (.49, .51)]:
            self.assertEqual(await self.quote(bid, ask), [])

    async def test_fixed_direction_filtered_and_no_price_mirrored(self):
        self.settings.mm_outcome = 'YES'
        self.assertEqual(await self.quote(.30, .31), [])
        for outcome in ['NO', 'KC']:
            self.settings.mm_outcome = outcome
            intents = await self.quote(.30, .31)
            self.assertEqual(intents[0].outcome, 'KC')
            self.assertGreater(intents[0].price_per_share, .65)
        self.settings.mm_outcome = 'typo'
        self.assertEqual(await self.quote(.30, .31), [])

    async def test_cancel_old_quotes_when_threshold_fails(self):
        self.strategy.record_quote('old')
        calls = []
        async def cancel(hashes):
            calls.append(list(hashes))
            return True
        self.orders.cancel_by_hashes.side_effect = cancel
        self.assertEqual(await self.quote(.49, .51), [])
        self.assertEqual(calls, [['old']])
        self.assertEqual(self.strategy._quote_hashes, [])

    async def test_cancel_failure_blocks_new_quotes_and_retries(self):
        self.strategy.record_quote('old')
        self.orders.cancel_by_hashes.return_value = False
        self.assertEqual(await self.quote(.30, .31), [])
        self.assertEqual(self.strategy._quote_hashes, ['old'])
        self.orders.cancel_by_hashes.return_value = True
        self.assertEqual(len(await self.quote(.30, .31)), 2)

    async def test_stop_cancels_quotes(self):
        self.strategy.record_quote('old')
        await self.strategy.on_stop()
        self.orders.cancel_by_hashes.assert_awaited_once_with(['old'])


if __name__ == '__main__':
    unittest.main()
