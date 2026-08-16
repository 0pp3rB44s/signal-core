"""Near-miss telemetry: can we reconstruct why an episode did or did not trade?

The 2026-08-16 AVAX loss was explained by a gate interaction that no existing log
could show: a good early signal blocked by the volatility floor, the impulse itself
blocked because alignment never held 2 s, and a late entry once the market calmed.
That sequence is the regression fixture here — if the telemetry cannot reproduce it,
it does not do its job.

Two properties matter more than any individual field:

* **Behaviour is unchanged.** `_direction` now delegates to `evaluate_gates`, so a
  test drives both against many synthetic snapshots and asserts they never disagree.
* **Telemetry cannot trade.** The post-hoc labeller is asserted to be free of
  execution imports and to mark every row as future-data-derived.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from microflow.candidates import CandidateEpisodeSampler, FrozenResearchSpec
from microflow.near_miss import (
    EpisodeState, GateEvaluation, NearMissTracker, ResetCause, evaluate_gates,
)

SPEC = FrozenResearchSpec()


def snap(ts_ms, *, symbol="AVAXUSDT", ofi=-0.9, imb=-0.6, edge=-0.5, spread=0.5,
         range60=15.0, trade_age=100, book_age=100, mid=6.30, seq=True):
    """A snapshot shaped exactly like the collector's, with tunable gate inputs."""
    return {
        "symbol": symbol,
        "timestamp_local": ts_ms,
        "trade_flow": {
            "1s": {"ofi": ofi}, "5s": {"ofi": ofi}, "15s": {"ofi": ofi},
            "30s": {"ofi": ofi},
            "60s": {"ofi": ofi, "realized_range_bps": range60},
        },
        "book": {"book_imbalance_top5": imb, "book_imbalance_top1": imb, "spread_bps": spread},
        "microprice": {"microprice_vs_mid_bps": edge, "mid_price": mid},
        "freshness": {"sequence_valid": seq, "trade_stream_age_ms": trade_age,
                      "book_stream_age_ms": book_age},
    }


def tracker():
    return NearMissTracker(SPEC, heartbeat_ms=5_000)


def states(rows):
    return [r["state"] for r in rows]


# --- the decision itself must not have changed -------------------------------


def test_evaluate_gates_never_disagrees_with_the_sampler_decision():
    """The refactor's whole safety argument, exercised across the gate boundaries."""
    sampler = CandidateEpisodeSampler(SPEC)
    grid = itertools.product(
        (-1.0, -0.3, -0.25, -0.24, 0.0, 0.24, 0.25, 0.3, 1.0),   # ofi
        (-0.6, -0.2, -0.19, 0.0, 0.19, 0.2, 0.6),                # imbalance
        (-0.5, 0.0, 0.5),                                        # microprice edge
        (0.5, 5.0, 5.1),                                         # spread
        (9.9, 10.0, 15.0),                                       # 60s range
    )
    checked = 0
    for ofi, imb, edge, spread, rng in grid:
        s = snap(1_000, ofi=ofi, imb=imb, edge=edge, spread=spread, range60=rng)
        assert evaluate_gates(s, SPEC).direction == sampler._direction(s), (
            f"divergence at ofi={ofi} imb={imb} edge={edge} spread={spread} range={rng}")
        checked += 1
    assert checked > 400


@pytest.mark.parametrize("field,value,gate", [
    ("range60", 9.9, "volatility_floor"),
    ("spread", 5.1, "spread"),
    ("trade_age", 1_001, "freshness"),
    ("book_age", 1_001, "freshness"),
    ("seq", False, "freshness"),
    ("ofi", -0.24, "ofi"),
    ("imb", -0.19, "imbalance"),
    ("edge", 0.0, "microprice"),
])
def test_the_blocking_gate_is_named(field, value, gate):
    ev = evaluate_gates(snap(1_000, **{field: value}), SPEC)
    assert ev.direction is None
    assert ev.last_failed_gate == gate


def test_distance_to_pass_quantifies_the_shortfall():
    ev = evaluate_gates(snap(1_000, range60=6.01), SPEC)
    assert ev.distance_to_pass["volatility_floor"] == pytest.approx(3.99, abs=1e-6)


def test_intent_survives_a_failing_gate():
    """A blocked episode still needs a side, or it cannot be labelled later."""
    ev = evaluate_gates(snap(1_000, range60=6.01), SPEC)
    assert ev.direction is None and ev.intent == "SHORT"


# --- episode lifecycle -------------------------------------------------------


def test_episode_opens_and_reaches_near_candidate():
    t = tracker()
    rows = t.observe(snap(1_000), evaluate_gates(snap(1_000), SPEC))
    assert EpisodeState.PRE_SIGNAL in states(rows)
    assert EpisodeState.NEAR_CANDIDATE in states(rows)
    assert rows[0]["side"] == "SHORT"


def test_candidate_transition_carries_the_candidate_id():
    t = tracker()
    t.observe(snap(1_000), evaluate_gates(snap(1_000), SPEC))
    s = snap(3_200)
    rows = t.observe(s, evaluate_gates(s, SPEC), candidate_id="abc123")
    cand = [r for r in rows if r["state"] == EpisodeState.CANDIDATE]
    assert cand and cand[0]["candidate_id"] == "abc123"
    assert cand[0]["alignment_ms"] == 2_200


def test_persistence_reset_records_its_cause():
    t = tracker()
    t.observe(snap(1_000), evaluate_gates(snap(1_000), SPEC))
    broken = snap(1_800, imb=-0.19)          # book flips below threshold
    rows = t.observe(broken, evaluate_gates(broken, SPEC))
    blocked = [r for r in rows if r["state"] == EpisodeState.BLOCKED]
    assert blocked and blocked[0]["reason"] == ResetCause.BOOK_FLIP
    assert blocked[0]["resets"] == 1
    assert blocked[0]["reset_log"][0]["held_ms"] == 800


def test_max_alignment_survives_a_reset():
    """The longest run is the number that decides whether persistence was reachable."""
    t = tracker()
    t.observe(snap(0), evaluate_gates(snap(0), SPEC))
    s = snap(1_200)
    t.observe(s, evaluate_gates(s, SPEC))
    broken = snap(1_300, ofi=0.0)
    rows = t.observe(broken, evaluate_gates(broken, SPEC))
    assert rows[-1]["max_alignment_ms"] == 1_200


def test_direction_change_expires_the_episode():
    t = tracker()
    t.observe(snap(1_000), evaluate_gates(snap(1_000), SPEC))
    flipped = snap(2_000, ofi=0.9, imb=0.6, edge=0.5)
    rows = t.observe(flipped, evaluate_gates(flipped, SPEC))
    assert EpisodeState.EXPIRED in states(rows)
    assert rows[0]["reason"] == ResetCause.DIRECTION_CHANGE


def test_idle_episodes_are_expired_not_leaked():
    t = tracker()
    t.observe(snap(1_000), evaluate_gates(snap(1_000), SPEC))
    assert t.expire_stale(2_000) == []
    rows = t.expire_stale(1_000 + 60_000)
    assert rows and rows[0]["state"] == EpisodeState.EXPIRED
    assert t._episodes == {}


# --- volatility/persistence interaction: the AVAX mechanism -------------------


def test_range_is_captured_at_each_persistence_milestone():
    """Shows whether volatility rises *while* an alignment matures."""
    t = tracker()
    for ts, rng in ((0, 11.0), (600, 12.0), (1_100, 13.0), (1_600, 14.0), (2_100, 15.0)):
        s = snap(ts, range60=rng)
        t.observe(s, evaluate_gates(s, SPEC))
    ep = t._episodes["AVAXUSDT"]
    assert ep.range_at_first_alignment == 11.0
    assert ep.range_at_milestone["500ms"] == 12.0
    assert ep.range_at_milestone["2000ms"] == 15.0


def test_avax_sequence_is_reconstructable():
    """The 2026-08-16 regression fixture, in its three real phases.

    Early signal blocked by the floor, impulse blocked by persistence, late entry
    passes. Values are the ones measured on the Runner.
    """
    t = tracker()

    # 23:59:05 — 3673 ms of alignment, range 6.01, below the 10 bps floor.
    early = []
    for ts in range(0, 3_700, 500):
        s = snap(ts, range60=6.01)
        early += t.observe(s, evaluate_gates(s, SPEC))
    assert all(r["gates"]["volatility_floor"] is False for r in early)
    assert early[0]["last_failed_gate"] == "volatility_floor"
    assert early[0]["distance_to_pass"]["volatility_floor"] == pytest.approx(3.99, abs=1e-6)
    assert not any(r["state"] == EpisodeState.NEAR_CANDIDATE for r in early), \
        "the floor must prevent alignment being credited"

    # 00:00 — range is ample, but alignment never survives 2 s.
    t2 = tracker()
    impulse = []
    for i in range(6):
        base = 10_000 + i * 2_000
        for ts in (base, base + 600, base + 1_200):
            s = snap(ts, range60=37.75)
            impulse += t2.observe(s, evaluate_gates(s, SPEC))
        broken = snap(base + 1_400, imb=-0.19, range60=37.75)
        impulse += t2.observe(broken, evaluate_gates(broken, SPEC))
    blocked = [r for r in impulse if r["state"] == EpisodeState.BLOCKED]
    assert blocked, "resets must be visible"
    assert max(r["max_alignment_ms"] for r in impulse) < SPEC.persistence_ms
    assert all(r["gates"]["volatility_floor"] for r in impulse)
    assert blocked[0]["reason"] == ResetCause.BOOK_FLIP

    # 00:09 — range 11.18, alignment holds 6255 ms, candidate fires.
    t3 = tracker()
    late = []
    for ts in range(0, 6_300, 500):
        s = snap(ts, range60=11.18)
        late += t3.observe(s, evaluate_gates(s, SPEC))
    final = snap(6_255, range60=11.18)
    late += t3.observe(final, evaluate_gates(final, SPEC), candidate_id="avax-late")
    cand = [r for r in late if r["state"] == EpisodeState.CANDIDATE]
    assert cand and cand[0]["alignment_ms"] == 6_255
    assert cand[0]["gates"]["volatility_floor"] is True
    assert cand[0]["range_at_milestone_bps"]["2000ms"] == 11.18


# --- price context -----------------------------------------------------------


def test_prior_moves_use_only_past_prices():
    t = tracker()
    for i in range(60):
        s = snap(i * 1_000, mid=6.30, range60=15.0)
        t.observe(s, evaluate_gates(s, SPEC))
    s = snap(60_000, mid=6.20, range60=15.0)
    rows = t.observe(s, evaluate_gates(s, SPEC))
    ctx = rows[-1]["context"] if rows else t._prior_moves("AVAXUSDT", 60_000, 6.20)
    assert ctx["prior_move_30s_bps"] == pytest.approx(-158.7, abs=1.0)
    assert ctx["range_position"] == pytest.approx(0.0, abs=0.01)


# --- volume discipline -------------------------------------------------------


def test_a_steady_episode_does_not_emit_per_frame():
    """Telemetry must not outweigh the data it describes."""
    t = tracker()
    total = 0
    for ts in range(0, 20_000, 100):        # 200 frames, 20 s
        s = snap(ts, range60=15.0)
        total += len(t.observe(s, evaluate_gates(s, SPEC)))
    assert total <= 8, f"emitted {total} rows for 200 frames"


def test_reset_log_is_bounded():
    t = tracker()
    for i in range(200):
        base = i * 1_000
        s = snap(base, range60=15.0)
        t.observe(s, evaluate_gates(s, SPEC))
        broken = snap(base + 400, imb=-0.19, range60=15.0)
        t.observe(broken, evaluate_gates(broken, SPEC))
    ep = t._episodes.get("AVAXUSDT")
    if ep:
        assert len(ep.reset_log) <= NearMissTracker.MAX_RESET_LOG


def test_price_history_is_bounded():
    t = tracker()
    for i in range(6_000):
        t._record_price("AVAXUSDT", i * 100, 6.30)
    assert len(t._prices["AVAXUSDT"]) <= 4_096


# --- research separation -----------------------------------------------------


def test_labeller_never_imports_execution():
    src = (Path(__file__).resolve().parents[1] / "scripts" / "label_near_miss.py").read_text()
    for forbidden in ("from execution", "import execution", "place_futures", "set_futures_leverage"):
        assert forbidden not in src, f"labeller reaches into execution: {forbidden}"


def test_labels_are_marked_as_future_derived():
    src = (Path(__file__).resolve().parents[1] / "scripts" / "label_near_miss.py").read_text()
    assert '"uses_future_data": True' in src
    assert '"research_only": True' in src


def test_telemetry_rows_are_marked_research_only():
    t = tracker()
    rows = t.observe(snap(1_000), evaluate_gates(snap(1_000), SPEC))
    assert all(r["research_only"] is True for r in rows)
