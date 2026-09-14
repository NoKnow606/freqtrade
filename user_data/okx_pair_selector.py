"""Pure selection helpers for an OKX USDT linear perpetual pairlist."""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DAY_MS = 24 * 60 * 60 * 1000
MAX_MINIMUM_NOTIONAL = 60.0


@dataclass(frozen=True)
class SelectorConfig:
    min_quote_volume: float = 5_000_000
    max_spread_ratio: float = 0.003
    min_listing_days: int = 7
    rank_size: int = 10
    refresh_seconds: int = 300
    max_failures: int = 3


DEFAULT_CONFIG = SelectorConfig()


@dataclass(frozen=True)
class TickerMetrics:
    percentage: float
    quote_volume: float
    spread_ratio: float
    last: float


def _positive_number(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def is_eligible_market(
    market: Mapping[str, Any],
    now_ms: int,
    config: SelectorConfig = DEFAULT_CONFIG,
) -> bool:
    """Return whether a market has all required OKX perpetual metadata."""
    if not (
        market.get("active") is True
        and market.get("swap") is True
        and market.get("spot") is not True
        and market.get("linear") is True
        and market.get("quote") == "USDT"
        and market.get("settle") == "USDT"
    ):
        return False

    precision = market.get("precision")
    limits = market.get("limits")
    if not isinstance(precision, Mapping) or not isinstance(limits, Mapping):
        return False
    amount_limits = limits.get("amount")
    if not isinstance(amount_limits, Mapping):
        return False
    if any(
        value is None
        for value in (
            _positive_number(precision.get("price")),
            _positive_number(precision.get("amount")),
            _positive_number(market.get("contractSize")),
            _positive_number(amount_limits.get("min")),
        )
    ):
        return False

    info = market.get("info")
    if not isinstance(info, Mapping):
        return False
    list_time = _positive_number(info.get("listTime"))
    if list_time is None:
        return False
    return now_ms - list_time >= config.min_listing_days * DAY_MS


def ticker_metrics(ticker: Mapping[str, Any]) -> TickerMetrics | None:
    """Normalize the ticker fields used by filters and rankings."""
    bid = _positive_number(ticker.get("bid"))
    ask = _positive_number(ticker.get("ask"))
    last = _positive_number(ticker.get("last"))
    if bid is None or ask is None or last is None or ask < bid:
        return None

    info = ticker.get("info")
    quote_volume_value = ticker.get("quoteVolume")
    if quote_volume_value is None:
        if not isinstance(info, Mapping):
            return None
        base_volume = _positive_number(info.get("volCcy24h"))
        quote_volume = _positive_number(base_volume * last) if base_volume is not None else None
    else:
        quote_volume = _positive_number(quote_volume_value)
    if quote_volume is None:
        return None

    percentage = ticker.get("percentage")
    if percentage is None:
        if not isinstance(info, Mapping):
            return None
        open_price = _positive_number(info.get("open24h"))
        if open_price is None:
            return None
        percentage_number = (last / open_price - 1) * 100
    else:
        try:
            percentage_number = float(percentage)
        except (TypeError, ValueError):
            return None
    if not math.isfinite(percentage_number):
        return None

    return TickerMetrics(
        percentage=percentage_number,
        quote_volume=quote_volume,
        spread_ratio=1 - bid / ask,
        last=last,
    )


def minimum_notional(market: Mapping[str, Any], price: float) -> float:
    """Return the binding minimum notional, failing closed with infinity."""
    normalized_price = _positive_number(price)
    contract_size = _positive_number(market.get("contractSize"))
    limits = market.get("limits")
    if normalized_price is None or contract_size is None or not isinstance(limits, Mapping):
        return math.inf
    amount_limits = limits.get("amount")
    if not isinstance(amount_limits, Mapping):
        return math.inf
    minimum_amount = _positive_number(amount_limits.get("min"))
    if minimum_amount is None:
        return math.inf
    cost_limits = limits.get("cost")
    minimum_cost = 0.0
    if isinstance(cost_limits, Mapping) and cost_limits.get("min") is not None:
        parsed_cost = _positive_number(cost_limits.get("min"))
        if parsed_cost is None:
            return math.inf
        minimum_cost = parsed_cost
    return max(minimum_cost, minimum_amount * contract_size * normalized_price)


def select_pairs(
    markets: Mapping[str, Mapping[str, Any]],
    tickers: Mapping[str, Mapping[str, Any]],
    now_ms: int,
    config: SelectorConfig = DEFAULT_CONFIG,
) -> list[str]:
    """Filter markets, then concatenate gainer, loser and volume top lists."""
    candidates: list[tuple[str, TickerMetrics]] = []
    for symbol, market in markets.items():
        if not is_eligible_market(market, now_ms, config):
            continue
        ticker = tickers.get(symbol)
        if ticker is None:
            continue
        metrics = ticker_metrics(ticker)
        if metrics is None:
            continue
        if metrics.quote_volume < config.min_quote_volume:
            continue
        if metrics.spread_ratio > config.max_spread_ratio:
            continue
        if minimum_notional(market, metrics.last) > MAX_MINIMUM_NOTIONAL:
            continue
        candidates.append((symbol, metrics))

    gainers = sorted(candidates, key=lambda item: (-item[1].percentage, item[0]))
    losers = sorted(candidates, key=lambda item: (item[1].percentage, item[0]))
    volume = sorted(candidates, key=lambda item: (-item[1].quote_volume, item[0]))

    selected: list[str] = []
    seen: set[str] = set()
    for ranking in (gainers, losers, volume):
        for symbol, _ in ranking[: config.rank_size]:
            if symbol not in seen:
                selected.append(symbol)
                seen.add(symbol)
    return selected


def atomic_write_pairlist(
    destination: str | os.PathLike[str],
    pairs: Sequence[str],
    refresh_period: int = DEFAULT_CONFIG.refresh_seconds,
) -> None:
    """Atomically replace a RemotePairList-compatible JSON file."""
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(
                {"pairs": list(pairs), "refresh_period": refresh_period},
                temporary,
                ensure_ascii=True,
                indent=2,
            )
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.replace(path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
