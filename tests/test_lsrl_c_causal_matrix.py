"""PERMANENT REGRESSION: Agent C's exact 64-row causal attack matrix (32 cases x LONG/SHORT), driven through the
PRODUCTION path (Bitget-format JSON -> BitgetWsFeed -> EventBus -> LSRLRuntime -> SimDriver/recorder -> BookObserver ->
LSRLEngine), each row recorded and replayed from genesis by a fresh engine.

Vendored from research_lsrl_v1_final_acceptance_2026-09-21/scripts/s2_causal_attack.py (case builders, truth table and
classification unchanged). Ground truth is defined by the case, never by the engine. The requirement:

    0 cancel-before-touch false positives, while a genuine execution stays classifiable when the evidence is unambiguous.

Blocker 1 (2026-09-21): Bitget guarantees no order between the depth and the trade channel (the depth update that reflects a
trade is stamped a median ~50 ms AFTER it, tails -400/+500 ms), and the engine used to accept a cancel that was stamped 1 ms
after the first print (16 of 64 rows). No offset is invented: acceptance now rests on order-free conservation of volume (R6).
"""
from __future__ import annotations

import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from lsrl.core.types import BarClosed, FeedMarker, RawBook, RawTrade
from lsrl.modes import Mode
from tests.lsrl_fixtures import ASK_POOLS, BID_POOL, CENTER, GRID, S, bar, px
from tests.lsrl_prod_rig import ProdRig, book_json, trade_json
from tests.lsrl_raw_feed import GroundBook, Truth, drop, fmt, mirror_raw, retime, to_raw

POOL_LEVELS = [px(BID_POOL[0] - i * GRID) for i in range(BID_POOL[1])]     # 149.60 .. 149.35, 40 each -> 240
T = lambda sec: S + int(round(sec * 1000))
SAFETY_REASONS = ("FEED_UNSAFE", "ORDER_AMBIGUOUS", "STALE", "LIQUIDITY_DATA_STALE", "TAPE_UNAVAILABLE")


def approach(truth, cancel_pool_at=None):
    t = -240.0
    while t <= 30.0 + 1e-9:
        bid = px(round((149.95 - max(t, 0.0) * 0.01) / GRID) * GRID)
        truth.book(T(t), bid, px(bid + 0.10), bid_pools=(BID_POOL,), ask_pools=ASK_POOLS)
        t += 5.0
    return GroundBook(truth)


def bounce(gb, at):
    gb.set(T(at), "bid", {149.70: 20.0, 149.65: 20.0})


def live_bar(low):
    return [bar(S, CENTER, 150.05, low, 149.70)]


# ------------------------------------------------------------------------------- ground truths
def tl_consumed(exec_units=240.0, lag=(100, 80), first_at=33.5):
    """The pool trades: SELL aggressor volume at the pool's own price levels, then reclaim."""
    truth = Truth(); gb = approach(truth)
    gb.sell(T(33.0), 149.65, 10.0)                                     # last non-pool level: the touch
    left = exec_units
    for i, price in enumerate(POOL_LEVELS):
        take = min(40.0, left)
        if take <= 0: break
        gb.sell(T(first_at) + i * 100, price, take); left -= take
    for i, price in enumerate((149.30, 149.25, 149.20)):
        gb.sell(T(first_at + 0.7) + i * 100, price, 10.0)
    bounce(gb, 36.5)
    return truth, dict(book_lag_ms=lag[0], trade_lag_ms=lag[1])


def tl_cancel_before_touch(baseline=10.0, cancel_at=32.0, touch_at=35.0):
    """Pool cancelled 3 s BEFORE price arrives; price later crosses and trades only ordinary (baseline) liquidity."""
    truth = Truth(); gb = approach(truth)
    gb.set(T(cancel_at), "bid", {p: baseline for p in POOL_LEVELS})
    gb.sell(T(touch_at), 149.65, 10.0)
    for i, price in enumerate(POOL_LEVELS):
        gb.sell(T(touch_at + 0.5) + i * 100, price, baseline)
    for i, price in enumerate((149.30, 149.25, 149.20)):
        gb.sell(T(touch_at + 1.2) + i * 100, price, 10.0)
    bounce(gb, 38.0)
    return truth, dict()


def tl_replenished():
    truth = Truth(); gb = approach(truth)
    gb.sell(T(33.0), 149.65, 10.0)
    for i, (price, size) in enumerate(zip(POOL_LEVELS[:5], (40.0, 40.0, 40.0, 40.0, 8.0))):
        gb.sell(T(33.5) + i * 100, price, size)                        # 168 = 70 % executed
    gb.set(T(35.0), "bid", {p: 40.0 for p in POOL_LEVELS})             # replenished
    bounce(gb, 36.0)
    return truth, dict()


def tl_partial_cancel_after_touch(exec_units=48.0):
    truth = Truth(); gb = approach(truth)
    gb.sell(T(33.0), 149.65, 10.0)
    gb.set(T(33.4), "bid", {149.45: 10.0, 149.40: 10.0, 149.35: 10.0})  # 90 pulled AFTER the touch
    gb.sell(T(33.6), 149.60, 40.0); gb.sell(T(33.7), 149.55, exec_units - 40.0)
    bounce(gb, 35.0)
    return truth, dict()


def tl_partial_cancel_before_touch():
    """Half of every level is pulled BEFORE the touch (pool shrinks to 20/level = still a pool), then 24 executes (20 % of the frozen 120)."""
    truth = Truth(); gb = approach(truth)
    gb.set(T(31.5), "bid", {p: 20.0 for p in POOL_LEVELS})
    gb.sell(T(33.0), 149.65, 10.0)
    gb.sell(T(33.5), 149.60, 20.0); gb.sell(T(33.6), 149.55, 4.0)
    for i, price in enumerate((149.30, 149.25, 149.20)):
        gb.sell(T(34.2) + i * 100, price, 10.0)
    bounce(gb, 36.0)
    return truth, dict()


def tl_tape_first(delta_ms=None, cancel_before_touch=False):
    """FIRST evidence of the touch is a TRADE at the pool (the depth update reflecting it arrives later).
    delta_ms is None: a genuine consumption.  Otherwise the pool is really cancelled at touch - 1 ms, but the depth
    update that carries the cancel is STAMPED touch + delta_ms (cross-channel clock skew) and arrives after the trade."""
    truth = Truth(); gb = approach(truth)
    if delta_ms is None:
        for i, price in enumerate(POOL_LEVELS):
            gb.sell(T(33.0) + i * 3, price, 40.0)                     # sweep begins ON the pool: no non-pool touch print
        for i, price in enumerate((149.30, 149.25, 149.20)):
            gb.sell(T(33.7) + i * 100, price, 10.0)
        bounce(gb, 36.0)
        return truth, dict(book_lag_ms=250, trade_lag_ms=20)
    # truth order: cancel (T(35.000) - 1 ms), then first pool print T(35.000)
    if delta_ms == "skew":
        gb.set(T(34.999), "bid", {p: 10.0 for p in POOL_LEVELS})
        for i, price in enumerate(POOL_LEVELS):
            gb.sell(T(35.0) + i * 3, price, 10.0)
        for i, price in enumerate((149.30, 149.25, 149.20)):
            gb.sell(T(35.7) + i * 100, price, 10.0)
        bounce(gb, 38.0)
        return truth, dict(book_lag_ms=250, trade_lag_ms=20)
    gb.set(T(34.999), "bid", {p: 10.0 for p in POOL_LEVELS})
    for i, price in enumerate(POOL_LEVELS):
        gb.sell(T(35.0) + i * 3, price, 10.0)
    for i, price in enumerate((149.30, 149.25, 149.20)):
        gb.sell(T(35.7) + i * 100, price, 10.0)
    bounce(gb, 38.0)
    return truth, dict(book_lag_ms=250, trade_lag_ms=20, restamp_cancel=delta_ms)


# ------------------------------------------------------------------------------- stream surgery helpers
def truth_at_factory(truth, mirrored):
    def f(t):
        best = truth.states[0]
        for st in truth.states:
            if st[0] <= t: best = st
        _ts, bids, asks = best
        if mirrored:
            return {px(300 - p): s for p, s in asks.items()}, {px(300 - p): s for p, s in bids.items()}
        return dict(bids), dict(asks)
    return f


def restamp_cancel(events, delta_ms, first_trade_ts):
    """Give the depth update that carries the pool cancel an exchange stamp of first_pool_trade_ts + delta (skew)."""
    out = []
    for e in events:
        if isinstance(e, RawBook) and e.action == "update" and e.exchange_ts_ms is not None \
                and abs(e.exchange_ts_ms - T(34.999)) <= 1:
            e = replace(e, exchange_ts_ms=first_trade_ts + delta_ms)
        out.append(e)
    return out


def is_book(e): return isinstance(e, RawBook)
def is_trade(e): return isinstance(e, RawTrade)


def build(name, direction):
    base = {"full": lambda: tl_consumed(240.0), "boundary25": lambda: tl_consumed(60.0), "below25": lambda: tl_consumed(59.9),
            "b_cancel": lambda: tl_cancel_before_touch(10.0), "b_cancel_heavy_baseline": lambda: tl_cancel_before_touch(16.0),
            "c_replenish": tl_replenished, "d_cancel_after": tl_partial_cancel_after_touch,
            "d_cancel_before": tl_partial_cancel_before_touch,
            "depth_first": lambda: (tl_consumed(240.0)[0], dict(book_lag_ms=20, trade_lag_ms=150)),
            "trade_first_genuine": lambda: tl_tape_first(None),
            "tie": lambda: tl_tape_first(0), "near1": lambda: tl_tape_first(1), "near3": lambda: tl_tape_first(3),
            "earlier5": lambda: tl_tape_first(-5)}
    for n in (-5, 0, 1, 2, 3, 5, 10, 20, 50):
        base[f"skew{n}"] = (lambda: tl_tape_first("skew"))
    fam = name.split("+")[0]
    truth, kw = base[fam]()
    restamp = kw.pop("restamp_cancel", None)
    skew = int(fam[4:]) if fam.startswith("skew") else None
    low = 149.20 if fam not in ("c_replenish", "d_cancel_after", "d_cancel_before") else (149.40 if fam == "c_replenish" else 149.55)
    events = to_raw(truth, bars=live_bar(low), keepalive_until_ms=T(95), **kw)
    if restamp is not None:
        first_pool_trade = min(t[0] for t in truth.trades if t[1] in POOL_LEVELS)
        events = restamp_cancel(events, restamp, first_pool_trade)
    if skew is not None:                       # the whole depth clock runs `skew` ms AHEAD of the trade clock
        events = [replace(e, exchange_ts_ms=e.exchange_ts_ms + skew) if is_book(e) and e.exchange_ts_ms is not None else e for e in events]
    # ---- perturbations (suffix after '+')
    for mod in name.split("+")[1:]:
        if mod == "dup_trade":
            out = []
            for e in events:
                out.append(e)
                if is_trade(e) and e.action == "update": out.append(replace(e, t_ms=e.t_ms + 5))
            events = sorted(out, key=lambda e: e.t_ms)
        elif mod == "seq_gap":
            victim = next(e for e in events if is_book(e) and e.action == "update" and T(33.6) <= e.exchange_ts_ms <= T(34.5))
            events = [e for e in events if e is not victim]
        elif mod == "out_of_order":
            ups = [i for i, e in enumerate(events) if is_book(e) and e.action == "update" and T(33.6) <= e.exchange_ts_ms <= T(34.5)]
            i, j = ups[0], ups[1]
            a, b = events[i], events[j]
            events[i], events[j] = replace(b, t_ms=a.t_ms), replace(a, t_ms=b.t_ms)
        elif mod == "stale":
            batch = [e for e in events if is_book(e) and e.action == "update" and T(33.6) <= e.exchange_ts_ms < T(41.0)]
            ids = {id(e) for e in batch}
            events = sorted([replace(e, t_ms=T(43.0) + k) if id(e) in ids else e for k, e in enumerate(events)], key=lambda e: e.t_ms)
        elif mod == "crossed":
            ups = [e for e in events if is_book(e) and e.action == "update" and e.exchange_ts_ms >= T(34.0)]
            v = ups[0]
            bad = replace(v, bids=(("150.6", "5"),) + tuple(v.bids))
            events = [bad if e is v else e for e in events]
        elif mod == "disconnect":
            t_d = T(33.8); t_c = T(34.5)
            events = sorted(events + [FeedMarker(t_d, "DISCONNECT", "conn-1"), FeedMarker(t_c, "CONNECT", "conn-2")], key=lambda e: e.t_ms)
            events = [e for e in events if not ((is_book(e) or is_trade(e)) and t_d <= e.t_ms < t_c)]
        elif mod == "replay":
            first = [e for e in events if is_trade(e) and e.action == "update" and e.exchange_ts_ms >= T(33.4)][:3]
            events = sorted(events + [replace(e, t_ms=e.t_ms + 400) for e in first], key=lambda e: e.t_ms)
        elif mod == "replay_after_reconnect":
            first = [e for e in events if is_trade(e) and e.action == "update" and e.exchange_ts_ms >= T(33.4)]
            events = sorted(events + [FeedMarker(T(33.8), "DISCONNECT", "conn-1"), FeedMarker(T(34.5), "CONNECT", "conn-2")], key=lambda e: e.t_ms)
            events = [e for e in events if not ((is_book(e) or is_trade(e)) and T(33.8) <= e.t_ms < T(34.5))]
            events = sorted(events + [replace(e, t_ms=T(34.9) + k, conn_id="conn-2") for k, e in enumerate(first)], key=lambda e: e.t_ms)
    mirrored = direction == "SHORT"
    if mirrored:
        events = mirror_raw(events)
    return events, truth_at_factory(truth, mirrored)


# name, truth (VALID/INVALID), note
CASES = [
    ("full", "VALID", "A: pool genuinely executed 100 %, reclaim"),
    ("boundary25", "VALID", "exactly 25.0 % executed (>= threshold)"),
    ("below25", "INVALID", "24.96 % executed (< threshold)"),
    ("b_cancel", "INVALID", "B: cancelled 3 s before touch; baseline volume = exactly 25 % of frozen depth"),
    ("b_cancel_heavy_baseline", "INVALID", "B': cancelled before touch; ordinary volume = 40 % of frozen depth"),
    ("c_replenish", "VALID", "C: 70 % executed then replenished"),
    ("d_cancel_after", "INVALID", "D: 90 pulled AFTER touch + 48 executed (20 %)"),
    ("d_cancel_before", "INVALID", "D': half pulled BEFORE touch, executed 20 % of the frozen pool"),
    ("depth_first", "VALID", "depth update arrives BEFORE the trade prints"),
    ("trade_first_genuine", "VALID", "trade prints arrive BEFORE the depth update that reflects them (first evidence = tape)"),
    ("tie", "INVALID", "cancel stamped the SAME ms as the first pool print"),
    ("near1", "INVALID", "cancel truly before touch but stamped +1 ms after it (skew)"),
    ("near3", "INVALID", "... stamped +3 ms after"),
    ("skew-5", "INVALID", "cancel-before-touch, depth clock 5 ms BEHIND the trade clock (uniform)"),
    ("skew0", "INVALID", "cancel-before-touch, clocks aligned: cancel stamped 1 ms before the first print"),
    ("skew1", "INVALID", "cancel-before-touch, depth clock 1 ms AHEAD (uniform)"),
    ("skew2", "INVALID", "... 2 ms ahead"),
    ("skew3", "INVALID", "... 3 ms ahead"),
    ("skew5", "INVALID", "... 5 ms ahead"),
    ("skew10", "INVALID", "... 10 ms ahead"),
    ("skew20", "INVALID", "... 20 ms ahead"),
    ("skew50", "INVALID", "... 50 ms ahead"),
    ("earlier5", "INVALID", "cancel stamped 5 ms BEFORE the first print"),
    ("full+dup_trade", "VALID", "every trade frame delivered twice (same tradeId)"),
    ("b_cancel+dup_trade", "INVALID", "B with duplicated trades"),
    ("full+replay", "VALID", "first 3 pool trades replayed 400 ms later as updates (no reconnect)"),
    ("full+seq_gap", "AMBIGUOUS", "one depth update lost during the sweep"),
    ("full+out_of_order", "AMBIGUOUS", "two depth updates delivered in swapped order"),
    ("full+stale", "AMBIGUOUS", "depth stream silent for 6+ s during the sweep"),
    ("full+crossed", "AMBIGUOUS", "an update produces a crossed reconstructed book"),
    ("full+disconnect", "AMBIGUOUS", "connection dropped mid-sweep, resnapshot after reconnect"),
    ("full+replay_after_reconnect", "AMBIGUOUS", "reconnect mid-sweep, then all trades replayed as updates"),
]


def classify(truth, rig):
    ok_sweep = rig.ladders() > 0
    reasons = rig.reasons()
    safety = any(any(k in (r or "") for k in SAFETY_REASONS) for r in reasons)
    if truth == "VALID":
        return "OK" if ok_sweep else ("FAIL_CLOSED" if safety else "FALSE_NEGATIVE")
    if truth == "INVALID":
        return "VIOLATION" if ok_sweep else "OK"
    return "VIOLATION" if ok_sweep else "OK"          # AMBIGUOUS truth: accepting a ladder is a fail-open




# =============================================================================================== the permanent regression
# The two rows where the engine legitimately refuses a genuine signal (documented, safe): the first evidence of the touch is a
# TRADE and the depth update that reflects it arrives later (R3) -> ORDER_AMBIGUOUS, no signal.
FAIL_CLOSED_OK = {"trade_first_genuine"}
# The 16 rows Agent C found accepted (cancel truly before the touch, stamped +1..+50 ms after the first print). They must be
# refused BY THE CONSERVATION RULE (not by luck of a timestamp comparison).
FORMERLY_ACCEPTED = {"near1", "near3", "skew2", "skew3", "skew5", "skew10", "skew20", "skew50"}
ROWS = [(name, truth, direction) for name, truth, _note in CASES for direction in ("LONG", "SHORT")]


def run_row(name, truth, direction):
    events, truth_at = build(name, direction)
    tmp = Path(tempfile.mkdtemp(prefix="c_causal_"))
    try:
        rig = ProdRig(tmp, truth_at=truth_at, start_ms=events[0].t_ms - 1000).run(events, tail_ms=2500)
        return dict(verdict=classify(truth, rig), ladders=rig.ladders(), reasons=[r for r in rig.reasons() if r],
                    verify=rig.verify(), consumption=rig.consumption())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.mark.parametrize("name,truth,direction", ROWS, ids=[f"{n}-{d}" for n, _t, d in ROWS])
def test_agent_c_row(name, truth, direction):
    row = run_row(name, truth, direction)
    assert row["verify"]["inputs_chain"] == "OK" and row["verify"]["decisions_chain"] == "OK"
    assert row["verify"]["genesis_replay"] == "EQUIVALENT"          # the recorded run replays decision for decision
    if truth == "VALID" and name not in FAIL_CLOSED_OK:
        assert row["verdict"] == "OK" and row["ladders"] > 0        # genuine executions stay classifiable
    elif truth == "VALID":
        assert row["verdict"] in ("OK", "FAIL_CLOSED")
    else:
        assert row["ladders"] == 0 and row["verdict"] == "OK"       # cancel-before-touch / ambiguous: NO SIGNAL
    if name in FORMERLY_ACCEPTED:
        assert "POOL_REMOVAL_NOT_EXPLAINED_BY_EXECUTION" in row["reasons"]
        cons = row["consumption"]
        assert cons["fraction"] >= 0.25 and cons["unexplained_removal"] > 0     # the tape alone WOULD have confirmed it
