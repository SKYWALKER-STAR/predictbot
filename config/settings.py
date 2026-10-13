from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    dry_run: bool = Field(default=True, alias="DRY_RUN")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_file: str = Field(default="logs/predictbot.log", alias="LOG_FILE")
    poll_interval_seconds: float = Field(default=2.0, alias="POLL_INTERVAL_SECONDS", ge=0.2)

    predict_env: str = Field(default="prod", alias="PREDICT_ENV")
    predict_api_key: str = Field(default="", alias="PREDICT_API_KEY")
    predict_proxy_url: str = Field(default="", alias="PREDICT_PROXY_URL")
    predict_wallet_private_key: str = Field(default="", alias="PREDICT_WALLET_PRIVATE_KEY")
    predict_signer_address: str = Field(default="", alias="PREDICT_SIGNER_ADDRESS")
    predict_account_address: str = Field(default="", alias="PREDICT_ACCOUNT_ADDRESS")

    target_market_ids: str = Field(default="", alias="TARGET_MARKET_IDS")

    max_order_usdt: float = Field(default=20.0, alias="MAX_ORDER_USDT", gt=0)
    max_open_orders: int = Field(default=5, alias="MAX_OPEN_ORDERS", ge=1)

    strategy_enabled: bool = Field(default=True, alias="STRATEGY_ENABLED")
    strategy_mode: str = Field(default="market_maker", alias="STRATEGY_MODE")
    strategy_base_order_size: float = Field(default=10.0, alias="STRATEGY_BASE_ORDER_SIZE", gt=0)
    strategy_min_spread: float = Field(default=0.02, alias="STRATEGY_MIN_SPREAD", gt=0)
    strategy_min_imbalance: float = Field(default=0.10, alias="STRATEGY_MIN_IMBALANCE", ge=0)
    strategy_cooldown_seconds: float = Field(default=3.0, alias="STRATEGY_COOLDOWN_SECONDS", ge=0)

    mm_quote_size: float = Field(default=8.0, alias="MM_QUOTE_SIZE", gt=0)
    mm_min_order_value_usdt: float = Field(default=0.95, alias="MM_MIN_ORDER_VALUE_USDT", gt=0)
    mm_half_spread_bps: float = Field(default=60.0, alias="MM_HALF_SPREAD_BPS", gt=0)
    mm_min_spread_bps: float = Field(default=20.0, alias="MM_MIN_SPREAD_BPS", ge=0)
    mm_refresh_seconds: float = Field(default=2.5, alias="MM_REFRESH_SECONDS", ge=0.2)
    mm_min_level_size: float = Field(default=1.0, alias="MM_MIN_LEVEL_SIZE", ge=0)
    mm_outcome: str = Field(default="YES", alias="MM_OUTCOME")

    order_expiration_seconds: int = Field(default=300, alias="ORDER_EXPIRATION_SECONDS", ge=30)

    eip712_domain_name: str = Field(default="PredictExchange", alias="EIP712_DOMAIN_NAME")
    eip712_domain_version: str = Field(default="1", alias="EIP712_DOMAIN_VERSION")
    eip712_chain_id: int = Field(default=56, alias="EIP712_CHAIN_ID")
    eip712_verifying_contract: str = Field(default="", alias="EIP712_VERIFYING_CONTRACT")

    database_url: str = Field(default="sqlite:///predictbot.db", alias="DATABASE_URL")

    @field_validator("predict_env")
    @classmethod
    def _validate_env(cls, value: str) -> str:
        lowered = value.lower().strip()
        if lowered not in {"prod", "testnet"}:
            raise ValueError("PREDICT_ENV must be either 'prod' or 'testnet'")
        return lowered

    @property
    def api_base_url(self) -> str:
        if self.predict_env == "testnet":
            return "https://api-testnet.predict.fun"
        return "https://api.predict.fun"

    @property
    def ws_url(self) -> str:
        return "wss://ws.predict.fun/ws"

    @property
    def wallet_address(self) -> str:
        if self.predict_account_address:
            return self.predict_account_address
        return self.predict_signer_address

    @property
    def parsed_market_ids(self) -> list[int]:
        ids: list[int] = []
        for part in self.target_market_ids.split(","):
            p = part.strip()
            if not p:
                continue
            try:
                ids.append(int(p))
            except ValueError:
                continue
        return ids


settings = Settings()
