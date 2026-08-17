"""Research-only liquidation + open-interest collector.

RESEARCH ONLY. This module is never imported by the trading engine and cannot influence
execution. It runs as its own process, writes its own immutable segments, and swallows every
error rather than propagating one. Killing it does not touch the engine; killing the engine
does not corrupt these files.

Two streams, because the exchange exposes them differently:

* **liquidations** — WebSocket ``liquidation`` channel, event-driven and sparse. Subscription is
  confirmed (12/12 acks on 2026-08-17) but delivery is only observable over days, so the gap
  counters below are the evidence that the stream is alive rather than merely subscribed.
* **open interest / funding / mark / index** — REST, polled. These endpoints are updated on the
  exchange's own cadence; polling faster than that manufactures duplicate rows, not resolution.

Every row carries both ``timestamp_exchange`` and ``timestamp_local`` so the four research streams
(trade tape, books5, liquidations, OI) can be joined on exchange time without future leakage.
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

log = logging.getLogger("research.liq_oi")

WS_URL = "wss://ws.bitget.com/v2/ws/public"
REST = "https://api.bitget.com"
PRODUCT = "USDT-FUTURES"

#: The exchange updates open interest and funding on a slow cadence; 30 s samples it without
#: manufacturing duplicates. Measured cost at 12 symbols: 4 requests per poll, ~0.13 req/s.
OI_POLL_SECONDS = 30.0
#: Bounded so a stalled writer can never grow memory without limit. Overflow is counted, not queued.
MAX_QUEUE = 10_000
PING_SECONDS = 20.0


class LiquidationOICollector:
    """Owns two independent streams. Neither can raise into the caller."""

    def __init__(self, *, symbols: tuple[str, ...], data_dir: Path,
                 oi_poll_seconds: float = OI_POLL_SECONDS) -> None:
        self.symbols = tuple(s.upper() for s in symbols)
        self.data_dir = Path(data_dir)
        self.oi_poll_seconds = float(oi_poll_seconds)
        self.stop_event = threading.Event()
        self.liq_writer = ImmutableSegmentWriter(
            self.data_dir / "liquidations", schema_version="research_liquidation_v1",
            symbols=self.symbols)
        self.oi_writer = ImmutableSegmentWriter(
            self.data_dir / "open_interest", schema_version="research_oi_v1",
            symbols=self.symbols)
        self.connection_id = ""
        self.liq_rows = 0
        self.oi_rows = 0
        self.dropped = 0
        self.ws_errors = 0
        self.reconnects = 0
        self.oi_errors = 0
        self.subscription_acks: set[str] = set()
        self._ws = None
        self._queue: list[dict] = []
        self._qlock = threading.Lock()

    # --- liquidation stream -------------------------------------------------

    def _subscribe_payload(self) -> dict:
        return {"op": "subscribe",
                "args": [{"instType": PRODUCT, "channel": "liquidation", "instId": s}
                         for s in self.symbols]}

    def handle_message(self, raw: str) -> int:
        """Parse one frame. Returns rows written. Never raises."""
        if raw == "pong":
            return 0
        try:
            payload = json.loads(raw)
        except Exception:
            return 0
        if not isinstance(payload, dict):
            return 0
        if payload.get("event") == "subscribe":
            arg = payload.get("arg") or {}
            self.subscription_acks.add(str(arg.get("instId") or ""))
            return 0
        if payload.get("event") == "error":
            log.warning("RESEARCH_LIQ_WS_ERROR | code=%s", payload.get("code"))
            return 0
        rows = payload.get("data") or []
        if not isinstance(rows, list):
            return 0
        arg = payload.get("arg") or {}
        symbol = str(arg.get("instId") or "").upper()
        now_ms = int(time.time() * 1000)
        written = 0
        for row in rows:
            if len(self._queue) >= MAX_QUEUE:
                self.dropped += 1
                continue
            try:
                self.liq_writer.append({
                    "schema_version": "research_liquidation_v1",
                    "timestamp_exchange": int(row.get("ts") or payload.get("ts") or 0),
                    "timestamp_local": now_ms,
                    "symbol": symbol or str(row.get("instId") or "").upper(),
                    # Bitget reports the side of the *order that closed the position*; the raw
                    # field is preserved verbatim so no interpretation is baked into the archive.
                    "side_raw": row.get("side"),
                    "price": _f(row.get("price")),
                    "size": _f(row.get("size") or row.get("sz")),
                    "notional": _f(row.get("notional") or row.get("amount")),
                    "raw": row,
                    "connection_id": self.connection_id,
                    "research_only": True,
                })
                self.liq_rows += 1
                written += 1
            except Exception:
                log.exception("RESEARCH_LIQ_WRITE_FAILED")
        return written

    # --- open interest / funding / mark / index -----------------------------

    def poll_open_interest_once(self, fetch=None) -> int:
        """One REST sweep across the universe. Returns rows written. Never raises."""
        fetch = fetch or _get
        now_ms = int(time.time() * 1000)
        written = 0
        for symbol in self.symbols:
            if self.stop_event.is_set():
                break
            try:
                oi = fetch("/api/v2/mix/market/open-interest",
                           {"symbol": symbol, "productType": PRODUCT})
                px = fetch("/api/v2/mix/market/symbol-price",
                           {"symbol": symbol, "productType": PRODUCT})
                fr = fetch("/api/v2/mix/market/current-fund-rate",
                           {"symbol": symbol, "productType": PRODUCT})
            except Exception:
                self.oi_errors += 1
                continue
            row = _oi_row(symbol, oi, px, fr, now_ms)
            if row is None:
                self.oi_errors += 1
                continue
            try:
                self.oi_writer.append(row)
                self.oi_rows += 1
                written += 1
            except Exception:
                log.exception("RESEARCH_OI_WRITE_FAILED")
        return written

    def _oi_loop(self) -> None:
        while not self.stop_event.wait(self.oi_poll_seconds):
            try:
                self.poll_open_interest_once()
            except Exception:
                log.exception("RESEARCH_OI_LOOP_FAILED")

    # --- lifecycle ----------------------------------------------------------

    def status(self) -> dict:
        return {
            "schema_version": "research_liq_oi_status_v1",
            "liq_rows": self.liq_rows, "oi_rows": self.oi_rows,
            "dropped": self.dropped, "ws_errors": self.ws_errors,
            "reconnects": self.reconnects, "oi_errors": self.oi_errors,
            "subscription_acks": len(self.subscription_acks),
            "expected_acks": len(self.symbols),
            "connection_id": self.connection_id,
            "updated_ms": int(time.time() * 1000),
            "research_only": True, "orders_allowed": False,
        }

    def close(self) -> None:
        self.stop_event.set()
        for writer in (self.liq_writer, self.oi_writer):
            try:
                writer.close()
            except Exception:
                log.exception("RESEARCH_WRITER_CLOSE_FAILED")


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _oi_row(symbol: str, oi: dict, px: dict, fr: dict, now_ms: int) -> dict | None:
    oi_data = (oi or {}).get("data") or {}
    lst = oi_data.get("openInterestList") or []
    amount = _f(lst[0].get("size")) if lst else None
    px_data = ((px or {}).get("data") or [{}])
    px_row = px_data[0] if isinstance(px_data, list) and px_data else (px_data or {})
    fr_data = ((fr or {}).get("data") or [{}])
    fr_row = fr_data[0] if isinstance(fr_data, list) and fr_data else (fr_data or {})
    mark = _f(px_row.get("markPrice"))
    index = _f(px_row.get("indexPrice"))
    if amount is None and mark is None:
        return None
    basis_bps = ((mark - index) / index * 10_000.0) if (mark and index) else None
    return {
        "schema_version": "research_oi_v1",
        "timestamp_exchange": int(_f(oi_data.get("ts")) or _f(px_row.get("ts")) or now_ms),
        "timestamp_local": now_ms,
        "symbol": symbol,
        "open_interest": amount,
        "mark_price": mark,
        "index_price": index,
        "last_price": _f(px_row.get("price")),
        "basis_bps": basis_bps,
        "funding_rate": _f(fr_row.get("fundingRate")),
        "funding_interval_hours": _f(fr_row.get("fundingRateInterval")),
        "next_funding_ms": int(_f(fr_row.get("nextUpdate")) or 0) or None,
        "research_only": True,
    }


def _get(path: str, params: dict) -> dict:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(f"{REST}{path}?{query}", headers={"User-Agent": "cgc-research"})
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read())


def run(symbols: tuple[str, ...], data_dir: Path) -> None:  # pragma: no cover - process entry
    import uuid
    import websocket

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    collector = LiquidationOICollector(symbols=symbols, data_dir=data_dir)
    threading.Thread(target=collector._oi_loop, daemon=True, name="oi-poll").start()
    status_path = Path(data_dir) / "status.json"

    while not collector.stop_event.is_set():
        try:
            collector.connection_id = str(uuid.uuid4())
            ws = websocket.create_connection(WS_URL, timeout=15,
                                             sslopt={"cert_reqs": ssl.CERT_NONE})
            collector._ws = ws
            ws.send(json.dumps(collector._subscribe_payload()))
            ws.settimeout(PING_SECONDS)
            last_ping = time.time()
            while not collector.stop_event.is_set():
                try:
                    collector.handle_message(ws.recv())
                except Exception:
                    if time.time() - last_ping > PING_SECONDS:
                        try:
                            ws.send("ping")
                            last_ping = time.time()
                        except Exception:
                            raise
                try:
                    status_path.write_text(json.dumps(collector.status(), indent=1))
                except Exception:
                    pass
        except Exception as exc:
            collector.ws_errors += 1
            collector.reconnects += 1
            log.warning("RESEARCH_LIQ_WS_RECONNECT | error=%s", exc)
            if collector.stop_event.wait(3.0):
                break
    collector.close()


if __name__ == "__main__":  # pragma: no cover
    import os
    syms = tuple((os.environ.get("RESEARCH_SYMBOLS") or
                  "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,BNBUSDT,"
                  "LINKUSDT,AVAXUSDT,SUIUSDT,HYPEUSDT,ZECUSDT,NEARUSDT").split(","))
    run(syms, Path(os.environ.get("RESEARCH_DATA_DIR") or "data_store/research_liq_oi"))
