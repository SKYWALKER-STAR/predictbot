from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
import secrets
from typing import Any
from typing import Protocol

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import to_checksum_address

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
        if not settings.eip712_verifying_contract:
            raise ValueError("EIP712_VERIFYING_CONTRACT is required for live signing")

        signer_addr = to_checksum_address(signer or self._account.address)
        maker_addr = to_checksum_address(maker or self._account.address)

        q_wei = _to_wei(quantity)
        p_wei = _to_wei(price_per_share)
        if q_wei <= 0 or p_wei <= 0:
            raise ValueError("quantity and price_per_share must be positive")

        if side == 0:
            maker_amount = (q_wei * p_wei) // 10**18
            taker_amount = q_wei
        elif side == 1:
            maker_amount = q_wei
            taker_amount = (q_wei * p_wei) // 10**18
        else:
            raise ValueError(f"unsupported order side: {side}")

        # Predict API expects decimal-like strings for large uint fields.
        unsigned_order: dict[str, Any] = {
            "salt": str(secrets.randbits(256)),
            "maker": maker_addr,
            "signer": signer_addr,
            "taker": "0x0000000000000000000000000000000000000000",
            "tokenId": str(int(token_id)),
            "makerAmount": str(int(maker_amount)),
            "takerAmount": str(int(taker_amount)),
            "expiration": int(expiration_s),
            "nonce": str(secrets.randbits(63)),
            "feeRateBps": str(int(fee_rate_bps)),
            "side": int(side),
            "signatureType": 0,
        }

        typed_data = self._build_typed_data(unsigned_order)
        signable = encode_typed_data(full_message=typed_data)
        signed = Account.sign_message(signable, private_key=self._private_key)

        order = {
            **unsigned_order,
            "hash": signed.message_hash.hex(),
            "signature": signed.signature.hex(),
        }
        return SignedContractOrder(order=order, price_per_share=_dec_to_str(price_per_share))

    @staticmethod
    def _build_typed_data(order: dict[str, Any]) -> dict[str, Any]:
        return {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "Order": [
                    {"name": "salt", "type": "uint256"},
                    {"name": "maker", "type": "address"},
                    {"name": "signer", "type": "address"},
                    {"name": "taker", "type": "address"},
                    {"name": "tokenId", "type": "uint256"},
                    {"name": "makerAmount", "type": "uint256"},
                    {"name": "takerAmount", "type": "uint256"},
                    {"name": "expiration", "type": "uint256"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "feeRateBps", "type": "uint256"},
                    {"name": "side", "type": "uint8"},
                    {"name": "signatureType", "type": "uint8"},
                ],
            },
            "primaryType": "Order",
            "domain": {
                "name": settings.eip712_domain_name,
                "version": settings.eip712_domain_version,
                "chainId": int(settings.eip712_chain_id),
                "verifyingContract": to_checksum_address(settings.eip712_verifying_contract),
            },
            "message": {
                "salt": int(order["salt"]),
                "maker": order["maker"],
                "signer": order["signer"],
                "taker": order["taker"],
                "tokenId": int(order["tokenId"]),
                "makerAmount": int(order["makerAmount"]),
                "takerAmount": int(order["takerAmount"]),
                "expiration": int(order["expiration"]),
                "nonce": int(order["nonce"]),
                "feeRateBps": int(order["feeRateBps"]),
                "side": int(order["side"]),
                "signatureType": int(order["signatureType"]),
            },
        }


def _to_wei(value: Decimal) -> int:
    scaled = (value * Decimal(10**18)).quantize(Decimal("1"), rounding=ROUND_DOWN)
    return int(scaled)


def _dec_to_str(value: Decimal) -> str:
    normalized = value.quantize(Decimal("0.000000000000000001"), rounding=ROUND_DOWN)
    return format(normalized, "f")


def build_default_signer() -> OrderSigner:
    if settings.dry_run:
        return DryRunOrderSigner()
    return EoaOrderSigner()
