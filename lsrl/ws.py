"""Bitget public WebSocket -> RAW feed events (RawBook / RawTrade / FeedMarker). No trading surface.

`parse_frame` is pure (unit-tested against real captured frames). `EventBus` gives every producer thread a
single ordered, monotonic stamping point: the local receipt time is taken UNDER THE QUEUE LOCK, so queue
order is non-decreasing in `t_ms` by construction (the engine requires that). `BitgetWsFeed` is the thin
transport: subscribe, ping/pong, silence watchdog, reconnect. It never interprets market data; the observer
does. REST is not used here at all: recovery is a fresh WebSocket snapshot.

Protocol facts (live-verified 2026-09-20, docs: classic > WebSocket > Futures > Public):
  * `books` = full depth: first push `snapshot`, then incremental `update` (150 ms cadence); `seq`/`pseq`
    let the client detect loss and out-of-order delivery; the `checksum` field was removed;
  * `trade` = taker-side public trades; first push `snapshot` is recent HISTORY (not live);
  * client must send the plain string "ping" at least every 30 s, the server answers the plain string
    "pong" (NOT JSON) and drops silent connections after 2 minutes; <= 10 client messages/s and
    240 subscribe requests/hour per connection.
"""
from __future__ import annotations

import json
import logging
import ssl
import threading
import time
import uuid
from collections import deque
from typing import Any, Callable

from lsrl.core.types import FeedMarker, RawBook, RawTrade
from lsrl.obs.observer import FeedConfig

BITGET_PUBLIC_WS = "wss://ws.bitget.com/v2/ws/public"
log = logging.getLogger("lsrl_ws")


# ---------------------------------------------------------------------------------------------- bus
class EventBus:
    """Thread-safe FIFO whose stamping and enqueueing are ONE atomic step (monotonic t_ms by construction)."""

    def __init__(self, clock: Callable[[], int] = lambda: int(time.time() * 1000)) -> None:
        self._lock = threading.Lock()
        self._queue: deque[Any] = deque()
        self._clock = clock
        self._last = 0

    def _stamp(self) -> int:
        self._last = max(int(self._clock()), self._last)
        return self._last

    def now(self) -> int:
        with self._lock:
            return self._stamp()

    def floor(self, t_ms: int) -> None:
        """Never stamp earlier than `t_ms` (e.g. the restored engine time after a clock stepped backwards)."""
        with self._lock:
            self._last = max(self._last, int(t_ms))

    def push(self, factory: Callable[[int], Any]) -> Any:
        with self._lock:
            ev = factory(self._stamp())
            self._queue.append(ev)
            return ev

    def push_many(self, factory: Callable[[int], list[Any]]) -> list[Any]:
        with self._lock:
            events = factory(self._stamp())
            self._queue.extend(events)
            return events

    def drain(self) -> list[Any]:
        with self._lock:
            items = list(self._queue)
            self._queue.clear()
            return items

    def __len__(self) -> int:
        with self._lock:
            return len(self._queue)


# ---------------------------------------------------------------------------------------------- parsing
def subscription_message(feed: FeedConfig) -> str:
    args = [{"instType": feed.inst_type, "channel": feed.book_channel, "instId": feed.symbol},
            {"instType": feed.inst_type, "channel": feed.trade_channel, "instId": feed.symbol}]
    return json.dumps({"op": "subscribe", "args": args}, separators=(",", ":"))


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _levels(rows: Any) -> tuple[tuple[str, str], ...]:
    out = []
    for row in rows or []:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            out.append((str(row[0]), str(row[1])))
    return tuple(out)


def parse_frame(raw: str, t_ms: int, conn_id: str, feed: FeedConfig) -> list[Any]:
    """One websocket text frame -> raw feed events (all stamped `t_ms`). Never raises."""
    if raw == "pong":
        return []
    try:
        d = json.loads(raw)
        if not isinstance(d, dict):
            raise ValueError("not an object")
    except ValueError:
        return [FeedMarker(t_ms, "BAD_FRAME", conn_id, str(raw)[:80])]
    arg = d.get("arg") or {}
    channel, inst = str(arg.get("channel") or ""), str(arg.get("instId") or "").upper()
    event = d.get("event")
    if event == "subscribe":
        return [FeedMarker(t_ms, "SUBSCRIBED", conn_id, channel)]
    if event == "error":
        return [FeedMarker(t_ms, "SUBSCRIBE_ERROR", conn_id, f"{d.get('code')}:{d.get('msg')}")]
    if event is not None or "data" not in d or inst != feed.symbol.upper():
        return []
    action = str(d.get("action") or "")
    out: list[Any] = []
    if channel == feed.book_channel:
        outer_ts = _int_or_none(d.get("ts"))
        for item in d.get("data") or []:
            if not isinstance(item, dict):
                continue
            ts = _int_or_none(item.get("ts"))
            out.append(RawBook(t_ms=t_ms, exchange_ts_ms=ts if ts is not None else outer_ts, action=action,
                               seq=_int_or_none(item.get("seq")), pseq=_int_or_none(item.get("pseq")),
                               bids=_levels(item.get("bids")), asks=_levels(item.get("asks")), conn_id=conn_id))
    elif channel == feed.trade_channel:
        for item in d.get("data") or []:
            if not isinstance(item, dict):
                continue
            out.append(RawTrade(t_ms=t_ms, exchange_ts_ms=_int_or_none(item.get("ts")), action=action,
                                trade_id=str(item.get("tradeId", "")), price=str(item.get("price", "")),
                                size=str(item.get("size", "")), side=str(item.get("side", "")), conn_id=conn_id))
    return out


def websocket_ssl_options() -> dict:
    options: dict[str, Any] = {"cert_reqs": ssl.CERT_REQUIRED}
    try:
        import certifi
        options["ca_certs"] = certifi.where()
    except ImportError:
        pass
    return options


# ---------------------------------------------------------------------------------------------- transport
class BitgetWsFeed:
    def __init__(self, bus: EventBus, feed: FeedConfig, *, url: str = BITGET_PUBLIC_WS,
                 ws_factory: Callable[..., Any] | None = None, ping_interval_s: float = 25.0,
                 silence_timeout_s: float = 60.0, backoff_s: tuple[float, ...] = (1, 2, 4, 8, 15, 30),
                 min_reconnect_gap_s: float = 2.0, clock_s: Callable[[], float] = time.monotonic) -> None:
        self.bus, self.feed, self.url = bus, feed, url
        self.ws_factory = ws_factory
        self.ping_interval_s, self.silence_timeout_s = ping_interval_s, silence_timeout_s
        self.backoff_s, self.min_reconnect_gap_s, self.clock_s = backoff_s, min_reconnect_gap_s, clock_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._watchdog: threading.Thread | None = None
        self._ws: Any = None
        self.conn_id = ""
        self._last_frame_s = self._last_ping_s = self._opened_s = 0.0
        self._last_reconnect_s = -1e9
        self._disconnect_reason = ""
        self.stats = {"frames": 0, "reconnects": 0, "pongs": 0, "bad_frames": 0, "connections": 0}

    # ------------------------------------------------------------------ callbacks (also driven by tests)
    def on_open(self, ws: Any) -> None:
        self._ws = ws
        self.conn_id = uuid.uuid4().hex
        now = self.clock_s()
        self._last_frame_s = self._last_ping_s = self._opened_s = now
        self.stats["connections"] += 1
        conn = self.conn_id
        self.bus.push(lambda t: FeedMarker(t, "CONNECT", conn))
        try:
            ws.send(subscription_message(self.feed))
        except Exception as exc:                       # no subscription = no data: do not sit on a dead socket
            log.warning("LSRL_WS_SUBSCRIBE_FAILED | %s", exc)
            self._disconnect_reason = f"SUBSCRIBE_SEND_FAILED:{type(exc).__name__}"
            self._close(ws)

    def on_message(self, ws: Any, message: Any) -> None:
        if ws is not self._ws:                         # a superseded connection must never feed the bus
            return
        self._last_frame_s = self.clock_s()
        text = message if isinstance(message, str) else bytes(message).decode("utf-8", "replace")
        if text == "pong":
            self.stats["pongs"] += 1
            return
        conn = self.conn_id
        events = self.bus.push_many(lambda t: parse_frame(text, t, conn, self.feed))
        self.stats["frames"] += 1
        for e in events:
            if isinstance(e, FeedMarker):
                if e.kind == "BAD_FRAME":
                    self.stats["bad_frames"] += 1
                elif e.kind == "SUBSCRIBE_ERROR":
                    self._disconnect_reason = "SUBSCRIBE_ERROR:" + e.detail
                    self._close(ws)

    def on_close(self, ws: Any, code: Any = None, reason: Any = None) -> None:
        if ws is not self._ws:
            return
        self._ws = None
        detail = self._disconnect_reason or f"CLOSED:{code}:{reason}"
        self._disconnect_reason = ""
        conn = self.conn_id
        self.bus.push(lambda t: FeedMarker(t, "DISCONNECT", conn, detail))

    def on_error(self, ws: Any, error: Any) -> None:
        log.warning("LSRL_WS_ERROR | %s", error)

    @staticmethod
    def _close(ws: Any) -> None:
        try:
            ws.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ heartbeat / watchdog
    def tick(self) -> None:
        """Ping on schedule; kill a silent connection. Called once a second by the watchdog thread."""
        ws = self._ws
        if ws is None:
            return
        now = self.clock_s()
        if now - self._last_frame_s > self.silence_timeout_s:
            self._disconnect_reason = f"SILENT_{int(now - self._last_frame_s)}s"
            self._close(ws)
            return
        if now - self._last_ping_s >= self.ping_interval_s:
            self._last_ping_s = now
            try:
                ws.send("ping")
            except Exception as exc:
                self._disconnect_reason = f"PING_FAILED:{type(exc).__name__}"
                self._close(ws)

    def request_reconnect(self, reason: str) -> bool:
        """Ask for a fresh connection (and therefore a fresh snapshot). Rate-limited."""
        ws = self._ws
        now = self.clock_s()
        if ws is None or now - self._last_reconnect_s < self.min_reconnect_gap_s:
            return False
        self._last_reconnect_s = now
        self._disconnect_reason = "RECONNECT_REQUESTED:" + reason
        self.stats["reconnects"] += 1
        self._close(ws)
        return True

    def parameters(self) -> dict[str, Any]:
        """Transport parameters as configured (recorded in every log header next to the feed configuration)."""
        return {"url": self.url, "ping_interval_s": self.ping_interval_s, "silence_timeout_s": self.silence_timeout_s,
                "backoff_s": list(self.backoff_s), "min_reconnect_gap_s": self.min_reconnect_gap_s,
                "subscription": json.loads(subscription_message(self.feed))["args"]}

    @property
    def connected(self) -> bool:
        return self._ws is not None

    def status(self) -> dict[str, Any]:
        return {**self.stats, "connected": self.connected, "conn_id": self.conn_id,
                "last_frame_age_s": round(self.clock_s() - self._last_frame_s, 3)}

    # ------------------------------------------------------------------ thread lifecycle
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="lsrl-ws", daemon=True)
        self._watchdog = threading.Thread(target=self._watch, name="lsrl-ws-watchdog", daemon=True)
        self._thread.start()
        self._watchdog.start()

    def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            self._close(self._ws)

    def _watch(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self.tick()
            except Exception:
                log.exception("LSRL_WS_WATCHDOG_ERROR")

    def _run(self) -> None:
        import websocket
        factory = self.ws_factory or websocket.WebSocketApp
        attempt = 0
        while not self._stop.is_set():
            started = self.clock_s()
            app = factory(self.url, on_open=self.on_open, on_message=self.on_message, on_close=self.on_close,
                          on_error=self.on_error)
            try:
                app.run_forever(sslopt=websocket_ssl_options())
            except Exception:
                log.exception("LSRL_WS_RUN_FOREVER_FAILED")
            if self._stop.is_set():
                break
            attempt = 0 if self.clock_s() - started > 60.0 else attempt + 1
            self._stop.wait(self.backoff_s[min(attempt, len(self.backoff_s) - 1)] if attempt else 1.0)
