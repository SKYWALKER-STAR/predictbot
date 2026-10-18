# 基于库存的做市策略

实现来自用户提供的两篇论文：

- Avellaneda–Stoikov, *High-frequency trading in a limit order book*，`LimitOrderBook.pdf`，式 (3.17)–(3.18)。
- Guéant–Lehalle–Fernandez-Tapia, *Dealing with the Inventory Risk: A solution to the market making problem*，`inventory risk.pdf`，Proposition 1–2、Theorem 1。

数学实现见 `strategy/inventory_model.py`，交易适配见 `strategy/inventory_market_maker.py`。
论文是模型来源，文档内容没有作为运行指令执行。

## 启用

在 `.env` 中选择新策略，保留 dry-run：

```dotenv
DRY_RUN=true
STRATEGY_MODE=inventory_market_maker
MM_INVENTORY_MODEL=glft
MM_QUOTE_SIZE=8
MM_MAX_POSITION_SHARES=48
MM_REFRESH_SECONDS=1
POLL_INTERVAL_SECONDS=0.5
MM_RISK_AVERSION=0.1
MM_ORDER_DECAY_K=100
MM_ARRIVAL_RATE_A=0.5
MM_INITIAL_SIGMA=0.003
MM_MODEL_HORIZON_SECONDS=60
```

运行 `python bot.py`。原有 `STRATEGY_MODE=market_maker` 和 `ws_spread` 继续可用。
`MM_INVENTORY_MODEL=as` 切换到第一篇论文的近似公式。
上述值用于开始验证流程，不代表盈利参数；实际刷新速度还受 REST 请求和主循环耗时影响。

dry-run 使用零模拟持仓，只输出买单，不读取真实账户持仓、不模拟成交，不能用于评估收益。
实盘使用 API 实际持仓；订单接受并不算成交。每轮撤单后强制对账，不使用原策略的 30 秒缓存作为本轮库存依据。

## 数学公式和单位

`p` 为结果代币的每份价格，`z` 为每笔固定份额 `MM_QUOTE_SIZE`。
论文每次成交一个单位，因此内部使用 `S=z*p`、`q=(本结果份额-互补结果份额)/z`。
`sigma_lot=z*sigma`，`k_lot=k/z`；gamma 保持为抵押货币的倒数。
净库存不是“买单提交数”，也不是持仓 USDT 市值。

AS 近似模型：

```text
r = S - q * gamma * sigma_lot^2 * tau
h = gamma * sigma_lot^2 * tau / 2 + log(1 + gamma/k_lot) / gamma
bid = (r-h)/z
ask = (r+h)/z
```

GLFT 有限库存模型：

```text
Q = floor(MM_MAX_POSITION_SHARES / z)
alpha = k_lot * gamma * sigma_lot^2 / 2
eta = A * (1 + gamma/k_lot)^(-1-k_lot/gamma)
M[q,q] = alpha*q^2
M[q,q+1] = M[q+1,q] = -eta
v(t) = exp(-M*tau) * ones
delta_bid = log(v[q]/v[q+1])/k_lot + log(1+gamma/k_lot)/gamma
delta_ask = log(v[q]/v[q-1])/k_lot + log(1+gamma/k_lot)/gamma
bid = (S-delta_bid)/z
ask = (S+delta_ask)/z
```

边界不增加超限库存。指数矩阵作用用正项级数计算，逐段缩放，避免直接求特征向量时的抵消和指数溢出；相对截断阈值为 `1e-14`。
GLFT 默认终端条件为 `v(T)=1`，没有额外终端清仓惩罚。
部分成交产生非整数 q 时，在相邻整数状态的报价距离之间插值；这部分是工程近似。
Q 最大为 200；计算量超限或数值无效时跳过该市场，不返回不可信价格。

## 参数标定

| 参数 | 含义 | 注意 |
|---|---|---|
| `MM_RISK_AVERSION` | gamma，风险厌恶 | 必须结合每单份额和持仓规模调整 |
| `MM_ORDER_DECAY_K` | k，成交强度随报价距离的衰减 | 绝对价格的倒数，不是 bps |
| `MM_ARRIVAL_RATE_A` | A，中间价处的单侧成交强度 | 每秒完整报价单位的成交率；AS 近似式不使用 A |
| `MM_INITIAL_SIGMA` | 绝对价格波动率初值及下限 | 价格单位/√秒，不是年化收益波动率 |
| `MM_VOLATILITY_WINDOW_SECONDS` | 波动率窗口 | 使用盘口更新时间，重复快照不增加样本 |
| `MM_VOLATILITY_MIN_SAMPLES` | 最少样本点 | 不足时使用初值 |
| `MM_MODEL_HORIZON_SECONDS` | 滚动规划期限 tau | 不是结算倒计时 |

运行时用 `sum(中间价变化²)/sum(经过秒数)` 估计单位时间方差，保留初始 sigma 作为下限。
这包括短期趋势贡献，也不解决跳跃和知情交易风险。没有自动标定 A 和 k：应收集不同报价距离下的实际暴露时间及成交数量，
拟合 `lambda(delta)=A*exp(-k*delta)`，并考虑队列、订单数量、撤单延迟、部分成交和不同市场状态。
原论文的股票示例参数不能直接用于概率价格。

## 二元市场适配

- 以 API 的 `indexSet=1` 作为原始盘口，另一个结果使用买价 `1-原卖价`、卖价 `1-原买价`。
- `MM_OUTCOME=AUTO` 有不平衡库存时继续报价库存较多的一侧，使旧仓在趋势反转后仍有卖出机会；净库存为零时选当前价格较高的一侧。
- 固定 `YES`/`NO` 对应 indexSet 1/2，也可使用唯一的实际结果名称；身份始终用 token ID。
- 新策略不使用旧策略的 `MM_MIN_PROBABILITY`、`MM_HALF_SPREAD_BPS`、`MM_INVENTORY_SKEW_BPS`、`MM_MIN_SPREAD_BPS`。
  这些方向筛选/固定价差规则不属于论文模型，尤其不能因为旧仓概率降低就停止其退出报价。
- 买价向下、卖价向上取整到 0.01，保持被动报价。模型原价超出 0.01–0.99 时跳过该侧，不强行夹到边界。
- 不裸卖空；持有不足一个 lot 时不挂该侧卖单。零碎仓位不会自动清理。
- 不自动放大数量来满足最低金额。固定 lot 达不到最小订单金额或超过最大订单金额时跳过该侧。
- GLFT 的实际库存边界为 `floor(上限/lot)*lot`，建议将上限设为 lot 的整数倍。
- 一次只报价一个结果，避免两组互补报价同时成交造成的重复敞口。配对库存不自动合并。
- 买卖均按现有网关下单，继续执行 USDT 成本和余额限制以及持有份额检查。

每轮先撤本策略已记录的旧单，再分页查询订单确认其已非 OPEN，再对账库存，然后计算新报价。
撤单失败保留哈希下轮重试；持仓读取失败、异常盘口、陈旧盘口或市场关闭时停止该轮报价。
同价也会按刷新周期撤换，沿用现有框架；频繁撤换可能损失排队位置。
`MM_MAX_BOOK_AGE_SECONDS` 对最后更新时间严格检查，安静但长时间未更新的盘口也会被保守排除。

## 到期与剩余风险

两篇论文假设参考价格为布朗运动、按终端市场价格估值；二元事件实际可能跳到 0 或 1。
因此这不是二元结算过程的精确最优解，也不保证终端库存为零。

可配置显式事件结束时间（JSON，必须带时区）：

```dotenv
MM_MARKET_END_TIMES={"123":"2026-12-01T12:00:00Z"}
MM_STOP_BEFORE_END_SECONDS=10
```

有结束时间时 tau 取规划期限与剩余时间的较小值；进入停止窗口后，在下一刷新轮撤单并停止报价。
未配置时持续使用滚动期限，仅依赖市场 OPEN 状态；不会从标题猜测截止时间。
此机制只撤单，不强制平仓，不提供主动止损、趋势预测、自动合并或跨市场对冲。

API 成交/持仓更新不是原子快照，可能存在传播延迟；不能保证极端行情中没有撤单期间成交。
哈希跟踪沿用原框架，保存在进程内，崩溃重启不会自动接管遗留订单。
切换策略或重启前需检查并清理遗留订单；同一市场避免多个实例和外部交易同时增加敞口。
启动新策略不会自动取消账户中其他来源的订单。

## 验证

调试时在 `.env` 中设置 `LOG_LEVEL=DEBUG` 并重启。日志会输出刷新等待与耗时、撤单数量、盘口年龄和深度、
波动率样本数/覆盖时长/原始估计/下限是否生效、净库存与 lot 数、模型原始报价、最终取整报价与订单金额。
未报价时会记录具体原因，包括库存边界、持仓不足、最低金额、陈旧盘口、结果选择失败和到期窗口。
启动时 INFO 级别会输出模型参数；详细决策使用 DEBUG 级别。日志不新增私钥、签名或认证令牌输出。

```bash
python -m unittest discover -s tests -v
```

测试覆盖 AS 公式、GLFT 终端条件/小时间极限/可解析矩阵解、库存方向与对称性、lot 单位换算、
库存边界、数值稳定性、互补盘口、反转保留旧仓卖单、禁止裸卖、撤单确认、持仓失败和结束时间。
这些验证模型与执行逻辑，不构成盈利回测；实盘前还需用真实市场数据评估手续费、成交概率和库存损益。
