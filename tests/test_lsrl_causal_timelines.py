"""LSRL_V1: the causal timelines that decide consumed-vs-cancelled, as permanent deterministic tests.

Each timeline defines GROUND TRUTH (what the exchange really did, at ms resolution) and is then OBSERVED as
raw Bitget frames (`tests/lsrl_raw_feed.py`) through the production path: RawBook/RawTrade -> BookObserver
-> engine (via SimDriver/FeedPipeline), and again through the recorder + a fresh-engine replay.

The four owner-mandated timelines (Agent C's P/Q/R/S reduced to their essence):
  A  pool exists, is genuinely traded (>= 25 % executed), price reclaims                        => VALID
  B  pool exists, is CANCELLED before the price touches it, price later crosses                 => INVALID
  C  pool is genuinely traded (70 %), then REPLENISHED before the next coarse observation       => VALID,
     and the consumption is retained
  D  pool is partly cancelled and partly traded, executed < 25 %                                => INVALID
Every one is also run as its exact SHORT mirror (bids <-> asks, taker side flipped).
"""
from __future__ import annotations

import pytest

from lsrl.adapters.sim import SimExchange
from lsrl.core.config import DEFAULT_CONFIG as CFG
from lsrl.core.engine import LSRLEngine
from lsrl.core.gates import NullRiskGate
from lsrl.core.types import BarClosed, Direction, FeedMarker, PlaceEntry, RawBook, RawTrade, TimerTick
from lsrl.driver import SimDriver
from lsrl.io.gate_log import RecordingRiskGate
from lsrl.io.recorder import LSRLRecorder, decision_files, input_files
from lsrl.io.replay import decisions_match, replay
from lsrl.modes import Mode
from lsrl.obs.observer import NO_BOOK, OK, UNSAFE
from tests.lsrl_fixtures import ASK_POOLS, BID_POOL, GRID, RULES, S, bar, px, warmup
from tests.lsrl_raw_feed import (GroundBook, Truth, drop, duplicate, fmt, mirror_raw, retime, to_raw,
                                 with_ticks)

POOL_LEVELS = [px(BID_POOL[0] - i * GRID) for i in range(BID_POOL[1])]      # 149.60 .. 149.35, 40 each
FROZEN_DEPTH = BID_POOL[1] * BID_POOL[2]                                    # 240


def T(seconds: float) -> int:
    return S + int(round(seconds * 1000))


def pre_touch(truth: Truth, *, pool=BID_POOL, cancel_pool_at: float | None = None) -> GroundBook:
    """t = -240 s .. 30 s: the pool rests while the touch glides down to 149.65 (still above the pool)."""
    t = -240.0
    while t <= 30.0 + 1e-9:
        bid = px(round((149.95 - max(t, 0.0) * 0.01) / GRID) * GRID)
        pools = (pool,) if pool is not None else ()
        truth.book(T(t), bid, px(bid + 0.10), bid_pools=pools, ask_pools=ASK_POOLS)
        t += 5.0
    return GroundBook(truth)


def bars(*, low: float, close: float = 149.70, high: float = 150.05) -> list[BarClosed]:
    return warmup(nav=None) and [b for b in warmup(nav=None) if isinstance(b, BarClosed)] + [
        bar(S, 150.00, high, low, close)]


def bounce(gb: GroundBook, at: float) -> None:
    """Buyers step in after the sweep: the market recovers above the original pool edge."""
    gb.set(T(at), "bid", {149.70: 20.0, 149.65: 20.0})


def timeline_a() -> tuple[list, dict]:
    truth = Truth()
    gb = pre_touch(truth)
    gb.sell(T(33.0), 149.65, 10.0)                                           # last level above the pool: touch
    for i, price in enumerate(POOL_LEVELS):
        gb.sell(T(33.5) + i * 100, price, 40.0)                              # the whole pool trades: 240
    for i, price in enumerate((149.30, 149.25, 149.20)):
        gb.sell(T(34.2) + i * 100, price, 10.0)
    bounce(gb, 36.0)
    return to_raw(truth, bars=bars(low=149.20)), {"fraction": 1.0}


def timeline_b() -> tuple[list, dict]:
    truth = Truth()
    gb = pre_touch(truth)
    gb.set(T(32.0), "bid", {p: 10.0 for p in POOL_LEVELS})                   # pool CANCELLED (baseline remains)
    gb.sell(T(35.0), 149.65, 10.0)                                           # price arrives 3 s later ...
    for i, price in enumerate(POOL_LEVELS):
        gb.sell(T(35.5) + i * 100, price, 10.0)                              # ... and trades only baseline: 60
    for i, price in enumerate((149.30, 149.25, 149.20)):
        gb.sell(T(36.2) + i * 100, price, 10.0)
    bounce(gb, 38.0)
    return to_raw(truth, bars=bars(low=149.20)), {}


def timeline_c() -> tuple[list, dict]:
    truth = Truth()
    gb = pre_touch(truth)
    gb.sell(T(33.0), 149.65, 10.0)
    for i, (price, size) in enumerate(zip(POOL_LEVELS[:5], (40.0, 40.0, 40.0, 40.0, 8.0))):
        gb.sell(T(33.5) + i * 100, price, size)                              # 168 = 70 % of the pool executes
    gb.set(T(35.0), "bid", {p: 40.0 for p in POOL_LEVELS})                   # ... then the pool is REPLENISHED
    bounce(gb, 36.0)
    return to_raw(truth, bars=bars(low=149.40)), {"fraction": 0.7}


def timeline_d() -> tuple[list, dict]:
    truth = Truth()
    gb = pre_touch(truth)
    gb.sell(T(33.0), 149.65, 10.0)                                           # touch; frozen pool is intact (240)
    gb.set(T(33.4), "bid", {149.45: 10.0, 149.40: 10.0, 149.35: 10.0})       # 90 pulled AFTER the touch
    gb.sell(T(33.6), 149.60, 40.0)
    gb.sell(T(33.7), 149.55, 8.0)                                            # 48 = 20 % executed
    bounce(gb, 35.0)
    return to_raw(truth, bars=bars(low=149.55)), {"fraction": 0.2}


TIMELINES = {"A_true_consumption": timeline_a, "B_cancel_before_touch": timeline_b,
             "C_consume_then_replenish": timeline_c, "D_partial_cancel_partial_trade": timeline_d}
VALID = {"A_true_consumption", "C_consume_then_replenish"}


def run(events: list, *, mode: Mode = Mode.PAPER, recorder: LSRLRecorder | None = None) -> SimDriver:
    holder: dict = {}
    gate = NullRiskGate()
    if recorder is not None:                       # gate verdicts are recorded inputs, so replay needs no live gate
        gate = RecordingRiskGate(gate, lambda *a: holder["d"].gate_sink(*a))
    engine = LSRLEngine(CFG, RULES, risk_gate=gate)
    holder["d"] = SimDriver(mode, engine, SimExchange(RULES, CFG), start_nav_usdt=1_000.0, recorder=recorder)
    holder["d"].run(events)
    return holder["d"]


def reasons(d: SimDriver) -> list[str]:
    return [r.data.get("reason") for r in d.records if r.kind == "setup_rejected"]


def is_short(events_direction: str) -> bool:
    return events_direction == "SHORT"


DIRECTIONS = ["LONG", "SHORT"]


def observed(name: str, direction: str) -> tuple[list, dict]:
    events, meta = TIMELINES[name]()
    return (mirror_raw(events) if direction == "SHORT" else events), meta


# ------------------------------------------------------------------------------------------ the four timelines
@pytest.mark.parametrize("direction", DIRECTIONS)
def test_a_a_pool_that_genuinely_traded_and_is_reclaimed_is_a_valid_sweep(direction):
    events, meta = observed("A_true_consumption", direction)
    d = run(events)
    s = d.engine.setup
    assert s is not None and s.state == "LADDER_PENDING", reasons(d)
    assert s.direction is (Direction.LONG if direction == "LONG" else Direction.SHORT)
    c = s.consumption
    assert c["method"] == "TAPE" and c["fraction"] == pytest.approx(meta["fraction"]) and c["consumed"] == 240.0
    assert len([i for i in d.intents if isinstance(i, PlaceEntry)]) == 5
    assert s.feed_evidence["ordering_ambiguous"] is None and s.feed_evidence["pre_touch_state_age_ms"] >= 0


@pytest.mark.parametrize("direction", DIRECTIONS)
def test_b_a_pool_cancelled_before_the_price_touches_it_is_never_a_sweep(direction):
    """The exact case the 10 s REST source could not tell from consumption: even though the tape later shows
    exactly 25 % of the frozen depth trading at the pool's prices (baseline liquidity), the pool had already
    left the book while the market was still 3 s away."""
    events, _ = observed("B_cancel_before_touch", direction)
    d = run(events)
    assert d.engine.setup is None and not [i for i in d.intents if isinstance(i, PlaceEntry)]
    assert "POOL_DISAPPEARED_BEFORE_INTERACTION" in reasons(d)
    assert not [r for r in d.records if r.data.get("event") == "SWEEP_CONFIRMED"]


@pytest.mark.parametrize("direction", DIRECTIONS)
def test_c_a_pool_that_traded_and_was_replenished_still_counts_and_the_consumption_is_retained(direction):
    events, meta = observed("C_consume_then_replenish", direction)
    d = run(events)
    s = d.engine.setup
    assert s is not None and s.state == "LADDER_PENDING", reasons(d)
    c = s.consumption
    assert c["fraction"] == pytest.approx(meta["fraction"]) and c["consumed"] == pytest.approx(168.0)
    # the book afterwards looks untouched (fully replenished), but the executed volume is what qualified it
    assert c["residual_depth"] == pytest.approx(FROZEN_DEPTH) and c["replenished_est"] > 0.0


@pytest.mark.parametrize("direction", DIRECTIONS)
def test_d_partly_cancelled_partly_traded_below_a_quarter_is_not_a_sweep(direction):
    events, meta = observed("D_partial_cancel_partial_trade", direction)
    d = run(events)
    assert d.engine.setup is None and "TOUCH_WITHOUT_SWEEP" in reasons(d)
    rej = next(r.data for r in d.records if r.data.get("reason") == "TOUCH_WITHOUT_SWEEP")
    c = rej["consumption"]
    assert c["fraction"] == pytest.approx(meta["fraction"]) and c["consumed"] == pytest.approx(48.0)
    assert c["cancelled_est"] > 0.0          # the book also shows depth that left WITHOUT executing: never consumption


@pytest.mark.parametrize("name", sorted(TIMELINES))
def test_every_timeline_is_the_exact_mirror_of_its_short_twin(name):
    long_d = run(observed(name, "LONG")[0])
    short_d = run(observed(name, "SHORT")[0])
    assert (long_d.engine.setup is None) == (short_d.engine.setup is None)
    assert reasons(long_d) == reasons(short_d)
    if long_d.engine.setup is not None:
        assert long_d.engine.setup.consumption["fraction"] == pytest.approx(short_d.engine.setup.consumption["fraction"])
        assert [round(300 - p, 3) for p in long_d.engine.setup.geometry["rungs"]] == pytest.approx(
            short_d.engine.setup.geometry["rungs"])


# ------------------------------------------------------------------------------------------ recorder + replay
@pytest.mark.parametrize("name", sorted(TIMELINES))
@pytest.mark.parametrize("direction", DIRECTIONS)
def test_each_timeline_is_recorded_raw_and_a_fresh_engine_reproduces_every_decision(tmp_path, name, direction):
    events, _ = observed(name, direction)
    rec = LSRLRecorder(tmp_path / "rec", CFG, RULES, mode="PAPER", min_free_gb=0.001)
    d = run(events, recorder=rec)
    rec.close()
    files = input_files(tmp_path / "rec")
    replayed = replay(files, anchor=tmp_path / "rec" / "anchor-inputs.jsonl")
    assert [r.to_dict() for r in replayed.records] == [r.to_dict() for r in d.records]
    assert replayed.engine.snapshot() == d.engine.snapshot()
    assert decisions_match(files, decision_files(tmp_path / "rec"))[0]


# ================================================================================================
# Adversarial timelines beyond the four mandated ones. Each is run LONG and as its exact SHORT mirror.
# ================================================================================================
def both(fn):
    return pytest.mark.parametrize("direction", DIRECTIONS)(fn)


def view(events: list, direction: str) -> list:
    return mirror_raw(events) if direction == "SHORT" else events


def evidence(d: SimDriver) -> list[dict]:
    return [r.data["feed_evidence"] for r in d.records if r.kind == "setup_rejected" and r.data.get("feed_evidence")]


def cancelled_pool_truth(*, cancel_ts: float, book_lag_ms: int, trade_lag_ms: int) -> list:
    """Ground truth B, tightened to milliseconds: the pool is cancelled at `cancel_ts`; the first print in the
    pool zone follows 20 ms later and only baseline liquidity trades."""
    truth = Truth()
    gb = pre_touch(truth)
    gb.set(T(cancel_ts), "bid", {p: 10.0 for p in POOL_LEVELS})
    gb.sell(T(cancel_ts) + 10, 149.65, 10.0)                          # the level above the pool
    for i, price in enumerate(POOL_LEVELS):
        gb.sell(T(cancel_ts) + 20 + i * 20, price, 10.0)              # 60 = exactly 25 % of the frozen 240
    bounce(gb, cancel_ts + 3.0)
    return to_raw(truth, bars=bars(low=149.30), book_lag_ms=book_lag_ms, trade_lag_ms=trade_lag_ms)


# ---------------------------------------------------------------- R3: a cancel that may PRECEDE the first print
@both
def test_e_a_cancel_that_arrives_after_the_first_print_but_is_timestamped_before_it_is_ambiguous_no_signal(direction):
    """Trades overtook the book channel by 50 ms: the bot saw the pool 'intact' when price first traded it,
    and only afterwards received a cancellation that the exchange stamped BEFORE that trade. Seen this way
    the sweep looks valid (25 % traded); ordering is unknowable, so: NO SIGNAL, and nothing is inferred."""
    d = run(view(cancelled_pool_truth(cancel_ts=34.990, book_lag_ms=100, trade_lag_ms=40), direction))
    assert d.engine.setup is None and not [i for i in d.intents if isinstance(i, PlaceEntry)]
    assert "ORDER_AMBIGUOUS" in reasons(d)
    assert evidence(d)[0]["ordering_ambiguous"] == "R3_DEPTH_UPDATE_NOT_AFTER_FIRST_TRADE"


@both
def test_e_control_the_same_ground_truth_with_normal_latencies_is_simply_a_cancel_before_touch(direction):
    d = run(view(cancelled_pool_truth(cancel_ts=34.990, book_lag_ms=30, trade_lag_ms=80), direction))
    assert d.engine.setup is None and "POOL_DISAPPEARED_BEFORE_INTERACTION" in reasons(d)
    assert "ORDER_AMBIGUOUS" not in reasons(d)


# ---------------------------------------------------------------- R1 / R2: ties cannot be ordered either
@both
def test_f_a_first_print_in_the_same_millisecond_as_the_last_pre_touch_update_is_ambiguous(direction):
    truth = Truth()
    gb = pre_touch(truth)
    gb.set(T(34.0), "bid", {149.10: 10.1})                            # the last pre-touch update, at ms X
    gb.sell(T(34.0), 149.65, 10.0)                                    # ... and one taker order sweeps the level
    gb.sell(T(34.0), 149.60, 40.0)                                    # above the pool and the pool's top, also at ms X
    for i, price in enumerate(POOL_LEVELS[1:]):
        gb.sell(T(34.2) + i * 100, price, 40.0)
    bounce(gb, 36.0)
    d = run(view(to_raw(truth, bars=bars(low=149.30), book_lag_ms=60, trade_lag_ms=80), direction))
    assert d.engine.setup is None and "ORDER_AMBIGUOUS" in reasons(d)
    assert evidence(d)[0]["ordering_ambiguous"][:3] in ("R1_", "R2_")


# ---------------------------------------------------------------- the owner's rule: depth alone is never consumption
@both
def test_k_depth_that_vanishes_without_a_single_executed_print_is_cancellation_never_consumption(direction):
    """The pool is gone from the book exactly as in a real sweep (A) - but the tape is empty. Disappearance is
    all the old proxy had; it must not qualify."""
    events, _ = timeline_a()
    d = run(view(drop(events, lambda e: isinstance(e, RawTrade) and e.action == "update"
                      and e.trade_id.startswith("t")), direction))
    assert d.engine.setup is None and "TOUCH_WITHOUT_SWEEP" in reasons(d)
    c = next(r.data for r in d.records if r.data.get("reason") == "TOUCH_WITHOUT_SWEEP")["consumption"]
    assert c["consumed"] == 0.0 and c["fraction"] == 0.0 and c["cancelled_est"] > 0.0


@both
def test_engine_without_a_tape_can_never_confirm_a_sweep(direction):
    events, _ = timeline_a()
    engine = LSRLEngine(CFG, RULES, risk_gate=NullRiskGate(), tape_available=False)
    d = SimDriver(Mode.PAPER, engine, SimExchange(RULES, CFG), start_nav_usdt=1_000.0)
    d.run(view(events, direction))
    assert d.engine.setup is None and "TAPE_UNAVAILABLE" in reasons(d)


# ---------------------------------------------------------------- which trades count as consumption
def timeline_d_with(extra_trades) -> list:
    truth = Truth()
    gb = pre_touch(truth)
    gb.sell(T(33.0), 149.65, 10.0)
    gb.set(T(33.4), "bid", {149.45: 10.0, 149.40: 10.0, 149.35: 10.0})
    gb.sell(T(33.6), 149.60, 40.0)
    gb.sell(T(33.7), 149.55, 8.0)                                     # 48 = 20 % executed
    extra_trades(gb)
    bounce(gb, 36.0)
    return to_raw(truth, bars=bars(low=149.55))


@both
def test_l_history_trades_delivered_at_subscribe_are_never_evidence(direction):
    events, _ = timeline_a()
    replay_as_history = [replace_action(e) if isinstance(e, RawTrade) and e.trade_id.startswith("t") else e
                         for e in events]
    d = run(view(replay_as_history, direction))
    assert d.engine.setup is None and d.observer.stats.get("history_trade_ignored", 0) >= 6
    assert "TOUCH_WITHOUT_SWEEP" in reasons(d)


def replace_action(t: RawTrade) -> RawTrade:
    from dataclasses import replace as dc_replace
    return dc_replace(t, action="snapshot")


@both
def test_m_a_trade_delivered_twice_is_counted_once(direction):
    """48 executed is 20 %. Double counting would read 96 = 40 % and wrongly confirm the sweep."""
    events = timeline_d_with(lambda gb: None)
    d = run(view(duplicate(events, lambda e: isinstance(e, RawTrade) and e.trade_id.startswith("t")), direction))
    assert d.engine.setup is None and "TOUCH_WITHOUT_SWEEP" in reasons(d)
    assert next(r.data for r in d.records if r.data.get("reason") == "TOUCH_WITHOUT_SWEEP")["consumption"][
        "consumed"] == pytest.approx(48.0)
    assert d.observer.stats["duplicate_trade"] >= 3


@both
def test_n_taker_buys_at_the_pool_prices_are_not_consumption_of_a_bid_pool(direction):
    def buyers(gb):                                                   # the price rebounds INTO the pool zone
        for i in range(2):                                            # (149.60 is the only pool price above the bids)
            gb.set(T(34.5) + i * 100, "ask", {149.60: 100.0})
            gb.buy(T(34.55) + i * 100, 149.60, 100.0)                 # 200 lifted at a pool price by TAKER BUYS
    events = timeline_d_with(buyers)
    d = run(view(events, direction))
    assert d.engine.setup is None
    assert next(r.data for r in d.records if r.data.get("reason") == "TOUCH_WITHOUT_SWEEP")["consumption"][
        "consumed"] == pytest.approx(48.0)


@both
def test_o_volume_executed_beyond_the_pool_is_not_consumption_of_it(direction):
    def through(gb):
        gb.set(T(34.0), "bid", {149.30: 500.0})
        gb.sell(T(34.1), 149.30, 400.0)                               # a huge print BELOW the pool's lowest price
    events = timeline_d_with(through)
    d = run(view(events, direction))
    assert d.engine.setup is None
    assert next(r.data for r in d.records if r.data.get("reason") == "TOUCH_WITHOUT_SWEEP")["consumption"][
        "consumed"] == pytest.approx(48.0)


# ---------------------------------------------------------------- feed integrity: fail closed, then rebuild causally
@both
def test_g_a_lost_packet_during_the_interaction_ends_the_candidate_and_stops_signals(direction):
    events, _ = timeline_a()
    lost = [e for e in events if isinstance(e, RawBook) and e.action == "update" and e.exchange_ts_ms == T(33.7)]
    assert lost, "the scenario must contain the frame we drop"
    d = run(view(drop(events, lambda e: e in lost), direction))
    assert d.engine.setup is None and "FEED_UNSAFE" in reasons(d)
    assert d.engine.signal_allowed is False and d.observer.state == UNSAFE
    assert d.observer.unsafe_reason.startswith("SEQ_GAP")


@both
def test_h_a_feed_that_goes_quiet_for_six_seconds_is_stale_and_ends_the_candidate(direction):
    events, _ = timeline_a()
    quiet = [e for e in events if isinstance(e, RawBook) and T(31.0) <= e.exchange_ts_ms <= T(37.0)]
    events = with_ticks(drop(events, lambda e: e in quiet), start_ms=T(-100), end_ms=T(62))
    d = run(view(events, direction))
    assert d.engine.setup is None and d.observer.state == UNSAFE
    assert d.observer.unsafe_reason.startswith("STALE_BOOK") and "FEED_UNSAFE" in reasons(d)


@both
def test_i_updates_after_a_reconnect_are_ignored_until_a_valid_snapshot_reconstructs_the_book(direction):
    events, _ = timeline_a()
    cut = next(i for i, e in enumerate(events) if isinstance(e, RawBook) and e.exchange_ts_ms >= T(10))
    later = [e for e in events[cut:] if isinstance(e, RawBook) and e.action == "update"][:5]
    stream = events[:cut] + [FeedMarker(events[cut].t_ms, "DISCONNECT", "conn-1", "test"),
                             FeedMarker(events[cut].t_ms + 5, "CONNECT", "conn-2")]
    stream += [replace_conn(u, "conn-2") for u in later]                    # updates with NO snapshot in front
    d = run(view(stream, direction))
    assert d.observer.state == NO_BOOK and d.engine.signal_allowed is False
    assert d.observer.stats["dropped_update_book_not_ok"] == 5 and d.engine.last_snapshot is None


def replace_conn(ev, conn):
    from dataclasses import replace as dc_replace
    return dc_replace(ev, conn_id=conn)


@both
def test_j_pool_age_never_bridges_an_uncertain_interval(direction):
    """The same pool, present for 240 s, but the feed was rebuilt 20 s before the sweep. Its pre-existence can
    only be vouched for from the reconstruction onwards (~23 s < 180 s): the sweep must be rejected."""
    events, _ = timeline_a()
    cut = next(i for i, e in enumerate(events) if isinstance(e, RawBook) and e.exchange_ts_ms >= T(10))
    bids: dict[str, str] = {}
    asks: dict[str, str] = {}
    for e in events[:cut]:                                            # the book exactly as it stood at the cut
        if isinstance(e, RawBook):
            if e.action == "snapshot":
                bids.clear(), asks.clear()
            for side, rows in ((bids, e.bids), (asks, e.asks)):
                for price, size in rows:
                    side.pop(price, None) if float(size) == 0 else side.__setitem__(price, size)
    first_update = next(e for e in events[cut:] if isinstance(e, RawBook) and e.action == "update")
    resnap = RawBook(t_ms=events[cut].t_ms, exchange_ts_ms=first_update.exchange_ts_ms - 1, action="snapshot",
                     seq=first_update.pseq, pseq=0, bids=tuple(bids.items()), asks=tuple(asks.items()),
                     conn_id="conn-2")
    rest = [replace_conn(e, "conn-2") if isinstance(e, (RawBook, RawTrade)) else e for e in events[cut:]]
    stream = events[:cut] + [FeedMarker(resnap.t_ms - 1, "DISCONNECT", "conn-1", "test"),
                             FeedMarker(resnap.t_ms, "CONNECT", "conn-2"), resnap] + rest
    d = run(view(stream, direction))
    assert d.observer.epoch == 2 and d.observer.state == OK           # the book WAS causally reconstructed ...
    assert d.engine.setup is None and "POOL_TOO_YOUNG" in reasons(d)  # ... but the pool's age restarted with it
    assert "SWEEP_CONFIRMED" not in [r.data.get("event") for r in d.records]


# ---------------------------------------------------------------- the observed residual is measurable
@both
def test_every_candidate_records_how_stale_the_frozen_pre_touch_book_was_at_first_touch(direction):
    events, _ = observed("A_true_consumption", direction)
    d = run(events)
    ev = d.engine.setup.feed_evidence
    assert ev["frozen_seq"] is not None and ev["feed_epoch"] == 1
    assert 0 <= ev["pre_touch_state_age_ms"] <= 3_000 and ev["tape_first_ts_ms"] >= T(33.5)
    rejected = run(observed("B_cancel_before_touch", direction)[0])
    assert rejected.engine.setup is None


@both
def test_every_candidate_records_the_book_window_and_the_distance_reach_so_coverage_is_measurable(direction):
    events, _ = observed("A_true_consumption", direction)
    setup = run(events).engine.setup
    ev = setup.feed_evidence
    assert ev["book_window_bid"] > 0 and ev["book_window_ask"] > 0
    assert ev["distance_reach"] == pytest.approx(1.5 * setup.atr15)        # no 15m bar closes inside the scenario
