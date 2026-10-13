from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Protocol

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


def _to_wei(value: Decimal) -> int:
    scaled = (value * Decimal(10**18)).quantize(Decimal("1"), rounding=ROUND_DOWN)
    return int(scaled)


def _dec_to_str(value: Decimal) -> str:
    normalized = value.quantize(Decimal("0.000000000000000001"), rounding=ROUND_DOWN)
    return format(normalized, "f")


def build_default_signer() -> OrderSigner:
    return DryRunOrderSigner()
