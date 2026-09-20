"""Deterministic scenario factory for LSRL_V1 tests (no randomness, no clock).

Prices live on a 0.05 book grid (merged-depth style) with a 0.01 exchange tick.
LONG scenarios are generated directly; SHORT scenarios are produced by an EXACT
price mirror about 150.00 (bids<->asks, high<->low, aggressor flipped), so
symmetry is a checked property, not an assumption.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from lsrl.core.config import DEFAULT_CONFIG, LSRLConfig
from lsrl.core.engine import LSRLEngine
from lsrl.core.gates import NullRiskGate
from lsrl.core.types import (BarClosed, DepthSnapshot, FillEvent, NavEvent, OrderEvent, SymbolRules,
                             TradeTick)

RULES = SymbolRules("SOLUSDT", tick_size=0.01, qty_step=0.1, min_qty=0.1, min_notional_usdt=5.0)
CENTER = 150.00
GRID = 0.05
S = 1_800_000_000_000 + 40 * 60_000          # start of the sweep bar (multiple of 60_000)
MIN = 60_000


def px(x: float) -> float:
    return round(x, 2)


def make_book(t_ms: int, best_bid: float, best_ask: float, *, bid_pools: Iterable = (),
              ask_pools: Iterable = (), base: float = 10.0, levels: int = 50) -> DepthSnapshot:
    """(edge_price, n_levels, size): a bid pool extends DOWN from its edge, an ask pool UP.

    Levels are CONTIGUOUS on the 0.05 grid (like a real merged-depth listing) and extend at
    least `levels` deep, further if a pool sits beyond that window."""
    bid_pools, ask_pools = tuple(bid_pools), tuple(ask_pools)
    n_bid, n_ask = levels, levels
    for edge, n, _size in bid_pools:
        n_bid = max(n_bid, round((best_bid - (edge - (n - 1) * GRID)) / GRID) + 2)
    for edge, n, _size in ask_pools:
        n_ask = max(n_ask, round(((edge + (n - 1) * GRID) - best_ask) / GRID) + 2)
    bids = {px(best_bid - i * GRID): base for i in range(n_bid)}
    asks = {px(best_ask + i * GRID): base for i in range(n_ask)}
    for edge, n, size in bid_pools:
        for i in range(n):
            bids[px(edge - i * GRID)] = size
    for edge, n, size in ask_pools:
        for i in range(n):
            asks[px(edge + i * GRID)] = size
    return DepthSnapshot(
        t_ms=t_ms, exchange_ts_ms=t_ms,
        bids=tuple(sorted(bids.items(), key=lambda kv: -kv[0])),
        asks=tuple(sorted(asks.items(), key=lambda kv: kv[0])),
    )


def bar(open_ts: int, o: float, h: float, l: float, c: float, tf_s: int = 60, recv_delay_ms: int = 500) -> BarClosed:
    close_ts = open_ts + tf_s * 1000
    return BarClosed(t_ms=close_ts + recv_delay_ms, tf_s=tf_s, open_ts_ms=open_ts, close_ts_ms=close_ts,
                     open=o, high=h, low=l, close=c, volume=100.0)


ASK_POOLS = ((150.30, 5, 40.0), (150.70, 5, 40.0), (151.10, 5, 40.0))
BID_POOL = (149.60, 6, 40.0)


def warmup(*, atr15_range: float = 1.0, atr1m_range: float = 0.4, nav: float | None = 1000.0,
           flat: float = CENTER) -> list:
    """20 x 15m and 20 x 1m completed bars before S (ATR ready), plus a NAV observation."""
    events: list = []
    for i in range(20):
        open_ts = S - (20 - i) * 15 * MIN - 15 * MIN
        events.append(bar(open_ts, flat, flat + atr15_range / 2, flat - atr15_range / 2, flat, tf_s=900))
    for i in range(20):
        open_ts = S - (20 - i) * MIN
        events.append(bar(open_ts, flat, flat + atr1m_range / 2, flat - atr1m_range / 2, flat))
    if nav is not None:
        events.append(NavEvent(t_ms=S - 100_000, nav_usdt=nav, source="test"))
    return events


DEFAULT_PATH = ((10, 150.00), (20, 149.85), (30, 149.70), (40, 149.30), (50, None), (60, None))


def long_sweep(*, pool: tuple = BID_POOL, pool_first_seen_s: int = -240, pool_cancel_at_s: int | None = None,
               ask_pools: tuple = ASK_POOLS, ask_pools_first_seen_s: int = -240, path: tuple | None = None,
               sweep_low: float = 149.20, sweep_close: float = 149.70, consume: bool = True,
               pool_after_touch: tuple | None = None, spread: float = 0.10, extra_bars: tuple = (),
               tail_snapshots: int = 12, nav: float | None = 1000.0, atr15_range: float = 1.0,
               atr1m_range: float = 0.4, drop_snapshots_after_s: int | None = None,
               sweep_high: float = 150.05, extra_bid_pools: tuple = (), warm: bool = True) -> list:
    """A complete LONG attempt.

    The pool is visible from `pool_first_seen_s`; the sweep bar starts at S. `path` is a
    tuple of (seconds_after_S, mid) for the snapshots inside the sweep bar (a None mid is
    replaced by `sweep_low + 0.05` at t=50 and `sweep_close` at t=60). While the market
    touch is at/below the pool edge the pool is REMOVED from the book (consumed) unless
    `consume=False`; `pool_after_touch` instead substitutes a different (partial) pool.
    """
    events = warmup(atr15_range=atr15_range, atr1m_range=atr1m_range, nav=nav) if warm else []
    half = spread / 2
    edge = pool[0]
    extras = tuple(extra_bid_pools)

    def touch(mid: float) -> tuple[float, float]:
        bid = px(round((mid - half) / GRID) * GRID)
        return bid, px(bid + max(GRID, round(spread / GRID) * GRID))

    state = {"touched": False, "extras": set()}

    def book(t_s: int, mid: float, *, force_no_pool: bool = False) -> DepthSnapshot:
        bid, ask = touch(mid)
        if bid <= edge:
            state["touched"] = True
        bp: list = []
        visible = t_s >= pool_first_seen_s and (pool_cancel_at_s is None or t_s < pool_cancel_at_s)
        if visible and not force_no_pool:
            if not state["touched"]:
                bp.append(pool)
            elif pool_after_touch is not None:
                bp.append(pool_after_touch)
            elif not consume:
                bp.append(pool)
        if not force_no_pool:
            for idx, extra in enumerate(extras):
                if bid <= extra[0]:
                    state["extras"].add(idx)
                if idx not in state["extras"]:
                    bp.append(extra)
        ap = ask_pools if t_s >= ask_pools_first_seen_s else ()
        return make_book(S + t_s * 1000, bid, ask, bid_pools=tuple(bp), ask_pools=ap)

    t = min(pool_first_seen_s, ask_pools_first_seen_s)
    while t < 0:
        events.append(book(t, CENTER))
        t += 10
    for t_s, mid in (path or DEFAULT_PATH):
        if mid is None:
            mid = sweep_low + 0.05 if t_s == 50 else sweep_close
        events.append(book(t_s, mid))
    events.append(bar(S, CENTER, sweep_high, sweep_low, sweep_close))
    cursor = 1
    for (o, h, l, c) in extra_bars:
        if nav is not None:      # the runtime refreshes NAV continuously; keep it fresh here too
            events.append(NavEvent(t_ms=S + cursor * MIN + 5_000, nav_usdt=nav, source="test"))
        for k in (1, 2, 3, 4, 5):
            events.append(book(cursor * 60 + k * 10, c, force_no_pool=True))
        events.append(bar(S + cursor * MIN, o, h, l, c))
        cursor += 1
    for k in range(tail_snapshots):
        events.append(book(cursor * 60 + (k + 1) * 5, extra_bars[-1][3] if extra_bars else sweep_close,
                           force_no_pool=True))
    events.sort(key=lambda e: e.t_ms)
    if drop_snapshots_after_s is not None:
        limit = S + drop_snapshots_after_s * 1000
        events = [e for e in events if not (isinstance(e, DepthSnapshot) and e.t_ms > limit)]
    return events


# ----------------------------------------------------------------- chaining helpers
def shift(events: Iterable, dt_ms: int) -> list:
    """Move a scenario later in time (all timestamps), preserving structure."""
    out = []
    for e in events:
        if isinstance(e, DepthSnapshot):
            out.append(replace(e, t_ms=e.t_ms + dt_ms, exchange_ts_ms=(e.exchange_ts_ms or 0) + dt_ms))
        elif isinstance(e, BarClosed):
            out.append(replace(e, t_ms=e.t_ms + dt_ms, open_ts_ms=e.open_ts_ms + dt_ms,
                               close_ts_ms=e.close_ts_ms + dt_ms))
        elif isinstance(e, (NavEvent, TradeTick)):
            out.append(replace(e, t_ms=e.t_ms + dt_ms))
        else:
            out.append(e)
    return out


def flat_bar_events(from_open_ts: int, until_open_ts: int, price: float = CENTER) -> list:
    """Contiguous flat 1m bars covering [from_open_ts, until_open_ts) (no gap anomaly)."""
    out, t = [], from_open_ts
    while t < until_open_ts:
        out.append(bar(t, price, price + 0.05, price - 0.05, price))
        t += MIN
    return out


def flat_bars(h: "Harness", until_open_ts: int, price: float = CENTER) -> list:
    """Bridge bars from the engine's last 1m bar up to (excluding) `until_open_ts`, as EVENTS to be
    time-merged with the next scenario (feeding them first would push the engine clock ahead of it)."""
    last = h.engine.last_bar1m_open_ts
    start = (last + MIN) if last is not None else until_open_ts
    return flat_bar_events(start, until_open_ts, price)


def quiet_book(t_ms: int, mid: float = CENTER, spread: float = 0.10) -> DepthSnapshot:
    bid = px(round((mid - spread / 2) / GRID) * GRID)
    return make_book(t_ms, bid, px(bid + max(GRID, round(spread / GRID) * GRID)))


def feed_quiet(h: "Harness", t_from_ms: int, t_to_ms: int, mid: float = CENTER, step_ms: int = 10_000) -> None:
    t = t_from_ms
    while t <= t_to_ms:
        h.feed(quiet_book(t, mid))
        t += step_ms


# ----------------------------------------------------------------- mirroring
def _m(price: float) -> float:
    return px(2 * CENTER - price)


def mirror_event(ev):
    if isinstance(ev, DepthSnapshot):
        return DepthSnapshot(
            t_ms=ev.t_ms, exchange_ts_ms=ev.exchange_ts_ms,
            bids=tuple((_m(p), s) for p, s in ev.asks),
            asks=tuple((_m(p), s) for p, s in ev.bids),
        )
    if isinstance(ev, BarClosed):
        return replace(ev, open=_m(ev.open), high=_m(ev.low), low=_m(ev.high), close=_m(ev.close))
    if isinstance(ev, TradeTick):
        return replace(ev, price=_m(ev.price), aggressor="BUY" if ev.aggressor == "SELL" else "SELL")
    return ev


def mirror(events: Iterable) -> list:
    return [mirror_event(e) for e in events]


# ----------------------------------------------------------------- harness
class Harness:
    def __init__(self, engine: LSRLEngine | None = None, cfg: LSRLConfig = DEFAULT_CONFIG, **kw):
        self.engine = engine or LSRLEngine(cfg, RULES, risk_gate=kw.pop("risk_gate", NullRiskGate()), **kw)
        self.intents: list = []
        self.records: list = []

    def feed(self, ev):
        i, r = self.engine.on_event(ev)
        self.intents.extend(i)
        self.records.extend(r)
        return i, r

    def run(self, events: Iterable):
        for ev in events:
            self.feed(ev)
        return self

    def kinds(self, kind: str) -> list:
        return [r for r in self.records if r.kind == kind]

    def rejected(self) -> list[str]:
        return [r.data.get("reason") for r in self.records if r.kind == "setup_rejected"]

    def intents_of(self, cls) -> list:
        return [i for i in self.intents if isinstance(i, cls)]


def ack_all(h: Harness, t_ms: int) -> None:
    """Adapter-style acknowledgement for every placed entry (used by lifecycle tests)."""
    s = h.engine.setup
    assert s is not None
    for rung in list(s.rungs.values()):
        if rung.status == "PLANNED":
            h.feed(OrderEvent(t_ms=t_ms, setup_id=s.setup_id, tag=rung.tag, status="ACCEPTED"))


def fill_entry(h: Harness, rung: int, t_ms: int, *, price: float | None = None, fee: float = 0.0):
    s = h.engine.setup
    assert s is not None
    r = s.rungs[rung]
    return h.feed(FillEvent(t_ms=t_ms, setup_id=s.setup_id, tag=r.tag, kind="ENTRY",
                            price=r.price if price is None else price, qty=r.qty, fee_usdt=fee, rung=rung))
