from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from predict_sdk import ChainId, OrderBuilder, OrderBuilderOptions

from config.settings import settings
from core.auth import PredictAuthService
from core.http_factory import build_async_client
from core.types import MarketOutcome, MarketSummary, OrderbookSnapshot

logger = logging.getLogger(__name__)


class PredictHttpClient:
    def __init__(self, auth: PredictAuthService) -> None:
        self._auth = auth
        self._client = build_async_client(base_url=settings.api_base_url, timeout_seconds=15.0)
        self._balance_builder: OrderBuilder | None = None
        self._balance_builder_error = False

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

    async def get_orders(self, first: int = 100, after: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"first": first}
        if after:
            params["after"] = after
        resp = await self._client.get("/v1/orders", headers=self._headers(with_jwt=True), params=params)
        resp.raise_for_status()
        return resp.json()

    async def get_open_buy_reserved_usdt(self, first: int = 200) -> Decimal:
        rows = await self._collect_pages(self.get_orders, first=first)
        total = Decimal("0")
        for row in rows:
            if not isinstance(row.get("status"), str):
                raise ValueError("Missing order status")
            if str(row.get("status", "")).upper() != "OPEN":
                continue
            order = row.get("order")
            if not isinstance(order, dict):
                raise ValueError("Missing open order data")
            if str(order.get("side")) not in {"0", "1"}:
                raise ValueError("Invalid open order side")
            if int(order["side"]) != 0:
                continue
            maker_amount_wei = order.get("makerAmount")
            # Conservatively reserve the full original amount for partial fills.
            total += self._nonnegative_decimal(maker_amount_wei, "makerAmount") / Decimal(10**18)
        return total

    async def get_open_order_hashes(self) -> set[str]:
        """Full paginated snapshot used to confirm quote cancellation."""
        hashes: set[str] = set()
        for row in await self._collect_pages(self.get_orders, first=200):
            status = row.get("status")
            if not isinstance(status, str):
                raise ValueError("Missing order status during cancellation verification")
            if status.upper() != "OPEN":
                continue
            order = row.get("order")
            if not isinstance(order, dict) or not order.get("hash"):
                raise ValueError("Missing open order hash")
            hashes.add(str(order["hash"]))
        return hashes

    async def get_wallet_usdt_balance(self) -> Decimal | None:
        builder = self._get_balance_builder()
        if builder is None:
            return None
        try:
            wei = await builder.balance_of_async("USDT")
            return Decimal(int(wei)) / Decimal(10**18)
        except Exception as exc:
            logger.warning("failed to query USDT balance via SDK: %s", exc)
            return None

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

    async def get_positions(self, first: int = 100, include_lost: bool = False, market_id: int | None = None, after: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"first": first, "includeLost": str(include_lost).lower()}
        if market_id is not None:
            params["marketId"] = market_id
        if after:
            params["after"] = after
        resp = await self._client.get(
            "/v1/positions",
            headers=self._headers(with_jwt=True),
            params=params,
        )
        resp.raise_for_status()
        return resp.json()

    async def _collect_pages(self, fetch, **kwargs) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        after = None
        for _ in range(1000):
            payload = await fetch(after=after, **kwargs)
            if not isinstance(payload, dict) or payload.get("success") is False:
                raise ValueError("Unsuccessful paginated response")
            page = payload.get("data")
            if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
                raise ValueError("Invalid paginated response data")
            rows.extend(page)
            cursor = payload.get("cursor")
            if not cursor:
                return rows
            if not isinstance(cursor, str) or cursor in seen:
                raise ValueError("Invalid or repeated pagination cursor")
            seen.add(cursor)
            after = cursor
        raise ValueError("Pagination limit exceeded")

    async def get_all_positions(self, market_id: int | None = None) -> list[dict[str, Any]]:
        return await self._collect_pages(
            self.get_positions, first=200, include_lost=False, market_id=market_id,
        )

    async def get_market_holdings(self, market_id: int) -> dict[str, Decimal]:
        holdings: dict[str, Decimal] = {}
        for row in await self.get_all_positions(market_id=market_id):
            row_market = row.get("marketId", (row.get("market") or {}).get("id"))
            if row_market is None or int(row_market) != market_id:
                raise ValueError("Unexpected position market")
            token = (row.get("outcome") or {}).get("onChainId")
            if not token:
                raise ValueError("Missing position token")
            self._nonnegative_decimal(row.get("amount"), "position amount")
            amount = self._parse_position_amount(row["amount"])
            holdings[str(token)] = holdings.get(str(token), Decimal(0)) + amount
        return holdings

    async def get_market_buy_hashes(self, market_id: int) -> list[str]:
        hashes = []
        for row in await self._collect_pages(self.get_orders, first=200):
            if not isinstance(row.get("status"), str):
                raise ValueError("Missing order status")
            if row["status"].upper() != "OPEN":
                continue
            row_market = row.get("marketId", (row.get("market") or {}).get("id"))
            if row_market is None:
                raise ValueError("Missing open order market")
            if int(row_market) != market_id:
                continue
            order = row.get("order") or {}
            if str(order.get("side")) not in {"0", "1"}:
                raise ValueError("Invalid order side")
            if str(order["side"]) == "0":
                if not order.get("hash"):
                    raise ValueError("Missing BUY hash")
                hashes.append(str(order["hash"]))
        return hashes

    @staticmethod
    def _nonnegative_decimal(raw: Any, field: str) -> Decimal:
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            raise ValueError(f"Missing or invalid {field}") from None
        if not value.is_finite() or value < 0:
            raise ValueError(f"Invalid {field}")
        return value

    async def get_position_cost_usdt(self) -> Decimal:
        """Fresh account-wide remaining cost; never substitute stale/zero data.

        Integer amount strings are wei shares, decimal amounts are shares (as
        in the existing position parser). averageBuyPriceUsd is decimal USD per
        share, NOT wei. API average-price accounting is used without adding fees.
        """
        total = Decimal("0")
        for row in await self.get_all_positions():
            raw = row.get("amount")
            amount = self._nonnegative_decimal(raw, "position amount")
            if "." not in str(raw):
                amount /= Decimal(10**18)
            if amount == 0:
                continue
            price = self._nonnegative_decimal(row.get("averageBuyPriceUsd"), "averageBuyPriceUsd")
            if price > 1:
                raise ValueError("averageBuyPriceUsd exceeds 1; verify API units")
            total += amount * price
        return total

    async def get_outcome_shares(self, token_id: str, market_id: int | None = None) -> Decimal:
        rows = await self.get_all_positions(market_id=market_id)
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
    def _parse_wei_to_decimal(raw: Any) -> Decimal:
        if raw is None:
            return Decimal("0")
        text = str(raw).strip()
        if not text:
            return Decimal("0")
        try:
            value = Decimal(text)
        except (InvalidOperation, ValueError):
            return Decimal("0")
        return value / Decimal(10**18)

    def _get_balance_builder(self) -> OrderBuilder | None:
        if self._balance_builder_error:
            return None
        if self._balance_builder is not None:
            return self._balance_builder
        if not settings.predict_wallet_private_key:
            self._balance_builder_error = True
            return None
        try:
            chain_id = ChainId.BNB_MAINNET if settings.predict_env == "prod" else ChainId.BNB_TESTNET
            options = (
                OrderBuilderOptions(predict_account=settings.predict_account_address)
                if settings.predict_account_address
                else None
            )
            self._balance_builder = OrderBuilder.make(chain_id, settings.predict_wallet_private_key, options)
            return self._balance_builder
        except Exception as exc:
            self._balance_builder_error = True
            logger.warning("failed to initialize balance builder: %s", exc)
            return None

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
