import json
from dataclasses import replace
from pathlib import Path

import pytest

from user_data.okx_pair_selector import (
    SelectorRuntime,
    SelectorConfig,
    atomic_write_pairlist,
    is_eligible_market,
    minimum_notional,
    select_pairs,
    ticker_metrics,
)


NOW_MS = 1_800_000_000_000
DAY_MS = 24 * 60 * 60 * 1000


def make_market(symbol: str, **overrides: object) -> dict:
    market = {
        "symbol": symbol,
        "active": True,
        "spot": False,
        "swap": True,
        "linear": True,
        "quote": "USDT",
        "settle": "USDT",
        "contractSize": 0.1,
        "precision": {"price": 0.0001, "amount": 1},
        "limits": {"amount": {"min": 1}, "cost": {"min": None}},
        "info": {"listTime": str(NOW_MS - 30 * DAY_MS)},
    }
    market.update(overrides)
    return market


def make_ticker(
    percentage: float,
    quote_volume: float = 10_000_000,
    bid: float = 100,
    ask: float = 100.1,
) -> dict:
    return {
        "percentage": percentage,
        "quoteVolume": quote_volume,
        "bid": bid,
        "ask": ask,
        "last": (bid + ask) / 2,
    }


def make_okx_swap_ticker() -> dict:
    return {
        "symbol": "BTC/USDT:USDT",
        "percentage": None,
        "quoteVolume": None,
        "baseVolume": 9_999_999,
        "bid": 101.9,
        "ask": 102.1,
        "last": 102.0,
        "info": {
            "instType": "SWAP",
            "instId": "BTC-USDT-SWAP",
            "last": "102.0",
            "open24h": "100.0",
            "volCcy24h": "60000",
            "vol24h": "9999999",
        },
    }


class SequenceExchange:
    def __init__(self, outcomes: list[tuple[dict, dict] | Exception]) -> None:
        self.outcomes = outcomes
        self.index = 0
        self.public_calls: list[str] = []

    def load_markets(self) -> dict:
        self.public_calls.append("load_markets")
        outcome = self.outcomes[self.index]
        if isinstance(outcome, Exception):
            self.index += 1
            raise outcome
        return outcome[0]

    def fetch_tickers(self) -> dict:
        self.public_calls.append("fetch_tickers")
        outcome = self.outcomes[self.index]
        assert not isinstance(outcome, Exception)
        self.index += 1
        return outcome[1]


class ManualClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def pairlist_payload(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def successful_outcome(symbol: str) -> tuple[dict, dict]:
    return ({symbol: make_market(symbol)}, {symbol: make_ticker(1)})


def test_selector_config_has_conservative_defaults() -> None:
    assert SelectorConfig() == SelectorConfig(
        min_quote_volume=5_000_000,
        max_spread_ratio=0.003,
        min_listing_days=7,
        rank_size=10,
        refresh_seconds=300,
        max_failures=3,
    )


def test_market_and_ticker_helpers_fail_closed_on_incomplete_data() -> None:
    market = make_market("GOOD/USDT:USDT")

    assert is_eligible_market(market, NOW_MS)
    assert not is_eligible_market({**market, "precision": {"price": 0.1}}, NOW_MS)
    assert not is_eligible_market({**market, "limits": {"amount": {}, "cost": {"min": 5}}}, NOW_MS)
    assert minimum_notional({**market, "contractSize": 1}, price=50) == 50
    assert (
        minimum_notional({**market, "limits": {"amount": {"min": 2}, "cost": {"min": 120}}}, 50)
        == 120
    )

    metrics = ticker_metrics(make_ticker(percentage=4.2, quote_volume=7_000_000, bid=99, ask=101))
    assert metrics is not None
    assert metrics.percentage == 4.2
    assert metrics.quote_volume == 7_000_000
    assert metrics.spread_ratio == pytest.approx(1 - 99 / 101)
    assert metrics.last == 100
    assert ticker_metrics({"percentage": 1, "quoteVolume": 10_000_000, "bid": 100}) is None


def test_ticker_metrics_uses_okx_swap_raw_volume_and_open_fallbacks() -> None:
    ticker = make_okx_swap_ticker()

    metrics = ticker_metrics(ticker)

    assert metrics is not None
    assert metrics.quote_volume == 6_120_000
    assert metrics.percentage == pytest.approx(2.0)
    for invalid_info in (
        {"open24h": "100"},
        {"volCcy24h": "60000"},
        {"volCcy24h": "0", "open24h": "100"},
        {"volCcy24h": "60000", "open24h": "nan"},
    ):
        assert ticker_metrics({**ticker, "info": invalid_info}) is None


def test_select_pairs_filters_then_returns_ordered_deduplicated_rankings() -> None:
    markets: dict[str, dict] = {}
    tickers: dict[str, dict] = {}

    def add(symbol: str, ticker: dict, market: dict | None = None) -> None:
        markets[symbol] = market or make_market(symbol)
        tickers[symbol] = ticker

    add("OVERLAP/USDT:USDT", make_ticker(99, quote_volume=99_000_000))
    for index in range(10):
        add(f"G{index:02}/USDT:USDT", make_ticker(20 - index, quote_volume=10_000_000 + index))
        add(f"L{index:02}/USDT:USDT", make_ticker(-20 + index, quote_volume=11_000_000 + index))
        add(f"V{index:02}/USDT:USDT", make_ticker(index / 100, quote_volume=50_000_000 - index))

    invalid = {
        "INACTIVE/USDT:USDT": make_market("INACTIVE/USDT:USDT", active=False),
        "SPOT/USDT": make_market("SPOT/USDT", spot=True, swap=False, settle=None),
        "QUOTE/BTC:USDT": make_market("QUOTE/BTC:USDT", quote="BTC"),
        "SETTLE/USDT:USDC": make_market("SETTLE/USDT:USDC", settle="USDC"),
        "INVERSE/USDT:USDT": make_market("INVERSE/USDT:USDT", linear=False),
        "RECENT/USDT:USDT": make_market(
            "RECENT/USDT:USDT", info={"listTime": str(NOW_MS - 6 * DAY_MS)}
        ),
        "NO_LIST_TIME/USDT:USDT": make_market("NO_LIST_TIME/USDT:USDT", info={}),
        "TOO_LARGE/USDT:USDT": make_market(
            "TOO_LARGE/USDT:USDT",
            limits={"amount": {"min": 1}, "cost": {"min": 61}},
        ),
    }
    for symbol, market in invalid.items():
        add(symbol, make_ticker(200, quote_volume=200_000_000), market)
    add("LOW_VOLUME/USDT:USDT", make_ticker(200, quote_volume=4_999_999))
    add("WIDE_SPREAD/USDT:USDT", make_ticker(200, quote_volume=200_000_000, bid=99, ask=101))

    selected = select_pairs(markets, tickers, NOW_MS)

    assert selected == (
        ["OVERLAP/USDT:USDT"]
        + [f"G{index:02}/USDT:USDT" for index in range(9)]
        + [f"L{index:02}/USDT:USDT" for index in range(10)]
        + [f"V{index:02}/USDT:USDT" for index in range(9)]
    )
    assert not set(invalid).intersection(selected)
    assert "LOW_VOLUME/USDT:USDT" not in selected
    assert "WIDE_SPREAD/USDT:USDT" not in selected


def test_select_pairs_uses_symbol_as_a_deterministic_tiebreaker() -> None:
    config = replace(SelectorConfig(), rank_size=1)
    markets = {symbol: make_market(symbol) for symbol in ("B/USDT:USDT", "A/USDT:USDT")}
    tickers = {symbol: make_ticker(1, quote_volume=10_000_000) for symbol in markets}

    assert select_pairs(markets, tickers, NOW_MS, config) == ["A/USDT:USDT"]


def test_atomic_write_pairlist_replaces_file_with_valid_json(tmp_path: Path) -> None:
    destination = tmp_path / "pairlist.json"
    destination.write_text('["OLD/USDT:USDT"]\n', encoding="utf-8")

    atomic_write_pairlist(destination, ["BTC/USDT:USDT", "ETH/USDT:USDT"])

    assert json.loads(destination.read_text(encoding="utf-8")) == {
        "pairs": ["BTC/USDT:USDT", "ETH/USDT:USDT"],
        "refresh_period": 300,
    }
    assert list(tmp_path.iterdir()) == [destination]


def test_runtime_preserves_last_success_for_two_failures_then_fails_closed(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "pairlist.json"
    exchange = SequenceExchange(
        [
            successful_outcome("BTC/USDT:USDT"),
            RuntimeError("first"),
            RuntimeError("second"),
            RuntimeError("third"),
        ]
    )
    runtime = SelectorRuntime(exchange, destination, clock=ManualClock(NOW_MS / 1000))

    runtime.refresh_once()
    runtime.refresh_once()
    assert runtime.consecutive_failures == 1
    assert pairlist_payload(destination)["pairs"] == ["BTC/USDT:USDT"]

    runtime.refresh_once()
    assert runtime.consecutive_failures == 2
    assert pairlist_payload(destination)["pairs"] == ["BTC/USDT:USDT"]

    runtime.refresh_once()
    assert runtime.consecutive_failures == 3
    assert pairlist_payload(destination) == {"pairs": [], "refresh_period": 300}


def test_runtime_fails_closed_when_last_success_is_over_fifteen_minutes_old(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "pairlist.json"
    clock = ManualClock(NOW_MS / 1000)
    runtime = SelectorRuntime(
        SequenceExchange([successful_outcome("BTC/USDT:USDT"), RuntimeError("stale")]),
        destination,
        clock=clock,
    )

    runtime.refresh_once()
    clock.value += 15 * 60 + 1
    runtime.refresh_once()

    assert runtime.consecutive_failures == 1
    assert pairlist_payload(destination) == {"pairs": [], "refresh_period": 300}


def test_runtime_success_resets_failure_count_and_replaces_preserved_pairs(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "pairlist.json"
    runtime = SelectorRuntime(
        SequenceExchange(
            [
                successful_outcome("BTC/USDT:USDT"),
                RuntimeError("first"),
                RuntimeError("second"),
                successful_outcome("ETH/USDT:USDT"),
                RuntimeError("after recovery"),
            ]
        ),
        destination,
        clock=ManualClock(NOW_MS / 1000),
    )

    for _ in range(4):
        runtime.refresh_once()

    assert runtime.consecutive_failures == 0
    assert pairlist_payload(destination)["pairs"] == ["ETH/USDT:USDT"]

    runtime.refresh_once()
    assert runtime.consecutive_failures == 1
    assert pairlist_payload(destination)["pairs"] == ["ETH/USDT:USDT"]


def test_runtime_has_no_static_fallback_for_initial_failure_or_empty_selection(
    tmp_path: Path,
) -> None:
    initial_failure = tmp_path / "initial-failure.json"
    failed_runtime = SelectorRuntime(
        SequenceExchange([RuntimeError("unavailable")]),
        initial_failure,
        clock=ManualClock(NOW_MS / 1000),
    )

    failed_runtime.refresh_once()
    assert pairlist_payload(initial_failure) == {"pairs": [], "refresh_period": 300}

    empty_selection = tmp_path / "empty-selection.json"
    empty_runtime = SelectorRuntime(
        SequenceExchange([({}, {})]),
        empty_selection,
        clock=ManualClock(NOW_MS / 1000),
    )

    empty_runtime.refresh_once()
    assert pairlist_payload(empty_selection) == {"pairs": [], "refresh_period": 300}


def test_runtime_uses_only_public_exchange_calls_and_emits_structured_events(
    tmp_path: Path,
) -> None:
    events: list[dict] = []
    exchange = SequenceExchange(
        [successful_outcome("BTC/USDT:USDT"), RuntimeError("public outage")]
    )
    runtime = SelectorRuntime(
        exchange,
        tmp_path / "pairlist.json",
        clock=ManualClock(NOW_MS / 1000),
        event_sink=events.append,
    )

    runtime.refresh_once()
    runtime.refresh_once()

    assert exchange.public_calls == ["load_markets", "fetch_tickers", "load_markets"]
    assert events[0]["event"] == "refresh_success"
    assert events[0]["counts"]["markets_total"] == 1
    assert events[0]["rankings"] == {
        "gainers": ["BTC/USDT:USDT"],
        "losers": ["BTC/USDT:USDT"],
        "volume": ["BTC/USDT:USDT"],
    }
    assert events[0]["pairs"] == ["BTC/USDT:USDT"]
    assert events[1]["event"] == "refresh_failure"
    assert events[1]["error_type"] == "RuntimeError"
    assert events[1]["consecutive_failures"] == 1
    assert events[1]["fail_closed"] is False
