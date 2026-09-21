"""LSRL_V1 causal observation layer: raw Bitget frames -> reconstructed book, tape and feed health.

Every integrity rule fails CLOSED and stays closed until a NEW valid snapshot reconstructs the book. The real
captured frames (tests/fixtures/bitget_ws_solusdt_real_capture.jsonl, public unauthenticated SOLUSDT capture)
are replayed through the parser + observer and compared with an independent reconstruction.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lsrl.core.types import DepthSnapshot, FeedHealth, FeedMarker, RawBook, RawTrade, TimerTick, TradeTick
from lsrl.obs.observer import NO_BOOK, OK, UNSAFE, BookObserver, FeedConfig
from lsrl.ws import parse_frame

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "bitget_ws_solusdt_real_capture.jsonl"
C = "c1"


def snap(t=1_000, seq=100, ts=None, bids=(("100.0", "5"), ("99.9", "6")), asks=(("100.1", "4"), ("100.2", "7")),
         conn=C) -> RawBook:
    return RawBook(t_ms=t, exchange_ts_ms=t - 100 if ts is None else ts, action="snapshot", seq=seq, pseq=0,
                   bids=bids, asks=asks, conn_id=conn)


def upd(t, seq, pseq, ts=None, bids=(), asks=(), conn=C) -> RawBook:
    return RawBook(t_ms=t, exchange_ts_ms=t - 100 if ts is None else ts, action="update", seq=seq, pseq=pseq,
                   bids=bids, asks=asks, conn_id=conn)


def trade(t, ts=None, tid="t1", price="100.0", size="1", side="sell", action="update", conn=C) -> RawTrade:
    return RawTrade(t_ms=t, exchange_ts_ms=t - 100 if ts is None else ts, action=action, trade_id=tid, price=price,
                    size=size, side=side, conn_id=conn)


def live(obs: BookObserver | None = None) -> BookObserver:
    obs = obs or BookObserver()
    obs.on_event(FeedMarker(0, "SESSION_START"))
    obs.on_event(FeedMarker(1, "CONNECT", C))
    out = obs.on_event(snap())
    assert obs.state == OK and [type(e) for e in out] == [FeedHealth, DepthSnapshot]
    return obs


def kinds(events) -> list[str]:
    return [type(e).__name__ for e in events]


# ------------------------------------------------------------------ snapshot / bootstrap
def test_first_snapshot_bootstraps_the_book_and_reports_health_epoch_1():
    obs = BookObserver()
    out = obs.on_event(snap(seq=100))
    health, depth = out
    assert health == FeedHealth(1_000, True, "BOOTSTRAP", 1)
    assert depth.bids == ((100.0, 5.0), (99.9, 6.0)) and depth.asks == ((100.1, 4.0), (100.2, 7.0))
    assert depth.seq == 100 and depth.epoch == 1 and depth.exchange_ts_ms == 900 and depth.t_ms == 1_000


@pytest.mark.parametrize("bad, reason", [
    (dict(bids=(("x", "5"),)), "BAD_SNAPSHOT_LEVEL"),
    (dict(bids=(("100", "-1"),)), "BAD_SNAPSHOT_LEVEL"),
    (dict(bids=(("0", "1"),)), "BAD_SNAPSHOT_LEVEL"),
    (dict(bids=(("nan", "1"),)), "BAD_SNAPSHOT_LEVEL"),
    (dict(bids=(), asks=(("100.1", "4"),)), "EMPTY_SNAPSHOT_SIDE"),
    (dict(bids=(("100.2", "5"),), asks=(("100.1", "4"),)), "CROSSED_SNAPSHOT"),
])
def test_an_invalid_snapshot_is_never_accepted(bad, reason):
    obs = BookObserver()
    assert obs.on_event(snap(**bad)) == []                      # not ok before, nothing to withdraw
    assert obs.state == UNSAFE and obs.unsafe_reason == reason and obs.resnapshot_needed


def test_a_snapshot_without_seq_or_exchange_time_is_refused():
    obs = BookObserver()
    obs.on_event(RawBook(1_000, None, "snapshot", 100, 0, (("100", "1"),), (("101", "1"),), C))
    assert obs.state == UNSAFE and obs.unsafe_reason == "SNAPSHOT_WITHOUT_SEQ_OR_TS"


@pytest.mark.parametrize("ts, prefix", [(1_000 - 6_000, "CLOCK_LAG"), (1_000 + 3_000, "CLOCK_AHEAD")])
def test_a_clock_violation_on_the_snapshot_fails_closed(ts, prefix):
    obs = BookObserver()
    obs.on_event(snap(ts=ts))
    assert obs.state == UNSAFE and obs.unsafe_reason.startswith(prefix)


# ------------------------------------------------------------------ depth updates
def test_updates_that_chain_by_pseq_are_applied_and_a_zero_size_removes_the_level():
    obs = live()
    out = obs.on_event(upd(1_100, seq=140, pseq=100, bids=(("100.0", "0"), ("99.95", "3")), asks=(("100.3", "9"),)))
    (depth,) = out
    assert depth.bids == ((99.95, 3.0), (99.9, 6.0)) and depth.asks == ((100.1, 4.0), (100.2, 7.0), (100.3, 9.0))
    assert depth.seq == 140 and depth.epoch == 1
    out = obs.on_event(upd(1_200, seq=190, pseq=140, bids=(("99.9", "0"),)))          # seq jumps are normal
    assert out[0].bids == ((99.95, 3.0),)


def test_a_zero_size_for_a_level_that_is_not_in_the_book_is_benign():
    obs = live()
    out = obs.on_event(upd(1_100, seq=140, pseq=100, bids=(("50.0", "0"),)))          # ~2.4 % of real level changes
    assert kinds(out) == ["DepthSnapshot"] and obs.state == OK


def test_an_exact_redelivery_of_the_last_update_is_ignored_and_counted():
    obs = live()
    first = upd(1_100, seq=140, pseq=100, bids=(("100.0", "9"),))
    obs.on_event(first)
    assert obs.on_event(first) == [] and obs.state == OK and obs.stats["duplicate_update"] == 1


def test_an_old_or_out_of_order_update_is_a_failure_not_a_duplicate():
    obs = live()
    obs.on_event(upd(1_100, seq=140, pseq=100))
    out = obs.on_event(upd(1_200, seq=120, pseq=100))
    assert out == [FeedHealth(1_200, False, "OUT_OF_ORDER_SEQ:120<140", 1)]
    assert obs.state == UNSAFE


def test_a_missing_packet_breaks_the_pseq_chain_and_fails_closed():
    obs = live()
    out = obs.on_event(upd(1_100, seq=200, pseq=150))                                   # 150 was never received
    assert out == [FeedHealth(1_100, False, "SEQ_GAP:expected_pseq=100,got=150", 1)]
    assert obs.state == UNSAFE and obs.resnapshot_needed


def test_exchange_time_that_goes_backwards_fails_closed():
    obs = live()
    obs.on_event(upd(1_100, seq=140, pseq=100, ts=1_050))
    out = obs.on_event(upd(1_200, seq=190, pseq=140, ts=1_049))
    assert out[0].reason.startswith("EXCHANGE_TS_REGRESSION") and obs.state == UNSAFE


def test_equal_exchange_times_are_allowed():
    obs = live()
    obs.on_event(upd(1_100, seq=140, pseq=100, ts=1_050))
    assert kinds(obs.on_event(upd(1_100, seq=190, pseq=140, ts=1_050))) == ["DepthSnapshot"]


@pytest.mark.parametrize("bids, asks, reason", [
    ((("100.5", "1"),), (), "CROSSED_BOOK"),
    ((("100.0", "0"), ("99.9", "0")), (), "EMPTY_BOOK_SIDE"),
    ((("oops", "1"),), (), "BAD_UPDATE_LEVEL"),
])
def test_a_bad_update_never_half_applies_and_never_emits_a_book(bids, asks, reason):
    obs = live()
    out = obs.on_event(upd(1_100, seq=140, pseq=100, bids=bids, asks=asks))
    assert [type(e) for e in out] == [FeedHealth] and out[0].reason == reason
    assert obs.state == UNSAFE and obs.window_width() is None      # the poisoned book is gone, not half-updated


def test_an_update_with_no_sequence_information_cannot_be_chained():
    obs = live()
    obs.on_event(RawBook(1_100, 1_000, "update", None, None, (), (), C))
    assert obs.state == UNSAFE and obs.unsafe_reason == "UPDATE_WITHOUT_SEQ_OR_TS"


def test_a_clock_violation_on_an_update_fails_closed():
    obs = live()
    out = obs.on_event(upd(9_000, seq=140, pseq=100, ts=1_000))                       # 8 s late
    assert out[0].reason.startswith("CLOCK_LAG") and obs.state == UNSAFE


# ------------------------------------------------------------------ connection lifecycle
def test_updates_before_any_snapshot_are_dropped():
    obs = BookObserver()
    obs.on_event(FeedMarker(1, "CONNECT", C))
    assert obs.on_event(upd(1_100, seq=140, pseq=100)) == [] and obs.stats["dropped_update_book_not_ok"] == 1
    assert obs.state == NO_BOOK


def test_a_disconnect_withdraws_the_view_and_a_reconnect_needs_a_valid_snapshot():
    obs = live()
    out = obs.on_event(FeedMarker(2_000, "DISCONNECT", C, "closed"))
    assert out == [FeedHealth(2_000, False, "DISCONNECTED", 1)] and obs.state == NO_BOOK
    obs.on_event(FeedMarker(2_100, "CONNECT", "c2"))
    assert obs.on_event(upd(2_200, seq=140, pseq=100, conn="c2")) == []              # no snapshot yet: still nothing
    assert obs.state == NO_BOOK
    out = obs.on_event(snap(t=2_300, seq=900, conn="c2"))
    assert out[0] == FeedHealth(2_300, True, "RESNAPSHOT", 2) and obs.epoch == 2 and obs.state == OK


def test_nothing_is_heard_between_a_disconnect_and_the_next_connect():
    obs = live()
    obs.on_event(FeedMarker(2_000, "DISCONNECT", C))
    assert obs.on_event(snap(t=2_100, seq=500, conn=C)) == [] and obs.state == NO_BOOK      # the dead socket's late frame
    assert obs.on_event(trade(2_150, tid="late")) == []
    assert obs.stats["foreign_connection_messages"] == 2
    obs.on_event(FeedMarker(2_200, "CONNECT", "c2"))
    assert kinds(obs.on_event(snap(t=2_300, seq=600, conn="c2"))) == ["FeedHealth", "DepthSnapshot"] and obs.state == OK


def test_frames_from_a_superseded_connection_are_ignored():
    obs = live()
    obs.on_event(FeedMarker(2_000, "DISCONNECT", C))
    obs.on_event(FeedMarker(2_100, "CONNECT", "c2"))
    assert obs.on_event(snap(t=2_200, seq=500, conn=C)) == []                          # the OLD socket's late frame
    assert obs.state == NO_BOOK and obs.stats["foreign_connection_messages"] == 1


def test_an_unrequested_snapshot_on_a_live_book_is_an_uncertain_interval():
    obs = live()
    out = obs.on_event(snap(t=5_000, seq=777))
    assert [type(e).__name__ for e in out] == ["FeedHealth", "FeedHealth", "DepthSnapshot"]
    assert (out[0].ok, out[0].reason, out[1].ok, out[1].epoch) == (False, "UNEXPECTED_SNAPSHOT", True, 2)


def test_session_start_is_a_hard_reset():
    obs = live()
    out = obs.on_event(FeedMarker(9_000, "SESSION_START"))
    assert out == [FeedHealth(9_000, False, "SESSION_START", 0)] and obs.state == NO_BOOK and obs.epoch == 0


def test_a_feed_reset_marker_and_unknown_markers():
    obs = live()
    assert obs.on_event(FeedMarker(2_000, "SUBSCRIBED", C, "books")) == [] and obs.state == OK
    assert obs.on_event(FeedMarker(2_100, "BAD_FRAME", C, "junk")) == [] and obs.state == OK
    assert obs.on_event(FeedMarker(2_200, "RESET", C))[0].reason == "FEED_RESET" and obs.state == NO_BOOK


# ------------------------------------------------------------------ trade tape
def test_a_taker_buy_and_sell_become_ticks_with_the_aggressor_side_and_exchange_time():
    obs = live()
    b, s = obs.on_event(trade(1_100, ts=1_040, tid="a", price="100.1", size="2.5", side="buy")), \
        obs.on_event(trade(1_200, ts=1_150, tid="b", price="100.0", size="0.4", side="sell"))
    assert b == [TradeTick(1_100, 1_040, 100.1, 2.5, "BUY", "a")] and s == [TradeTick(1_200, 1_150, 100.0, 0.4, "SELL", "b")]


def test_history_delivered_at_subscribe_is_never_evidence_but_is_remembered_for_dedupe():
    obs = live()
    assert obs.on_event(trade(1_050, tid="old", action="snapshot")) == []
    assert obs.on_event(trade(1_100, tid="old")) == [] and obs.stats["duplicate_trade"] == 1


def test_the_same_trade_id_is_counted_once():
    obs = live()
    assert len(obs.on_event(trade(1_100, tid="x"))) == 1
    assert obs.on_event(trade(1_101, tid="x")) == [] and obs.stats["duplicate_trade"] == 1


def test_trades_are_dropped_while_the_book_is_not_ok():
    obs = BookObserver()
    obs.on_event(FeedMarker(1, "CONNECT", C))
    assert obs.on_event(trade(1_100)) == [] and obs.stats["dropped_trade_book_not_ok"] == 1


@pytest.mark.parametrize("kw", [dict(side="hold"), dict(price="oops"), dict(size="0"), dict(size="-3"), dict(price="0")])
def test_an_unclassifiable_trade_fails_closed(kw):
    obs = live()
    obs.on_event(trade(1_100, **kw))
    assert obs.state == UNSAFE


def test_a_trade_without_exchange_time_fails_closed():
    obs = live()
    obs.on_event(RawTrade(1_100, None, "update", "z", "100", "1", "buy", C))
    assert obs.state == UNSAFE and obs.unsafe_reason == "TRADE_WITHOUT_TS"


def test_millisecond_jitter_in_trade_batches_is_counted_but_not_fatal():
    """Observed on the live feed: a trade stamped 4 ms earlier than one already received."""
    obs = live()
    obs.on_event(trade(1_200, ts=1_112, tid="a"))
    out = obs.on_event(trade(1_205, ts=1_108, tid="b"))
    assert len(out) == 1 and obs.state == OK and obs.stats["trade_ts_minor_regression"] == 1


def test_a_large_trade_time_regression_is_a_clock_fault():
    obs = live()
    obs.on_event(trade(5_000, ts=4_900, tid="a"))
    obs.on_event(trade(5_100, ts=2_000, tid="b"))
    assert obs.state == UNSAFE and obs.unsafe_reason.startswith("TRADE_TS_REGRESSION")


# ------------------------------------------------------------------ staleness (judged on recorded ticks)
def test_a_book_that_stops_updating_is_stale_after_the_threshold_only():
    obs = live()
    assert obs.on_event(TimerTick(1_000 + 5_000)) == [] and obs.state == OK              # exactly at the limit
    out = obs.on_event(TimerTick(1_000 + 5_001))
    assert out and out[0].reason.startswith("STALE_BOOK") and obs.state == UNSAFE


def test_a_tape_that_stays_silent_is_stale_only_after_its_own_longer_threshold():
    obs = live()
    for i in range(1, 46):                                                                # keep the BOOK alive
        obs.on_event(upd(1_000 + i * 4_000, seq=100 + i * 10, pseq=100 + (i - 1) * 10))
    assert obs.state == OK
    assert obs.on_event(TimerTick(1_000 + 180_000)) == []                                 # exactly at the limit
    out = obs.on_event(TimerTick(1_000 + 180_001))
    assert out and out[0].reason.startswith("STALE_TAPE")


def test_ticks_before_a_book_exists_do_nothing():
    obs = BookObserver()
    assert obs.on_event(TimerTick(10_000_000)) == [] and obs.state == NO_BOOK


# ------------------------------------------------------------------ determinism (replay relies on it)
def test_the_observer_is_a_pure_function_of_its_input_stream():
    stream = [FeedMarker(0, "SESSION_START"), FeedMarker(1, "CONNECT", C), snap(), trade(1_050, tid="h", action="snapshot"),
              upd(1_100, seq=140, pseq=100, bids=(("100.0", "8"),)), trade(1_150, tid="a", price="100.0"),
              upd(1_200, seq=190, pseq=140, asks=(("100.1", "0"),)), TimerTick(1_300), upd(1_400, seq=999, pseq=5)]
    runs = []
    for _ in range(2):
        obs, out = BookObserver(), []
        for ev in stream:
            out.extend(obs.on_event(ev))
        runs.append((out, obs.state, obs.epoch, dict(obs.stats)))
    assert runs[0] == runs[1]


def test_the_feed_configuration_has_a_stable_digest_recorded_in_headers():
    a, b = FeedConfig(), FeedConfig(book_stale_ms=9_999)
    assert a.digest() == FeedConfig().digest() and a.digest() != b.digest()


# ------------------------------------------------------------------ real captured Bitget frames
def real_frames():
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


def run_real(observer: BookObserver | None = None):
    feed = FeedConfig()
    obs = observer or BookObserver(feed)
    obs.on_event(FeedMarker(0, "SESSION_START"))
    obs.on_event(FeedMarker(1, "CONNECT", C))
    out = []
    for row in real_frames():
        for ev in parse_frame(row["raw"], row["recv_ms"], C, feed):
            out.extend(obs.on_event(ev))
    return obs, out


def independent_book():
    """A second, deliberately naive reconstruction of the same frames (no shared code with the observer)."""
    bids, asks = {}, {}
    for row in real_frames():
        if row["raw"] == "pong":
            continue
        d = json.loads(row["raw"])
        if d.get("arg", {}).get("channel") != "books" or "data" not in d:
            continue
        item = d["data"][0]
        if d["action"] == "snapshot":
            bids, asks = {float(p): float(s) for p, s in item["bids"]}, {float(p): float(s) for p, s in item["asks"]}
        else:
            for book, rows in ((bids, item["bids"]), (asks, item["asks"])):
                for p, s in rows:
                    if float(s) == 0:
                        book.pop(float(p), None)
                    else:
                        book[float(p)] = float(s)
    return tuple(sorted(bids.items(), reverse=True)), tuple(sorted(asks.items()))


def test_real_bitget_frames_flow_through_without_a_single_false_alarm():
    obs, out = run_real()
    health = [e for e in out if isinstance(e, FeedHealth)]
    assert [(h.ok, h.reason) for h in health] == [(True, "BOOTSTRAP")]
    assert obs.state == OK and obs.epoch == 1
    assert not [k for k in obs.stats if k.startswith("unsafe:")]
    assert obs.stats["history_trade_ignored"] >= 1
    assert len([e for e in out if isinstance(e, DepthSnapshot)]) > 300 and len([e for e in out if isinstance(e, TradeTick)]) > 10


def test_the_reconstructed_real_book_equals_an_independent_reconstruction():
    _obs, out = run_real()
    final = [e for e in out if isinstance(e, DepthSnapshot)][-1]
    want_bids, want_asks = independent_book()
    assert final.bids == want_bids and final.asks == want_asks
    assert len(final.bids) == 200 and len(final.asks) == 200 and final.best_bid < final.best_ask


def test_real_trade_sides_are_taker_sides_the_book_confirms_it():
    """side=buy lifts an ASK (price >= best ask at the time), side=sell hits a BID."""
    obs, out = run_real()
    book, buys_ok = None, [0, 0]
    sells_ok = [0, 0]
    for e in out:
        if isinstance(e, DepthSnapshot):
            book = e
        elif isinstance(e, TradeTick) and book is not None:
            if e.aggressor == "BUY":
                buys_ok[0] += e.price >= book.best_ask - 1e-9
                buys_ok[1] += 1
            else:
                sells_ok[0] += e.price <= book.best_bid + 1e-9
                sells_ok[1] += 1
    assert buys_ok[1] and sells_ok[1]
    assert buys_ok[0] / buys_ok[1] >= 0.9 and sells_ok[0] / sells_ok[1] >= 0.9        # the rest are ms-scale ordering noise


def test_the_real_frames_replayed_twice_give_identical_streams():
    a = run_real()[1]
    b = run_real()[1]
    assert a == b
