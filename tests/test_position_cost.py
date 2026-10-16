import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, Mock, patch

from config.settings import Settings
from core.http_client import PredictHttpClient
from core.order_manager import OrderManager
from core.position_tracker import PositionTracker
from core.types import TradeIntent


class PositionCostTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.http = PredictHttpClient.__new__(PredictHttpClient)

    async def test_cost_across_pages_and_units(self):
        self.http.get_positions = AsyncMock(side_effect=[
            {'data': [{'amount': '10000000000000000000', 'averageBuyPriceUsd': '0.6'}], 'cursor': 'next'},
            {'data': [{'amount': '5.0', 'averageBuyPriceUsd': '0.8'}], 'cursor': None},
        ])
        self.assertEqual(await self.http.get_position_cost_usdt(), Decimal('10'))
        self.assertEqual(self.http.get_positions.await_args_list[1].kwargs['after'], 'next')

    async def test_missing_invalid_cost_fails_closed(self):
        for price in [None, '', 'NaN', 'Infinity', '-0.1', '600000000000000000']:
            with self.subTest(price=price):
                self.http.get_positions = AsyncMock(return_value={
                    'data': [{'amount': '10.0', 'averageBuyPriceUsd': price}]})
                with self.assertRaises(ValueError):
                    await self.http.get_position_cost_usdt()

    async def test_empty_and_zero_positions(self):
        for rows in [[], [{'amount': '0'}]]:
            self.http.get_positions = AsyncMock(return_value={'data': rows})
            self.assertEqual(await self.http.get_position_cost_usdt(), 0)

    async def test_bad_pagination_fails_closed(self):
        for payload in [{'success': False, 'data': []}, {}, {'data': [None]}]:
            self.http.get_positions = AsyncMock(return_value=payload)
            with self.assertRaises(ValueError):
                await self.http.get_all_positions()
        self.http.get_positions = AsyncMock(return_value={'data': [], 'cursor': 'same'})
        with self.assertRaises(ValueError):
            await self.http.get_all_positions()

    async def test_reserved_buy_pagination_and_full_partial_order_amount(self):
        def order(side, amount, status='OPEN'):
            return {'status': status, 'order': {'side': side, 'makerAmount': amount}}
        self.http.get_orders = AsyncMock(side_effect=[
            {'data': [order(0, str(10 * 10**18)), order(1, str(20 * 10**18))], 'cursor': 'next'},
            {'data': [order(0, str(5 * 10**18)), order(0, 'bad', 'FILLED')]},
        ])
        self.assertEqual(await self.http.get_open_buy_reserved_usdt(), 15)

    async def test_tracker_supports_both_market_id_shapes(self):
        self.http.get_all_positions = AsyncMock(return_value=[
            {'market': {'id': 1}, 'outcome': {'onChainId': 'a'}, 'amount': '2.0'},
            {'marketId': 2, 'outcome': {'onChainId': 'b'}, 'amount': '3.0'},
        ])
        tracker = PositionTracker(self.http)
        tracker._sync_to_redis = AsyncMock()
        await tracker.force_reconcile()
        self.assertEqual(await tracker.get_net_shares(1, 'a'), 2)
        self.assertEqual(await tracker.get_net_shares(2, 'b'), 3)


class CostLimitOrderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = Settings(_env_file=None, DRY_RUN=False, MAX_POSITION_COST_RATIO=0.4)
        patcher = patch('core.order_manager.settings', self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.http = Mock(spec=PredictHttpClient)
        self.http._nonnegative_decimal = PredictHttpClient._nonnegative_decimal
        self.http.get_wallet_usdt_balance = AsyncMock(return_value=Decimal('700'))
        self.http.get_position_cost_usdt = AsyncMock(return_value=Decimal('300'))
        self.http.get_open_buy_reserved_usdt = AsyncMock(return_value=Decimal('90'))
        self.http.get_outcome_shares = AsyncMock(return_value=Decimal('100'))
        self.http.create_order = AsyncMock(return_value={'data': {'orderHash': 'hash'}})
        signer = Mock(sign_order=AsyncMock(return_value=Mock(
            order={'makerAmount': str(10 * 10**18)}, price_per_share='0.5')))
        self.manager = OrderManager(self.http, signer, Mock())
        self.manager._create_local = Mock(return_value=1)
        self.manager._update_local = Mock()

    async def submit(self, side='BUY'):
        intent = TradeIntent(1, 'YES', side, 'LIMIT', 20, 0.5, 'token')
        return await self.manager.place_order(intent, token_id='token', fee_rate_bps=0,
                                              is_neg_risk=False, is_yield_bearing=False)

    async def test_boundary_allowed_then_next_buy_blocked(self):
        self.assertTrue((await self.submit()).success)
        self.http.get_open_buy_reserved_usdt.return_value = Decimal('100')
        result = await self.submit()
        self.assertFalse(result.success)
        self.assertIn('position cost cap', result.error)
        self.assertEqual(self.http.create_order.await_count, 1)

    async def test_dynamic_balance_changes_limit(self):
        self.http.get_wallet_usdt_balance.return_value = Decimal('600')
        self.assertFalse((await self.submit()).success)
        self.http.get_wallet_usdt_balance.return_value = Decimal('800')
        self.assertTrue((await self.submit()).success)

    async def test_missing_balance_or_cost_error_blocks_buy(self):
        self.http.get_wallet_usdt_balance.return_value = None
        self.assertFalse((await self.submit()).success)
        self.http.get_wallet_usdt_balance.return_value = Decimal('700')
        self.http.get_position_cost_usdt.side_effect = ValueError('missing average price')
        self.assertFalse((await self.submit()).success)
        self.http.create_order.assert_not_awaited()

    async def test_sell_unaffected_by_cost_failure(self):
        self.http.get_position_cost_usdt.side_effect = ValueError('missing price')
        self.assertTrue((await self.submit('SELL')).success)
        self.http.get_position_cost_usdt.assert_not_awaited()

    async def test_zero_ratio_blocks_buys(self):
        self.settings.max_position_cost_ratio = 0
        self.assertFalse((await self.submit()).success)

    async def test_dry_run_does_not_query_account_or_submit(self):
        self.settings.dry_run = True
        self.assertTrue((await self.submit()).is_dry_run)
        self.http.get_position_cost_usdt.assert_not_awaited()
        self.http.create_order.assert_not_awaited()

    def test_ratio_validation(self):
        for value in [-0.1, 1.1, float('nan'), float('inf')]:
            with self.assertRaises(ValueError):
                Settings(_env_file=None, MAX_POSITION_COST_RATIO=value)


if __name__ == '__main__':
    unittest.main()
