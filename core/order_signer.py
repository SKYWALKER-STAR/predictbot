from __future__ import annotations

from datetime import UTC, datetime
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Protocol

from eth_account import Account
from predict_sdk import BuildOrderInput, ChainId, LimitHelperInput, OrderBuilder, OrderBuilderOptions, Side

from config.settings import settings


@dataclass
class SignedContractOrder:
    order: dict
    price_per_share: str


class OrderSigner(Protocol):
    async def sign_order(
        self,
        *,
        maker: str,
        signer: str,
        token_id: str,
        side: int,
        quantity: Decimal,
        price_per_share: Decimal,
        fee_rate_bps: int,
        expiration_s: int,
        is_neg_risk: bool,
        is_yield_bearing: bool,
    ) -> SignedContractOrder:
        ...


class DryRunOrderSigner:
    """
    Lightweight placeholder signer for architecture completeness.

    In DRY_RUN mode, this fake signer lets strategy and order-manager flow run end-to-end
    without chain dependencies. For live trading, replace with a concrete signer built
    against Predict SDK / contract typed-data spec.
    """

    async def sign_order(
        self,
        *,
        maker: str,
        signer: str,
        token_id: str,
        side: int,
        quantity: Decimal,
        price_per_share: Decimal,
        fee_rate_bps: int,
        expiration_s: int,
        is_neg_risk: bool,
        is_yield_bearing: bool,
    ) -> SignedContractOrder:
        q_wei = _to_wei(quantity)
        p_wei = _to_wei(price_per_share)

        if side == 0:
            maker_amount = str((q_wei * p_wei) // 10**18)
            taker_amount = str(q_wei)
        else:
            maker_amount = str(q_wei)
            taker_amount = str((q_wei * p_wei) // 10**18)

        pseudo_order = {
            "hash": "0xDRYRUN",
            "salt": "0",
            "maker": maker,
            "signer": signer,
            "taker": "0x0000000000000000000000000000000000000000",
            "tokenId": token_id,
            "makerAmount": maker_amount,
            "takerAmount": taker_amount,
            "expiration": expiration_s,
            "nonce": "0",
            "feeRateBps": str(fee_rate_bps),
            "side": side,
            "signatureType": 0,
            "signature": "0x",
        }
        return SignedContractOrder(order=pseudo_order, price_per_share=_dec_to_str(price_per_share))


class EoaOrderSigner:
    """Real EOA EIP-712 signer for Predict orders."""

    def __init__(self) -> None:
        if not settings.predict_wallet_private_key:
            raise ValueError("PREDICT_WALLET_PRIVATE_KEY is required for live signing")
        self._private_key = settings.predict_wallet_private_key
        self._account = Account.from_key(self._private_key)
        chain_id = ChainId.BNB_MAINNET if settings.predict_env == "prod" else ChainId.BNB_TESTNET
        options = (
            OrderBuilderOptions(predict_account=settings.predict_account_address)
            if settings.predict_account_address
            else None
        )
        self._builder = OrderBuilder.make(chain_id, self._private_key, options)

    async def sign_order(
        self,
        *,
        maker: str,
        signer: str,
        token_id: str,
        side: int,
        quantity: Decimal,
        price_per_share: Decimal,
        fee_rate_bps: int,
        expiration_s: int,
        is_neg_risk: bool,
        is_yield_bearing: bool,
    ) -> SignedContractOrder:
        q_wei = _to_wei(quantity)
        p_wei = _to_wei(price_per_share)
        if q_wei <= 0 or p_wei <= 0:
            raise ValueError("quantity and price_per_share must be positive")

        if side == 0:
            sdk_side = Side.BUY
        elif side == 1:
            sdk_side = Side.SELL
        else:
            raise ValueError(f"unsupported order side: {side}")

        amounts = self._builder.get_limit_order_amounts(
            LimitHelperInput(side=sdk_side, price_per_share_wei=p_wei, quantity_wei=q_wei)
        )
        order = self._builder.build_order(
            "LIMIT",
            BuildOrderInput(
                side=sdk_side,
                token_id=token_id,
                maker_amount=amounts.maker_amount,
                taker_amount=amounts.taker_amount,
                fee_rate_bps=fee_rate_bps,
                maker=maker or None,
                signer=signer or None,
                expires_at=datetime.fromtimestamp(expiration_s, tz=UTC),
            ),
        )
        typed_data = self._builder.build_typed_data(
            order,
            is_neg_risk=is_neg_risk,
            is_yield_bearing=is_yield_bearing,
        )
        signed = self._builder.sign_typed_data_order(typed_data)
        order_hash = signed.hash or self._builder.build_typed_data_hash(typed_data)

        api_order = {
            "salt": str(order.salt),
            "maker": order.maker,
            "signer": order.signer,
            "taker": order.taker,
            "tokenId": str(order.token_id),
            "makerAmount": str(order.maker_amount),
            "takerAmount": str(order.taker_amount),
            "expiration": int(order.expiration),
            "nonce": str(order.nonce),
            "feeRateBps": str(order.fee_rate_bps),
            "side": int(order.side.value),
            "signatureType": int(order.signature_type.value),
            "signature": signed.signature,
            "hash": order_hash,
        }
        # Predict expects pricePerShare in wei-like integer string.
        return SignedContractOrder(order=api_order, price_per_share=str(amounts.price_per_share))


def _to_wei(value: Decimal) -> int:
    scaled = (value * Decimal(10**18)).quantize(Decimal("1"), rounding=ROUND_DOWN)
    return int(scaled)


def _dec_to_str(value: Decimal) -> str:
    normalized = value.quantize(Decimal("0.000000000000000001"), rounding=ROUND_DOWN)
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def build_default_signer() -> OrderSigner:
    if settings.dry_run:
        return DryRunOrderSigner()
    return EoaOrderSigner()
