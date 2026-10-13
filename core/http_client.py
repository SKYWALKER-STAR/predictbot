from __future__ import annotations

import logging
from typing import Any

from config.settings import settings
from core.auth import PredictAuthService
from core.http_factory import build_async_client
from core.types import MarketOutcome, MarketSummary, OrderbookSnapshot

logger = logging.getLogger(__name__)


class PredictHttpClient:
    def __init__(self, auth: PredictAuthService) -> None:
        self._auth = auth
        self._client = build_async_client(base_url=settings.api_base_url, timeout_seconds=15.0)

    async def close(self) -> None:
        await self._client.aclose()

    def _headers(self, *, with_jwt: bool = False) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if settings.predict_api_key:
            headers["x-api-key"] = settings.predict_api_key
        if with_jwt and self._auth.jwt:
            headers["Authorization"] = f"Bearer {self._auth.jwt}"
        return headers

    async def get_markets(self, *, status: str = "OPEN", first: int = 100) -> list[MarketSummary]:
        resp = await self._client.get(
            "/v1/markets",
            headers=self._headers(),
            params={"status": status, "first": str(first)},
        )
        resp.raise_for_status()
        payload = resp.json()
        rows = payload.get("data", [])
        return [self._parse_market(row) for row in rows]

    async def get_orderbook(self, market_id: int) -> OrderbookSnapshot:
        resp = await self._client.get(f"/v1/markets/{market_id}/orderbook", headers=self._headers())
        resp.raise_for_status()
        data = resp.json().get("data", {})
        asks = [(float(level[0]), float(level[1])) for level in data.get("asks", [])]
        bids = [(float(level[0]), float(level[1])) for level in data.get("bids", [])]
        return OrderbookSnapshot(
            market_id=int(data.get("marketId", market_id)),
            asks=asks,
            bids=bids,
            update_timestamp_ms=int(data.get("updateTimestampMs", 0)),
        )

    async def create_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = self._headers(with_jwt=True)
        resp = await self._client.post("/v1/orders", headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()

    async def remove_orders_by_hash(self, order_hashes: list[str]) -> dict[str, Any]:
        headers = self._headers(with_jwt=True)
        body = {"data": {"hashes": order_hashes}}
        resp = await self._client.post("/orders/remove-by-hash", headers=headers, json=body)
        resp.raise_for_status()
        return resp.json()

    async def get_orders(self, first: int = 100) -> dict[str, Any]:
        resp = await self._client.get("/v1/orders", headers=self._headers(with_jwt=True), params={"first": first})
        resp.raise_for_status()
        return resp.json()

    async def get_positions(self, first: int = 100, include_lost: bool = False) -> dict[str, Any]:
        resp = await self._client.get(
            "/v1/positions",
            headers=self._headers(with_jwt=True),
            params={"first": first, "includeLost": str(include_lost).lower()},
        )
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _parse_market(row: dict[str, Any]) -> MarketSummary:
        outcomes: list[MarketOutcome] = []
        for item in row.get("outcomes", []):
            best_bid = item.get("bestBid")
            best_ask = item.get("bestAsk")
            outcomes.append(
                MarketOutcome(
                    name=str(item.get("name", "")),
                    index_set=int(item.get("indexSet", 0)),
                    on_chain_id=str(item.get("onChainId", "")),
                    best_bid_price=(float(best_bid.get("price")) if isinstance(best_bid, dict) and best_bid.get("price") is not None else None),
                    best_bid_size=(float(best_bid.get("size")) if isinstance(best_bid, dict) and best_bid.get("size") is not None else None),
                    best_ask_price=(float(best_ask.get("price")) if isinstance(best_ask, dict) and best_ask.get("price") is not None else None),
                    best_ask_size=(float(best_ask.get("size")) if isinstance(best_ask, dict) and best_ask.get("size") is not None else None),
                )
            )

        return MarketSummary(
            market_id=int(row.get("id")),
            title=str(row.get("title", "")),
            question=str(row.get("question", "")),
            trading_status=str(row.get("tradingStatus", "")),
            status=str(row.get("status", "")),
            fee_rate_bps=int(row.get("feeRateBps", 0)),
            is_neg_risk=bool(row.get("isNegRisk", False)),
            is_yield_bearing=bool(row.get("isYieldBearing", False)),
            outcomes=outcomes,
        )
