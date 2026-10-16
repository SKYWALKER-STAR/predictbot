# PredictBot

Global live BUY cost cap: set `MAX_POSITION_COST_RATIO=0.30` (default 30%, range
0–1; zero blocks new buys). Remaining position cost plus open BUY reservations
plus the new order must not exceed `(wallet USDT balance + position cost) * ratio`.
Position cost uses API `averageBuyPriceUsd` times remaining shares, not market
value. Missing risk data blocks buys; sells retain their existing share checks.
Dry runs do not simulate account-level risk. See the Chinese README for unit
assumptions, pagination, conservative partial-fill reservations and API latency limits.

中文文档: [README.zh-CN.md](README.zh-CN.md)

PredictBot is a Python trading bot framework for predict.fun, designed with a structure similar to polybot and optimized around WebSocket market data.

Default mode is safe: `DRY_RUN=true`.

## Features

- Polybot-like layered architecture (`config`, `core`, `strategy`, `database`, `audit`)
- WebSocket-first market data flow (orderbook/trading status/market status)
- Predict REST API integration (markets, orderbook fallback, orders, positions)
- JWT auth flow (`GET /v1/auth/message` + signed message + `POST /v1/auth`)
- Strategy abstraction with a built-in WS spread strategy
- Local order/audit persistence via SQLAlchemy
- Dry-run by default, no live order submission unless explicitly enabled

## Project Structure

```text
predictbot/
├── bot.py
├── requirements.txt
├── .env.example
├── audit/
│   └── logger.py
├── config/
│   └── settings.py
├── core/
│   ├── auth.py
│   ├── http_client.py
│   ├── market_data.py
│   ├── order_manager.py
│   ├── order_signer.py
│   ├── position_tracker.py
│   ├── redis_client.py
│   ├── types.py
│   └── ws_feed.py
├── database/
│   ├── connection.py
│   └── models.py
└── strategy/
	 ├── base.py
	 └── maker_spread.py
```

## Architecture Overview

1. `PredictBot` starts database, services, and strategy lifecycle.
2. `PredictWsFeed` connects to `wss://ws.predict.fun/ws` and subscribes to:
	- `predictOrderbook/{marketId}`
	- `predictTradingStatus/{marketId}`
	- `predictMarketStatus/{marketId}`
	- optional `predictWalletEvents/{jwt}` for live account events
3. WS heartbeat handling follows official protocol: echo latest heartbeat timestamp.
4. Strategy consumes market state and emits `TradeIntent`.
5. `OrderManager` runs risk checks, persists local record, then:
	- dry-run: no REST order submission
	- live: signs and submits `POST /v1/orders`

## Important Notes for Live Trading

- `core/order_signer.py` currently provides a dry-run placeholder signer.
- To trade live, implement a real signer compatible with Predict typed-data order format.
- The REST order endpoint expects a fully signed contract order (`data.order`).

## Environment Variables

Copy and edit:

```bash
cp .env.example .env
```

Key values:

- `DRY_RUN`: `true` or `false`
- `PREDICT_ENV`: `prod` or `testnet`
- `PREDICT_API_KEY`: required on mainnet
- `PREDICT_WALLET_PRIVATE_KEY`: used for JWT auth and signing
- `PREDICT_SIGNER_ADDRESS`: signer EOA address
- `PREDICT_ACCOUNT_ADDRESS`: optional smart account/deposit address
- `TARGET_MARKET_IDS`: comma-separated market IDs, e.g. `123,456`

Strategy/risk:

- `STRATEGY_BASE_ORDER_SIZE`
- `STRATEGY_MIN_SPREAD`
- `STRATEGY_MIN_IMBALANCE`
- `STRATEGY_COOLDOWN_SECONDS`
- `MAX_ORDER_USDT`

Market maker strategy:

- `MM_QUOTE_SIZE`: shares per order (default `8`)
- `MM_HALF_SPREAD_BPS`: half-spread in basis points (default `60`)
- `MM_MIN_SPREAD_BPS`: minimum book spread to quote (default `20`)
- `MM_REFRESH_SECONDS`: minimum seconds between quote refreshes (default `2.5`)
- `MM_MIN_LEVEL_SIZE`: minimum top-of-book size to proceed (default `1`)
- `MM_OUTCOME`: `AUTO` (recommended) — dynamically quotes the high-probability side; or `YES`/`NO` to fix the outcome
- `MM_MIN_PROBABILITY`: minimum probability the favoured side must reach before quoting (default `0.55`); prevents quoting near-50/50 markets
- `MM_MAX_POSITION_SHARES`: inventory cap before BUY orders are suppressed (default `50`)
- `MM_INVENTORY_SKEW_BPS`: max price skew applied at full inventory (default `30`)
- `MM_POSITION_RECONCILE_SECONDS`: how often to reconcile positions with the API (default `30`)

Redis (optional):

- `REDIS_URL`: connection URL, e.g. `redis://localhost:6379/0`; leave empty to disable
- `REDIS_POSITIONS_KEY`: Redis Hash key for inventory data (default `predictbot:positions`)
- `REDIS_POSITIONS_TTL`: TTL in seconds for the Redis key (default `300`)

```bash
pip install -r requirements.txt
```

## Run

```bash
python bot.py
```

## WS-First Behavior

- Market snapshots come from WS first.
- If WS cache is missing, bot falls back to `GET /v1/markets/{id}/orderbook`.
- Reconnect uses exponential backoff and re-subscribes existing topics.

## Strategy: `WsSpreadStrategy`

Current built-in strategy logic:

- reads YES orderbook depth from WS
- checks spread threshold
- checks top-of-book imbalance
- emits a LIMIT buy/sell intent on YES token with small edge pricing

This is a framework baseline, not a production alpha strategy.

## Data Persistence

Default DB is SQLite (`predictbot.db`):

- `orders`: local order attempts and state
- `audit_logs`: action and error trail

Use `DATABASE_URL` to switch to Postgres if needed.

## Redis Inventory Cache

When `REDIS_URL` is configured, `PositionTracker` writes the current inventory to a Redis Hash after every API reconcile:

```
Key:   predictbot:positions   (REDIS_POSITIONS_KEY)
Field: {market_id}:{token_id}
Value: decimal share amount string
TTL:   300 s                  (REDIS_POSITIONS_TTL)
```

On startup the bot attempts to warm-start from Redis before the first API reconcile, so inventory is available immediately after a restart.

If `REDIS_URL` is empty, all Redis operations are skipped and the bot falls back to in-memory-only behavior.

## Security Checklist

- Keep `DRY_RUN=true` until strategy behavior is verified.
- Use a dedicated key with limited funds for bot testing.
- Add circuit breakers (max daily loss, max position, cancel-all trigger).
- Rotate API keys and monitor `audit_logs`.

## Next Extension Points

- Implement a production `OrderSigner` using Predict SDK/order typed-data.
- Add multiple strategies and per-strategy risk limits.
- Track fills from `predictWalletEvents/{jwt}` and reconcile local positions.
- Add backtesting/replay based on stored orderbook snapshots.
