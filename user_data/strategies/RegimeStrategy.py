"""Signal-driven strategy: an external regime signal selects which parameter
profile the strategy runs with. Structure changes ("switch from trend to
mean-reversion") are expressed as profile changes so they stay backtestable.

Every hook reads the regime through ``current_time`` supplied by freqtrade,
never the wall clock, so live, dry-run and backtest see identical signals.

Note: no ``from __future__ import annotations`` here on purpose — freqtrade
loads strategy files without registering them in ``sys.modules`` and
``dataclass`` cannot resolve string annotations in that situation.
"""

import logging
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import talib.abstract as ta
from pandas import DataFrame

from freqtrade.persistence import Order, Trade
from freqtrade.strategy import IStrategy, stoploss_from_open


_USER_DATA = Path(__file__).resolve().parent.parent
if str(_USER_DATA) not in sys.path:
    sys.path.insert(0, str(_USER_DATA))

from regime_signals import (  # noqa: E402
    DEFAULT_REGIME,
    Signal,
    SignalStore,
    latest_as_of,
    merge_signals_asof,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RegimeProfile:
    style: str = "trend"  # trend | meanrev | off
    allow_long: bool = True
    allow_short: bool = True
    stoploss: float = -0.04
    stake_multiplier: float = 1.0
    leverage: float = 1.0
    min_confidence: float = 0.0
    exit_on_regime_change: bool = False
    rsi_trend_min: float = 52.0
    rsi_meanrev_long: float = 30.0
    rsi_meanrev_short: float = 70.0


DEFAULT_PROFILES: dict[str, RegimeProfile] = {
    "neutral": RegimeProfile(stake_multiplier=0.5, leverage=1.0),
    "trend_up": RegimeProfile(style="trend", allow_short=False, stoploss=-0.05, leverage=2.0),
    "trend_down": RegimeProfile(style="trend", allow_long=False, stoploss=-0.05, leverage=2.0),
    "range": RegimeProfile(style="meanrev", stoploss=-0.03, stake_multiplier=0.75),
    "risk_off": RegimeProfile(style="off", stake_multiplier=0.0, exit_on_regime_change=True),
}


class RegimeStrategy(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "15m"
    can_short = True
    startup_candle_count = 60
    process_only_new_candles = True

    # Hard floor; the active profile tightens it via custom_stoploss.
    stoploss = -0.10
    use_custom_stoploss = True
    minimal_roi = {"0": 0.20}

    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }

    ema_fast = 12
    ema_slow = 34
    rsi_period = 14

    # ---- lifecycle -------------------------------------------------------

    def bot_start(self, **kwargs) -> None:
        cfg: dict[str, Any] = self.config.get("regime", {})
        signal_file = cfg.get("signal_file") or str(
            Path(self.config["user_data_dir"]) / "signals" / "regime.jsonl"
        )
        self.signal_store = SignalStore(signal_file)
        self.default_regime: str = cfg.get("default_regime", DEFAULT_REGIME)
        self.stale_after = timedelta(minutes=int(cfg.get("stale_after_minutes", 240)))
        self.profiles = self._build_profiles(cfg.get("profiles", {}))
        self._signals = self.signal_store.load()
        self._signals_mtime = self._file_mtime()
        logger.info(
            "RegimeStrategy: %d signals from %s, profiles=%s",
            len(self._signals),
            signal_file,
            sorted(self.profiles),
        )

    def bot_loop_start(self, current_time: datetime, **kwargs) -> None:
        mtime = self._file_mtime()
        if mtime != self._signals_mtime:
            self._signals = self.signal_store.load()
            self._signals_mtime = mtime
            logger.info("RegimeStrategy: reloaded %d signals", len(self._signals))

    # ---- regime resolution ----------------------------------------------

    def current_signal(self, pair: str, now: datetime) -> Signal | None:
        sig = latest_as_of(self._signals, pair, now)
        if sig is None:
            return None
        if now - sig.ts > self.stale_after:
            return None
        return sig

    def current_regime(self, pair: str, now: datetime) -> str:
        sig = self.current_signal(pair, now)
        return sig.regime if sig else self.default_regime

    def profile_for(self, regime: str) -> RegimeProfile:
        return self.profiles.get(regime) or self.profiles[self.default_regime]

    def _build_profiles(self, overrides: dict[str, dict[str, Any]]) -> dict[str, RegimeProfile]:
        profiles = dict(DEFAULT_PROFILES)
        for name, fields in overrides.items():
            base = profiles.get(name, RegimeProfile())
            profiles[name] = replace(base, **fields)
        if self.default_regime not in profiles:
            raise ValueError(f"default_regime '{self.default_regime}' has no profile")
        return profiles

    def _file_mtime(self) -> float:
        try:
            return self.signal_store.path.stat().st_mtime_ns
        except FileNotFoundError:
            return -1.0

    # ---- indicators & signals -------------------------------------------

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=self.ema_fast)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=self.ema_slow)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=self.rsi_period)
        dataframe = merge_signals_asof(
            dataframe,
            self._signals,
            pair=metadata["pair"],
            timeframe=self.timeframe,
            default_regime=self.default_regime,
        )
        # Vectorised per-row profile lookup so entry rules can vary by regime.
        prof = dataframe["regime"].map(self.profile_for)
        dataframe["p_style"] = prof.map(lambda p: p.style)
        dataframe["p_allow_long"] = prof.map(lambda p: p.allow_long)
        dataframe["p_allow_short"] = prof.map(lambda p: p.allow_short)
        dataframe["p_min_conf"] = prof.map(lambda p: p.min_confidence)
        dataframe["p_rsi_trend_min"] = prof.map(lambda p: p.rsi_trend_min)
        dataframe["p_rsi_mr_long"] = prof.map(lambda p: p.rsi_meanrev_long)
        dataframe["p_rsi_mr_short"] = prof.map(lambda p: p.rsi_meanrev_short)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        conf_ok = df["regime_confidence"] >= df["p_min_conf"]
        trend = df["p_style"] == "trend"
        meanrev = df["p_style"] == "meanrev"

        cross_up = (df["ema_fast"] > df["ema_slow"]) & (
            df["ema_fast"].shift(1) <= df["ema_slow"].shift(1)
        )
        cross_down = (df["ema_fast"] < df["ema_slow"]) & (
            df["ema_fast"].shift(1) >= df["ema_slow"].shift(1)
        )

        long_trend = trend & cross_up & (df["rsi"] > df["p_rsi_trend_min"])
        short_trend = trend & cross_down & (df["rsi"] < 100 - df["p_rsi_trend_min"])
        long_mr = meanrev & (df["rsi"] < df["p_rsi_mr_long"])
        short_mr = meanrev & (df["rsi"] > df["p_rsi_mr_short"])

        df.loc[conf_ok & df["p_allow_long"] & (long_trend | long_mr), "enter_long"] = 1
        df.loc[conf_ok & df["p_allow_short"] & (short_trend | short_mr), "enter_short"] = 1
        df.loc[df["enter_long"] == 1, "enter_tag"] = "long_" + df["regime"].astype(str)
        df.loc[df["enter_short"] == 1, "enter_tag"] = "short_" + df["regime"].astype(str)
        return df

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        meanrev = df["p_style"] == "meanrev"
        df.loc[(df["ema_fast"] < df["ema_slow"]) | (meanrev & (df["rsi"] > 55)), "exit_long"] = 1
        df.loc[(df["ema_fast"] > df["ema_slow"]) | (meanrev & (df["rsi"] < 45)), "exit_short"] = 1
        return df

    # ---- per-trade hooks (run every loop, regime read as of current_time) --

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> bool:
        # The dataframe signal may be up to one candle old; re-check against the
        # regime known right now so a fresh risk_off blocks the entry.
        sig = self.current_signal(pair, current_time)
        regime = sig.regime if sig else self.default_regime
        prof = self.profile_for(regime)
        if prof.style == "off" or prof.stake_multiplier <= 0:
            return False
        if side == "long" and not prof.allow_long:
            return False
        if side == "short" and not prof.allow_short:
            return False
        if sig and sig.confidence < prof.min_confidence:
            return False
        return True

    def order_filled(
        self, pair: str, trade: Trade, order: Order, current_time: datetime, **kwargs
    ) -> None:
        if order.ft_order_side == trade.entry_side and trade.get_custom_data("regime") is None:
            sig = self.current_signal(pair, current_time)
            trade.set_custom_data("regime", sig.regime if sig else self.default_regime)
            trade.set_custom_data("regime_ts", sig.ts.isoformat() if sig else None)

    def custom_stake_amount(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_stake: float,
        min_stake: float | None,
        max_stake: float,
        leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> float:
        prof = self.profile_for(self.current_regime(pair, current_time))
        stake = proposed_stake * prof.stake_multiplier
        if stake <= 0:
            return 0.0
        if min_stake is not None and stake < min_stake:
            return 0.0
        return min(stake, max_stake)

    def leverage(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> float:
        prof = self.profile_for(self.current_regime(pair, current_time))
        return max(1.0, min(prof.leverage, max_leverage))

    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs,
    ) -> float | None:
        # Tightest of entry-time and current profile: a regime change may
        # tighten protection but never loosens it for an open trade. Profile
        # stoploss is expressed relative to entry; freqtrade wants it relative
        # to the current rate, and only ever ratchets it in our favour.
        entry_prof = self.profile_for(trade.get_custom_data("regime") or self.default_regime)
        now_prof = self.profile_for(self.current_regime(pair, current_time))
        target = max(entry_prof.stoploss, now_prof.stoploss)
        return stoploss_from_open(
            target, current_profit, is_short=trade.is_short, leverage=trade.leverage
        )

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ) -> str | bool | None:
        regime = self.current_regime(pair, current_time)
        prof = self.profile_for(regime)
        entry_regime = trade.get_custom_data("regime")
        if prof.exit_on_regime_change and regime != entry_regime:
            return f"regime_{regime}"
        if trade.is_short and not prof.allow_short and prof.style == "off":
            return f"regime_{regime}"
        if not trade.is_short and not prof.allow_long and prof.style == "off":
            return f"regime_{regime}"
        return None
