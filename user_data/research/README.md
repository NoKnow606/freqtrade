# 策略研究

| 文档 | 内容 |
|---|---|
| [okx_scalper_study.md](okx_scalper_study.md) | 一位 OKX 高频剥头皮交易者 69 天账单的量化拆解：打法、赢/亏行为分界、反事实回放、可借鉴部分与优化方案 |
| [okx_bill_load.py](okx_bill_load.py) | 账单加载、round-trip 重建、规则叠加回放的可复用代码 |
| [okx_trips.csv](okx_trips.csv) | 重建出的 954 笔交易，含 mae/mfe/size_pct/day_pnl_before 等特征 |

相关实现：

- `../strategies/RegimeStrategy.py` — 信号驱动的动态参数策略，执行层（仓位倍数、止损、时间出场、regime 切换平仓）可复用
- `../regime_signals.py` — 信号存储与 as-of join，保证回测无前视
- `../regime_producer.py` — 信号生产者（规则分类器 / 回填 / 守护进程）
- `../deploy/` — Docker Compose 部署
