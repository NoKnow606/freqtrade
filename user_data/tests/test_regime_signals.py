from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from user_data.regime_signals import (
    GLOBAL_PAIR,
    Signal,
    SignalStore,
    latest_as_of,
    merge_signals_asof,
)


T0 = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


def sig(minutes: int, regime: str, pair: str = GLOBAL_PAIR, conf: float = 1.0, **params) -> Signal:
    return Signal(ts=T0 + timedelta(minutes=minutes), regime=regime, pair=pair,
                  confidence=conf, params=params)


def candles(n: int, tf_minutes: int = 15) -> pd.DataFrame:
    dates = [T0 + timedelta(minutes=tf_minutes * i) for i in range(n)]
    return pd.DataFrame({"date": pd.to_datetime(dates, utc=True), "close": range(n)})


# ---- Signal -------------------------------------------------------------

def test_signal_requires_tz_aware_ts():
    with pytest.raises(ValueError, match="timezone-aware"):
        Signal(ts=datetime(2026, 9, 1), regime="range")


def test_signal_confidence_bounds():
    with pytest.raises(ValueError):
        Signal(ts=T0, regime="range", confidence=1.5)


def test_signal_record_roundtrip():
    s = sig(5, "trend_up", pair="BTC/USDT:USDT", conf=0.8, stoploss=-0.03)
    assert Signal.from_record(s.to_record()) == s


# ---- SignalStore --------------------------------------------------------

def test_store_append_and_load_sorted(tmp_path):
    store = SignalStore(tmp_path / "s.jsonl")
    store.append(sig(30, "range"))
    store.append(sig(10, "trend_up"), sig(20, "neutral"))
    df = store.load()
    assert list(df["regime"]) == ["trend_up", "neutral", "range"]
    assert df["ts"].dt.tz is not None


def test_store_load_missing_file_is_empty(tmp_path):
    df = SignalStore(tmp_path / "nope.jsonl").load()
    assert df.empty
    assert "regime" in df.columns


def test_store_rewrite_replaces_content(tmp_path):
    store = SignalStore(tmp_path / "s.jsonl")
    store.append(sig(10, "a"), sig(20, "b"))
    store.rewrite([sig(5, "c")])
    assert list(store.load()["regime"]) == ["c"]


# ---- latest_as_of -------------------------------------------------------

def test_latest_as_of_ignores_future_signals(tmp_path):
    store = SignalStore(tmp_path / "s.jsonl")
    store.append(sig(10, "range"), sig(50, "trend_up"))
    df = store.load()
    assert latest_as_of(df, "BTC/USDT:USDT", T0 + timedelta(minutes=30)).regime == "range"
    assert latest_as_of(df, "BTC/USDT:USDT", T0 + timedelta(minutes=50)).regime == "trend_up"
    assert latest_as_of(df, "BTC/USDT:USDT", T0 + timedelta(minutes=9)) is None


def test_latest_as_of_pair_specific_beats_global_on_tie(tmp_path):
    store = SignalStore(tmp_path / "s.jsonl")
    store.append(sig(10, "global_r"), sig(10, "pair_r", pair="ETH/USDT:USDT"))
    df = store.load()
    assert latest_as_of(df, "ETH/USDT:USDT", T0 + timedelta(minutes=10)).regime == "pair_r"
    assert latest_as_of(df, "BTC/USDT:USDT", T0 + timedelta(minutes=10)).regime == "global_r"


def test_latest_as_of_newer_global_overrides_older_pair(tmp_path):
    store = SignalStore(tmp_path / "s.jsonl")
    store.append(sig(10, "pair_r", pair="ETH/USDT:USDT"), sig(20, "risk_off"))
    df = store.load()
    assert latest_as_of(df, "ETH/USDT:USDT", T0 + timedelta(minutes=25)).regime == "risk_off"


def test_latest_as_of_rejects_naive_now():
    with pytest.raises(ValueError):
        latest_as_of(SignalStore("/nonexistent").load(), "X", datetime(2026, 1, 1))


# ---- merge_signals_asof (the no-look-ahead guarantee) -------------------

def test_merge_attaches_signal_to_closing_candle_not_earlier(tmp_path):
    # 15m candles open at 0,15,30,45. Signal known at minute 20 must first
    # appear on the candle that closes at >=20, i.e. the one opening at 15.
    store = SignalStore(tmp_path / "s.jsonl")
    store.append(sig(20, "trend_up"))
    out = merge_signals_asof(candles(4), store.load(), "BTC/USDT:USDT", "15m")
    assert list(out["regime"]) == ["neutral", "trend_up", "trend_up", "trend_up"]


def test_merge_signal_exactly_at_close_is_visible(tmp_path):
    store = SignalStore(tmp_path / "s.jsonl")
    store.append(sig(15, "range"))
    out = merge_signals_asof(candles(3), store.load(), "BTC/USDT:USDT", "15m")
    assert list(out["regime"]) == ["range", "range", "range"]


def test_merge_no_signals_uses_default(tmp_path):
    out = merge_signals_asof(candles(3), SignalStore(tmp_path / "x").load(),
                             "BTC/USDT:USDT", "15m", default_regime="flat")
    assert set(out["regime"]) == {"flat"}
    assert set(out["regime_confidence"]) == {0.0}


def test_merge_preserves_row_order_and_index(tmp_path):
    # Candle opens: 0, 15, 30, 45, 60.  Close times: 15, 30, 45, 60, 75.
    # sig(0, "a") known at minute 0 → first visible at candle whose close >= 0 → candle 0.
    # sig(45, "b") known at minute 45 → first visible at candle whose close >= 45 → candle 2 (close=45).
    store = SignalStore(tmp_path / "s.jsonl")
    store.append(sig(0, "a"), sig(45, "b"))
    df = candles(5)
    out = merge_signals_asof(df, store.load(), "BTC/USDT:USDT", "15m")
    assert list(out.index) == list(df.index)
    assert list(out["close"]) == list(df["close"])
    assert list(out["regime"]) == ["a", "a", "b", "b", "b"]


def test_merge_pair_scoped_over_global(tmp_path):
    store = SignalStore(tmp_path / "s.jsonl")
    store.append(sig(0, "g"), sig(30, "p", pair="ETH/USDT:USDT"))
    eth = merge_signals_asof(candles(4), store.load(), "ETH/USDT:USDT", "15m")
    btc = merge_signals_asof(candles(4), store.load(), "BTC/USDT:USDT", "15m")
    assert list(eth["regime"]) == ["g", "p", "p", "p"]
    assert list(btc["regime"]) == ["g", "g", "g", "g"]


def test_merge_handles_mixed_datetime_resolutions(tmp_path):
    # Feather/parquet candle data is datetime64[ns]; JSON-parsed signal ts may be
    # datetime64[us]. merge_asof refuses mismatched units unless we normalise.
    store = SignalStore(tmp_path / "s.jsonl")
    store.append(sig(15, "range"))
    signals = store.load()
    signals["ts"] = signals["ts"].dt.as_unit("us")
    df = candles(3)
    df["date"] = df["date"].dt.as_unit("ns")
    out = merge_signals_asof(df, signals, "BTC/USDT:USDT", "15m")
    assert list(out["regime"]) == ["range", "range", "range"]


def test_merge_never_leaks_future_signal_bruteforce(tmp_path):
    # For every candle, the attached regime must equal latest_as_of(close_time).
    store = SignalStore(tmp_path / "s.jsonl")
    store.append(*[sig(m, f"r{m}") for m in (3, 17, 29, 44, 61, 90)])
    df = store.load()
    out = merge_signals_asof(candles(8), df, "BTC/USDT:USDT", "15m")
    for _, row in out.iterrows():
        close_t = row["date"].to_pydatetime() + timedelta(minutes=15)
        expected = latest_as_of(df, "BTC/USDT:USDT", close_t)
        assert row["regime"] == (expected.regime if expected else "neutral")
