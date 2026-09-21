"""Ground truth -> RAW Bitget frames, for tests that must go through the production observation layer.

A `Truth` is what really happened on the exchange, at millisecond resolution: the book at each instant and
the trades that executed. `to_raw` OBSERVES it the way the bot would: an initial `snapshot`, then only the
changed levels as `update` frames with a `seq`/`pseq` chain, trades as separate `RawTrade` frames, each with
its own network latency, merged into ARRIVAL order (the order the recorder would persist). Bars are merged
in by their own receipt time.

This is the same method as Agent C's acceptance timelines (truth defined first, observation second), except
that the observation is the real `RawBook`/`RawTrade` stream, so every test using it exercises
`BookObserver` -> engine exactly as production does.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable

from lsrl.core.types import BarClosed, DepthSnapshot, FeedMarker, RawBook, RawTrade
from tests.lsrl_fixtures import GRID, S, make_book, mirror_event, px

CONN = "conn-1"


def fmt(x: float) -> str:
    """Price/size as an exchange string ('109.78', '21', '0.6')."""
    text = f"{x:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def book_state(snap: DepthSnapshot) -> tuple[dict[float, float], dict[float, float]]:
    return dict(snap.bids), dict(snap.asks)


@dataclass
class Truth:
    states: list[tuple[int, dict[float, float], dict[float, float]]] = field(default_factory=list)
    trades: list[tuple[int, float, float, str]] = field(default_factory=list)   # (ts, price, size, taker side)
    history: list[tuple[int, float, float, str]] = field(default_factory=list)  # trades BEFORE the subscription

    def book(self, ts: int, best_bid: float, best_ask: float, *, bid_pools=(), ask_pools=(), **kw) -> "Truth":
        snap = make_book(ts, best_bid, best_ask, bid_pools=bid_pools, ask_pools=ask_pools, **kw)
        self.states.append((ts, *book_state(snap)))
        return self

    def state(self, ts: int, bids: dict[float, float], asks: dict[float, float]) -> "Truth":
        self.states.append((ts, dict(bids), dict(asks)))
        return self

    def trade(self, ts: int, price: float, size: float, side: str = "sell") -> "Truth":
        self.trades.append((ts, px(price), size, side))
        return self


def _levels(d: dict[float, float]) -> tuple[tuple[str, str], ...]:
    return tuple((fmt(p), fmt(s)) for p, s in d.items())


def _diff(prev: dict[float, float], cur: dict[float, float]) -> tuple[tuple[str, str], ...]:
    changes = [(fmt(p), fmt(s)) for p, s in cur.items() if prev.get(p) != s]
    changes += [(fmt(p), "0") for p in prev if p not in cur]
    return tuple(changes)


def _with_keepalive(states: list, keepalive_ms: int, until_ms: int) -> list:
    """The real book pushes ~10 updates/s even when nothing interesting happens. A synthetic timeline that
    changes nothing for minutes would (correctly) look like a dead feed, so quiet stretches are filled with a
    harmless deep-book size wobble, exactly as the noise a live book carries."""
    if keepalive_ms <= 0:
        return list(states)
    out: list = []
    wobble = 0

    def emit_gap(ts_from: int, ts_to: int, bids: dict, asks: dict) -> None:
        nonlocal wobble
        t = ts_from + keepalive_ms
        while t < ts_to:
            wobble += 1
            deep = min(bids)
            b = dict(bids)
            b[deep] = round(bids[deep] + (0.1 if wobble % 2 else 0.0), 6)
            out.append((t, b, dict(asks)))
            t += keepalive_ms

    for i, (ts, bids, asks) in enumerate(states):
        out.append((ts, bids, asks))
        nxt = states[i + 1][0] if i + 1 < len(states) else until_ms
        emit_gap(ts, nxt, bids, asks)
    return out


def to_raw(truth: Truth, *, book_lag_ms: int = 100, trade_lag_ms: int = 80, conn_id: str = CONN,
           seq0: int = 176_000_000_000, bars: Iterable[BarClosed] = (), connect_recv_ms: int | None = None,
           tape_snapshot: bool = True, extra: Iterable[Any] = (), keepalive_ms: int = 2_000,
           keepalive_until_ms: int | None = None, tape_keepalive_ms: int = 20_000) -> list[Any]:
    """Raw events in ARRIVAL order (non-decreasing t_ms). `extra` events are merged by their own t_ms."""
    events: list[Any] = []
    bars = list(bars)
    horizon = keepalive_until_ms if keepalive_until_ms is not None else max(
        [b.t_ms for b in bars] + [truth.states[-1][0]])
    first_ts = truth.states[0][0]
    connect_ms = connect_recv_ms if connect_recv_ms is not None else first_ts + book_lag_ms - 50
    events.append((connect_ms, 0, FeedMarker(connect_ms, "CONNECT", conn_id)))
    seq = seq0
    prev_bids: dict[float, float] = {}
    prev_asks: dict[float, float] = {}
    prev_seq = 0
    for i, (ts, bids, asks) in enumerate(_with_keepalive(truth.states, keepalive_ms, horizon)):
        if i == 0:
            frame = RawBook(t_ms=ts + book_lag_ms, exchange_ts_ms=ts, action="snapshot", seq=seq, pseq=0,
                            bids=_levels(bids), asks=_levels(asks), conn_id=conn_id)
        else:
            b, a = _diff(prev_bids, bids), _diff(prev_asks, asks)
            if not b and not a:
                continue
            prev_seq = seq
            seq += 100 + (i % 7)                       # real seq values jump; never assume +1
            frame = RawBook(t_ms=ts + book_lag_ms, exchange_ts_ms=ts, action="update", seq=seq, pseq=prev_seq,
                            bids=b, asks=a, conn_id=conn_id)
        events.append((frame.t_ms, 2, frame))
        prev_bids, prev_asks = bids, asks
    if tape_snapshot:
        for j, (ts, price, size, side) in enumerate(truth.history):
            recv = connect_ms + 10
            events.append((recv, 1, RawTrade(t_ms=recv, exchange_ts_ms=ts, action="snapshot", trade_id=f"h{j}",
                                             price=fmt(price), size=fmt(size), side=side, conn_id=conn_id)))
    # A live tape is never silent for minutes: trickle 0.1-lot prints at the touch until the scripted trades start.
    # (They stay strictly on the market side of every scripted pool, so they never touch one.)
    first_trade = min((t[0] for t in truth.trades), default=horizon)
    t = first_ts + tape_keepalive_ms
    k = 0
    while tape_keepalive_ms > 0 and t < min(first_trade - 500, horizon):
        live = [(ts, bids, asks) for ts, bids, asks in truth.states if ts <= t] or [truth.states[0]]
        _ts, bids, asks = live[-1]
        sell = k % 2 == 0
        price = max(bids) if sell else min(asks)
        events.append((t + trade_lag_ms, 3, RawTrade(t_ms=t + trade_lag_ms, exchange_ts_ms=t, action="update",
                                                     trade_id=f"k{k}", price=fmt(price), size="0.1",
                                                     side="sell" if sell else "buy", conn_id=conn_id)))
        t += tape_keepalive_ms
        k += 1
    for j, (ts, price, size, side) in enumerate(truth.trades):
        recv = ts + trade_lag_ms
        events.append((recv, 3, RawTrade(t_ms=recv, exchange_ts_ms=ts, action="update", trade_id=f"t{j}",
                                         price=fmt(price), size=fmt(size), side=side, conn_id=conn_id)))
    for bar in bars:
        events.append((bar.t_ms, 4, bar))
    for e in extra:
        events.append((e.t_ms, 5, e))
    events.sort(key=lambda x: (x[0], x[1]))
    out = [e for _, _, e in events]
    for prev, cur in zip(out, out[1:]):
        assert cur.t_ms >= prev.t_ms
    return out


# ------------------------------------------------------------------------------------------ mirroring
def _reflect(price_str: str) -> str:
    return fmt(px(300.0 - float(price_str)))


def mirror_raw_event(ev: Any) -> Any:
    """Exact price mirror about 150.00: bids<->asks, price reflected, taker side flipped, bars mirrored."""
    if isinstance(ev, RawBook):
        return replace(ev, bids=tuple((_reflect(p), s) for p, s in ev.asks),
                       asks=tuple((_reflect(p), s) for p, s in ev.bids))
    if isinstance(ev, RawTrade):
        return replace(ev, price=_reflect(ev.price), side="buy" if ev.side == "sell" else "sell")
    if isinstance(ev, (BarClosed, DepthSnapshot)):
        return mirror_event(ev)
    return ev


def mirror_raw(events: Iterable[Any]) -> list[Any]:
    return [mirror_raw_event(e) for e in events]


# ------------------------------------------------------------------------------------------ mutable truth
class GroundBook:
    """A running true book. Every mutation appends a timestamped state (and trades append to the tape), so a
    timeline reads like the events it describes: sell 40 at 149.60 at S+33.5 s, cancel these levels, ..."""

    def __init__(self, truth: Truth) -> None:
        _ts, bids, asks = truth.states[-1]
        self.truth, self.bids, self.asks = truth, dict(bids), dict(asks)

    def _mark(self, ts: int) -> None:
        self.truth.state(ts, dict(self.bids), dict(self.asks))

    def sell(self, ts: int, price: float, size: float) -> "GroundBook":
        """A taker SELL executes against the resting bid at `price`."""
        price = px(price)
        have = self.bids[price]
        assert size <= have + 1e-9, (price, size, have)
        if have - size <= 1e-9:
            del self.bids[price]
        else:
            self.bids[price] = round(have - size, 6)
        self.truth.trade(ts, price, size, "sell")
        self._mark(ts)
        return self

    def buy(self, ts: int, price: float, size: float) -> "GroundBook":
        price = px(price)
        have = self.asks[price]
        assert size <= have + 1e-9, (price, size, have)
        if have - size <= 1e-9:
            del self.asks[price]
        else:
            self.asks[price] = round(have - size, 6)
        self.truth.trade(ts, price, size, "buy")
        self._mark(ts)
        return self

    def set(self, ts: int, side: str, levels: dict[float, float]) -> "GroundBook":
        """Place / resize / (size 0) remove resting orders."""
        book = self.bids if side == "bid" else self.asks
        for price, size in levels.items():
            if size <= 0:
                book.pop(px(price), None)
            else:
                book[px(price)] = size
        self._mark(ts)
        return self

    def snap(self, ts: int) -> "GroundBook":
        self._mark(ts)
        return self


# ------------------------------------------------------------------------------------------ stream surgery
def drop(events: list, pred) -> list:
    """Frames the network lost."""
    return [e for e in events if not pred(e)]


def retime(events: list, pred, *, recv_ms: int | None = None, delay_ms: int = 0) -> list:
    """Change WHEN matching frames arrive (their exchange time is untouched) and re-sort into arrival order."""
    out = []
    for e in events:
        if pred(e):
            e = replace(e, t_ms=recv_ms if recv_ms is not None else e.t_ms + delay_ms)
        out.append(e)
    return sorted(out, key=lambda e: e.t_ms)      # stable: equal receipt times keep their original order


def duplicate(events: list, pred) -> list:
    """The same frame delivered twice, back to back."""
    out = []
    for e in events:
        out.append(e)
        if pred(e):
            out.append(e)
    return out


def with_ticks(events: list, *, start_ms: int, end_ms: int, every_ms: int = 1_000) -> list:
    """The runtime's once-a-second TimerTick (staleness is judged on recorded ticks)."""
    from lsrl.core.types import TimerTick
    ticks = [TimerTick(t) for t in range(start_ms, end_ms, every_ms)]
    return sorted(list(events) + ticks, key=lambda e: e.t_ms)
