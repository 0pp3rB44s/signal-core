"""STRATEGY_HARDENING_2026-09-13: enforce the already-computed
_master_entry_quality_gate() result for the three strategies that previously
had no strategy-specific entry-quality check of their own in their verdict
path (momentum_breakout, momentum_breakdown, trend_continuation).

Regression evidence this pins: Sunday-shadow forward-paper trades opened
2026-09-12/13 for these strategies with entry_quality well below 75 or a
missing continuation-structure confirmation (exhaustion_risk=True, "late
long entry near candle high") were previously EXECUTABLE and, in every one
of the 5 real cases so far, net-negative. low_vol_reclaim_v2 and
adaptive_momentum_continuation are untouched -- they already have their own
gate (day_defensive_block / the hard adaptive checks below) -- so this suite
also proves the new code path leaves them alone.
"""

from unittest.mock import MagicMock

from planning.trade_planner import TradePlanner, _MASTER_GATE_ENFORCED_STRATEGIES
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


def _candidate(strategy: str, direction: str, notes: list[str]) -> MagicMock:
    c = MagicMock()
    c.symbol = "DOGEUSDT"
    c.strategy = strategy
    c.direction = direction
    c.candidate_candle_open_timestamp_ms = 1_700_000_000_000
    c.candidate_id = deterministic_candidate_id(strategy, c.symbol, direction, c.candidate_candle_open_timestamp_ms)
    c.notes = notes
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


HEALTHY_MOMENTUM_NOTES = [
    "entry_quality_long=90.0",
    "pressure_score=60.0",
    "expansion_prob=80.0",
    "participation_score=1.00",
    "followthrough_volume_ratio=0.50",
    "volume_ratio=1.00",
]

WEAK_MOMENTUM_NOTES = [
    # Exactly the pattern from the two real momentum_breakout losers: a late,
    # exhausted entry with entry_quality well below the 75 hard threshold.
    "entry_quality_long=45.0",
    "pressure_score=55.0",
    "expansion_prob=55.0",
    "participation_score=1.50",
    "followthrough_volume_ratio=1.50",
    "volume_ratio=0.62",
]

HEALTHY_CONTINUATION_NOTES = [
    "continuation_quality=true",
    "structure_ok=true",
    "pressure_ok=true",
    "participation_ok=true",
    "pressure_score=60.0",
    "participation_score=1.20",
    "entry_quality_short=90.0",
]


def test_scope_is_pinned_to_exactly_the_three_unprotected_strategies():
    assert _MASTER_GATE_ENFORCED_STRATEGIES == frozenset(
        {"momentum_breakout", "momentum_breakdown", "trend_continuation"}
    )


def test_momentum_breakout_with_weak_entry_quality_is_now_blocked():
    planner = TradePlanner(settings=_settings())
    plan = planner.build(_candidate("momentum_breakout", "LONG", WEAK_MOMENTUM_NOTES), _score(), _risk())
    assert plan.verdict == "BLOCKED"
    assert any("master_entry_quality_gate_enforced_momentum_breakout" in n for n in plan.notes)
    assert any("master_entry_quality_would_have_blocked=true" in n for n in plan.notes)


def test_momentum_breakdown_with_weak_entry_quality_is_now_blocked():
    planner = TradePlanner(settings=_settings())
    plan = planner.build(_candidate("momentum_breakdown", "SHORT", WEAK_MOMENTUM_NOTES), _score(), _risk())
    assert plan.verdict == "BLOCKED"
    assert any("master_entry_quality_gate_enforced_momentum_breakdown" in n for n in plan.notes)


def test_momentum_breakout_with_healthy_entry_quality_is_not_blocked_by_the_new_clause():
    """The fix must not over-block genuinely clean setups. The new clause lives
    strictly inside the pre-existing `if verdict == "EXECUTABLE" and not
    master_gate_passed:` block, so it can only ever fire when the gate has
    already failed -- proven directly on the gate function itself, avoiding
    the unrelated geometry/sizing machinery `build()` also runs (see
    test_trade_planner_single_tp.py for that separately-owned contract)."""
    passed, reasons = TradePlanner._master_entry_quality_gate(
        candidate=_candidate("momentum_breakout", "LONG", HEALTHY_MOMENTUM_NOTES),
        score_total=80.0, entry_quality=90.0, pressure_score=60.0, expansion_prob=80.0,
        participation_score=1.00, followthrough_volume_ratio=0.50, volume_ratio=1.00,
    )
    assert passed, reasons


def test_trend_continuation_missing_structure_confirmation_is_now_blocked():
    planner = TradePlanner(settings=_settings())
    plan = planner.build(_candidate("trend_continuation", "SHORT", []), _score(), _risk())
    assert plan.verdict == "BLOCKED"
    assert any("master_entry_quality_gate_enforced_trend_continuation" in n for n in plan.notes)


def test_trend_continuation_with_full_confirmation_is_not_blocked_by_the_new_clause():
    passed, reasons = TradePlanner._master_entry_quality_gate(
        candidate=_candidate("trend_continuation", "SHORT", HEALTHY_CONTINUATION_NOTES),
        score_total=80.0, entry_quality=90.0, pressure_score=60.0, expansion_prob=80.0,
        participation_score=1.20, followthrough_volume_ratio=0.50, volume_ratio=1.00,
    )
    assert passed, reasons


def test_low_vol_reclaim_is_not_touched_by_the_new_enforced_set():
    """low_vol_reclaim_v2 already has its own day_defensive_block; this change
    must not add a second, redundant enforcement path for it."""
    planner = TradePlanner(settings=_settings())
    plan = planner.build(_candidate("low_vol_reclaim_v2", "LONG", WEAK_MOMENTUM_NOTES), _score(), _risk())
    assert not any("master_entry_quality_gate_enforced" in n for n in plan.notes)


def test_adaptive_momentum_continuation_is_not_touched_by_the_new_enforced_set():
    """This strategy already has its own hard ADAPTIVE_ENTRY_QUALITY block."""
    planner = TradePlanner(settings=_settings())
    plan = planner.build(
        _candidate("adaptive_momentum_continuation", "LONG", HEALTHY_MOMENTUM_NOTES), _score(), _risk()
    )
    assert not any("master_entry_quality_gate_enforced" in n for n in plan.notes)
