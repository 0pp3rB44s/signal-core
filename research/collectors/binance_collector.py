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

import certifi
import hashlib
import json
import logging
import ssl
import threading
import time
import urllib.error
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
TRADE_POLL_SECONDS = 15.0
MARK_POLL_SECONDS = 5.0
AGG_TRADE_LIMIT = 500
# A stream is stale once it has produced nothing for this long. bookTicker and the REST
# trade/mark cursors all cycle in seconds, so a minute of silence is a fault, not a lull.
STREAM_STALE_SECONDS = {"book": 60.0, "trade": 60.0, "mark": 60.0, "oi": 180.0, "depth": 60.0}
MAX_QUEUE = 20_000
# Binance bans the IP outright on abuse (HTTP 418) rather than just throttling, and says so in
# the ban body: "Please use the websocket... to avoid bans." PR #66 polled REST for 12 symbols
# every 2 s with no weight budget and no backoff -- roughly 1440 weight/min against a public-IP
# limit far below that -- and got the Runner's shared home IP banned outright. This constant is
# the floor while a ban is in effect if Binance omits Retry-After.
DEFAULT_RATE_LIMIT_COOLDOWN_S = 60.0
REST_INTER_REQUEST_S = 0.15
#: Binance closes idle sockets at 24 h; reconnecting well before that avoids a mid-stream drop.
RECONNECT_SECONDS = 6 * 3600


def _ssl_context() -> ssl.SSLContext:
    """Verified TLS against certifi's roots, never the interpreter default.

    The default store is whatever the host's Python build points at, and on at least one of
    our machines that path resolves to a bundle that fails verification outright -- which
    silently turned every REST sweep into an error while curl on the same box succeeded.
    Pinning certifi keeps verification ON and makes the result host-independent.
    """
    return ssl.create_default_context(cafile=certifi.where())


class RateLimited(Exception):
    """HTTP 418 (banned) or 429 (throttled). Carries how long Binance says to back off.

    Distinguished from a generic network failure because the correct response is opposite:
    a generic failure should be retried soon; this one must NOT be retried until it clears,
    or hammering it converts a temporary throttle into an outright ban (or extends one).
    """

    def __init__(self, retry_after_s: float, status: int, body: str):
        self.retry_after_s = retry_after_s
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body[:200]}")


def _get(path: str, params: dict) -> dict:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{REST_BASE}{path}" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, headers={"User-Agent": "cgc-research"})
    try:
        with urllib.request.urlopen(req, timeout=10, context=_ssl_context()) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (418, 429):
            body = exc.read().decode("utf-8", errors="replace")
            retry_after = exc.headers.get("Retry-After")
            cooldown = float(retry_after) if retry_after else DEFAULT_RATE_LIMIT_COOLDOWN_S
            raise RateLimited(cooldown, exc.code, body) from exc
        raise


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
        self.depth_writer = mk("depth5", "depth")
        self.mark_writer = mk("mark_price", "mark")
        self.liq_writer = mk("liquidations", "liq")
        self.oi_writer = mk("open_interest", "oi")
        self.health_writer = mk("health", "health")
        self.counts = {"trade": 0, "book": 0, "mark": 0, "liq": 0, "oi": 0, "depth": 0}
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
        self._seen: set[tuple] = set()
        # Per-stream freshness. A healthy book stream must never make a dead trade stream
        # look alive, so every stream carries its own last-row clock and its own verdict.
        self._last_row_ms: dict[str, int] = {}
        self.trade_errors = 0
        self.mark_errors = 0
        # Shared across trade/mark/OI/clock-offset: they hit the same IP-wide Binance limit,
        # so one ban must pause all of them, not just the endpoint that triggered it.
        self._rest_cooldown_until_ms = 0
        self.rate_limit_hits = 0
        self.rate_limited_skips = 0
        self.same_ms_distinct_retained = 0
        # Cursor per symbol into Binance's aggregate-trade id space. Polling by id is gapless:
        # we resume from the last id we actually wrote, so a slow tick cannot silently skip trades.
        self._trade_cursor: dict[str, int] = {}
        self._same_ms: set[tuple] = set()

    # --- streams ----------------------------------------------------------

    def stream_path(self) -> str:
        # Only streams this endpoint actually delivers. aggTrade and markPrice@1s are acked,
        # listed in LIST_SUBSCRIPTIONS, and never sent, so they are collected over REST instead
        # (see poll_trades_once / poll_mark_once). Subscribing to them here would reintroduce a
        # stream that reports itself subscribed while producing nothing.
        parts = []
        for sym in self.symbols:
            low = sym.lower()
            parts += [f"{low}@bookTicker", f"{low}@depth5@100ms"]
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
            if kind == "depthUpdate":
                return self._write("depth", self.depth_writer, payload, now_ms, dict(
                    symbol=payload.get("s"),
                    first_update_id=payload.get("U"), final_update_id=payload.get("u"),
                    prev_final_update_id=payload.get("pu"),
                    bids=payload.get("b"), asks=payload.get("a"),
                    transaction_time_ms=payload.get("T")))
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

    # Exchange-native identity beats any clock. bookTicker carries an order-book update id
    # (`u`) and aggTrade an aggregate-trade id (`a`); both are unique and monotonic per symbol.
    # markPrice is timer-driven at 1 Hz so its event time is already unique. Anything else
    # falls back to a payload fingerprint, which preserves distinct events instead of guessing.
    _ID_FIELD = {"book": "u", "trade": "a", "oi": None, "depth": "u"}

    def _rest_paused(self) -> bool:
        return int(time.time() * 1000) < self._rest_cooldown_until_ms

    def _enter_cooldown(self, exc: "RateLimited") -> None:
        self._rest_cooldown_until_ms = int(time.time() * 1000) + int(exc.retry_after_s * 1000)
        self.rate_limit_hits += 1
        log.warning("RESEARCH_BINANCE_RATE_LIMITED | status=%s retry_after_s=%.1f body=%s",
                    exc.status, exc.retry_after_s, exc.body[:200])

    def _identity(self, key: str, symbol: str, payload: dict, fields: dict, event_ms) -> tuple:
        field = self._ID_FIELD.get(key)
        if field is not None:
            native = payload.get(field)
            if native is not None:
                return (EXCHANGE, key, symbol, "id", str(native))
        if key == "mark":
            return (EXCHANGE, key, symbol, "ts", int(event_ms or 0))
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]
        return (EXCHANGE, key, symbol, "fp", fingerprint)

    def _write(self, key: str, writer, payload: dict, now_ms: int, fields: dict) -> int:
        event_ms = payload.get("E")
        symbol = fields.get("symbol") or ""
        if len(self._seen) >= MAX_QUEUE:
            self._seen.clear()          # bounded; identity window resets rather than growing
        ident = self._identity(key, str(symbol), payload, fields, event_ms)
        if ident in self._seen:
            self.duplicates += 1
            return 0
        # Two updates in the same millisecond with different exchange ids are two real events.
        # The previous key was (kind, symbol, event_ms), which collapsed them and threw away
        # 5,870 genuine bookTicker rows in the first 90 s of collection.
        if (key, str(symbol), int(event_ms or 0)) in self._same_ms:
            self.same_ms_distinct_retained += 1
        self._same_ms.add((key, str(symbol), int(event_ms or 0)))
        if len(self._same_ms) >= MAX_QUEUE:
            self._same_ms.clear()
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
        self._last_row_ms[key] = now_ms
        return 1

    # --- REST: open interest ----------------------------------------------

    def poll_open_interest_once(self, fetch=None) -> int:
        """One sweep. Each row is stamped at its OWN fetch, never once per sweep."""
        fetch = fetch or _get
        written = 0
        for symbol in self.symbols:
            if self.stop_event.is_set():
                break
            if self._rest_paused():
                self.rate_limited_skips += 1
                continue
            started = int(time.time() * 1000)
            try:
                oi = fetch("/fapi/v1/openInterest", {"symbol": symbol})
                pi = fetch("/fapi/v1/premiumIndex", {"symbol": symbol})
            except RateLimited as exc:
                self._enter_cooldown(exc)
                self.oi_errors += 1
                break
            except Exception:
                log.warning("RESEARCH_BINANCE_OI_FETCH_FAILED | symbol=%s", symbol, exc_info=True)
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
                self._last_row_ms["oi"] = completed
                written += 1
            except Exception:
                log.exception("RESEARCH_BINANCE_OI_WRITE_FAILED")
                self.oi_errors += 1
        return written

    # --- REST: trades and mark price --------------------------------------
    #
    # These two classes are NOT collected over the websocket, and that is deliberate.
    # `btcusdt@aggTrade` and `btcusdt@markPrice@1s` are accepted by the server, appear in
    # LIST_SUBSCRIPTIONS, and then deliver nothing -- markPrice@1s is timer-driven and owes
    # exactly one message per second, yet produced zero in 12 s on a raw single-stream socket
    # while bookTicker on the same socket produced 4,804. The server also accepted and listed
    # `btcusdt@totalGarbage`, a stream name that does not exist, so a SUBSCRIBE ack carries no
    # delivery information whatsoever. REST returns the same data correctly, so REST is the
    # honest transport here. Book stays on the websocket, where delivery is proven.

    def poll_trades_once(self, fetch=None) -> int:
        """One aggregate-trade sweep. Resumes from the last id written, so it cannot skip."""
        fetch = fetch or _get
        written = 0
        for symbol in self.symbols:
            if self.stop_event.is_set():
                break
            if self._rest_paused():
                self.rate_limited_skips += 1
                continue
            started = int(time.time() * 1000)
            cursor = self._trade_cursor.get(symbol)
            params = {"symbol": symbol, "limit": AGG_TRADE_LIMIT}
            if cursor is not None:
                params["fromId"] = cursor + 1
            try:
                rows = fetch("/fapi/v1/aggTrades", params)
            except RateLimited as exc:
                self._enter_cooldown(exc)
                self.trade_errors += 1
                break
            except Exception:
                log.warning("RESEARCH_BINANCE_TRADE_FETCH_FAILED | symbol=%s", symbol, exc_info=True)
                self.trade_errors += 1
                continue
            if len(self.symbols) > 1:
                time.sleep(REST_INTER_REQUEST_S)  # stagger the sweep instead of bursting it
            completed = int(time.time() * 1000)
            if not isinstance(rows, list):
                self.trade_errors += 1
                continue
            for row in rows:
                if not isinstance(row, dict):
                    self.parse_failures += 1
                    continue
                trade_id = row.get("a")
                event_ms = row.get("T")
                if trade_id is None or event_ms is None:
                    self.parse_failures += 1
                    continue
                ident = (EXCHANGE, "trade", symbol, "id", str(trade_id))
                if ident in self._seen:
                    self.duplicates += 1
                    continue
                if len(self._seen) >= MAX_QUEUE:
                    self._seen.clear()
                self._seen.add(ident)
                try:
                    self.trade_writer.append({
                        "schema_version": f"{SCHEMA}_trade", "exchange": EXCHANGE, "source": "rest",
                        "symbol": symbol,
                        "event_timestamp_ms": int(event_ms),
                        "receive_timestamp_ms": completed,
                        "write_timestamp_ms": int(time.time() * 1000),
                        "fetch_started_ms": started, "fetch_completed_ms": completed,
                        "price": _f(row.get("p")), "quantity": _f(row.get("q")),
                        # Binance flags the BUYER as maker; the aggressor is the opposite side.
                        "buyer_is_maker": row.get("m"),
                        "aggressor_side": ("sell" if row.get("m") else "buy"),
                        "trade_id": trade_id,
                        "first_trade_id": row.get("f"), "last_trade_id": row.get("l"),
                        "trade_time_ms": int(event_ms),
                        "raw": row, "research_only": True,
                    })
                except Exception:
                    log.exception("RESEARCH_BINANCE_TRADE_WRITE_FAILED")
                    self.trade_errors += 1
                    continue
                self.counts["trade"] += 1
                self._last_row_ms["trade"] = completed
                written += 1
                latency = completed - float(event_ms)
                self.latencies.append(latency)
                if latency < 0:
                    self.negative_latency += 1
                prev = self._last_event_ms.get(("trade", symbol))
                if prev is not None and int(event_ms) < prev:
                    self.out_of_order += 1
                else:
                    self._last_event_ms[("trade", symbol)] = int(event_ms)
                if cursor is None or int(trade_id) > cursor:
                    cursor = int(trade_id)
                    self._trade_cursor[symbol] = cursor
        return written

    def poll_mark_once(self, fetch=None) -> int:
        """One mark/index/funding sweep. Stamped per symbol, never once per sweep."""
        fetch = fetch or _get
        written = 0
        for symbol in self.symbols:
            if self.stop_event.is_set():
                break
            if self._rest_paused():
                self.rate_limited_skips += 1
                continue
            started = int(time.time() * 1000)
            try:
                pi = fetch("/fapi/v1/premiumIndex", {"symbol": symbol})
            except RateLimited as exc:
                self._enter_cooldown(exc)
                self.mark_errors += 1
                break
            except Exception:
                log.warning("RESEARCH_BINANCE_MARK_FETCH_FAILED | symbol=%s", symbol, exc_info=True)
                self.mark_errors += 1
                continue
            completed = int(time.time() * 1000)
            mark = _f((pi or {}).get("markPrice"))
            if mark is None:
                self.mark_errors += 1
                continue
            index = _f((pi or {}).get("indexPrice"))
            event_ms = int(_f((pi or {}).get("time")) or completed)
            ident = (EXCHANGE, "mark", symbol, "ts", event_ms)
            if ident in self._seen:
                self.duplicates += 1
                continue
            if len(self._seen) >= MAX_QUEUE:
                self._seen.clear()
            self._seen.add(ident)
            try:
                self.mark_writer.append({
                    "schema_version": f"{SCHEMA}_mark", "exchange": EXCHANGE, "source": "rest",
                    "symbol": symbol,
                    "event_timestamp_ms": event_ms,
                    "receive_timestamp_ms": completed,
                    "write_timestamp_ms": int(time.time() * 1000),
                    "fetch_started_ms": started, "fetch_completed_ms": completed,
                    "mark_price": mark, "index_price": index,
                    "estimated_settle": _f((pi or {}).get("estimatedSettlePrice")),
                    "funding_rate": _f((pi or {}).get("lastFundingRate")),
                    "interest_rate": _f((pi or {}).get("interestRate")),
                    "next_funding_ms": int(_f((pi or {}).get("nextFundingTime")) or 0) or None,
                    "basis_bps": ((mark - index) / index * 10_000.0) if (mark and index) else None,
                    "raw": pi, "research_only": True,
                })
            except Exception:
                log.exception("RESEARCH_BINANCE_MARK_WRITE_FAILED")
                self.mark_errors += 1
                continue
            self.counts["mark"] += 1
            self._last_row_ms["mark"] = completed
            written += 1
        return written

    def _trade_loop(self) -> None:  # pragma: no cover - thread body
        while not self.stop_event.wait(TRADE_POLL_SECONDS):
            try:
                self.poll_trades_once()
            except Exception:
                log.exception("RESEARCH_BINANCE_TRADE_LOOP_FAILED")

    def _mark_loop(self) -> None:  # pragma: no cover - thread body
        while not self.stop_event.wait(MARK_POLL_SECONDS):
            try:
                self.poll_mark_once()
            except Exception:
                log.exception("RESEARCH_BINANCE_MARK_LOOP_FAILED")

    def _oi_loop(self) -> None:  # pragma: no cover - thread body
        while not self.stop_event.wait(self.oi_poll_seconds):
            try:
                self.poll_open_interest_once()
            except Exception:
                log.exception("RESEARCH_BINANCE_OI_LOOP_FAILED")

    # --- health -----------------------------------------------------------

    def clock_offsets(self, fetch=None) -> dict:
        """Local clock against Binance. A claimed 200 ms lead is meaningless without this.

        Skips the call entirely while in cooldown -- during the ban that motivated this fix,
        this endpoint was still being hit every health cycle for a result that was going to be
        418 regardless, which is exactly the behavior that produced the ban in the first place.
        """
        fetch = fetch or _get
        out = {}
        for label, path, key in (("binance", "/fapi/v1/time", "serverTime"),):
            if self._rest_paused():
                out[f"clock_offset_{label}_ms"] = None
                continue
            try:
                t0 = time.time() * 1000
                d = fetch(path, {})
                t1 = time.time() * 1000
                srv = _f((d or {}).get(key))
                out[f"clock_offset_{label}_ms"] = ((t0 + t1) / 2 - srv) if srv else None
            except RateLimited as exc:
                self._enter_cooldown(exc)
                out[f"clock_offset_{label}_ms"] = None
            except Exception:
                out[f"clock_offset_{label}_ms"] = None
        return out

    def stream_age_s(self, key: str) -> float | None:
        last = self._last_row_ms.get(key)
        return None if last is None else max(0.0, (int(time.time() * 1000) - last) / 1000.0)

    def stream_health(self, key: str) -> str:
        """One stream's verdict, derived ONLY from that stream's own rows.

        Nothing here reads another stream's counters. A healthy book stream must never be
        able to make a dead trade stream look alive -- that inheritance is exactly how the
        PR #65 deployment reported itself healthy while aggTrade and markPrice were silent.
        """
        if self.counts.get(key, 0) <= 0:
            return "NO_DELIVERY_PROVEN"
        age = self.stream_age_s(key)
        if age is None:
            # Rows counted but no freshness clock means the write path forgot to stamp itself.
            # Reporting HEALTHY here would let a stream that died hours ago coast indefinitely.
            return "UNKNOWN_AGE"
        if age > STREAM_STALE_SECONDS.get(key, 60.0):
            return "STALE"
        return "HEALTHY"

    def per_stream_health(self) -> dict:
        out = {}
        for key in ("book", "trade", "mark", "oi", "depth"):
            out[key] = {"rows": self.counts.get(key, 0),
                        "last_event_age_s": self.stream_age_s(key),
                        "status": self.stream_health(key)}
        out["liq"] = {"rows": self.counts.get("liq", 0),
                      "last_event_age_s": self.stream_age_s("liq"),
                      "status": self.liquidation_status()}
        return out

    def liquidation_status(self) -> str:
        """`HEALTHY_NO_EVENT_OBSERVED` is not the same claim as `NO_DELIVERY_PROVEN`."""
        if self.liq_events_total > 0:
            return "DELIVERING"
        # `!forceOrder@arr` rides the same websocket that provably refuses to deliver aggTrade
        # and markPrice while acking both, so websocket silence here is not evidence about the
        # market. Trade rows now arrive over REST and say nothing about this socket, so they
        # must NOT be used to upgrade this verdict -- that inference would be unfounded.
        if self.counts.get("book", 0) > 0:
            return "HEALTHY_NO_EVENTS_OBSERVED"
        return "NO_DELIVERY_PROVEN"

    def health(self) -> dict:
        lat = sorted(self.latencies[-5000:])
        now = int(time.time() * 1000)
        return {
            "schema_version": f"{SCHEMA}_health", "exchange": EXCHANGE,
            "write_timestamp_ms": now, "uptime_s": (now - self.started_ms) / 1000.0,
            "rows": dict(self.counts),
            "streams": self.per_stream_health(),
            "book_stream_health": self.stream_health("book"),
            "trade_stream_health": self.stream_health("trade"),
            "mark_stream_health": self.stream_health("mark"),
            "oi_stream_health": self.stream_health("oi"),
            "liq_stream_health": self.liquidation_status(),
            "trade_transport": "rest", "mark_transport": "rest", "book_transport": "websocket",
            "parse_failures": self.parse_failures, "dropped": self.dropped,
            "duplicates": self.duplicates, "out_of_order": self.out_of_order,
            "negative_latency_rows": self.negative_latency,
            "latency_median_ms": lat[len(lat) // 2] if lat else None,
            "latency_p95_ms": lat[int(0.95 * len(lat))] if lat else None,
            "reconnects": self.reconnects, "ws_errors": self.ws_errors,
            "oi_errors": self.oi_errors, "trade_errors": self.trade_errors,
            "mark_errors": self.mark_errors,
            "rate_limit_hits": self.rate_limit_hits, "rate_limited_skips": self.rate_limited_skips,
            "rest_cooldown_active": self._rest_paused(),
            "rest_cooldown_remaining_s": max(0.0, (self._rest_cooldown_until_ms - int(time.time()*1000))/1000.0),
            "same_ms_distinct_events_retained": self.same_ms_distinct_retained,
            "liq_events_total": self.liq_events_total,
            "last_liq_event_age_s": ((now - self.last_liq_event_ms) / 1000.0
                                     if self.last_liq_event_ms else None),
            "liquidation_status": self.liquidation_status(),
            "symbols_expected": len(self.symbols),
            "research_only": True, "orders_allowed": False,
        }

    def close(self) -> None:
        self.stop_event.set()
        for w in (self.trade_writer, self.book_writer, self.depth_writer, self.mark_writer,
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
    threading.Thread(target=c._trade_loop, daemon=True, name="bn-trade").start()
    threading.Thread(target=c._mark_loop, daemon=True, name="bn-mark").start()
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
