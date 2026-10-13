from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

from audit.logger import AuditLogger
from config.settings import settings
from core.auth import PredictAuthService
from core.http_client import PredictHttpClient
from core.market_data import MarketDataService
from core.order_manager import OrderManager
from core.order_signer import build_default_signer
from core.ws_feed import PredictWsFeed
from database.connection import init_db
from strategy.base import BaseStrategy
from strategy.market_maker import BasicMarketMakerStrategy
from strategy.maker_spread import WsSpreadStrategy

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    Path(settings.log_file).parent.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)-8s %(name)s | %(message)s"
    level = logging.getLevelName(settings.log_level.upper())
    logging.basicConfig(
        level=level,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(settings.log_file, encoding="utf-8"),
        ],
        force=True,
    )


class PredictBot:
    def __init__(self) -> None:
        self._strategies: list[BaseStrategy] = []
        self._running = False

        self._auth = PredictAuthService()
        self._http = PredictHttpClient(self._auth)
        self._ws = PredictWsFeed()
        self._audit = AuditLogger()
        self._market_data = MarketDataService(self._http, self._ws)
        self._order_manager = OrderManager(self._http, build_default_signer(), self._audit)
        self._strategies = []

    async def start(self) -> None:
        logger.info("=== PredictBot starting: env=%s dry_run=%s ===", settings.predict_env, settings.dry_run)

        init_db()

        open_markets = await self._market_data.refresh_open_markets()
        if not open_markets:
            logger.warning("No OPEN markets found")

        selected_market_ids = settings.parsed_market_ids
        if not selected_market_ids:
            selected_market_ids = [m.market_id for m in open_markets[:3]]
            logger.info("TARGET_MARKET_IDS empty; auto-selecting first markets: %s", selected_market_ids)
        else:
            refreshed = await self._market_data.refresh_markets_for_ids(selected_market_ids)
            logger.info(
                "Refreshed explicit target markets: requested=%s loaded=%s",
                selected_market_ids,
                [m.market_id for m in refreshed],
            )

        self._strategies = self._build_strategies(selected_market_ids)

        await self._ws.subscribe_market_topics(selected_market_ids)

        if not settings.dry_run:
            await self._auth.refresh_jwt()
            if self._auth.jwt:
                await self._ws.subscribe_wallet_events(self._auth.jwt)

        ws_task = asyncio.create_task(self._ws.run(), name="predict-ws")

        for strategy in self._strategies:
            await strategy.on_start()

        self._running = True
        try:
            while self._running:
                await self._market_data.refresh_open_markets()
                if selected_market_ids:
                    await self._market_data.refresh_markets_for_ids(selected_market_ids)

                for strategy in self._strategies:
                    intents = await strategy.on_tick()
                    for intent in intents:
                        market = self._market_data.get_market(intent.market_id)
                        if market is None:
                            continue
                        logger.info("Processing intent: market=%s outcome=%s side=%s price=%.4f qty=%.4f",
                                    intent.market_id,
                                    intent.outcome,
                                    intent.side,
                                    intent.price_per_share,
                                    intent.quantity)

                        token_id = self._resolve_token_id(market.outcomes, intent.outcome)
                        if not token_id:
                            logger.warning("No tokenId found: market=%s outcome=%s", intent.market_id, intent.outcome)
                            continue
                        logger.info("Resolved token_id: %s for market=%s outcome=%s", token_id, intent.market_id, intent.outcome)

                        result = await self._order_manager.place_order(
                            intent,
                            token_id=token_id,
                            fee_rate_bps=market.fee_rate_bps,
                            is_neg_risk=market.is_neg_risk,
                            is_yield_bearing=market.is_yield_bearing,
                        )
                        if result.success:
                            logger.info(
                                "Order accepted: market=%s side=%s price=%.4f qty=%.4f dry=%s",
                                intent.market_id,
                                intent.side,
                                intent.price_per_share,
                                intent.quantity,
                                result.is_dry_run,
                            )
                        else:
                            logger.error("Order failed: %s", result.error)
                    logger.info("Finished processing intent for strategy: %s", strategy.__class__.__name__)

                await asyncio.sleep(settings.poll_interval_seconds)
        finally:
            for strategy in self._strategies:
                await strategy.on_stop()
            self._ws.stop()
            ws_task.cancel()
            try:
                await ws_task
            except asyncio.CancelledError:
                pass
            await self._http.close()
            logger.info("=== PredictBot stopped ===")

    def stop(self) -> None:
        self._running = False

    @staticmethod
    def _resolve_token_id(outcomes: list, target_outcome: str) -> str:
        target = target_outcome.strip().lower()
        for outcome in outcomes:
            if outcome.name.strip().lower() == target:
                return outcome.on_chain_id
        if outcomes:
            return outcomes[0].on_chain_id
        return ""

    def _build_strategies(self, market_ids: list[int]) -> list[BaseStrategy]:
        if not settings.strategy_enabled:
            return []
        mode = settings.strategy_mode.strip().lower()
        if mode == "ws_spread":
            return [WsSpreadStrategy(market_data=self._market_data, market_ids=market_ids)]
        if mode == "market_maker":
            return [BasicMarketMakerStrategy(market_data=self._market_data, market_ids=market_ids)]
        logger.warning("Unknown STRATEGY_MODE=%s, fallback to market_maker", settings.strategy_mode)
        return [BasicMarketMakerStrategy(market_data=self._market_data, market_ids=market_ids)]


async def main() -> None:
    setup_logging()
    bot = PredictBot()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, bot.stop)

    await bot.start()


if __name__ == "__main__":
    asyncio.run(main())
