from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

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
        logger.debug("Fetched markets: %s", rows)
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

    async def get_market_by_id(self, market_id: int) -> MarketSummary | None:
        resp = await self._client.get(f"/v1/markets/{market_id}", headers=self._headers())
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        payload = resp.json()
        row = payload.get("data")
        if not isinstance(row, dict):
            return None
        return self._parse_market(row)

    async def create_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = self._headers(with_jwt=True)
        resp = await self._client.post("/v1/orders", headers=headers, json=payload)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail: str
            try:
                detail = json.dumps(resp.json(), ensure_ascii=True)
            except Exception:
                detail = resp.text
            logger.error("create_order failed status=%s detail=%s", resp.status_code, detail)
            raise RuntimeError(f"create_order failed: status={resp.status_code} detail={detail}") from exc
        return resp.json()

    async def remove_orders_by_hash(self, order_hashes: list[str]) -> dict[str, Any]:
        headers = self._headers(with_jwt=True)
        body = {"data": {"hashes": order_hashes}}
        resp = await self._client.post("/v1/orders/remove-by-hash", headers=headers, json=body)
        resp.raise_for_status()
        return resp.json()

    async def get_orders(self, first: int = 100) -> dict[str, Any]:
        resp = await self._client.get("/v1/orders", headers=self._headers(with_jwt=True), params={"first": first})
        resp.raise_for_status()
        return resp.json()

    async def has_open_order(self, *, market_id: int, token_id: str, side: int) -> bool:
        payload = await self.get_orders(first=200)
        for row in payload.get("data", []):
            if not isinstance(row, dict):
                continue
            if str(row.get("status", "")).upper() != "OPEN":
                continue
            if int(row.get("marketId", -1)) != int(market_id):
                continue
            order = row.get("order")
            if not isinstance(order, dict):
                continue
            if str(order.get("tokenId", "")) != str(token_id):
                continue
            if int(order.get("side", -1)) != int(side):
                continue
            return True
        return False

    async def get_positions(self, first: int = 100, include_lost: bool = False, market_id: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"first": first, "includeLost": str(include_lost).lower()}
        if market_id is not None:
            params["marketId"] = market_id
        resp = await self._client.get(
            "/v1/positions",
            headers=self._headers(with_jwt=True),
            params=params,
        )
        resp.raise_for_status()
        return resp.json()

    async def get_outcome_shares(self, token_id: str, market_id: int | None = None) -> Decimal:
        payload = await self.get_positions(first=200, include_lost=False, market_id=market_id)
        rows = payload.get("data", [])
        total = Decimal("0")
        for row in rows:
            if not isinstance(row, dict):
                continue
            outcome = row.get("outcome")
            if not isinstance(outcome, dict):
                continue
            if str(outcome.get("onChainId", "")) != str(token_id):
                continue
            total += self._parse_position_amount(row.get("amount"))
        return total

    @staticmethod
    def _parse_position_amount(raw: Any) -> Decimal:
        if raw is None:
            return Decimal("0")
        text = str(raw).strip()
        if not text:
            return Decimal("0")
        try:
            amount = Decimal(text)
        except (InvalidOperation, ValueError):
            return Decimal("0")
        # API schema marks `amount` as bigint string (wei shares).
        if "." not in text:
            return amount / Decimal(10**18)
        return amount

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
