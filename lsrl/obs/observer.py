"""Causal observation layer: raw Bitget WebSocket frames -> engine events (pure, deterministic, no clock).

The strategy engine never sees a socket. It sees (a) `DepthSnapshot`s that are the CAUSALLY RECONSTRUCTED
book after each validated depth update, (b) `TradeTick`s from the public trade tape (taker side), and
(c) `FeedHealth`. Everything here is a function of the raw event stream (which is what the recorder
persists), so a fresh observer + engine replaying the raw log reproduces every decision.

Integrity model (Bitget `books` channel; verified on live frames 2026-09-20):
  * first push = full snapshot (200 levels/side), later pushes = incremental updates (size "0" removes a level);
  * every update carries `seq` and `pseq` (the previous message's seq): the chain must be unbroken.
    The documentation states the `checksum` field was REMOVED from these channels, and none is present in
    live frames, so the seq/pseq chain + book-validity checks are the consistency test;
  * `trade` pushes: a `snapshot` of recent history at subscribe (ignored as evidence: it precedes the book
    baseline), then live `update`s; `side` is the TAKER side; `tradeId` is NOT monotonic (dedupe only).

Fail closed: any sequence gap, out-of-order/regressing exchange time, clock violation, crossed/empty/garbled
book, stale feed, disconnect, or update-before-snapshot makes the view UNSAFE (FeedHealth ok=False). An
UNSAFE view emits no market data until a NEW valid snapshot reconstructs the book (epoch += 1).
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import OrderedDict
from dataclasses import asdict, dataclass
from typing import Any

from lsrl.core.types import (DepthSnapshot, FeedHealth, FeedMarker, RawBook, RawTrade, TimerTick,
                             TradeTick)

NO_BOOK, OK, UNSAFE = "NO_BOOK", "OK", "UNSAFE"


@dataclass(frozen=True, slots=True)
class FeedConfig:
    """Operational feed parameters (NOT strategy parameters). Recorded in every log header."""
    exchange: str = "bitget"
    inst_type: str = "USDT-FUTURES"
    symbol: str = "SOLUSDT"
    book_channel: str = "books"
    trade_channel: str = "trade"
    expected_book_levels: int = 200        # informational: 200/side observed on the live channel
    book_stale_ms: int = 5_000             # no depth push for this long = STALE (live rate ~10/s)
    tape_stale_ms: int = 180_000           # no trade for this long = STALE (SOLUSDT trades every few seconds)
    max_lag_ms: int = 5_000                # local receipt - exchange ts above this = clock/latency violation
    max_ahead_ms: int = 2_000              # exchange ts ahead of the local clock above this = violation
    trade_dedupe_window: int = 50_000
    trade_ts_tolerance_ms: int = 1_000     # real trade batches jitter by a few ms; a regression beyond this = clock fault

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _parse_levels(target: dict[float, float], levels: tuple[tuple[str, str], ...]) -> bool:
    """Apply (price, size) strings to `target`. False on any garbled level."""
    for raw_price, raw_size in levels:
        try:
            price, size = float(raw_price), float(raw_size)
        except (TypeError, ValueError):
            return False
        if not (math.isfinite(price) and math.isfinite(size)) or price <= 0.0 or size < 0.0:
            return False
        if size == 0.0:
            target.pop(price, None)
        else:
            target[price] = size
    return True


class BookObserver:
    def __init__(self, cfg: FeedConfig | None = None) -> None:
        self.cfg = cfg or FeedConfig()
        self.reset()

    def reset(self) -> None:
        """Back to the state of a brand-new process: no book, epoch 0, nothing remembered."""
        self.state = NO_BOOK
        self.unsafe_reason = "NO_BOOK_YET"
        self.epoch = 0
        self.conn_id = ""
        self.disconnected = False                  # between DISCONNECT and the next CONNECT there is no connection to hear from
        self.resnapshot_needed = True
        self._bids: dict[float, float] = {}
        self._asks: dict[float, float] = {}
        self._last_seq: int | None = None
        self._last_book_ts: int | None = None
        self._last_book_recv: int | None = None
        self._epoch_start_recv: int | None = None
        self._last_trade_ts: int | None = None
        self._last_trade_recv: int | None = None
        self._seen: OrderedDict[str, None] = OrderedDict()
        self.stats: dict[str, int] = {}

    # ------------------------------------------------------------------ helpers
    def _count(self, key: str, n: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + n

    def _remember(self, trade_id: str) -> bool:
        """True if new. Bounded so memory cannot grow without limit."""
        if trade_id in self._seen:
            return False
        self._seen[trade_id] = None
        while len(self._seen) > self.cfg.trade_dedupe_window:
            self._seen.popitem(last=False)
        return True

    def _clock_violation(self, t_ms: int, exchange_ts: int) -> str | None:
        lag = t_ms - exchange_ts
        if lag > self.cfg.max_lag_ms:
            return f"CLOCK_LAG:{lag}ms"
        if -lag > self.cfg.max_ahead_ms:
            return f"CLOCK_AHEAD:{-lag}ms"
        return None

    def _clear_book(self) -> None:
        self._bids, self._asks = {}, {}
        self._last_seq = self._last_book_ts = self._last_book_recv = None
        self._epoch_start_recv = self._last_trade_ts = self._last_trade_recv = None

    def _go_unsafe(self, t_ms: int, reason: str) -> list[Any]:
        was_ok = self.state == OK
        self.state, self.unsafe_reason, self.resnapshot_needed = UNSAFE, reason, True
        self._clear_book()
        self._count("unsafe:" + reason.split(":", 1)[0])
        return [FeedHealth(t_ms, False, reason, self.epoch)] if was_ok else []

    def _depth(self, ev: RawBook) -> DepthSnapshot:
        return DepthSnapshot(
            t_ms=ev.t_ms, exchange_ts_ms=ev.exchange_ts_ms,
            bids=tuple(sorted(self._bids.items(), key=lambda kv: -kv[0])),
            asks=tuple(sorted(self._asks.items(), key=lambda kv: kv[0])),
            seq=ev.seq, epoch=self.epoch)

    def window_width(self) -> tuple[float, float] | None:
        """(bid window, ask window) in price units for the current book; None if there is no book."""
        if not self._bids or not self._asks:
            return None
        return (max(self._bids) - min(self._bids), max(self._asks) - min(self._asks))

    # ------------------------------------------------------------------ dispatch
    def on_event(self, ev: Any) -> list[Any]:
        """Derived engine events for one RAW event, in order. The caller forwards the raw TimerTick itself."""
        if isinstance(ev, (RawBook, RawTrade)):
            if self.disconnected or (ev.conn_id and self.conn_id and ev.conn_id != self.conn_id):
                self._count("foreign_connection_messages")
                return []
            if isinstance(ev, RawBook):
                return self._on_snapshot(ev) if ev.action == "snapshot" else self._on_update(ev)
            return self._on_trade(ev)
        if isinstance(ev, FeedMarker):
            return self._on_marker(ev)
        if isinstance(ev, TimerTick):
            return self._on_tick(ev)
        return []

    # ------------------------------------------------------------------ markers
    def _on_marker(self, ev: FeedMarker) -> list[Any]:
        kind = ev.kind
        if kind == "SESSION_START":
            self.reset()
            return [FeedHealth(ev.t_ms, False, "SESSION_START", 0)]
        if kind in ("CONNECT", "DISCONNECT", "RESET"):
            was_ok = self.state == OK
            self.conn_id = ev.conn_id if kind == "CONNECT" else (self.conn_id if kind == "RESET" else "")
            self.disconnected = kind == "DISCONNECT"
            self.state, self.resnapshot_needed = NO_BOOK, True
            self.unsafe_reason = {"CONNECT": "RECONNECTED_AWAITING_SNAPSHOT", "DISCONNECT": "DISCONNECTED",
                                  "RESET": "FEED_RESET"}[kind]
            self._clear_book()
            self._count("marker:" + kind)
            return [FeedHealth(ev.t_ms, False, self.unsafe_reason, self.epoch)] if was_ok else []
        return []                                # SUBSCRIBED and unknown informational kinds

    # ------------------------------------------------------------------ depth
    def _on_snapshot(self, ev: RawBook) -> list[Any]:
        bids: dict[float, float] = {}
        asks: dict[float, float] = {}
        if not (_parse_levels(bids, ev.bids) and _parse_levels(asks, ev.asks)):
            return self._go_unsafe(ev.t_ms, "BAD_SNAPSHOT_LEVEL")
        if not bids or not asks:
            return self._go_unsafe(ev.t_ms, "EMPTY_SNAPSHOT_SIDE")
        if max(bids) >= min(asks):
            return self._go_unsafe(ev.t_ms, "CROSSED_SNAPSHOT")
        if ev.seq is None or ev.exchange_ts_ms is None:
            return self._go_unsafe(ev.t_ms, "SNAPSHOT_WITHOUT_SEQ_OR_TS")
        violation = self._clock_violation(ev.t_ms, ev.exchange_ts_ms)
        if violation:
            return self._go_unsafe(ev.t_ms, violation)
        out: list[Any] = []
        if self.state == OK:                         # a baseline we did not ask for: the interval is uncertain
            out.append(FeedHealth(ev.t_ms, False, "UNEXPECTED_SNAPSHOT", self.epoch))
            self._count("unexpected_snapshot")
        self.epoch += 1
        self.state, self.unsafe_reason, self.resnapshot_needed = OK, "", False
        self._bids, self._asks = bids, asks
        self._last_seq, self._last_book_ts = ev.seq, ev.exchange_ts_ms
        self._last_book_recv = self._epoch_start_recv = ev.t_ms
        self._last_trade_ts = self._last_trade_recv = None
        out.append(FeedHealth(ev.t_ms, True, "BOOTSTRAP" if self.epoch == 1 else "RESNAPSHOT", self.epoch))
        out.append(self._depth(ev))
        return out

    def _on_update(self, ev: RawBook) -> list[Any]:
        if self.state != OK:
            self._count("dropped_update_book_not_ok")
            return []
        if ev.seq is None or ev.pseq is None or ev.exchange_ts_ms is None:
            return self._go_unsafe(ev.t_ms, "UPDATE_WITHOUT_SEQ_OR_TS")
        assert self._last_seq is not None
        if ev.seq == self._last_seq:                 # exact redelivery of the last applied message
            self._count("duplicate_update")
            return []
        if ev.seq < self._last_seq:
            return self._go_unsafe(ev.t_ms, f"OUT_OF_ORDER_SEQ:{ev.seq}<{self._last_seq}")
        if ev.pseq != self._last_seq:
            return self._go_unsafe(ev.t_ms, f"SEQ_GAP:expected_pseq={self._last_seq},got={ev.pseq}")
        if self._last_book_ts is not None and ev.exchange_ts_ms < self._last_book_ts:
            return self._go_unsafe(ev.t_ms, f"EXCHANGE_TS_REGRESSION:{ev.exchange_ts_ms}<{self._last_book_ts}")
        violation = self._clock_violation(ev.t_ms, ev.exchange_ts_ms)
        if violation:
            return self._go_unsafe(ev.t_ms, violation)
        bids, asks = dict(self._bids), dict(self._asks)      # apply atomically: a bad message never half-applies
        if not (_parse_levels(bids, ev.bids) and _parse_levels(asks, ev.asks)):
            return self._go_unsafe(ev.t_ms, "BAD_UPDATE_LEVEL")
        if not bids or not asks:
            return self._go_unsafe(ev.t_ms, "EMPTY_BOOK_SIDE")
        if max(bids) >= min(asks):
            return self._go_unsafe(ev.t_ms, "CROSSED_BOOK")
        self._bids, self._asks = bids, asks
        self._last_seq, self._last_book_ts, self._last_book_recv = ev.seq, ev.exchange_ts_ms, ev.t_ms
        return [self._depth(ev)]

    # ------------------------------------------------------------------ tape
    def _on_trade(self, ev: RawTrade) -> list[Any]:
        if ev.action == "snapshot":                  # history that PRECEDES our baseline: never evidence
            self._remember(ev.trade_id)
            self._count("history_trade_ignored")
            return []
        if self.state != OK:
            self._count("dropped_trade_book_not_ok")
            return []
        if not self._remember(ev.trade_id):
            self._count("duplicate_trade")
            return []
        side = str(ev.side).lower()
        if side not in ("buy", "sell"):
            return self._go_unsafe(ev.t_ms, f"BAD_TRADE_SIDE:{ev.side}")
        if ev.exchange_ts_ms is None:
            return self._go_unsafe(ev.t_ms, "TRADE_WITHOUT_TS")
        try:
            price, size = float(ev.price), float(ev.size)
        except (TypeError, ValueError):
            return self._go_unsafe(ev.t_ms, "BAD_TRADE_VALUE")
        if not (math.isfinite(price) and math.isfinite(size)) or price <= 0.0 or size <= 0.0:
            return self._go_unsafe(ev.t_ms, "BAD_TRADE_VALUE")
        if self._last_trade_ts is not None and ev.exchange_ts_ms < self._last_trade_ts:
            # Live trade batches are not strictly time-ordered (a few ms). Consumption is order-insensitive and
            # the detector judges every trade against the frozen state on its own exchange time, so jitter is
            # counted, not fatal; only a regression beyond the tolerance is a clock fault.
            if self._last_trade_ts - ev.exchange_ts_ms > self.cfg.trade_ts_tolerance_ms:
                return self._go_unsafe(ev.t_ms, f"TRADE_TS_REGRESSION:{ev.exchange_ts_ms}<{self._last_trade_ts}")
            self._count("trade_ts_minor_regression")
        violation = self._clock_violation(ev.t_ms, ev.exchange_ts_ms)
        if violation:
            return self._go_unsafe(ev.t_ms, violation)
        self._last_trade_ts = max(self._last_trade_ts or 0, ev.exchange_ts_ms)
        self._last_trade_recv = ev.t_ms
        return [TradeTick(t_ms=ev.t_ms, exchange_ts_ms=ev.exchange_ts_ms, price=price, size=size,
                          aggressor="BUY" if side == "buy" else "SELL", trade_id=ev.trade_id)]

    # ------------------------------------------------------------------ staleness (driven by recorded ticks)
    def _on_tick(self, ev: TimerTick) -> list[Any]:
        if self.state != OK or self._last_book_recv is None:
            return []
        if ev.t_ms - self._last_book_recv > self.cfg.book_stale_ms:
            return self._go_unsafe(ev.t_ms, f"STALE_BOOK:{ev.t_ms - self._last_book_recv}ms")
        tape_ref = self._last_trade_recv if self._last_trade_recv is not None else self._epoch_start_recv
        if tape_ref is not None and ev.t_ms - tape_ref > self.cfg.tape_stale_ms:
            return self._go_unsafe(ev.t_ms, f"STALE_TAPE:{ev.t_ms - tape_ref}ms")
        return []
