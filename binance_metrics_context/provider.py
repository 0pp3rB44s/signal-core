"""Orchestrates fetch + short-lived cache + causal alignment for one symbol.

Telemetry only, fail-open by design: `get_context()` NEVER raises and never
blocks meaningfully -- a network fetch has a 3s timeout (client.py), and any
exception anywhere in this path returns UNAVAILABLE rather than propagating.
Shadow/paper signal generation must never stall or fail because Binance is
slow or down.

The short TTL cache exists only to avoid re-fetching the same symbol's data
for every signal in a burst -- it does not change causal correctness, since
`build_metrics_context` still re-filters the (possibly cached) rows against
each call's own `signal_time_ms`.
"""
from __future__ import annotations

import time

from . import client
from .causal import MetricsContext, UNAVAILABLE, build_metrics_context

_PERIOD_MS = {"5m": 5 * 60 * 1000, "15m": 15 * 60 * 1000}


class BinanceMetricsContextProvider:
    def __init__(
        self,
        *,
        period: str = "5m",
        cache_ttl_s: float = 60.0,
        max_fresh_age_s: float = 600.0,
        clock=time.time,
        fetch_open_interest=client.fetch_open_interest,
        fetch_taker_buy_sell_ratio=client.fetch_taker_buy_sell_ratio,
        fetch_top_trader_ratio=client.fetch_top_trader_ratio,
        fetch_global_ratio=client.fetch_global_ratio,
    ) -> None:
        if period not in _PERIOD_MS:
            raise ValueError(f"unsupported period: {period}")
        self._period = period
        self._period_ms = _PERIOD_MS[period]
        self._cache_ttl_s = cache_ttl_s
        self._max_fresh_age_s = max_fresh_age_s
        self._clock = clock
        self._fetch_open_interest = fetch_open_interest
        self._fetch_taker_buy_sell_ratio = fetch_taker_buy_sell_ratio
        self._fetch_top_trader_ratio = fetch_top_trader_ratio
        self._fetch_global_ratio = fetch_global_ratio
        self._cache: dict[str, tuple[float, dict]] = {}

    def _raw_rows(self, symbol: str) -> dict:
        now = self._clock()
        cached = self._cache.get(symbol)
        if cached is not None and (now - cached[0]) < self._cache_ttl_s:
            return cached[1]
        raw = {
            "oi": self._fetch_open_interest(symbol, period=self._period),
            "taker": self._fetch_taker_buy_sell_ratio(symbol, period=self._period),
            "top_trader": self._fetch_top_trader_ratio(symbol, period=self._period),
            "global": self._fetch_global_ratio(symbol, period=self._period),
        }
        self._cache[symbol] = (now, raw)
        return raw

    def get_context(self, symbol: str, signal_time_ms: int) -> MetricsContext:
        try:
            raw = self._raw_rows(symbol)
            return build_metrics_context(
                oi_rows=raw["oi"],
                taker_ratio_rows=raw["taker"],
                top_trader_ratio_rows=raw["top_trader"],
                global_ratio_rows=raw["global"],
                period_ms=self._period_ms,
                signal_time_ms=signal_time_ms,
                max_fresh_age_s=self._max_fresh_age_s,
            )
        except Exception:  # noqa: BLE001 -- telemetry-only, must never block a signal
            return UNAVAILABLE
