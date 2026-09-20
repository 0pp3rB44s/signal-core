"""Live market-data feed: the project's REST client -> LSRL engine events (no project imports).

Point-in-time liquidity source (v1): Bitget REST merge-depth via `get_orderbook` (50 levels,
exchange timestamp + local receipt time, no sequence id), polled at `depth_interval_s`. No trade
tape is wired in v1, so consumed-vs-cancelled uses the price-gated depth-removal fallback and
absorption is recorded as unavailable (never invented).

Causality: an event's `t_ms` is the LOCAL RECEIPT time. A candle is emitted only once it is
complete (`open + tf + bar_lag <= now`); the still-forming last row is never emitted.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from lsrl.core.types import BarClosed, DepthSnapshot

GRANULARITY = {60: "1m", 900: "15m"}


class LiveFeed:
    def __init__(self, client: Any, symbol: str, product_type: str, *,
                 now_ms: Callable[[], int] = lambda: int(time.time() * 1000), depth_limit: int = 50,
                 bar_lag_ms: int = 2_000) -> None:
        self.client, self.symbol, self.product_type = client, symbol.upper(), product_type
        self.now_ms, self.depth_limit, self.bar_lag_ms = now_ms, depth_limit, bar_lag_ms
        self.last_open: dict[int, int | None] = {60: None, 900: None}
        self._last_t = 0

    def _now(self) -> int:
        self._last_t = max(self._last_t, int(self.now_ms()))       # never step backwards on a clock slew
        return self._last_t

    def now(self) -> int:
        return self._now()

    def depth(self) -> DepthSnapshot:
        ob = self.client.get_orderbook(self.symbol, limit=self.depth_limit)
        raw = ob.get("raw_payload") or {}
        try:
            exchange_ts = int(raw.get("ts")) if raw.get("ts") else None
        except (TypeError, ValueError):
            exchange_ts = None
        return DepthSnapshot(
            t_ms=self._now(), exchange_ts_ms=exchange_ts,
            bids=tuple((float(r["price"]), float(r["size"])) for r in ob.get("bids") or []),
            asks=tuple((float(r["price"]), float(r["size"])) for r in ob.get("asks") or []))

    def bars(self, tf_s: int, limit: int) -> list[BarClosed]:
        payload = self.client.get_candles(self.symbol, self.product_type, granularity=GRANULARITY[tf_s], limit=limit)
        rows = [r for r in (payload.get("data") or []) if isinstance(r, list) and len(r) >= 6]
        rows.sort(key=lambda r: int(r[0]))
        now, out = self._now(), []
        for r in rows:
            open_ts = int(r[0])
            close_ts = open_ts + tf_s * 1000
            if close_ts + self.bar_lag_ms > now:                     # still forming
                continue
            last = self.last_open[tf_s]
            if last is not None and open_ts <= last:                 # already delivered
                continue
            out.append(BarClosed(t_ms=now, tf_s=tf_s, open_ts_ms=open_ts, close_ts_ms=close_ts,
                                 open=float(r[1]), high=float(r[2]), low=float(r[3]), close=float(r[4]),
                                 volume=float(r[5])))
            self.last_open[tf_s] = open_ts
        return out
