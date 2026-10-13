# PredictBot 中文说明

PredictBot 是一个面向 predict.fun 的 Python 交易机器人框架，整体架构参考 polybot，并优先使用 WebSocket 行情。

默认安全模式：DRY_RUN=true。

English version: [README.md](README.md)

## 功能特点

- 分层架构，和 polybot 风格一致（config/core/strategy/database/audit）
- WS 优先行情链路（orderbook/trading status/market status）
- Predict REST API 集成（markets、orderbook 回退、orders、positions）
- JWT 鉴权流程（GET /v1/auth/message + 签名 + POST /v1/auth）
- 策略抽象，内置 WS spread 示例策略
- 使用 SQLAlchemy 持久化本地订单与审计日志
- 默认 dry-run，不会实盘下单

## 目录结构

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
│   ├── types.py
│   └── ws_feed.py
├── database/
│   ├── connection.py
│   └── models.py
└── strategy/
    ├── base.py
    └── maker_spread.py
```

## 架构流程

1. PredictBot 启动数据库、基础服务与策略生命周期。
2. PredictWsFeed 连接 wss://ws.predict.fun/ws 并订阅：
   - predictOrderbook/{marketId}
   - predictTradingStatus/{marketId}
   - predictMarketStatus/{marketId}
   - 可选 predictWalletEvents/{jwt}（账户事件）
3. 心跳处理遵循官方协议，原样回传最新 heartbeat 时间戳。
4. 策略消费行情并产出 TradeIntent。
5. OrderManager 进行风控、落库，并根据模式执行：
   - dry-run：仅记录，不发真实订单
   - live：签名并提交 POST /v1/orders

## 实盘注意事项

- core/order_signer.py 当前是 dry-run 占位签名器。
- 要实盘，需实现兼容 Predict typed-data 的真实签名器。
- REST 下单接口要求完整签名后的合约订单（data.order）。

## 环境变量

先复制模板：

```bash
cp .env.example .env
```

关键变量：

- DRY_RUN：true 或 false
- PREDICT_ENV：prod 或 testnet
- PREDICT_API_KEY：主网通常需要
- PREDICT_WALLET_PRIVATE_KEY：用于 JWT 鉴权与签名
- PREDICT_SIGNER_ADDRESS：EOA 地址
- PREDICT_ACCOUNT_ADDRESS：可选，Predict Account/deposit address
- TARGET_MARKET_IDS：逗号分隔的市场 ID，例如 123,456

策略与风控：

- STRATEGY_BASE_ORDER_SIZE
- STRATEGY_MIN_SPREAD
- STRATEGY_MIN_IMBALANCE
- STRATEGY_COOLDOWN_SECONDS
- MAX_ORDER_USDT

## 安装

```bash
pip install -r requirements.txt
```

## 运行

```bash
python bot.py
```

## WS 优先行为

- 优先使用 WS 快照与增量更新。
- 若 WS 缓存暂不可用，自动回退到 GET /v1/markets/{id}/orderbook。
- 断线后使用指数退避重连并自动重新订阅。

## 内置策略：WsSpreadStrategy

示例策略当前逻辑：

- 读取 YES 盘口深度
- 判断 spread 是否达到阈值
- 判断买卖盘不平衡度
- 以小幅 edge 价格发出 LIMIT 买卖意图

该策略定位为框架示例，不是生产级 alpha 策略。

## 数据落库

默认使用 SQLite（predictbot.db）：

- orders：本地下单尝试与状态
- audit_logs：操作与错误审计

可通过 DATABASE_URL 切换到 Postgres。

## 安全建议

- 在策略验证完成前保持 DRY_RUN=true。
- 使用小额、隔离的钱包和 API Key 进行测试。
- 增加熔断和风险上限（如日损失上限、持仓上限、一键撤单）。
- 定期轮换密钥并审计 audit_logs。

## 后续可扩展项

- 实现生产级 OrderSigner（对接 Predict SDK 或自建 typed-data 签名流程）
- 多策略组合与策略级别风控
- 订阅 wallet events 并做成交回报与持仓对账
- 基于订单簿快照做回放与回测
