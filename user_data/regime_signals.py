"""Append-only regime signal store shared by signal producers and RegimeStrategy.

Every signal carries the moment it became known (``ts``). Consumers must only
see signals with ``ts <= now`` — ``latest_as_of`` and ``merge_signals_asof``
are the two sanctioned read paths and both enforce that rule, which is what
keeps a backtest honest.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd


GLOBAL_PAIR = "*"
DEFAULT_REGIME = "neutral"

SIGNAL_COLUMNS = ["ts", "pair", "regime", "confidence", "params", "source"]


@dataclass(frozen=True)
class Signal:
    ts: datetime
    regime: str
    pair: str = GLOBAL_PAIR
    confidence: float = 1.0
    params: dict[str, Any] = field(default_factory=dict)
    source: str = ""

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None:
            raise ValueError("Signal.ts must be timezone-aware")
        if not self.regime:
            raise ValueError("Signal.regime must be non-empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("Signal.confidence must be within [0, 1]")

    def to_record(self) -> dict[str, Any]:
        rec = asdict(self)
        rec["ts"] = self.ts.astimezone(UTC).isoformat()
        rec["params"] = json.dumps(self.params, sort_keys=True)
        return rec

    @classmethod
    def from_record(cls, rec: dict[str, Any]) -> Signal:
        params = rec.get("params") or {}
        if isinstance(params, str):
            params = json.loads(params) if params else {}
        return cls(
            ts=pd.Timestamp(rec["ts"]).tz_convert(UTC).to_pydatetime(),
            regime=str(rec["regime"]),
            pair=str(rec.get("pair") or GLOBAL_PAIR),
            confidence=float(rec.get("confidence", 1.0)),
            params=params,
            source=str(rec.get("source") or ""),
        )


def _empty_frame() -> pd.DataFrame:
    df = pd.DataFrame({c: pd.Series(dtype="object") for c in SIGNAL_COLUMNS})
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df["confidence"] = df["confidence"].astype(float)
    return df


class SignalStore:
    """JSON-lines file, one signal per line, appended atomically.

    The file format is deliberately boring so any language can produce it and
    so a backtest can replay exactly what a live bot saw.
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)

    def append(self, *signals: Signal) -> None:
        if not signals:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(json.dumps(s.to_record(), sort_keys=True) + "\n" for s in signals)
        # O_APPEND writes below PIPE_BUF are atomic on POSIX; signals are tiny.
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())

    def load(self) -> pd.DataFrame:
        if not self.path.exists():
            return _empty_frame()
        rows: list[dict[str, Any]] = []
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        if not rows:
            return _empty_frame()
        df = pd.DataFrame(rows)
        for col in SIGNAL_COLUMNS:
            if col not in df.columns:
                df[col] = None
        df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.as_unit("ns")
        df["pair"] = df["pair"].fillna(GLOBAL_PAIR).astype(str)
        df["confidence"] = pd.to_numeric(df["confidence"], errors="coerce").fillna(1.0)
        df["params"] = df["params"].apply(_coerce_params)
        df["source"] = df["source"].fillna("").astype(str)
        return df[SIGNAL_COLUMNS].sort_values("ts", kind="stable").reset_index(drop=True)

    def rewrite(self, signals: list[Signal]) -> None:
        """Replace the whole file atomically (maintenance / compaction only)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".signals-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for s in sorted(signals, key=lambda s: s.ts):
                    fh.write(json.dumps(s.to_record(), sort_keys=True) + "\n")
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def timeframe_to_minutes(timeframe: str) -> int:
    """Same semantics as freqtrade.exchange.timeframe_to_minutes, kept local so
    this module (and its tests) run without the full freqtrade dependency tree."""
    unit = timeframe[-1]
    amount = int(timeframe[:-1])
    factor = {"m": 1, "h": 60, "d": 1440, "w": 10080}.get(unit)
    if factor is None:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    return amount * factor


def _coerce_params(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        return json.loads(value)
    return {}


def _pair_scoped(df: pd.DataFrame, pair: str) -> pd.DataFrame:
    return df[(df["pair"] == pair) | (df["pair"] == GLOBAL_PAIR)]


def latest_as_of(df: pd.DataFrame, pair: str, now: datetime) -> Signal | None:
    """Most recent signal for ``pair`` (pair-specific beats global on ties) known at ``now``."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    scoped = _pair_scoped(df, pair)
    scoped = scoped[scoped["ts"] <= pd.Timestamp(now)]
    if scoped.empty:
        return None
    # Stable sort keeps file order; pair-specific rows rank after global on equal ts
    # so they win when we take the last row.
    scoped = scoped.assign(_prio=(scoped["pair"] == pair).astype(int))
    row = scoped.sort_values(["ts", "_prio"], kind="stable").iloc[-1]
    return Signal.from_record(row.to_dict())


def merge_signals_asof(
    dataframe: pd.DataFrame,
    signals: pd.DataFrame,
    pair: str,
    timeframe: str,
    default_regime: str = DEFAULT_REGIME,
    date_column: str = "date",
) -> pd.DataFrame:
    """Attach ``regime`` / ``regime_confidence`` columns to a candle dataframe.

    A candle at open time ``t`` with timeframe ``tf`` is only fully known at
    ``t + tf``; we therefore join on candle *close* time so a signal generated
    mid-candle attaches to that candle's decision, never to the previous one.
    The look-ahead analyser in freqtrade will flag any drift from this rule.
    """
    out = dataframe.copy()
    if out.empty:
        out["regime"] = pd.Series(dtype="object")
        out["regime_confidence"] = pd.Series(dtype=float)
        return out

    # Normalise both join keys to UTC nanoseconds: candle data may arrive as
    # datetime64[ns] while JSON-parsed timestamps come back as datetime64[us],
    # and merge_asof refuses to join mismatched resolutions.
    close_time = pd.to_datetime(out[date_column], utc=True).dt.as_unit("ns") + pd.Timedelta(
        minutes=timeframe_to_minutes(timeframe)
    )
    left = pd.DataFrame({"_close": close_time.values, "_idx": out.index})

    scoped = _pair_scoped(signals, pair)
    if scoped.empty:
        out["regime"] = default_regime
        out["regime_confidence"] = 0.0
        return out

    scoped = scoped.assign(_prio=(scoped["pair"] == pair).astype(int))
    scoped = scoped.sort_values(["ts", "_prio"], kind="stable")
    # Keep one row per ts: the pair-specific one when both exist.
    scoped = scoped.drop_duplicates(subset="ts", keep="last")
    right = pd.DataFrame(
        {
            "ts": pd.to_datetime(scoped["ts"], utc=True).dt.as_unit("ns").values,
            "regime": scoped["regime"].values,
            "regime_confidence": scoped["confidence"].astype(float).values,
        }
    )

    merged = pd.merge_asof(
        left.sort_values("_close"),
        right,
        left_on="_close",
        right_on="ts",
        direction="backward",
        allow_exact_matches=True,
    ).sort_values("_idx")

    out["regime"] = merged["regime"].fillna(default_regime).values
    out["regime_confidence"] = merged["regime_confidence"].fillna(0.0).values
    return out
