from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


SUPPORTED_TIMEFRAMES = [
    "1m",
    "5m",
    "15m",
    "1h",
    "4h",
]

TIMEFRAME_WEIGHTS = {
    "1m": 0.15,
    "5m": 0.30,
    "15m": 0.25,
    "1h": 0.20,
    "4h": 0.10,
}


@dataclass
class CandleCache:
    candles: list[dict[str, Any]] = field(default_factory=list)
    last_update: datetime | None = None
    stale: bool = True


class MultiTimeframeCache:

    def __init__(self) -> None:
        self.cache: dict[str, dict[str, CandleCache]] = {}

    def ensure_symbol(self, symbol: str) -> None:
        if symbol not in self.cache:
            self.cache[symbol] = {
                tf: CandleCache()
                for tf in SUPPORTED_TIMEFRAMES
            }

    def update(
        self,
        symbol: str,
        timeframe: str,
        candles: list[dict[str, Any]],
    ) -> None:
        self.ensure_symbol(symbol)

        if timeframe not in self.cache[symbol]:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        self.cache[symbol][timeframe].candles = candles
        self.cache[symbol][timeframe].last_update = datetime.now(timezone.utc)
        self.cache[symbol][timeframe].stale = False

    def get(
        self,
        symbol: str,
        timeframe: str,
    ) -> list[dict[str, Any]]:
        self.ensure_symbol(symbol)

        return self.cache[symbol][timeframe].candles

    def mark_stale(
        self,
        symbol: str,
        timeframe: str,
    ) -> None:
        self.ensure_symbol(symbol)

        self.cache[symbol][timeframe].stale = True

    def is_stale(
        self,
        symbol: str,
        timeframe: str,
    ) -> bool:
        self.ensure_symbol(symbol)

        return self.cache[symbol][timeframe].stale

    def get_last_update(
        self,
        symbol: str,
        timeframe: str,
    ) -> datetime | None:
        self.ensure_symbol(symbol)

        return self.cache[symbol][timeframe].last_update

    def is_symbol_stale(self, symbol: str) -> bool:
        self.ensure_symbol(symbol)

        return any(
            cache.stale
            for cache in self.cache[symbol].values()
        )

    def get_available_timeframes(self, symbol: str) -> list[str]:
        self.ensure_symbol(symbol)

        return [
            timeframe
            for timeframe, cache in self.cache[symbol].items()
            if cache.candles
        ]

    def get_symbol_snapshot(self, symbol: str) -> dict[str, Any]:
        self.ensure_symbol(symbol)

        snapshot: dict[str, Any] = {}

        for timeframe, cache in self.cache[symbol].items():
            snapshot[timeframe] = {
                "candles": len(cache.candles),
                "stale": cache.stale,
                "last_update": (
                    cache.last_update.isoformat()
                    if cache.last_update
                    else None
                ),
            }

        return snapshot

    def get_timeframe_health(
        self,
        symbol: str,
        timeframe: str,
    ) -> dict[str, Any]:
        self.ensure_symbol(symbol)

        cache = self.cache[symbol][timeframe]

        latest_close = None
        latest_volume = None
        candle_count = len(cache.candles)

        if cache.candles:
            latest = cache.candles[-1]
            latest_close = latest.get("close")
            latest_volume = latest.get("volume")

        return {
            "timeframe": timeframe,
            "weight": TIMEFRAME_WEIGHTS.get(timeframe, 0.0),
            "stale": cache.stale,
            "candles": candle_count,
            "latest_close": latest_close,
            "latest_volume": latest_volume,
            "last_update": (
                cache.last_update.isoformat()
                if cache.last_update
                else None
            ),
        }

    def get_mtf_snapshot(self, symbol: str) -> dict[str, Any]:
        self.ensure_symbol(symbol)

        snapshot: dict[str, Any] = {
            "symbol": symbol,
            "timeframes": {},
            "healthy_timeframes": 0,
            "stale_timeframes": 0,
            "total_candles": 0,
            "weighted_health": 0.0,
        }

        weighted_health = 0.0

        for timeframe in SUPPORTED_TIMEFRAMES:
            health = self.get_timeframe_health(symbol, timeframe)
            snapshot["timeframes"][timeframe] = health

            snapshot["total_candles"] += int(health["candles"])

            if health["stale"]:
                snapshot["stale_timeframes"] += 1
            else:
                snapshot["healthy_timeframes"] += 1
                weighted_health += float(health["weight"])

        snapshot["weighted_health"] = round(weighted_health, 4)
        snapshot["fully_healthy"] = (
            snapshot["healthy_timeframes"] == len(SUPPORTED_TIMEFRAMES)
        )

        return snapshot

    def update_many(self, symbol: str, timeframe_payload: dict[str, list[dict[str, Any]]]) -> None:
        self.ensure_symbol(symbol)

        for timeframe, candles in timeframe_payload.items():
            if timeframe not in SUPPORTED_TIMEFRAMES:
                continue
            self.update(symbol, timeframe, candles)