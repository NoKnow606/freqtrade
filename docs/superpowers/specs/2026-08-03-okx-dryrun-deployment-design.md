# OKX 永续动态币池 Dry-run 部署设计

## 目标

在本机现有 `NoKnow606/freqtrade` 仓库上部署 `MultiTfBreakout1mV8`，只连接 OKX 公共行情并运行 Freqtrade `dry_run`。部署不得使用或要求 API Key，不得产生真实订单，也不把短期模拟结果表述为未来收益。

初始参数固定为：USDT 结算永续、逐仓、双向交易、100 USDT 模拟钱包、每笔 20 USDT 保证金、最多 3 笔同时持仓、动态 3–10 倍杠杆。策略按用户提供的 1m/15m/1h 逻辑运行。

## 约束与已知事实

- OKX 远端默认仓库分支为 `develop`；本地代码当前与 `origin/develop` 同步。
- 本机系统 Python 3.14 不适合作为项目运行环境，因此使用 Docker 隔离依赖。
- Freqtrade 内置 ticker 币池缓存 30 分钟，不能满足严格的 5 分钟三榜单刷新，因此采用用户批准的 A 方案：独立选币 sidecar。
- 用户粘贴的源码带有 Markdown 围栏且类内缩进丢失。实施时只恢复合法 Python 结构并做兼容性修正，不改变交易条件。
- 策略当前只使用突破后的回踩/反抽入场；源码中计算了 `long_breakout` 和 `short_breakout`，但没有把二者直接用于入场。这一行为保持不变。
- Freqtrade 将杠杆场景下的 `stoploss = -0.035` 解释为单笔风险比例。20 USDT 保证金对应计划亏损约 0.70 USDT，未计手续费、资金费率、点差和滑点；不是源码注释中的约 1.20 USDT。3–10 倍杠杆下，对应标的价格约 1.17%–0.35% 的反向波动。

## 架构

部署使用一个独立 Compose 文件和两个受 Docker 管理的服务，共享只包含部署运行数据的 `user_data` 目录：

1. `okx-pair-selector`：每 300 秒读取 OKX 公共市场、合约和 ticker 数据，生成本地 RemotePairList JSON。
2. `freqtrade-okx-dryrun`：等待首个有效币池后启动 Freqtrade，读取策略、dry-run 配置和 RemotePairList。

两个服务使用从当前仓库提交构建的同一 Freqtrade 镜像，避免系统 Python 与 Docker 镜像版本不一致。Compose 使用独立项目名和 `restart: unless-stopped`；日志、SQLite 数据库、生成币池和服务状态文件均持久化在 `user_data`，不进入 Git。

## 动态币池数据流

每次刷新先从 OKX USDT 线性永续市场中建立候选集，只保留：

- 市场处于 active 状态，类型为 swap，quote 和 settle 均为 USDT；
- 24 小时 quote volume 不低于 5,000,000 USDT；
- 有效 bid/ask 的点差 `1 - bid / ask` 不超过 0.003；
- OKX `listTime` 显示已上市至少 7 个完整自然日；无法验证上市时间时保守排除；
- ticker、价格、数量精度和最小下单条件完整，且可由 20 USDT 保证金、最低 3 倍杠杆形成有效订单。

在合格候选集中分别取得：24 小时涨幅前 10、跌幅前 10、成交额前 10。按“涨幅榜、跌幅榜、成交量榜”的顺序合并并去重，最多 30 个交易对，使用 CCXT/Freqtrade 永续命名，例如 `BTC/USDT:USDT`。

sidecar 以临时文件加原子替换的方式写出：

```json
{
  "pairs": ["BTC/USDT:USDT"],
  "refresh_period": 300
}
```

Freqtrade 使用本地 `RemotePairList` 每 300 秒读取文件。交易对离开币池不会强制平仓；已有仓位仍由止损、ROI、趋势退出和时间退出管理。

## 失败处理

- 单次 OKX 超时、限流或数据缺字段时保留上一份有效币池，并记录具体原因。
- 连续 3 次刷新失败或最后成功数据超过 15 分钟时，sidecar 原子写入空币池，阻止新增仓位；已有仓位继续由机器人管理。
- 恢复取得完整行情后自动恢复正常币池。
- 首次启动没有有效币池时，Freqtrade 不启动交易循环，且不会回退到未经筛选的静态币池。
- 生成结果为空时同样不新增仓，并把各过滤阶段数量写入日志，便于区分市场条件和程序错误。

## Freqtrade 配置

本地配置明确设置：

- `dry_run: true`，交易所凭据保持空值，API Server 和 Telegram 关闭；
- `exchange.name: okx`，`trading_mode: futures`，`margin_mode: isolated`；
- `stake_currency: USDT`，`dry_run_wallet: 100`，`stake_amount: 20`，`max_open_trades: 3`；
- `tradable_balance_ratio: 0.99`，避免模拟钱包全部占满；
- `liquidation_buffer: 0.10`，作为比默认值更保守的强平缓冲；
- 1m 主周期，策略类 `MultiTfBreakout1mV8`，本地 RemotePairList；
- 使用独立 dry-run SQLite 数据库和日志文件，避免覆盖其他机器人数据。

策略保留市场进出场和市场止损配置。需要明确：在 `dry_run` 中不会向 OKX 放置 `stoploss_on_exchange` 订单，止损由 Freqtrade 本地模拟。若未来考虑实盘，必须另行确认 OKX Buy/Sell 单向持仓模式、最小权限 API、reduce-only、标记价格止损、订单同步和断线恢复；本设计不授权实盘迁移。

## 实施文件

实施阶段预计创建以下本地部署文件：

- `user_data/strategies/MultiTfBreakout1mV1.py`：恢复格式后的用户策略，类名保持 `MultiTfBreakout1mV8`；
- `user_data/okx_pair_selector.py`：公共行情选币 sidecar；
- `user_data/config-okx-dryrun.json`：无密钥 dry-run 配置；
- `user_data/docker-compose.okx-dryrun.yml`：独立双服务 Compose；
- `user_data/runtime/okx-ranked-pairs.json`：运行时生成币池；
- `user_data/logs/` 与独立 SQLite 文件：运行日志和模拟交易记录。

这些文件位于仓库忽略的 `user_data` 中，属于本机部署，不推送到远端，也不创建 PR。设计文档本身单独提交，便于审计。

## 验证与验收

实施按以下顺序验证：

1. 对选币器使用合成市场/ticker 数据做单元测试，覆盖三榜单合并、成交额、点差、上市时间、精度、空池和连续失败关闭新增仓。
2. 对恢复后的策略执行 Python 编译检查和 Freqtrade `list-strategies`，确认类可加载、无未来函数引用错误。
3. 运行选币器 one-shot，检查输出仅含有效 OKX USDT 永续、最多 30 对且每项过滤规则可解释。
4. 运行 Freqtrade `test-pairlist` 和配置校验。
5. 启动两个持久容器并检查健康状态、策略加载、1m/15m/1h 数据预热、动态币池刷新、日志和 SQLite 写入。

初次交付只证明服务已就绪并持续运行，不以“已有交易”作为启动成功条件。策略门槛较严，短期没有信号属于正常结果。首次效果评估建议至少运行 24 小时，较可靠的观察期为 7 天；后续报告应至少包含交易数、胜率、盈亏比、净收益、手续费、资金费率、最大回撤、最大连续亏损、长短方向分布和动态杠杆分布。dry-run 无法完整复现市场冲击、极端滑点、真实强平和断线期间的成交结果。

## 持久服务交付

启动成功后交付 Compose 项目名、容器状态、日志位置、数据库位置和明确的停止命令。服务由 Docker 管理并设置自动重启；在没有额外系统级 supervisor 的前提下，跨 Docker Desktop 或主机重启的存活仍是 best-effort。该持久服务是交付物，不代表代理会在后台等待 24 小时后自动回报；用户可在观察期后触发新的统计检查。

## 风险边界

本设计仅用于策略研究和模拟盘验证，不构成个性化投资建议，也不保证盈利。资金费率、手续费、市场订单滑点、低流动性、标记价格与成交价偏离以及极端行情跳空都可能使实盘结果显著差于 dry-run。未经新的逐项实盘授权和上线前审查，不得加入密钥或切换 `dry_run: false`。
