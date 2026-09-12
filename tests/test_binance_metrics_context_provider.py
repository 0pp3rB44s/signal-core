"""Tests for BinanceMetricsContextProvider's caching and fail-open behavior.

Network is never touched: every fetch function is replaced with a fake.
"""
from __future__ import annotations

from binance_metrics_context.causal import MetricsRow
from binance_metrics_context.provider import BinanceMetricsContextProvider


def test_get_context_returns_populated_result_from_fetchers():
    provider = BinanceMetricsContextProvider(
        fetch_open_interest=lambda symbol, period: [MetricsRow(0, 100.0)],
        fetch_taker_buy_sell_ratio=lambda symbol, period: [MetricsRow(0, 1.2)],
        fetch_top_trader_ratio=lambda symbol, period: [MetricsRow(0, 1.5)],
        fetch_global_ratio=lambda symbol, period: [MetricsRow(0, 0.9)],
    )
    ctx = provider.get_context("BTCUSDT", decision_time_ms=6 * 60_000)
    assert ctx.metrics_available is True
    assert ctx.open_interest == 100.0
    assert ctx.taker_buy_sell_volume_ratio == 1.2


def test_fetch_exception_is_swallowed_and_reports_unavailable():
    def _raise(symbol, period):
        raise RuntimeError("network is down")

    provider = BinanceMetricsContextProvider(
        fetch_open_interest=_raise,
        fetch_taker_buy_sell_ratio=lambda s, period: [],
        fetch_top_trader_ratio=lambda s, period: [],
        fetch_global_ratio=lambda s, period: [],
    )
    ctx = provider.get_context("BTCUSDT", decision_time_ms=1_000_000)
    assert ctx.metrics_available is False


def test_repeated_calls_within_ttl_do_not_refetch():
    calls = {"n": 0}

    def _counting_fetch(symbol, period):
        calls["n"] += 1
        return [MetricsRow(0, 100.0)]

    now = {"t": 1_000.0}
    provider = BinanceMetricsContextProvider(
        cache_ttl_s=60.0,
        clock=lambda: now["t"],
        fetch_open_interest=_counting_fetch,
        fetch_taker_buy_sell_ratio=lambda s, period: [],
        fetch_top_trader_ratio=lambda s, period: [],
        fetch_global_ratio=lambda s, period: [],
    )
    provider.get_context("BTCUSDT", decision_time_ms=6 * 60_000)
    provider.get_context("BTCUSDT", decision_time_ms=7 * 60_000)
    assert calls["n"] == 1  # second call served from cache

    now["t"] += 120.0  # advance past the 60s TTL
    provider.get_context("BTCUSDT", decision_time_ms=8 * 60_000)
    assert calls["n"] == 2  # cache expired, refetched


def test_different_symbols_are_cached_independently():
    calls = []

    def _fetch(symbol, period):
        calls.append(symbol)
        return [MetricsRow(0, 100.0)]

    provider = BinanceMetricsContextProvider(
        fetch_open_interest=_fetch,
        fetch_taker_buy_sell_ratio=lambda s, period: [],
        fetch_top_trader_ratio=lambda s, period: [],
        fetch_global_ratio=lambda s, period: [],
    )
    provider.get_context("BTCUSDT", decision_time_ms=6 * 60_000)
    provider.get_context("ETHUSDT", decision_time_ms=6 * 60_000)
    assert calls == ["BTCUSDT", "ETHUSDT"]
