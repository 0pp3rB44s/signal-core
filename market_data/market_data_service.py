from __future__ import annotations

import logging
from typing import Any

from clients.bitget_rest import BitgetRestClient
from market_data.multi_timeframe_cache import MultiTimeframeCache


DEFAULT_TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h"]


class MarketDataService:
    log = logging.getLogger("market_data_service")

    def __init__(self, rest_client: BitgetRestClient, cache: MultiTimeframeCache) -> None:
        self.rest = rest_client
        self.cache = cache

    def refresh_symbol(self, symbol: str, timeframes: list[str] | None = None, limit: int = 200) -> dict[str, list[dict[str, Any]]]:
        selected_timeframes = timeframes or DEFAULT_TIMEFRAMES
        payload = self.rest.get_multi_timeframe_candles(symbol=symbol, timeframes=selected_timeframes, limit=limit)
        self.cache.update_many(symbol, payload)
        mtf_snapshot = self.cache.get_mtf_snapshot(symbol)

        self.log.info(
            "MTF_REFRESH | %s | healthy=%s/%s | weighted_health=%.2f | stale=%s | candles=%s",
            symbol,
            mtf_snapshot.get("healthy_timeframes"),
            len(selected_timeframes),
            float(mtf_snapshot.get("weighted_health", 0.0)),
            mtf_snapshot.get("stale_timeframes"),
            mtf_snapshot.get("total_candles"),
        )
        return payload

    def refresh_many(self, symbols: list[str], timeframes: list[str] | None = None, limit: int = 200) -> dict[str, dict[str, list[dict[str, Any]]]]:
        result = {}
        for symbol in symbols:
            try:
                result[symbol] = self.refresh_symbol(
                    symbol=symbol,
                    timeframes=timeframes,
                    limit=limit,
                )
            except Exception as exc:
                self.log.error(
                    "MTF_SYMBOL_REFRESH_FAILED | %s | error=%s",
                    symbol,
                    exc,
                )
                continue
        return result

    def get_symbol_snapshot(self, symbol: str) -> dict[str, Any]:
        snapshot = self.cache.get_symbol_snapshot(symbol)
        snapshot["mtf"] = self.cache.get_mtf_snapshot(symbol)
        return snapshot

    def is_symbol_ready(self, symbol: str) -> bool:
        available = self.cache.get_available_timeframes(symbol)
        mtf_snapshot = self.cache.get_mtf_snapshot(symbol)
        weighted_health = float(mtf_snapshot.get("weighted_health", 0.0))
        ready = len(available) >= 3 and weighted_health >= 0.60

        self.log.info(
            "MTF_READY_CHECK | %s | ready=%s | available=%s | weighted_health=%.2f | healthy=%s | stale=%s",
            symbol,
            ready,
            len(available),
            weighted_health,
            mtf_snapshot.get("healthy_timeframes"),
            mtf_snapshot.get("stale_timeframes"),
        )
        return ready
