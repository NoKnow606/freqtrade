from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from user_data.regime_signals import GLOBAL_PAIR, Signal, SignalStore

# freqtrade is needed for strategy tests; skip the whole file gracefully when
# the full install is not present (e.g. lightweight CI venv without TA-Lib).
pytest.importorskip("freqtrade", reason="freqtrade not installed")

from freqtrade.persistence import Trade  # noqa: E402
from freqtrade.persistence.custom_data import CustomDataWrapper  # noqa: E402
from freqtrade.resolvers import StrategyResolver  # noqa: E402


USER_DATA = Path(__file__).resolve().parents[1]
PAIR = "BTC/USDT:USDT"
T0 = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _custom_data_in_memory():
    CustomDataWrapper.use_db = False
    CustomDataWrapper.reset_custom_data()
    yield
    CustomDataWrapper.reset_custom_data()
    CustomDataWrapper.use_db = True


@pytest.fixture
def signal_file(tmp_path):
    return tmp_path / "regime.jsonl"


def base_conf() -> dict:
    return {
        "max_open_trades": 3,
        "stake_currency": "USDT",
        "stake_amount": 100,
        "timeframe": "15m",
        "dry_run": True,
        "dry_run_wallet": 1000,
        "stoploss": -0.10,
        "minimal_roi": {"0": 0.2},
        "trading_mode": "futures",
        "margin_mode": "isolated",
        "exchange": {"name": "okx", "pair_whitelist": [PAIR], "pair_blacklist": []},
        "pairlists": [{"method": "StaticPairList"}],
        "strategy": "RegimeStrategy",
        "strategy_path": str(USER_DATA / "strategies"),
        "user_data_dir": USER_DATA,
        "runmode": "backtest",
    }


@pytest.fixture
def make_strategy(signal_file):
    def _make(**regime_cfg):
        conf = base_conf()
        conf["regime"] = {"signal_file": str(signal_file), **regime_cfg}
        strategy = StrategyResolver.load_strategy(conf)
        strategy.bot_start()
        return strategy

    return _make


def emit(signal_file: Path, minutes: int, regime: str, pair: str = GLOBAL_PAIR, conf: float = 1.0):
    SignalStore(signal_file).append(
        Signal(ts=T0 + timedelta(minutes=minutes), regime=regime, pair=pair, confidence=conf)
    )


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def make_trade(is_short: bool = False, leverage: float = 1.0) -> Trade:
    trade = Trade(
        id=1,
        pair=PAIR,
        open_rate=100.0,
        amount=1.0,
        fee_open=0.0005,
        fee_close=0.0005,
        is_short=is_short,
        leverage=leverage,
        stake_amount=100.0,
        open_date=at(0),
    )
    return trade


def candles(n: int, tf_minutes: int = 15) -> pd.DataFrame:
    dates = pd.to_datetime([T0 + timedelta(minutes=tf_minutes * i) for i in range(n)], utc=True)
    close = pd.Series([100 + (i % 7) - 3 for i in range(n)], dtype=float)
    return pd.DataFrame(
        {"date": dates, "open": close, "high": close + 1, "low": close - 1,
         "close": close, "volume": 10.0}
    )


# ---- loading & config ----------------------------------------------------

def test_loads_with_defaults(make_strategy):
    s = make_strategy()
    assert s.default_regime == "neutral"
    assert set(s.profiles) >= {"neutral", "trend_up", "trend_down", "range", "risk_off"}


def test_profile_overrides_merge_into_defaults(make_strategy):
    s = make_strategy(profiles={"range": {"stoploss": -0.02}, "custom": {"style": "meanrev"}})
    assert s.profiles["range"].stoploss == -0.02
    assert s.profiles["range"].style == "meanrev"  # untouched field kept
    assert s.profiles["custom"].style == "meanrev"


def test_unknown_default_regime_rejected(make_strategy):
    with pytest.raises(ValueError, match="no profile"):
        make_strategy(default_regime="ghost")


# ---- regime resolution ---------------------------------------------------

def test_current_regime_uses_signal_time_not_wall_clock(make_strategy, signal_file):
    emit(signal_file, 30, "trend_up")
    s = make_strategy()
    assert s.current_regime(PAIR, at(29)) == "neutral"
    assert s.current_regime(PAIR, at(30)) == "trend_up"


def test_stale_signal_falls_back_to_default(make_strategy, signal_file):
    emit(signal_file, 0, "trend_up")
    s = make_strategy(stale_after_minutes=60)
    assert s.current_regime(PAIR, at(59)) == "trend_up"
    assert s.current_regime(PAIR, at(61)) == "neutral"


def test_bot_loop_start_hot_reloads_new_signals(make_strategy, signal_file):
    emit(signal_file, 0, "range")
    s = make_strategy()
    assert s.current_regime(PAIR, at(100)) == "range"
    emit(signal_file, 50, "risk_off")
    s.bot_loop_start(current_time=at(100))
    assert s.current_regime(PAIR, at(100)) == "risk_off"


# ---- per-trade hooks -----------------------------------------------------

def test_confirm_entry_blocked_in_risk_off(make_strategy, signal_file):
    emit(signal_file, 0, "risk_off")
    s = make_strategy()
    ok = s.confirm_trade_entry(pair=PAIR, order_type="limit", amount=1, rate=100,
                               time_in_force="gtc", current_time=at(5), entry_tag=None, side="long")
    assert ok is False


def test_confirm_entry_respects_direction(make_strategy, signal_file):
    emit(signal_file, 0, "trend_up")
    s = make_strategy()
    kw = dict(pair=PAIR, order_type="limit", amount=1, rate=100, time_in_force="gtc",
              current_time=at(5), entry_tag=None)
    assert s.confirm_trade_entry(side="long", **kw) is True
    assert s.confirm_trade_entry(side="short", **kw) is False


def test_confirm_entry_min_confidence(make_strategy, signal_file):
    emit(signal_file, 0, "trend_up", conf=0.4)
    s = make_strategy(profiles={"trend_up": {"min_confidence": 0.6}})
    ok = s.confirm_trade_entry(pair=PAIR, order_type="limit", amount=1, rate=100,
                               time_in_force="gtc", current_time=at(5), entry_tag=None, side="long")
    assert ok is False


def test_stake_and_leverage_follow_profile(make_strategy, signal_file):
    emit(signal_file, 0, "trend_up")
    s = make_strategy(profiles={"trend_up": {"stake_multiplier": 0.5, "leverage": 3.0}})
    kw = dict(pair=PAIR, current_time=at(5), current_rate=100, entry_tag=None, side="long")
    assert s.custom_stake_amount(proposed_stake=100, min_stake=10, max_stake=1000, leverage=1, **kw) == 50
    assert s.leverage(proposed_leverage=1, max_leverage=10, **kw) == 3.0
    assert s.leverage(proposed_leverage=1, max_leverage=2, **kw) == 2.0


def test_stake_zero_when_profile_disables(make_strategy, signal_file):
    emit(signal_file, 0, "risk_off")
    s = make_strategy()
    stake = s.custom_stake_amount(pair=PAIR, current_time=at(5), current_rate=100,
                                  proposed_stake=100, min_stake=10, max_stake=1000,
                                  leverage=1, entry_tag=None, side="long")
    assert stake == 0.0


def test_stoploss_only_tightens_on_regime_change(make_strategy, signal_file):
    emit(signal_file, 0, "trend_up")   # stoploss -0.05
    s = make_strategy()
    trade = make_trade()
    trade.set_custom_data("regime", "trend_up")
    # Regime tightens to range (-0.03): stop moves closer.
    emit(signal_file, 10, "range")
    s.bot_loop_start(current_time=at(20))
    sl_tight = s.custom_stoploss(PAIR, trade, at(20), 100.0, 0.0, after_fill=False)
    assert sl_tight == pytest.approx(0.03)
    # Regime loosens back to neutral (-0.04): still capped at entry's -0.05, so -0.04.
    emit(signal_file, 30, "neutral")
    s.bot_loop_start(current_time=at(40))
    sl_now = s.custom_stoploss(PAIR, trade, at(40), 100.0, 0.0, after_fill=False)
    assert sl_now == pytest.approx(0.04)


def test_custom_exit_on_regime_change_when_profile_says_so(make_strategy, signal_file):
    emit(signal_file, 0, "trend_up")
    s = make_strategy()
    trade = make_trade()
    trade.set_custom_data("regime", "trend_up")
    assert s.custom_exit(PAIR, trade, at(5), 100.0, 0.0) is None
    emit(signal_file, 10, "risk_off")
    s.bot_loop_start(current_time=at(15))
    assert s.custom_exit(PAIR, trade, at(15), 100.0, 0.0) == "regime_risk_off"


# ---- dataframe path ------------------------------------------------------

def test_populate_indicators_attaches_regime_columns(make_strategy, signal_file):
    emit(signal_file, 0, "range")
    # Signal known at minute 900. Candle 58 closes at 885 (not visible),
    # candle 59 closes at 900 (visible on close), candle 60 onwards visible.
    emit(signal_file, 60 * 15, "trend_up")
    s = make_strategy()
    df = s.populate_indicators(candles(120), {"pair": PAIR})
    assert {"regime", "regime_confidence", "p_style", "p_allow_long"} <= set(df.columns)
    assert df["regime"].iloc[0] == "range"
    assert df["regime"].iloc[58] == "range"
    assert df["regime"].iloc[59] == "trend_up"
    assert df["p_style"].iloc[0] == "meanrev"
    assert df["p_style"].iloc[59] == "trend"


def test_entry_trend_respects_regime_direction(make_strategy, signal_file):
    emit(signal_file, 0, "trend_down")
    s = make_strategy()
    df = s.populate_indicators(candles(200), {"pair": PAIR})
    df = s.populate_entry_trend(df, {"pair": PAIR})
    assert df.get("enter_long", pd.Series(dtype=float)).fillna(0).sum() == 0
