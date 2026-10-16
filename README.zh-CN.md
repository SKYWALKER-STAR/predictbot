# PredictBot 中文说明

PredictBot 是一个面向 predict.fun 的 Python 交易机器人框架，整体架构参考 polybot，并优先使用 WebSocket 行情。

默认安全模式：DRY_RUN=true。

## 全局持仓成本限额

在 `.env` 配置 `MAX_POSITION_COST_RATIO=0.30`（默认 30%，范围 0 到 1；0 禁止新增买入）。
实盘每笔买单提交前检查：

```text
持仓成本 = 所有市场当前持仓份数 × 各自 averageBuyPriceUsd 的合计
上限 = (钱包 USDT 余额 + 持仓成本) × MAX_POSITION_COST_RATIO
持仓成本 + 未成交买单占用 + 新买单金额 <= 上限
```

余额使用 SDK 的链上 USDT `balanceOf`，未成交买单占用不再加入资金基数。
成本使用 API 平均买入价，不使用当前市值，也不自行额外计入手续费；按 API 的剩余持仓更新。
持仓数量沿用整数 wei（除以 10^18）、带小数点为份数的解析约定，平均价按十进制 USD/份解析。
正式启用前应使用实际账户响应核对单位、手续费和卖出后的平均价口径。

持仓和订单会完整遍历分页。成本、余额、订单数据读取失败或必要字段无效时，拒绝该笔买入。
卖出仍执行原有份额检查，不受成本限额阻止；超限不会强制平仓。
部分成交的 OPEN 买单保守地按原始金额预留，可能暂时多占额度。
同一 OrderManager 的检查和下单串行执行；API 数据存在延迟，且账户快照不是原子的，
因此不保证多进程、外部交易或成交同步延迟期间的严格总额度隔离。
`DRY_RUN=true` 保持本地模拟行为，不查询账户成本，也不模拟该额度限制。

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

做市策略（market_maker）专项变量：

- MM_QUOTE_SIZE：每单份额数（默认 8）
- MM_HALF_SPREAD_BPS：半价差（bps，默认 60）
- MM_MIN_SPREAD_BPS：触发报价所需的最小盘口价差（bps，默认 20）
- MM_REFRESH_SECONDS：最小报价刷新间隔秒数（默认 2.5）
- MM_MIN_LEVEL_SIZE：盘口最小深度要求（默认 1）
- MM_OUTCOME：`AUTO`（推荐）— 动态报价高概率那一边；或填 `YES`/`NO` 固定报价方向
- MM_MIN_PROBABILITY：自动选边时，高概率方的最低概率门槛（默认 0.55）；低于此值说明市场接近 50/50，跳过不报价
- MM_MAX_POSITION_SHARES：触发买单压制前的最大库存上限（默认 50）
- MM_INVENTORY_SKEW_BPS：满仓时的最大价格偏移（bps，默认 30）
- MM_POSITION_RECONCILE_SECONDS：向 API 对账持仓的频率秒数（默认 30）

Redis（可选）：

- REDIS_URL：连接地址，例如 redis://localhost:6379/0；留空则禁用
- REDIS_POSITIONS_KEY：库存数据写入的 Redis Hash 键名（默认 predictbot:positions）
- REDIS_POSITIONS_TTL：Redis 键 TTL 秒数（默认 300）

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

## Redis 库存缓存

配置 `REDIS_URL` 后，`PositionTracker` 在每次 API 对账后将库存写入 Redis Hash：

```
Key:   predictbot:positions   (REDIS_POSITIONS_KEY)
Field: {market_id}:{token_id}
Value: 份额数量（Decimal 字符串）
TTL:   300 s                  (REDIS_POSITIONS_TTL)
```

启动时机器人会先尝试从 Redis 加载库存（warm-start），再进行第一次 API 对账，重启后库存立即可用。

`REDIS_URL` 留空时，所有 Redis 操作被跳过，退回纯内存缓存行为。

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
