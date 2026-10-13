from __future__ import annotations

import logging

from core.http_client import PredictHttpClient
from core.types import MarketSummary, OrderbookSnapshot
from core.ws_feed import PredictWsFeed

logger = logging.getLogger(__name__)


class MarketDataService:
    def __init__(self, http_client: PredictHttpClient, ws_feed: PredictWsFeed) -> None:
        self._http = http_client
        self._ws = ws_feed
        self._market_cache: dict[int, MarketSummary] = {}

    async def refresh_open_markets(self) -> list[MarketSummary]:
        markets = await self._http.get_markets(status="OPEN", first=100)
        self._market_cache.update({m.market_id: m for m in markets})
        return markets

    async def refresh_markets_for_ids(self, market_ids: list[int]) -> list[MarketSummary]:
        refreshed: list[MarketSummary] = []
        for market_id in market_ids:
            market = await self._http.get_market_by_id(market_id)
            if market is None:
                continue
            self._market_cache[market.market_id] = market
            refreshed.append(market)
        return refreshed

    def get_market(self, market_id: int) -> MarketSummary | None:
        return self._market_cache.get(market_id)

    async def get_orderbook(self, market_id: int, *, prefer_ws: bool = True) -> OrderbookSnapshot:
        if prefer_ws:
            ws_data = self._ws.get_orderbook(market_id)
            if ws_data is not None:
                return ws_data
        return await self._http.get_orderbook(market_id)
