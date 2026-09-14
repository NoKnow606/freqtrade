"""Load an OKX unified-account bill export and rebuild per-(symbol, side) round trips.

Usage (from repo root, with the freqtrade venv):

    from user_data.research.okx_bill_load import load_bill, build_trips, overlay_rules, summarize
    df = load_bill("path/to/欧易统一交易账单_*.csv")
    trips = build_trips(df)          # one row per open->flat cycle, hedge-mode aware
    summarize(overlay_rules(trips, size_cap=20, time_stop_min=5))

Column semantics that were verified against the ledger (see okx_scalper_study.md):
- 仓位余额 is per-(symbol, side) and always >= 0 in hedge mode; a fill with
  仓位余额变动 > 0 is an *open*, < 0 is a *close*, regardless of 买入/卖出.
- 买入 + open  -> long ;  卖出 + open  -> short
- 卖出 + close -> long ;  买入 + close -> short
- 收益 is realised PnL on that fill; 手续费 is negative.
"""

from __future__ import annotations

import glob

import numpy as np
import pandas as pd


NUMERIC = ["数量", "成交价", "收益", "手续费", "仓位余额变动", "仓位余额", "交易账户余额变动", "交易账户余额"]


def load_bill(path_or_glob: str) -> pd.DataFrame:
    path = sorted(glob.glob(path_or_glob))[-1] if "*" in path_or_glob else path_or_glob
    df = pd.read_csv(path, skiprows=1, encoding="utf-8-sig")
    df.columns = [c.replace("\ufeff", "").strip() for c in df.columns]
    df["时间"] = pd.to_datetime(df["时间"])
    for c in NUMERIC:
        df[c] = pd.to_numeric(df[c].astype(str).str.replace("\ufeff", ""), errors="coerce")
    for c in ["id", "关联订单id"]:
        df[c] = df[c].astype(str).str.replace("\ufeff", "")
    return df.sort_values(["时间", "id"]).reset_index(drop=True)


def swap_fills(df: pd.DataFrame) -> pd.DataFrame:
    sw = df[(df["账单类型"] == "永续合约") & df["交易类型"].isin(["买入", "卖出"])].copy()
    sw["is_open"] = sw["仓位余额变动"] > 0
    sw["side"] = np.where(
        sw["is_open"],
        np.where(sw["交易类型"] == "买入", "long", "short"),
        np.where(sw["交易类型"] == "卖出", "long", "short"),
    )
    return sw.sort_values(["时间", "id"]).reset_index(drop=True)


def build_trips(df: pd.DataFrame) -> pd.DataFrame:
    """One row per (symbol, side) cycle from first open fill to position == 0.

    Features per trip:
      size_pct        peak notional / account balance at entry, in %
      build_s         seconds from first fill to peak position
      dur             minutes held
      adds / adds_worse   number of add fills, and how many were at a worse price than the first fill
      pre5 / pre30    % move of the symbol's own fill prices over the 5/30 min before entry
      mae / mfe       worst / best excursion (%) using the trader's own fill prints as ticks
      day_pnl_before  cumulative net PnL of trips closed earlier the same day
    """
    sw = swap_fills(df)
    allpx = {s: g.set_index("时间")["成交价"].sort_index() for s, g in sw.groupby("交易品种")}
    rows: list[dict] = []
    for (sym, side), g in sw.groupby(["交易品种", "side"]):
        g = g.sort_values(["时间", "id"])
        px = allpx[sym]
        cur: dict | None = None
        for _, r in g.iterrows():
            if cur is None:
                if not r["is_open"]:
                    continue
                t0, p0 = r["时间"], r["成交价"]
                pre = px[(px.index < t0) & (px.index >= t0 - pd.Timedelta(minutes=5))]
                pre30 = px[(px.index < t0) & (px.index >= t0 - pd.Timedelta(minutes=30))]
                cur = {
                    "sym": sym, "side": side, "open": t0, "p0": p0,
                    "bal0": r["交易账户余额"] - r["交易账户余额变动"],
                    "pre5": (p0 / pre.iloc[0] - 1) * 100 if len(pre) >= 3 else np.nan,
                    "pre30": (p0 / pre30.iloc[0] - 1) * 100 if len(pre30) >= 3 else np.nan,
                    "peak": 0.0, "t_peak": t0, "pnl": 0.0, "fee": 0.0,
                    "open_fills": 0, "close_fills": 0, "entry_notional": 0.0, "entry_qty": 0.0,
                    "adds": 0, "adds_worse": 0,
                }
            cur["pnl"] += r["收益"]
            cur["fee"] += r["手续费"]
            if r["is_open"]:
                cur["open_fills"] += 1
                cur["entry_notional"] += r["仓位余额变动"]
                cur["entry_qty"] += r["数量"]
                if r["仓位余额"] > cur["peak"]:
                    cur["peak"], cur["t_peak"] = r["仓位余额"], r["时间"]
                if cur["open_fills"] > 1:
                    cur["adds"] += 1
                    worse = r["成交价"] < cur["p0"] if side == "long" else r["成交价"] > cur["p0"]
                    cur["adds_worse"] += int(worse)
            else:
                cur["close_fills"] += 1
            if r["仓位余额"] < 1e-6:
                cur["close"], cur["p_exit"] = r["时间"], r["成交价"]
                path = px[(px.index >= cur["open"]) & (px.index <= cur["close"])]
                sgn = 1 if side == "long" else -1
                exc = (path / cur["p0"] - 1) * sgn * 100
                cur["mae"] = float(exc.min()) if len(exc) else 0.0
                cur["mfe"] = float(exc.max()) if len(exc) else 0.0
                rows.append(cur)
                cur = None
    t = pd.DataFrame(rows)
    t["net"] = t["pnl"] + t["fee"]
    t["ret"] = t["net"] / t["peak"] * 100
    t["dur"] = (t["close"] - t["open"]).dt.total_seconds() / 60
    t["build_s"] = (t["t_peak"] - t["open"]).dt.total_seconds()
    t["size_pct"] = t["peak"] / t["bal0"] * 100
    t["vwap_entry"] = t["entry_notional"] / t["entry_qty"]
    t["hour"] = t["open"].dt.hour
    t["date"] = t["open"].dt.date
    t = t.sort_values("open").reset_index(drop=True)
    t["day_pnl_before"] = t.groupby("date")["net"].cumsum() - t["net"]
    t["prev_same_net"] = t.groupby("sym")["net"].shift(1)
    t["prev_same_gap"] = (t["open"] - t.groupby("sym")["close"].shift(1)).dt.total_seconds() / 60
    return t


def overlay_rules(
    t: pd.DataFrame,
    size_cap: float | None = None,
    stop_pct: float | None = None,
    time_stop_min: float | None = None,
    day_halt: float | None = None,
) -> pd.DataFrame:
    """Counterfactual replay: apply execution-layer rules to the trader's own trips.

    Assumptions (conservative on both sides):
    - size_cap scales PnL linearly, i.e. same fills at smaller size.
    - stop_pct exits at exactly -stop_pct with round-trip fees; ignores slippage.
    - time_stop_min exits losing trips held longer than N min at half their MAE.
    - day_halt skips any trip opened after the day's cumulative net <= -day_halt.
    """
    x = t.copy()
    x["fee_rate"] = x["fee"].abs() / (x["entry_notional"] * 2)
    day_pnl: dict = {}
    out = []
    for _, r in x.iterrows():
        dp = day_pnl.get(r["date"], 0.0)
        if day_halt is not None and dp <= -day_halt:
            continue
        scale = 1.0 if size_cap is None or r["size_pct"] <= size_cap else size_cap / r["size_pct"]
        net, stopped = r["net"], False
        if stop_pct is not None and r["mae"] <= -stop_pct:
            net, stopped = (-stop_pct / 100 - 2 * r["fee_rate"]) * r["peak"], True
        elif time_stop_min is not None and r["dur"] > time_stop_min and r["net"] <= 0:
            net, stopped = (r["mae"] / 2 / 100 - 2 * r["fee_rate"]) * r["peak"], True
        net *= scale
        day_pnl[r["date"]] = dp + net
        out.append({"open": r["open"], "date": r["date"], "sym": r["sym"], "net": net, "stopped": stopped})
    return pd.DataFrame(out)


def summarize(o: pd.DataFrame) -> dict:
    w, l = o[o["net"] > 0], o[o["net"] <= 0]
    dcum = o.groupby("date")["net"].sum().cumsum()
    return {
        "n": len(o),
        "net": round(o["net"].sum()),
        "win_rate": round((o["net"] > 0).mean() * 100, 1),
        "payoff": round(w["net"].mean() / abs(l["net"].mean()), 2) if len(l) else None,
        "profit_factor": round(w["net"].sum() / abs(l["net"].sum()), 2) if len(l) and l["net"].sum() < 0 else None,
        "max_dd": round((dcum - dcum.cummax()).min()),
        "worst": round(o["net"].min()),
    }
