from __future__ import annotations

from datetime import datetime
from typing import Literal
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
    max_position_cost_ratio: float = Field(default=0.30, alias="MAX_POSITION_COST_RATIO", ge=0, le=1)
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
    mm_outcome: str = Field(default="AUTO", alias="MM_OUTCOME")
    mm_min_probability: float = Field(default=0.55, alias="MM_MIN_PROBABILITY", ge=0.5, le=1.0)
    mm_max_position_shares: float = Field(default=50.0, alias="MM_MAX_POSITION_SHARES", gt=0)
    mm_inventory_skew_bps: float = Field(default=30.0, alias="MM_INVENTORY_SKEW_BPS", ge=0)
    mm_position_reconcile_seconds: float = Field(default=30.0, alias="MM_POSITION_RECONCILE_SECONDS", ge=5.0)

    # Paper models: absolute probability-price units and seconds, not bps/returns.
    mm_inventory_model: Literal["as", "glft"] = Field(default="glft", alias="MM_INVENTORY_MODEL")
    mm_risk_aversion: float = Field(default=0.1, alias="MM_RISK_AVERSION", gt=0, allow_inf_nan=False)
    mm_order_decay_k: float = Field(default=100.0, alias="MM_ORDER_DECAY_K", gt=0, allow_inf_nan=False)
    mm_arrival_rate_a: float = Field(default=0.5, alias="MM_ARRIVAL_RATE_A", gt=0, allow_inf_nan=False)
    mm_initial_sigma: float = Field(default=0.003, alias="MM_INITIAL_SIGMA", gt=0, allow_inf_nan=False)
    mm_volatility_window_seconds: float = Field(default=120, alias="MM_VOLATILITY_WINDOW_SECONDS", gt=0)
    mm_volatility_min_samples: int = Field(default=10, alias="MM_VOLATILITY_MIN_SAMPLES", ge=2)
    mm_model_horizon_seconds: float = Field(default=60, alias="MM_MODEL_HORIZON_SECONDS", gt=0, le=3600)
    mm_max_book_age_seconds: float = Field(default=10, alias="MM_MAX_BOOK_AGE_SECONDS", gt=0)
    # Explicit UTC-aware end times; no inference from market titles or API fields.
    mm_market_end_times: dict[int, datetime] = Field(default_factory=dict, alias="MM_MARKET_END_TIMES")
    mm_stop_before_end_seconds: float = Field(default=10, alias="MM_STOP_BEFORE_END_SECONDS", ge=0)

    @field_validator("mm_market_end_times")
    @classmethod
    def _validate_end_times(cls, value: dict[int, datetime]) -> dict[int, datetime]:
        if any(end.tzinfo is None or end.utcoffset() is None for end in value.values()):
            raise ValueError("MM_MARKET_END_TIMES requires timezone-aware timestamps")
        return value

    order_expiration_seconds: int = Field(default=300, alias="ORDER_EXPIRATION_SECONDS", ge=30)

    eip712_domain_name: str = Field(default="PredictExchange", alias="EIP712_DOMAIN_NAME")
    eip712_domain_version: str = Field(default="1", alias="EIP712_DOMAIN_VERSION")
    eip712_chain_id: int = Field(default=56, alias="EIP712_CHAIN_ID")
    eip712_verifying_contract: str = Field(default="", alias="EIP712_VERIFYING_CONTRACT")

    database_url: str = Field(default="sqlite:///predictbot.db", alias="DATABASE_URL")

    redis_url: str = Field(default="", alias="REDIS_URL")
    redis_positions_key: str = Field(default="predictbot:positions", alias="REDIS_POSITIONS_KEY")
    redis_positions_ttl: int = Field(default=300, alias="REDIS_POSITIONS_TTL", ge=10)

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
