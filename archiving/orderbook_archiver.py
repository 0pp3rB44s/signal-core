from __future__ import annotations

import logging
import threading
import time
from typing import Any

from archiving.common import (ArchiveWriter, ArchiverConfig, DiskGuardTripped,
                              SourceHealth, backoff_delays, utc_iso, utc_now)
from market_data.orderbook_analyzer import OrderbookAnalyzer

log = logging.getLogger("archiving.orderbook")

BANDS_BPS = (10.0, 25.0, 50.0)


def _band_notionals(levels: list[dict], mid: float, side: str) -> dict[str, float]:
    out = {f"{int(b)}": 0.0 for b in BANDS_BPS}
    if mid <= 0:
        return out
    for row in levels:
        price, size = float(row.get("price") or 0.0), float(row.get("size") or 0.0)
        if price <= 0 or size <= 0:
            continue
        dist_bps = abs(price - mid) / mid * 10_000.0
        for b in BANDS_BPS:
            if dist_bps <= b:
                out[f"{int(b)}"] += price * size
    return {k: round(v, 2) for k, v in out.items()}


def build_record(orderbook: dict[str, Any], product_type: str,
                 depth_levels: int, recv_ts_ms: int) -> dict[str, Any]:
    """Normaliseert één get_orderbook-resultaat naar een archiveringsrecord."""
    bids = orderbook.get("bids") or []
    asks = orderbook.get("asks") or []
    mid = float(orderbook.get("mid_price") or 0.0)
    best_bid = float(orderbook.get("best_bid") or 0.0)
    best_ask = float(orderbook.get("best_ask") or 0.0)
    raw = orderbook.get("raw_payload") or {}
    exchange_ts = int(raw.get("ts") or 0) or None

    analysis = OrderbookAnalyzer().analyze(orderbook)
    crossed = bool(best_bid > 0 and best_ask > 0 and best_bid >= best_ask)
    empty_side = not bids or not asks
    lag_ms = (recv_ts_ms - exchange_ts) if exchange_ts else None
    stale = bool(lag_ms is not None and lag_ms > 30_000)
    status = "EMPTY" if empty_side else "DEGRADED" if (crossed or stale) else "OK"

    def wall(w: dict | None) -> dict | None:
        if not w:
            return None
        return {"price": w.get("price"), "size": w.get("size"),
                "ratio": round(float(w.get("wall_ratio") or 0.0), 4),
                "significant": bool(w.get("is_significant"))}

    return {
        "ts_utc": utc_iso(),
        "recv_ts_ms": recv_ts_ms,
        "exchange": "BITGET",
        "product_type": product_type,
        "symbol": str(orderbook.get("symbol") or "").upper(),
        "exchange_ts_ms": exchange_ts,
        "seq_available": False,  # REST merge-depth levert geen sequence-id
        "best_bid": best_bid,
        "best_ask": best_ask,
        "best_bid_size": float(bids[0]["size"]) if bids else None,
        "best_ask_size": float(asks[0]["size"]) if asks else None,
        "mid_price": mid,
        "spread": float(orderbook.get("spread") or 0.0),
        "spread_bps": round(float(orderbook.get("spread_bps") or 0.0), 4),
        "bids": [[row["price"], row["size"]] for row in bids[:depth_levels]],
        "asks": [[row["price"], row["size"]] for row in asks[:depth_levels]],
        "bid_depth_notional": round(float(orderbook.get("bid_depth_notional") or 0.0), 2),
        "ask_depth_notional": round(float(orderbook.get("ask_depth_notional") or 0.0), 2),
        "total_depth_notional": round(float(orderbook.get("total_depth_notional") or 0.0), 2),
        "imbalance": round(float(orderbook.get("depth_imbalance") or 0.0), 6),
        "band_bid_notional_bps": _band_notionals(bids, mid, "bid"),
        "band_ask_notional_bps": _band_notionals(asks, mid, "ask"),
        "largest_bid_wall": wall(analysis.get("largest_bid_wall")),
        "largest_ask_wall": wall(analysis.get("largest_ask_wall")),
        "bid_pressure": round(float(analysis.get("bid_pressure") or 0.0), 4),
        "ask_pressure": round(float(analysis.get("ask_pressure") or 0.0), 4),
        "spread_regime": analysis.get("spread_regime"),
        "quality": {"status": status, "crossed_book": crossed,
                    "empty_side": empty_side, "stale": stale,
                    "levels_bid": len(bids), "levels_ask": len(asks),
                    "exchange_lag_ms": lag_ms},
    }


class OrderbookArchiver(threading.Thread):
    def __init__(self, client, config: ArchiverConfig, stop_event: threading.Event,
                 health: SourceHealth) -> None:
        super().__init__(name="orderbook-archiver", daemon=True)
        self.client = client
        self.config = config
        self.stop_event = stop_event
        self.health = health
        self.writer = ArchiveWriter(config.archive_dir, "orderbook", config.min_free_gb)

    def poll_once(self) -> int:
        """Eén ronde over alle symbolen; retourneert aantal geschreven rijen."""
        written = 0
        spacing = self.config.orderbook_interval_s / max(1, len(self.config.symbols))
        for symbol in self.config.symbols:
            if self.stop_event.is_set():
                break
            started = time.monotonic()
            try:
                ob = self.client.get_orderbook(symbol, limit=50)
                record = build_record(ob, self.client.settings.bitget_product_type,
                                      self.config.depth_levels_stored,
                                      recv_ts_ms=int(time.time() * 1000))
                key = f"{record['symbol']}:{record['exchange_ts_ms'] or record['recv_ts_ms']}"
                if self.writer.append(record, dedupe_key=key):
                    written += 1
                self.health.ok(self.writer)
            except DiskGuardTripped as exc:
                self.health.fail(exc)
                log.critical("ORDERBOOK_DISK_GUARD | %s", exc)
                self.stop_event.wait(60)
            except Exception as exc:
                self.health.fail(exc)
                log.warning("ORDERBOOK_POLL_FAILED | symbol=%s | error=%s", symbol, exc)
            elapsed = time.monotonic() - started
            self.stop_event.wait(max(0.0, spacing - elapsed))
        return written

    def run(self) -> None:
        log.info("ORDERBOOK_ARCHIVER_START | symbols=%d | interval_s=%.1f | levels=%d",
                 len(self.config.symbols), self.config.orderbook_interval_s,
                 self.config.depth_levels_stored)
        delays = backoff_delays(base=2.0, cap=300.0)
        while not self.stop_event.is_set():
            cycle_start = time.monotonic()
            self.poll_once()
            if self.health.consecutive_errors >= len(self.config.symbols):
                delay = next(delays)
                log.warning("ORDERBOOK_BACKOFF | consecutive_errors=%d | sleep_s=%.0f",
                            self.health.consecutive_errors, delay)
                self.stop_event.wait(delay)
            else:
                delays = backoff_delays(base=2.0, cap=300.0)
            elapsed = time.monotonic() - cycle_start
            self.stop_event.wait(max(0.0, self.config.orderbook_interval_s - elapsed))
        self.writer.close()
        log.info("ORDERBOOK_ARCHIVER_STOP | rows=%d", self.writer.rows_written)
