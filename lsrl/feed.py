"""Completed-candle feed (REST). Bars only: NO order-book or trade data comes from REST.

Sweep classification is decided from the WebSocket depth + trade tape (`lsrl.ws` -> `lsrl.obs`). REST is
used here for 1m/15m candles (ATR and the reclaim close), and elsewhere for bootstrap/metadata only.

Causality: a candle is emitted only once it is complete (`open + tf + bar_lag <= now`); the still-forming last
row is never emitted. `t_ms` on the returned bars is a placeholder: the runtime restamps each bar with the
event bus's monotonic receipt time when it is queued.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from lsrl.core.types import BarClosed

GRANULARITY = {60: "1m", 900: "15m"}


class CandleFeed:
    def __init__(self, client: Any, symbol: str, product_type: str, *,
                 now_ms: Callable[[], int] = lambda: int(time.time() * 1000), bar_lag_ms: int = 2_000) -> None:
        self.client, self.symbol, self.product_type = client, symbol.upper(), product_type
        self.now_ms, self.bar_lag_ms = now_ms, bar_lag_ms
        self.last_open: dict[int, int | None] = {60: None, 900: None}

    def seed(self, *, last_open_1m: int | None, last_open_15m: int | None) -> None:
        """After a restart: never re-deliver bars the restored engine has already consumed."""
        self.last_open = {60: last_open_1m, 900: last_open_15m}

    def bars(self, tf_s: int, limit: int) -> list[BarClosed]:
        payload = self.client.get_candles(self.symbol, self.product_type, granularity=GRANULARITY[tf_s], limit=limit)
        rows = [r for r in (payload.get("data") or []) if isinstance(r, list) and len(r) >= 6]
        rows.sort(key=lambda r: int(r[0]))
        now, out = int(self.now_ms()), []
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
