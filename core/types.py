from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class MarketOutcome:
    name: str
    index_set: int
    on_chain_id: str
    best_bid_price: float | None = None
    best_bid_size: float | None = None
    best_ask_price: float | None = None
    best_ask_size: float | None = None


@dataclass
class MarketSummary:
    market_id: int
    title: str
    question: str
    trading_status: str
    status: str
    fee_rate_bps: int
    is_neg_risk: bool
    is_yield_bearing: bool
    outcomes: list[MarketOutcome] = field(default_factory=list)


@dataclass
class OrderbookSnapshot:
    market_id: int
    asks: list[tuple[float, float]]
    bids: list[tuple[float, float]]
    update_timestamp_ms: int

    @property
    def best_ask(self) -> tuple[float, float] | None:
        return self.asks[0] if self.asks else None

    @property
    def best_bid(self) -> tuple[float, float] | None:
        return self.bids[0] if self.bids else None

    @property
    def spread(self) -> float | None:
        if not self.best_ask or not self.best_bid:
            return None
        return self.best_ask[0] - self.best_bid[0]


@dataclass
class TradeIntent:
    market_id: int
    outcome: str
    side: str
    strategy: str
    quantity: float
    price_per_share: float
    token_id: str
    reason: str = ""


@dataclass
class PlaceOrderResult:
    success: bool
    local_id: int | None
    order_hash: str | None
    exchange_order_id: str | None
    is_dry_run: bool
    error: str | None = None


@dataclass
class WalletEvent:
    event_type: str
    order_id: str | None
    order_hash: str | None
    timestamp_ms: int | None
    payload: dict[str, Any]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
