import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, Mock, patch

from config.settings import Settings
from core.http_client import PredictHttpClient
from core.types import MarketOutcome, MarketSummary, OrderbookSnapshot
from strategy.maker_spread import WsSpreadStrategy


class SpreadExitTests(unittest.IsolatedAsyncioTestCase):
    async def test_opposite_position_exits_then_flat_can_buy(self):
        cfg = Settings(_env_file=None, DRY_RUN=False, STRATEGY_COOLDOWN_SECONDS=0,
                       MM_MIN_ORDER_VALUE_USDT=.1)
        market = MarketSummary(1, '', '', 'OPEN', '', 0, False, False,
                               [MarketOutcome('A', 1, 'a'), MarketOutcome('B', 2, 'b')])
        data = Mock(get_market=Mock(return_value=market), get_orderbook=AsyncMock(
            return_value=OrderbookSnapshot(1, [(.65, 10)], [(.55, 100)], 0)))
        holdings = {'a': 0, 'b': 2}
        positions = Mock(force_reconcile=AsyncMock(return_value=True),
                         get_net_shares=AsyncMock(side_effect=lambda market, token: holdings[token]))
        strategy = WsSpreadStrategy(data, [1], positions)
        with patch('strategy.maker_spread.settings', cfg):
            intents = await strategy.on_tick()
            self.assertEqual([(i.side, i.token_id, i.quantity) for i in intents], [('SELL', 'b', 2)])
            self.assertTrue(.35 <= intents[0].price_per_share <= .45)
            holdings['b'] = 0
            self.assertEqual([i.side for i in await strategy.on_tick()], ['BUY'])
            positions.force_reconcile.return_value = False
            self.assertEqual(await strategy.on_tick(), [])


class MarketSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_holdings_across_pages_and_units(self):
        http = PredictHttpClient.__new__(PredictHttpClient)
        http.get_positions = AsyncMock(side_effect=[
            {'data': [{'marketId': 1, 'outcome': {'onChainId': 'a'}, 'amount': '1000000000000000000'}], 'cursor': 'next'},
            {'data': [{'market': {'id': 1}, 'outcome': {'onChainId': 'b'}, 'amount': '0.5'}]}])
        self.assertEqual(await http.get_market_holdings(1), {'a': Decimal(1), 'b': Decimal('.5')})

    async def test_invalid_holdings_fail_closed(self):
        http = PredictHttpClient.__new__(PredictHttpClient)
        for row in [{'marketId': 2, 'outcome': {'onChainId': 'a'}, 'amount': '1.0'},
                    {'marketId': 1, 'outcome': {'onChainId': 'a'}, 'amount': 'NaN'},
                    {'marketId': 1, 'amount': '1.0'}]:
            http.get_all_positions = AsyncMock(return_value=[row])
            with self.assertRaises(ValueError):
                await http.get_market_holdings(1)

    async def test_market_buy_scan_across_pages(self):
        http = PredictHttpClient.__new__(PredictHttpClient)
        http.get_orders = AsyncMock(side_effect=[
            {'data': [{'marketId': 2, 'status': 'OPEN', 'order': {'side': 0, 'hash': 'other'}}], 'cursor': 'next'},
            {'data': [{'marketId': 1, 'status': 'OPEN', 'order': {'side': 0, 'hash': 'buy'}},
                      {'marketId': 1, 'status': 'OPEN', 'order': {'side': 1, 'hash': 'sell'}}]}])
        self.assertEqual(await http.get_market_buy_hashes(1), ['buy'])
