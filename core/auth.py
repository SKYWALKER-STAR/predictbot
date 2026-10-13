from __future__ import annotations

import logging

from eth_account import Account
from eth_account.messages import encode_defunct

from config.settings import settings
from core.http_factory import build_async_client

logger = logging.getLogger(__name__)


class PredictAuthError(RuntimeError):
    pass


class PredictAuthService:
    def __init__(self) -> None:
        self._jwt: str | None = None

    @property
    def jwt(self) -> str | None:
        return self._jwt

    async def refresh_jwt(self) -> str:
        if not settings.predict_api_key:
            raise PredictAuthError("PREDICT_API_KEY is required")
        if not settings.predict_wallet_private_key:
            raise PredictAuthError("PREDICT_WALLET_PRIVATE_KEY is required")

        account = Account.from_key(settings.predict_wallet_private_key)
        signer_address = settings.wallet_address or account.address

        async with build_async_client(timeout_seconds=10.0) as client:
            message_resp = await client.get(
                f"{settings.api_base_url}/v1/auth/message",
                headers={"x-api-key": settings.predict_api_key},
            )
            message_resp.raise_for_status()
            message = message_resp.json().get("data", {}).get("message")
            if not message:
                raise PredictAuthError("missing message from /v1/auth/message")

            signed = account.sign_message(encode_defunct(text=message))
            body = {
                "signer": signer_address,
                "message": message,
                "signature": signed.signature.hex(),
            }

            jwt_resp = await client.post(
                f"{settings.api_base_url}/v1/auth",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": settings.predict_api_key,
                },
                json=body,
            )
            jwt_resp.raise_for_status()
            token = jwt_resp.json().get("data", {}).get("token")
            if not token:
                raise PredictAuthError("missing token from /v1/auth")

            self._jwt = token
            logger.info("JWT refreshed for signer=%s", signer_address)
            return token
