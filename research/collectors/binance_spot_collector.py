"""Binance SPOT research collector. RESEARCH ONLY — no keys, no orders, no execution.

Companion to `binance_collector.py` (Binance USD-M futures). Exists for one reason: to make it
testable later whether spot leads perp, or diverges from it, ahead of a Bitget move. A separate
module rather than a parameterized class, deliberately — it is a second, independent process with
its own data directory, so a fault in one collector cannot take down the other.

HYPEUSDT has no Binance spot listing and is excluded here; it stays covered on the futures side.

Both streams collected here (`aggTrade`, `bookTicker`) were verified to deliver on a live raw
single-stream probe before being wired in — see `03_RESEARCH/BINANCE_CROSS_EXCHANGE_COLLECTION`.
Depth is intentionally NOT collected here yet (see vault: ADD_LATER, to avoid doubling collector
surface area in one change after futures depth was just added).
"""

from __future__ import annotations

import certifi
import hashlib
import json
import logging
import ssl
import threading
import time
from pathlib import Path

from microflow.segments import ImmutableSegmentWriter

log = logging.getLogger("research.binance_spot")

WS_BASE = "wss://stream.binance.com:9443"
SCHEMA = "research_binance_spot_v1"
EXCHANGE = "binance-spot"
MARKET_TYPE = "spot"

MAX_QUEUE = 20_000
STREAM_STALE_SECONDS = {"book": 60.0, "trade": 60.0}
RECONNECT_SECONDS = 6 * 3600

# Symbols with a Binance spot listing, of the 12 futures symbols this project tracks.
# Verified 2026-08-18 against GET /api/v3/exchangeInfo: HYPEUSDT is PERPETUAL-only, no spot pair.
SPOT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT",
                "LINKUSDT", "AVAXUSDT", "SUIUSDT", "ZECUSDT", "NEARUSDT")


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class BinanceSpotResearchCollector:
    """Owns the spot aggTrade + bookTicker websocket. Cannot raise into the caller."""

    def __init__(self, *, symbols: tuple[str, ...] = SPOT_SYMBOLS, data_dir: Path) -> None:
        self.symbols = tuple(s.upper() for s in symbols)
        self.data_dir = Path(data_dir)
        self.stop_event = threading.Event()
        mk = lambda name, ver: ImmutableSegmentWriter(
            self.data_dir / name, schema_version=f"{SCHEMA}_{ver}", symbols=self.symbols)
        self.trade_writer = mk("trades", "trade")
        self.book_writer = mk("book_ticker", "book")
        self.health_writer = mk("health", "health")
        self.counts = {"trade": 0, "book": 0}
        self.parse_failures = 0
        self.reconnects = 0
        self.ws_errors = 0
        self.out_of_order = 0
        self.duplicates = 0
        self.negative_latency = 0
        self.latencies: list[float] = []
        self.started_ms = int(time.time() * 1000)
        self._last_event_ms: dict[tuple[str, str], int] = {}
        self._last_row_ms: dict[str, int] = {}
        self._seen: set[tuple] = set()

    def stream_path(self) -> str:
        parts = []
        for s in self.symbols:
            low = s.lower()
            parts += [f"{low}@aggTrade", f"{low}@bookTicker"]
        return "/stream?streams=" + "/".join(parts)

    # Spot `bookTicker` frames carry no "e" field (unlike futures), so the kind is inferred from
    # which fields are present rather than from an event-type tag.
    def _classify(self, payload: dict) -> str | None:
        if payload.get("e") == "aggTrade":
            return "trade"
        if "u" in payload and "b" in payload and "B" in payload and "e" not in payload:
            return "book"
        return None

    def handle_message(self, raw: str, *, now_ms: int | None = None) -> int:
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        try:
            frame = json.loads(raw)
        except Exception:
            self.parse_failures += 1
            return 0
        if not isinstance(frame, dict):
            self.parse_failures += 1
            return 0
        payload = frame.get("data") if "data" in frame else frame
        if not isinstance(payload, dict):
            self.parse_failures += 1
            return 0
        kind = self._classify(payload)
        try:
            if kind == "trade":
                return self._write("trade", self.trade_writer, payload, now_ms, dict(
                    symbol=payload.get("s"), price=_f(payload.get("p")), quantity=_f(payload.get("q")),
                    buyer_is_maker=payload.get("m"),
                    aggressor_side=("sell" if payload.get("m") else "buy"),
                    trade_id=payload.get("a"), trade_time_ms=payload.get("T")))
            if kind == "book":
                return self._write("book", self.book_writer, payload, now_ms, dict(
                    symbol=payload.get("s"), best_bid=_f(payload.get("b")), bid_qty=_f(payload.get("B")),
                    best_ask=_f(payload.get("a")), ask_qty=_f(payload.get("A"))))
        except Exception:
            log.exception("RESEARCH_BINANCE_SPOT_WRITE_FAILED")
            self.parse_failures += 1
        return 0

    def _write(self, key: str, writer, payload: dict, now_ms: int, fields: dict) -> int:
        symbol = fields.get("symbol") or ""
        if key == "trade":
            native_id = payload.get("a")
            event_ms = payload.get("T")
        else:
            native_id = payload.get("u")
            event_ms = payload.get("E") or now_ms  # bookTicker carries no event time on spot
        ident = (EXCHANGE, key, symbol, str(native_id))
        if len(self._seen) >= MAX_QUEUE:
            self._seen.clear()
        if ident in self._seen:
            self.duplicates += 1
            return 0
        self._seen.add(ident)
        if event_ms is not None:
            latency = now_ms - float(event_ms)
            self.latencies.append(latency)
            if latency < 0:
                self.negative_latency += 1
            prev = self._last_event_ms.get((key, symbol))
            if prev is not None and int(event_ms) < prev:
                self.out_of_order += 1
            else:
                self._last_event_ms[(key, symbol)] = int(event_ms)
        writer.append({
            "schema_version": f"{SCHEMA}_{key}", "exchange": EXCHANGE, "market_type": MARKET_TYPE,
            "source": payload.get("e") or "bookTicker",
            "event_timestamp_ms": event_ms, "receive_timestamp_ms": now_ms,
            "write_timestamp_ms": int(time.time() * 1000),
            "raw": payload, "research_only": True,
            **fields,
        })
        self.counts[key] += 1
        self._last_row_ms[key] = now_ms
        return 1

    def stream_age_s(self, key: str) -> float | None:
        last = self._last_row_ms.get(key)
        return None if last is None else max(0.0, (int(time.time() * 1000) - last) / 1000.0)

    def stream_health(self, key: str) -> str:
        if self.counts.get(key, 0) <= 0:
            return "NO_DELIVERY_PROVEN"
        age = self.stream_age_s(key)
        if age is None:
            return "UNKNOWN_AGE"
        if age > STREAM_STALE_SECONDS.get(key, 60.0):
            return "STALE"
        return "HEALTHY"

    def health(self) -> dict:
        lat = sorted(self.latencies[-5000:])
        now = int(time.time() * 1000)
        return {
            "schema_version": f"{SCHEMA}_health", "exchange": EXCHANGE, "market_type": MARKET_TYPE,
            "write_timestamp_ms": now, "uptime_s": (now - self.started_ms) / 1000.0,
            "rows": dict(self.counts),
            "book_stream_health": self.stream_health("book"),
            "trade_stream_health": self.stream_health("trade"),
            "parse_failures": self.parse_failures, "duplicates": self.duplicates,
            "out_of_order": self.out_of_order, "negative_latency_rows": self.negative_latency,
            "latency_median_ms": lat[len(lat) // 2] if lat else None,
            "latency_p95_ms": lat[int(0.95 * len(lat))] if lat else None,
            "reconnects": self.reconnects, "ws_errors": self.ws_errors,
            "symbols_expected": len(self.symbols),
            "research_only": True, "orders_allowed": False,
        }

    def close(self) -> None:
        self.stop_event.set()
        for w in (self.trade_writer, self.book_writer, self.health_writer):
            try:
                w.close()
            except Exception:
                log.exception("RESEARCH_BINANCE_SPOT_WRITER_CLOSE_FAILED")


def run(symbols: tuple[str, ...], data_dir: Path) -> None:  # pragma: no cover - process entry
    import websocket
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    c = BinanceSpotResearchCollector(symbols=symbols, data_dir=data_dir)
    status = Path(data_dir) / "status.json"
    while not c.stop_event.is_set():
        opened = time.time()
        try:
            ws = websocket.create_connection(WS_BASE + c.stream_path(), timeout=20,
                                             sslopt={"cert_reqs": ssl.CERT_REQUIRED,
                                                     "ca_certs": certifi.where()})
            ws.settimeout(30)
            last_health = 0.0
            while not c.stop_event.is_set():
                if time.time() - opened > RECONNECT_SECONDS:
                    break
                try:
                    c.handle_message(ws.recv())
                except Exception:
                    try:
                        ws.ping()
                    except Exception:
                        raise
                if time.time() - last_health > 60:
                    last_health = time.time()
                    h = c.health()
                    try:
                        c.health_writer.append(h)
                        status.write_text(json.dumps(h, indent=1))
                    except Exception:
                        pass
            ws.close()
        except Exception as exc:
            c.ws_errors += 1
            c.reconnects += 1
            log.warning("RESEARCH_BINANCE_SPOT_RECONNECT | error=%s", exc)
            if c.stop_event.wait(5.0):
                break
    c.close()


if __name__ == "__main__":  # pragma: no cover
    import os
    syms = tuple((os.environ.get("BINANCE_SPOT_SYMBOLS") or ",".join(SPOT_SYMBOLS)).split(","))
    run(syms, Path(os.environ.get("BINANCE_SPOT_DATA_DIR") or "data_store/research_binance_spot"))
