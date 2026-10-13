from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from config.settings import settings
from core.types import OrderbookSnapshot, WalletEvent

logger = logging.getLogger(__name__)


class PredictWsFeed:
    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._subscriptions: set[str] = set()
        self._request_id = 1
        self._ready = asyncio.Event()

        self._orderbooks: dict[int, OrderbookSnapshot] = {}
        self._trading_status: dict[int, str] = {}
        self._wallet_events: list[WalletEvent] = []
        self._ws: Any = None

    def stop(self) -> None:
        self._stop.set()

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    def get_orderbook(self, market_id: int) -> OrderbookSnapshot | None:
        return self._orderbooks.get(market_id)

    def latest_wallet_events(self, limit: int = 50) -> list[WalletEvent]:
        return self._wallet_events[-limit:]

    async def subscribe_market_topics(self, market_ids: Iterable[int]) -> None:
        topics = set()
        for mid in market_ids:
            topics.add(f"predictOrderbook/{mid}")
            topics.add(f"predictTradingStatus/{mid}")
            topics.add(f"predictMarketStatus/{mid}")

        new_topics = topics - self._subscriptions
        self._subscriptions.update(new_topics)
        if self._ws is not None:
            for topic in new_topics:
                await self._send_subscribe(topic)

    async def subscribe_wallet_events(self, jwt: str) -> None:
        topic = f"predictWalletEvents/{jwt}"
        if topic in self._subscriptions:
            return
        self._subscriptions.add(topic)
        if self._ws is not None:
            await self._send_subscribe(topic)

    async def run(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            try:
                await self._connect_and_loop()
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("WS disconnected: %s", exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)

    async def _connect_and_loop(self) -> None:
        headers: dict[str, str] = {}
        if settings.predict_api_key:
            headers["x-api-key"] = settings.predict_api_key

        async with websockets.connect(settings.ws_url, additional_headers=headers, ping_interval=None) as ws:
            self._ws = ws
            logger.info("WS connected: %s", settings.ws_url)
            for topic in sorted(self._subscriptions):
                await self._send_subscribe(topic)

            async for raw in ws:
                if self._stop.is_set():
                    break
                await self._on_message(raw)

    async def _send_subscribe(self, topic: str) -> None:
        await self._send({"method": "subscribe", "requestId": self._request_id, "params": [topic]})
        self._request_id += 1

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._ws is None:
            return
        await self._ws.send(json.dumps(payload))

    async def _on_message(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = message.get("type")
        if msg_type == "R":
            if not message.get("success", False):
                logger.warning("WS request failed: %s", message)
            return

        if msg_type != "M":
            return

        topic = str(message.get("topic", ""))
        data = message.get("data")

        if topic == "heartbeat":
            await self._send({"method": "heartbeat", "data": data})
            return

        if topic.startswith("predictOrderbook/") and isinstance(data, dict):
            snapshot = self._parse_orderbook(data)
            self._orderbooks[snapshot.market_id] = snapshot
            self._ready.set()
            return

        if topic.startswith("predictTradingStatus/") and isinstance(data, dict):
            market_id = int(data.get("marketId", -1))
            if market_id >= 0:
                self._trading_status[market_id] = str(data.get("tradingStatus", ""))
            return

        if topic.startswith("predictWalletEvents/") and isinstance(data, dict):
            evt = WalletEvent(
                event_type=str(data.get("type", "unknown")),
                order_id=data.get("orderId"),
                order_hash=data.get("orderHash"),
                timestamp_ms=data.get("timestamp"),
                payload=data,
            )
            self._wallet_events.append(evt)
            return

    @staticmethod
    def _parse_orderbook(data: dict[str, Any]) -> OrderbookSnapshot:
        asks = [(float(level[0]), float(level[1])) for level in data.get("asks", [])]
        bids = [(float(level[0]), float(level[1])) for level in data.get("bids", [])]
        return OrderbookSnapshot(
            market_id=int(data.get("marketId")),
            asks=asks,
            bids=bids,
            update_timestamp_ms=int(data.get("updateTimestampMs", 0)),
        )
