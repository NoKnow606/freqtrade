"""Reference signal producer for RegimeStrategy.

Runs as its own process on its own cadence and appends to the signal file the
strategy tails. Ships a rule-based classifier so the pipeline works end to end
without an LLM; swap ``classify`` for anything (LLM call, ML model, human
input) as long as it returns a ``Signal`` stamped with the time it was known.

    python user_data/regime_producer.py emit --regime risk_off --pair '*'
    python user_data/regime_producer.py classify --pair BTC/USDT:USDT --exchange okx
    python user_data/regime_producer.py backfill --pair BTC/USDT:USDT --exchange okx \
        --since 2026-06-01 --timeframe 4h
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from regime_signals import GLOBAL_PAIR, Signal, SignalStore, timeframe_to_minutes  # noqa: E402


DEFAULT_SIGNAL_FILE = Path(__file__).resolve().parent / "signals" / "regime.jsonl"


def classify(candles: pd.DataFrame, source: str = "rule_ema_adx") -> tuple[str, float]:
    """Trend/range/risk-off from a 4h OHLCV frame. Deterministic and cheap."""
    import talib.abstract as ta

    if len(candles) < 60:
        return "neutral", 0.0
    ema_fast = ta.EMA(candles, timeperiod=20)
    ema_slow = ta.EMA(candles, timeperiod=50)
    adx = ta.ADX(candles, timeperiod=14)
    atr = ta.ATR(candles, timeperiod=14)
    close = candles["close"]

    last_adx = float(adx.iloc[-1])
    atr_pct = float(atr.iloc[-1] / close.iloc[-1])
    ret_5 = float(close.iloc[-1] / close.iloc[-6] - 1)

    if atr_pct > 0.06 and ret_5 < -0.08:
        return "risk_off", min(1.0, atr_pct * 10)
    if last_adx >= 25:
        conf = min(1.0, (last_adx - 25) / 25 + 0.5)
        return ("trend_up" if ema_fast.iloc[-1] > ema_slow.iloc[-1] else "trend_down"), conf
    if last_adx < 18:
        return "range", min(1.0, (18 - last_adx) / 18 + 0.5)
    return "neutral", 0.3


def fetch_candles(exchange_id: str, pair: str, timeframe: str, since: datetime | None, limit: int):
    import ccxt

    ex = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    rows: list[list[float]] = []
    since_ms = int(since.timestamp() * 1000) if since else None
    while True:
        batch = ex.fetch_ohlcv(pair, timeframe, since=since_ms, limit=limit)
        if not batch:
            break
        rows.extend(batch)
        if since_ms is None or len(batch) < limit:
            break
        since_ms = batch[-1][0] + 1
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["date"], unit="ms", utc=True)
    return df.drop_duplicates("date").reset_index(drop=True)


def cmd_emit(args: argparse.Namespace) -> None:
    store = SignalStore(args.signal_file)
    sig = Signal(
        ts=datetime.now(UTC),
        regime=args.regime,
        pair=args.pair,
        confidence=args.confidence,
        source=args.source or "manual",
    )
    store.append(sig)
    print(f"appended {sig.regime} for {sig.pair} @ {sig.ts.isoformat()} -> {store.path}")


def cmd_classify(args: argparse.Namespace) -> None:
    store = SignalStore(args.signal_file)
    candles = fetch_candles(args.exchange, args.pair, args.timeframe, None, 200)
    regime, conf = classify(candles)
    sig = Signal(ts=datetime.now(UTC), regime=regime, pair=args.pair, confidence=conf,
                 source=f"rule:{args.exchange}:{args.timeframe}")
    store.append(sig)
    print(f"{args.pair}: {regime} ({conf:.2f}) -> {store.path}")


def cmd_backfill(args: argparse.Namespace) -> None:
    """Replay the classifier over history so a backtest has the same signal
    stream a live producer would have written. Each signal is stamped with the
    close time of the candle it was computed from, i.e. the earliest moment it
    could have existed."""
    from freqtrade.exchange import timeframe_to_minutes

    store = SignalStore(args.signal_file)
    since = datetime.fromisoformat(args.since).replace(tzinfo=UTC)
    candles = fetch_candles(args.exchange, args.pair, args.timeframe, since, 300)
    tf_minutes = timeframe_to_minutes(args.timeframe)
    signals: list[Signal] = []
    prev = None
    for end in range(60, len(candles) + 1):
        window = candles.iloc[:end]
        regime, conf = classify(window)
        if regime == prev and not args.every_candle:
            continue
        prev = regime
        known_at = window["date"].iloc[-1].to_pydatetime() + pd.Timedelta(minutes=tf_minutes)
        signals.append(Signal(ts=known_at, regime=regime, pair=args.pair, confidence=conf,
                              source=f"backfill:{args.exchange}:{args.timeframe}"))
    store.append(*signals)
    print(f"wrote {len(signals)} signals for {args.pair} -> {store.path}")


def cmd_daemon(args: argparse.Namespace) -> None:
    """Classify every pair on a fixed cadence, forever. Errors on one pair never
    stop the loop; a stale signal is preferable to a dead producer, and the
    strategy's stale_after_minutes falls back to default_regime anyway."""
    import time
    import traceback

    store = SignalStore(args.signal_file)
    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    print(f"daemon: {len(pairs)} pairs every {args.interval}s -> {store.path}", flush=True)
    while True:
        started = time.monotonic()
        for pair in pairs:
            try:
                candles = fetch_candles(args.exchange, pair, args.timeframe, None, 200)
                regime, conf = classify(candles)
                store.append(Signal(ts=datetime.now(UTC), regime=regime, pair=pair, confidence=conf,
                                    source=f"rule:{args.exchange}:{args.timeframe}"))
                print(f"{datetime.now(UTC).isoformat(timespec='seconds')} {pair}: {regime} ({conf:.2f})", flush=True)
            except Exception:
                traceback.print_exc()
        time.sleep(max(1.0, args.interval - (time.monotonic() - started)))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--signal-file", default=str(DEFAULT_SIGNAL_FILE))
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("emit", help="append a manual signal")
    e.add_argument("--regime", required=True)
    e.add_argument("--pair", default=GLOBAL_PAIR)
    e.add_argument("--confidence", type=float, default=1.0)
    e.add_argument("--source", default="")
    e.set_defaults(func=cmd_emit)

    c = sub.add_parser("classify", help="classify live candles once and append")
    c.add_argument("--pair", required=True)
    c.add_argument("--exchange", default="okx")
    c.add_argument("--timeframe", default="4h")
    c.set_defaults(func=cmd_classify)

    b = sub.add_parser("backfill", help="replay classifier over history for backtesting")
    b.add_argument("--pair", required=True)
    b.add_argument("--exchange", default="okx")
    b.add_argument("--timeframe", default="4h")
    b.add_argument("--since", required=True, help="ISO date, e.g. 2026-06-01")
    b.add_argument("--every-candle", action="store_true", help="emit even when regime unchanged")
    b.set_defaults(func=cmd_backfill)

    d = sub.add_parser("daemon", help="classify all pairs on a loop (for deployment)")
    d.add_argument("--pairs", required=True, help="comma-separated, e.g. BTC/USDT:USDT,ETH/USDT:USDT")
    d.add_argument("--exchange", default="okx")
    d.add_argument("--timeframe", default="4h")
    d.add_argument("--interval", type=int, default=900, help="seconds between passes")
    d.set_defaults(func=cmd_daemon)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
