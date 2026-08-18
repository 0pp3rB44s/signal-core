"""Binance cross-exchange research collector. RESEARCH ONLY — no keys, no orders, no execution.

This exists for one reason: to make it *testable later* whether another major venue moves before
Bitget. It is a sensor, not a strategy, and it is deliberately incapable of becoming one — nothing
here imports execution, risk, sizing or strategy code, and that boundary is enforced by test rather
than by convention.

Two design choices are carried directly from things this project already got wrong:

* **Every row is stamped at its own event, never once per sweep.** The Bitget OI stream showed 100%
  negative receive latency (median −6.2 s) because one sweep-level timestamp was reused across
  twelve symbols. For cross-venue lead/lag — which is the entire point here, at sub-second scale —
  that error would invert the analysis rather than degrade it. REST rows carry both
  `fetch_started_ms` and `fetch_completed_ms`; WS rows carry the exchange event time and the receive
  time separately.
* **A subscription ack is not delivery.** Bitget's `liquidation` channel acks 12/12 and never emits a
  frame. So the health record distinguishes `HEALTHY_NO_EVENT_OBSERVED` from `NO_DELIVERY_PROVEN`,
  and liquidation delivery is only ever claimed once a real frame has been seen.

Raw payloads are preserved. Deriving features at collection time bakes in an interpretation, and
every study so far has needed the raw bytes re-read under a different definition.
"""

from __future__ import annotations

import json
import logging
import ssl
import threading
import time
import urllib.request
from pathlib import Path

from microflow.segments import ImmutableSegmentWriter

log = logging.getLogger("research.binance")

WS_BASE = "wss://fstream.binance.com"
REST_BASE = "https://fapi.binance.com"
SCHEMA = "research_binance_v1"
EXCHANGE = "binance-usdm"

#: Binance publishes OI on a slow cadence and rate-limits REST. 60 s per symbol is safe and is not
#: meaningfully worse than the ~42 s Bitget sweep already accepted. Sub-second OI does not exist.
OI_POLL_SECONDS = 60.0
MAX_QUEUE = 20_000
#: Binance closes idle sockets at 24 h; reconnecting well before that avoids a mid-stream drop.
RECONNECT_SECONDS = 6 * 3600


def _get(path: str, params: dict) -> dict:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{REST_BASE}{path}" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, headers={"User-Agent": "cgc-research"})
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read())


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class BinanceResearchCollector:
    """Owns the WS market streams and a REST OI/funding loop. Neither can raise into the caller."""

    def __init__(self, *, symbols: tuple[str, ...], data_dir: Path,
                 oi_poll_seconds: float = OI_POLL_SECONDS) -> None:
        self.symbols = tuple(s.upper() for s in symbols)
        self.data_dir = Path(data_dir)
        self.oi_poll_seconds = float(oi_poll_seconds)
        self.stop_event = threading.Event()
        mk = lambda name, ver: ImmutableSegmentWriter(
            self.data_dir / name, schema_version=f"{SCHEMA}_{ver}", symbols=self.symbols)
        self.trade_writer = mk("trades", "trade")
        self.book_writer = mk("book_ticker", "book")
        self.mark_writer = mk("mark_price", "mark")
        self.liq_writer = mk("liquidations", "liq")
        self.oi_writer = mk("open_interest", "oi")
        self.health_writer = mk("health", "health")
        self.counts = {"trade": 0, "book": 0, "mark": 0, "liq": 0, "oi": 0}
        self.parse_failures = 0
        self.dropped = 0
        self.reconnects = 0
        self.ws_errors = 0
        self.oi_errors = 0
        self.out_of_order = 0
        self.duplicates = 0
        self.negative_latency = 0
        self.latencies: list[float] = []
        self.liq_events_total = 0
        self.last_liq_event_ms: int | None = None
        self.started_ms = int(time.time() * 1000)
        self._last_event_ms: dict[tuple[str, str], int] = {}
        self._seen: set[tuple[str, str, int]] = set()

    # --- streams ----------------------------------------------------------

    def stream_path(self) -> str:
        parts = []
        for s in self.symbols:
            low = s.lower()
            parts += [f"{low}@aggTrade", f"{low}@bookTicker", f"{low}@markPrice@1s"]
        parts.append("!forceOrder@arr")          # all-market, one stream, no per-symbol subscribe
        return "/stream?streams=" + "/".join(parts)

    def handle_message(self, raw: str, *, now_ms: int | None = None) -> int:
        """Parse one frame. Returns rows written. Never raises."""
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
        kind = payload.get("e")
        try:
            if kind == "aggTrade":
                return self._write("trade", self.trade_writer, payload, now_ms, dict(
                    symbol=payload.get("s"), price=_f(payload.get("p")), quantity=_f(payload.get("q")),
                    # Binance flags the BUYER as maker; aggressor is the opposite side.
                    buyer_is_maker=payload.get("m"),
                    aggressor_side=("sell" if payload.get("m") else "buy"),
                    trade_id=payload.get("a"), trade_time_ms=payload.get("T")))
            if kind == "bookTicker":
                return self._write("book", self.book_writer, payload, now_ms, dict(
                    symbol=payload.get("s"), best_bid=_f(payload.get("b")), bid_qty=_f(payload.get("B")),
                    best_ask=_f(payload.get("a")), ask_qty=_f(payload.get("A")),
                    transaction_time_ms=payload.get("T")))
            if kind == "markPriceUpdate":
                return self._write("mark", self.mark_writer, payload, now_ms, dict(
                    symbol=payload.get("s"), mark_price=_f(payload.get("p")),
                    index_price=_f(payload.get("i")), estimated_settle=_f(payload.get("P")),
                    funding_rate=_f(payload.get("r")), next_funding_ms=payload.get("T")))
            if kind == "forceOrder":
                order = payload.get("o") or {}
                self.liq_events_total += 1
                self.last_liq_event_ms = now_ms
                return self._write("liq", self.liq_writer, payload, now_ms, dict(
                    symbol=order.get("s"), side=order.get("S"), order_type=order.get("o"),
                    time_in_force=order.get("f"), quantity=_f(order.get("q")),
                    price=_f(order.get("p")), average_price=_f(order.get("ap")),
                    order_status=order.get("X"), last_filled_qty=_f(order.get("l")),
                    filled_accumulated_qty=_f(order.get("z")),
                    notional=(_f(order.get("ap")) or 0) * (_f(order.get("z")) or 0),
                    trade_time_ms=order.get("T")))
        except Exception:
            log.exception("RESEARCH_BINANCE_WRITE_FAILED")
            self.parse_failures += 1
        return 0

    def _write(self, key: str, writer, payload: dict, now_ms: int, fields: dict) -> int:
        event_ms = payload.get("E")
        symbol = fields.get("symbol") or ""
        if len(self._seen) >= MAX_QUEUE:
            self._seen.clear()          # bounded; identity window resets rather than growing
        ident = (key, str(symbol), int(event_ms or 0))
        if ident in self._seen:
            self.duplicates += 1
            return 0
        self._seen.add(ident)
        if event_ms is not None:
            latency = now_ms - float(event_ms)
            self.latencies.append(latency)
            if latency < 0:
                self.negative_latency += 1
            prev = self._last_event_ms.get((key, str(symbol)))
            if prev is not None and int(event_ms) < prev:
                self.out_of_order += 1
            else:
                self._last_event_ms[(key, str(symbol))] = int(event_ms)
        writer.append({
            "schema_version": f"{SCHEMA}_{key}",
            "exchange": EXCHANGE,
            "source": payload.get("e"),
            "event_timestamp_ms": event_ms,
            "receive_timestamp_ms": now_ms,
            "write_timestamp_ms": int(time.time() * 1000),
            "raw": payload,
            "research_only": True,
            **fields,
        })
        self.counts[key] += 1
        return 1

    # --- REST: open interest ----------------------------------------------

    def poll_open_interest_once(self, fetch=None) -> int:
        """One sweep. Each row is stamped at its OWN fetch, never once per sweep."""
        fetch = fetch or _get
        written = 0
        for symbol in self.symbols:
            if self.stop_event.is_set():
                break
            started = int(time.time() * 1000)
            try:
                oi = fetch("/fapi/v1/openInterest", {"symbol": symbol})
                pi = fetch("/fapi/v1/premiumIndex", {"symbol": symbol})
            except Exception:
                self.oi_errors += 1
                continue
            completed = int(time.time() * 1000)
            amount = _f((oi or {}).get("openInterest"))
            mark = _f((pi or {}).get("markPrice"))
            index = _f((pi or {}).get("indexPrice"))
            if amount is None and mark is None:
                self.oi_errors += 1
                continue
            try:
                self.oi_writer.append({
                    "schema_version": f"{SCHEMA}_oi", "exchange": EXCHANGE, "source": "rest",
                    "symbol": symbol,
                    "event_timestamp_ms": int(_f((oi or {}).get("time")) or _f((pi or {}).get("time")) or completed),
                    "receive_timestamp_ms": completed,
                    "write_timestamp_ms": int(time.time() * 1000),
                    "fetch_started_ms": started, "fetch_completed_ms": completed,
                    "open_interest": amount, "mark_price": mark, "index_price": index,
                    "funding_rate": _f((pi or {}).get("lastFundingRate")),
                    "next_funding_ms": int(_f((pi or {}).get("nextFundingTime")) or 0) or None,
                    "basis_bps": ((mark - index) / index * 10_000.0) if (mark and index) else None,
                    "research_only": True,
                })
                self.counts["oi"] += 1
                written += 1
            except Exception:
                log.exception("RESEARCH_BINANCE_OI_WRITE_FAILED")
                self.oi_errors += 1
        return written

    def _oi_loop(self) -> None:  # pragma: no cover - thread body
        while not self.stop_event.wait(self.oi_poll_seconds):
            try:
                self.poll_open_interest_once()
            except Exception:
                log.exception("RESEARCH_BINANCE_OI_LOOP_FAILED")

    # --- health -----------------------------------------------------------

    def clock_offsets(self, fetch=None) -> dict:
        """Local clock against both venues. A claimed 200 ms lead is meaningless without this."""
        fetch = fetch or _get
        out = {}
        for label, path, key in (("binance", "/fapi/v1/time", "serverTime"),):
            try:
                t0 = time.time() * 1000
                d = fetch(path, {})
                t1 = time.time() * 1000
                srv = _f((d or {}).get(key))
                out[f"clock_offset_{label}_ms"] = ((t0 + t1) / 2 - srv) if srv else None
            except Exception:
                out[f"clock_offset_{label}_ms"] = None
        return out

    def liquidation_status(self) -> str:
        """`HEALTHY_NO_EVENT_OBSERVED` is not the same claim as `NO_DELIVERY_PROVEN`."""
        if self.liq_events_total > 0:
            return "DELIVERY_PROVEN"
        # Ordinary streams flowing means the socket is fine, so silence is the market, not the feed.
        if self.counts["trade"] > 0:
            return "HEALTHY_NO_EVENT_OBSERVED"
        return "NO_DELIVERY_PROVEN"

    def health(self) -> dict:
        lat = sorted(self.latencies[-5000:])
        now = int(time.time() * 1000)
        return {
            "schema_version": f"{SCHEMA}_health", "exchange": EXCHANGE,
            "write_timestamp_ms": now, "uptime_s": (now - self.started_ms) / 1000.0,
            "rows": dict(self.counts),
            "parse_failures": self.parse_failures, "dropped": self.dropped,
            "duplicates": self.duplicates, "out_of_order": self.out_of_order,
            "negative_latency_rows": self.negative_latency,
            "latency_median_ms": lat[len(lat) // 2] if lat else None,
            "latency_p95_ms": lat[int(0.95 * len(lat))] if lat else None,
            "reconnects": self.reconnects, "ws_errors": self.ws_errors,
            "oi_errors": self.oi_errors,
            "liq_events_total": self.liq_events_total,
            "last_liq_event_age_s": ((now - self.last_liq_event_ms) / 1000.0
                                     if self.last_liq_event_ms else None),
            "liquidation_status": self.liquidation_status(),
            "symbols_expected": len(self.symbols),
            "research_only": True, "orders_allowed": False,
        }

    def close(self) -> None:
        self.stop_event.set()
        for w in (self.trade_writer, self.book_writer, self.mark_writer,
                  self.liq_writer, self.oi_writer, self.health_writer):
            try:
                w.close()
            except Exception:
                log.exception("RESEARCH_BINANCE_WRITER_CLOSE_FAILED")


def run(symbols: tuple[str, ...], data_dir: Path) -> None:  # pragma: no cover - process entry
    import websocket
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    c = BinanceResearchCollector(symbols=symbols, data_dir=data_dir)
    threading.Thread(target=c._oi_loop, daemon=True, name="bn-oi").start()
    status = Path(data_dir) / "status.json"
    while not c.stop_event.is_set():
        opened = time.time()
        try:
            ws = websocket.create_connection(WS_BASE + c.stream_path(), timeout=20,
                                             sslopt={"cert_reqs": ssl.CERT_NONE})
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
                    h.update(c.clock_offsets())
                    try:
                        c.health_writer.append(h)
                        status.write_text(json.dumps(h, indent=1))
                    except Exception:
                        pass
            ws.close()
        except Exception as exc:
            c.ws_errors += 1
            c.reconnects += 1
            log.warning("RESEARCH_BINANCE_RECONNECT | error=%s", exc)
            if c.stop_event.wait(5.0):
                break
    c.close()


if __name__ == "__main__":  # pragma: no cover
    import os
    syms = tuple((os.environ.get("BINANCE_SYMBOLS") or
                  "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,BNBUSDT,"
                  "LINKUSDT,AVAXUSDT,SUIUSDT,HYPEUSDT,ZECUSDT,NEARUSDT").split(","))
    run(syms, Path(os.environ.get("BINANCE_DATA_DIR") or "data_store/research_binance"))
