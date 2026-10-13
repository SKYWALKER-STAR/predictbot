from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from audit.logger import AuditLogger
from config.settings import settings
from core.http_client import PredictHttpClient
from core.order_signer import OrderSigner
from core.types import PlaceOrderResult, TradeIntent
from database.connection import get_session
from database.models import OrderRecord

logger = logging.getLogger(__name__)


class OrderManager:
    def __init__(self, http_client: PredictHttpClient, signer: OrderSigner, audit: AuditLogger) -> None:
        self._http = http_client
        self._signer = signer
        self._audit = audit

    async def place_order(
        self,
        intent: TradeIntent,
        *,
        token_id: str,
        fee_rate_bps: int,
        is_neg_risk: bool,
        is_yield_bearing: bool,
    ) -> PlaceOrderResult:
        if intent.quantity * intent.price_per_share > settings.max_order_usdt:
            msg = "order value exceeds MAX_ORDER_USDT"
            self._audit.record("PLACE_ORDER", "FAILURE", asdict(intent), msg)
            return PlaceOrderResult(False, None, None, None, settings.dry_run, error=msg)

        local_id = self._create_local(intent)

        if settings.dry_run:
            self._update_local(local_id, status="OPEN", is_dry_run=True)
            self._audit.record("PLACE_ORDER", "SKIPPED", {"local_id": local_id, "intent": asdict(intent)})
            return PlaceOrderResult(True, local_id, "0xDRYRUN", None, True)

        side = 0 if intent.side.upper() == "BUY" else 1
        expiration = int((datetime.now(timezone.utc) + timedelta(seconds=settings.order_expiration_seconds)).timestamp())

        try:
            if side == 1:
                available_shares = await self._http.get_outcome_shares(token_id, market_id=intent.market_id)
                required_shares = Decimal(str(intent.quantity))
                if available_shares < required_shares:
                    msg = (
                        "skip SELL due to insufficient shares: "
                        f"available={available_shares:.6f} required={required_shares:.6f} token_id={token_id}"
                    )
                    self._update_local(local_id, status="SKIPPED", error=msg)
                    self._audit.record("PLACE_ORDER", "SKIPPED", {"local_id": local_id, "intent": asdict(intent)}, msg)
                    logger.info(msg)
                    return PlaceOrderResult(False, local_id, None, None, False, error=msg)

            signed = await self._signer.sign_order(
                maker=settings.wallet_address,
                signer=settings.wallet_address,
                token_id=token_id,
                side=side,
                quantity=Decimal(str(intent.quantity)),
                price_per_share=Decimal(str(intent.price_per_share)),
                fee_rate_bps=fee_rate_bps,
                expiration_s=expiration,
                is_neg_risk=is_neg_risk,
                is_yield_bearing=is_yield_bearing,
            )

            if side == 0:
                new_buy_cost = Decimal(str(signed.order.get("makerAmount", "0"))) / Decimal(10**18)
                reserved_buy = await self._http.get_open_buy_reserved_usdt(first=200)
                usdt_balance = await self._http.get_wallet_usdt_balance()
                if usdt_balance is not None and reserved_buy + new_buy_cost > usdt_balance:
                    msg = (
                        "skip BUY due to collateral cap: "
                        f"balance={usdt_balance:.6f} reserved={reserved_buy:.6f} new={new_buy_cost:.6f}"
                    )
                    self._update_local(local_id, status="SKIPPED", error=msg)
                    self._audit.record("PLACE_ORDER", "SKIPPED", {"local_id": local_id, "intent": asdict(intent)}, msg)
                    logger.info(msg)
                    return PlaceOrderResult(False, local_id, None, None, False, error=msg)

            body = {
                "data": {
                    "order": signed.order,
                    "pricePerShare": signed.price_per_share,
                    "strategy": intent.strategy,
                    "isFillOrKill": False,
                    "isPostOnly": intent.strategy.upper() == "LIMIT",
                }
            }
            response = await self._http.create_order(body)
            logger.info("create_order response: %s", response)
            payload = response.get("data", {})
            order_hash = payload.get("orderHash")
            order_id = payload.get("orderId")

            self._update_local(local_id, status="OPEN", order_hash=order_hash, exchange_order_id=order_id)
            self._audit.record("PLACE_ORDER", "SUCCESS", {"local_id": local_id, "response": payload})
            return PlaceOrderResult(True, local_id, order_hash, order_id, False)
        except Exception as exc:
            self._update_local(local_id, status="FAILED", error=str(exc))
            self._audit.record("PLACE_ORDER", "FAILURE", {"local_id": local_id}, str(exc))
            logger.exception("place_order failed: %s", exc)
            return PlaceOrderResult(False, local_id, None, None, False, error=str(exc))

    async def cancel_by_hashes(self, order_hashes: list[str]) -> bool:
        if not order_hashes:
            return True

        if settings.dry_run:
            self._audit.record("CANCEL_ORDERS", "SKIPPED", {"order_hashes": order_hashes})
            return True

        try:
            await self._http.remove_orders_by_hash(order_hashes)
            self._audit.record("CANCEL_ORDERS", "SUCCESS", {"order_hashes": order_hashes})
            return True
        except Exception as exc:
            self._audit.record("CANCEL_ORDERS", "FAILURE", {"order_hashes": order_hashes}, str(exc))
            return False

    def _create_local(self, intent: TradeIntent) -> int:
        with get_session() as session:
            row = OrderRecord(
                market_id=intent.market_id,
                outcome=intent.outcome,
                side=intent.side,
                strategy=intent.strategy,
                price_per_share=intent.price_per_share,
                quantity=intent.quantity,
                status="PENDING",
                is_dry_run=settings.dry_run,
            )
            session.add(row)
            session.flush()
            return row.id

    def _update_local(
        self,
        local_id: int,
        *,
        status: str,
        is_dry_run: bool | None = None,
        order_hash: str | None = None,
        exchange_order_id: str | None = None,
        error: str | None = None,
    ) -> None:
        with get_session() as session:
            row = session.get(OrderRecord, local_id)
            if row is None:
                return
            row.status = status
            row.updated_at = datetime.now(timezone.utc)
            if is_dry_run is not None:
                row.is_dry_run = is_dry_run
            if order_hash is not None:
                row.order_hash = order_hash
            if exchange_order_id is not None:
                row.exchange_order_id = exchange_order_id
            if error is not None:
                row.error = error
