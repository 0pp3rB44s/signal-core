"""Thin, exception-safe fetch layer for Binance's public futures metrics.

Read-only, unauthenticated, free public endpoints -- the same data family
used for the pre-Sunday research (data.binance.vision), fetched live here
instead of from the historical archive. No credentials, no order authority.
Every function returns an empty list on any failure (timeout, HTTP error,
malformed payload) rather than raising -- the caller (provider.py) treats an
empty list exactly like "no data available yet", never a crash.
"""
from __future__ import annotations

import logging

import requests

from .causal import MetricsRow

logger = logging.getLogger(__name__)

BASE_URL = "https://fapi.binance.com/futures/data"
REQUEST_TIMEOUT_S = 3.0


def _fetch_rows(path: str, symbol: str, period: str, limit: int, value_key: str) -> list[MetricsRow]:
    try:
        resp = requests.get(
            f"{BASE_URL}/{path}",
            params={"symbol": symbol.upper(), "period": period, "limit": str(limit)},
            timeout=REQUEST_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return []
        rows = []
        for row in data:
            try:
                rows.append(MetricsRow(timestamp_ms=int(row["timestamp"]), value=float(row[value_key])))
            except (KeyError, TypeError, ValueError):
                continue
        return rows
    except Exception as exc:  # noqa: BLE001 -- telemetry-only, must never propagate
        logger.debug("BINANCE_METRICS_FETCH_FAILED | path=%s | symbol=%s | error=%s", path, symbol, exc)
        return []


def fetch_open_interest(symbol: str, period: str = "5m", limit: int = 5) -> list[MetricsRow]:
    return _fetch_rows("openInterestHist", symbol, period, limit, "sumOpenInterest")


def fetch_taker_buy_sell_ratio(symbol: str, period: str = "5m", limit: int = 2) -> list[MetricsRow]:
    return _fetch_rows("takerlongshortRatio", symbol, period, limit, "buySellRatio")


def fetch_top_trader_ratio(symbol: str, period: str = "5m", limit: int = 2) -> list[MetricsRow]:
    return _fetch_rows("topLongShortPositionRatio", symbol, period, limit, "longShortRatio")


def fetch_global_ratio(symbol: str, period: str = "5m", limit: int = 2) -> list[MetricsRow]:
    return _fetch_rows("globalLongShortAccountRatio", symbol, period, limit, "longShortRatio")
