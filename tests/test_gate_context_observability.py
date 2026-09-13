"""OBSERVABILITY_GATE_CONTEXT_2026-09-13: pins the fix for a real forensic gap.

Independent audit (2026-09-13, final clean shadow epoch) found that a symbol's
executed candidate can carry its entry_quality_long/short annotation on
candidate.market.notes rather than candidate.notes -- true for
liquidity_sweep_reversal and prearmed momentum_breakout/breakdown candidates.
_master_entry_quality_gate() correctly reads both sources via
TradePlanner._note_text() (candidate.notes + candidate.market.notes +
candidate.detection.reason_flags), but only candidate.notes ever reached
forward_paper persistence (via plan.notes). A post-hoc auditor reading a
different (e.g. 15m primary "SCAN") snapshot's entry_quality for the same
symbol at nearly the same time could -- and in that live incident, did --
misattribute the wrong number to the executed candidate.

These tests pin that plan.gate_context now captures the exact three note
sources the gate read, plus its own inputs/verdict, so this can never happen
silently again. No trading behavior, verdict, or threshold changes.
"""

from unittest.mock import MagicMock

from planning.trade_planner import TradePlanner
from candidate_lifecycle import deterministic_candidate_id


def _settings() -> MagicMock:
    s = MagicMock()
    s.planner_stop_buffer_bps = 8.0
    s.planner_ladder_steps = 3
    s.planner_min_rr = 1.2
    s.planner_min_rr_to_tp1 = 1.0
    s.planner_estimated_roundtrip_fee_bps = 12.0
    s.planner_minimum_net_edge_buffer_bps = 4.0
    s.planner_largest_loss_guard_bps = 85.0
    s.planner_min_live_notional_usdt = 10.0
    s.planner_max_notional_pct_of_equity = 35.0
    s.planner_max_notional_per_trade_usdt = 35.0
    s.account_equity_usdt = 100.0
    s.planner_adaptive_fallback_min_rr_to_tp1 = 0.70
    s.planner_strong_continuation_min_rr_to_tp1 = 0.75
    return s


def _candidate(strategy: str = "momentum_breakdown", direction: str = "SHORT") -> MagicMock:
    c = MagicMock()
    c.symbol = "DOGEUSDT"
    c.strategy = strategy
    c.direction = direction
    c.candidate_candle_open_timestamp_ms = 1_700_000_000_000
    c.candidate_id = deterministic_candidate_id(strategy, c.symbol, direction, c.candidate_candle_open_timestamp_ms)
    c.notes = []
    c.detection.entry_hint = 100.0
    c.detection.invalidation = 100.6 if direction == "SHORT" else 99.4
    c.detection.reclaim_level = 100.0
    c.detection.sweep_extreme = 100.3 if direction == "SHORT" else 99.7
    c.detection.reason_flags = []
    c.market.alignment = "aligned_bearish" if direction == "SHORT" else "aligned_bullish"
    c.market.volatility_rank = 10.0
    c.market.notes = []
    c.market.primary.atr_percent = 0.5
    c.market.primary.volume_ratio_20 = 1.2
    c.market.primary.trend = "bearish" if direction == "SHORT" else "bullish"
    c.market.primary.granularity = "5m"
    c.market.primary.latest_close = 100.0
    return c


def _score(total: float = 80.0) -> MagicMock:
    score = MagicMock()
    score.total = total
    score.verdict = "GO"
    score.reasons = []
    return score


def _risk() -> MagicMock:
    risk = MagicMock()
    risk.allowed = True
    risk.status = "GO"
    risk.reasons = []
    risk.account_risk_pct = 0.5
    risk.leverage = 3.0
    return risk


def test_gate_context_captures_entry_quality_from_market_notes_not_just_candidate_notes():
    """The TIAUSDT-shaped scenario: entry_quality lives ONLY on candidate.market.notes
    (as for liquidity_sweep_reversal / prearmed candidates), never on candidate.notes."""
    candidate = _candidate("momentum_breakdown", "SHORT")
    candidate.notes = ["fast_lane=true", "fast_lane_granularity=5m"]  # no entry_quality here
    candidate.market.notes = ["entry_quality_short=82", "pressure_score=60", "expansion_prob=80"]

    plan = TradePlanner(settings=_settings()).build(candidate, _score(90.0), _risk())

    assert plan.gate_context["candidate_notes"] == candidate.notes
    assert plan.gate_context["candidate_market_notes"] == candidate.market.notes
    assert plan.gate_context["candidate_detection_reason_flags"] == []
    # This is the crux: the gate's own recorded input must equal 82 (the
    # market-notes-only value), never 0 and never some other snapshot's number.
    assert plan.gate_context["planner_entry_quality"] == 82.0
    assert plan.gate_context["fast_lane"] is True
    assert plan.gate_context["fast_lane_granularity"] == "5m"
    assert plan.gate_context["source_timeframe"] == "5m"
    assert isinstance(plan.gate_context["master_entry_quality_passed"], bool)
    assert isinstance(plan.gate_context["master_entry_quality_reasons"], list)


def test_gate_context_reflects_reject_reasons_when_gate_fails():
    candidate = _candidate("momentum_breakdown", "SHORT")
    candidate.notes = []
    candidate.market.notes = ["entry_quality_short=40"]  # < 75 -> gate should fail

    plan = TradePlanner(settings=_settings()).build(candidate, _score(90.0), _risk())

    assert plan.gate_context["planner_entry_quality"] == 40.0
    assert plan.gate_context["master_entry_quality_passed"] is False
    assert any("entry_quality=40" in r for r in plan.gate_context["master_entry_quality_reasons"])


def test_gate_context_defaults_empty_for_hardcoded_observe_only_path():
    """adaptive_momentum_continuation is unconditionally BLOCKED before the gate
    ever runs -- gate_context should be the empty default, not fabricated data."""
    candidate = _candidate("adaptive_momentum_continuation", "LONG")
    plan = TradePlanner(settings=_settings()).build(candidate, _score(90.0), _risk())

    assert plan.verdict == "BLOCKED"
    assert plan.gate_context == {}


def test_gate_context_detection_reason_flags_are_captured_separately():
    candidate = _candidate("momentum_breakdown", "SHORT")
    candidate.notes = ["some_candidate_note=true"]
    candidate.market.notes = ["entry_quality_short=90"]
    candidate.detection.reason_flags = ["prearmed_expansion", "high_conviction"]

    plan = TradePlanner(settings=_settings()).build(candidate, _score(90.0), _risk())

    assert plan.gate_context["candidate_detection_reason_flags"] == ["prearmed_expansion", "high_conviction"]
    # None of the three sources are conflated with each other.
    assert plan.gate_context["candidate_notes"] == ["some_candidate_note=true"]
    assert plan.gate_context["candidate_market_notes"] == ["entry_quality_short=90"]
