"""Ranked-plan telemetry must report the ranking that actually traded.

The failure this guards against is not a crash. It is telemetry that drifts
away from the selector and quietly answers a different question than the one
the reader thinks they asked -- which is how a reconstructed ranking already
went wrong twice offline.

Every ordering test therefore asserts against ``select_execution_winner``'s own
output rather than against a hand-written expected order.
"""

from __future__ import annotations

import json

import pytest

from execution.portfolio_selector import select_execution_winner
from telemetry.ranked_plans import RankedPlanLogger


class _Plan:
    def __init__(self, plan_id, symbol="BTCUSDT", direction="LONG",
                 strategy="low_vol_reclaim", score=92.0, notes=None):
        self.plan_id = plan_id
        self.candidate_id = "cand_" + plan_id
        self.symbol = symbol
        self.direction = direction
        self.strategy = strategy
        self.score = score
        self.verdict = "EXECUTABLE"
        self.notes = notes or []
        self.reasons = []
        self.entry_prices = [100.0]
        self.stop_loss = 99.0
        self.take_profits = [101.0]


def _select(plans, scores=None):
    return select_execution_winner(plans, execution_scores=scores or {})


def _row(plans, scores=None):
    selection = _select(plans, scores)
    return RankedPlanLogger().build_row("scan-1", "2026-08-10T00:00:00Z", selection), selection


def _ids(row):
    return [p["plan_id"] for p in row["plans"]]


# --- the core invariant: telemetry order == selector order ------------------


def test_single_plan_cycle():
    row, sel = _row([_Plan("a")])
    assert _ids(row) == [sel.winner.plan_id]
    assert row["plans"][0]["rank"] == 0
    assert row["plans"][0]["selected"] is True
    assert row["ranked_count"] == 1


def test_multi_plan_same_strategy():
    plans = [_Plan("a", symbol="BTCUSDT"), _Plan("b", symbol="AAAUSDT")]
    row, sel = _row(plans, {"BTCUSDT": 90.0, "AAAUSDT": 80.0})
    assert _ids(row) == [r.plan.plan_id for r in sel.ranked]
    assert row["plans"][0]["plan_id"] == sel.winner.plan_id


def test_multi_strategy_true_tie_matches_selector():
    """All economic keys equal -> the selector falls through to strategy name.

    ``planner_entry_quality`` must be present and equal: that is what makes the
    tie real. When the marker is absent, ``_setup_quality`` falls back to
    ``plan.score`` and the higher-scoring strategy wins on key 3 instead --
    which is correct, and not the case this test is about. In production the
    marker is present and constant at 85.00, so this is the live condition.
    """
    eq = ["planner_entry_quality=85"]
    plans = [
        _Plan("t", strategy="trend_continuation", score=108.0, notes=eq),
        _Plan("l", strategy="low_vol_reclaim", score=92.0, notes=eq),
    ]
    row, sel = _row(plans, {"BTCUSDT": 90.0})
    assert _ids(row) == [r.plan.plan_id for r in sel.ranked]
    # Telemetry must not "helpfully" reorder by the higher plan score.
    assert row["plans"][0]["strategy"] == "low_vol_reclaim"
    assert row["plans"][0]["diagnostic"]["plan_score"] == 92.0
    assert row["plans"][1]["diagnostic"]["plan_score"] == 108.0


def test_differing_execution_score():
    plans = [_Plan("a", symbol="AAAUSDT"), _Plan("b", symbol="BBBUSDT")]
    row, sel = _row(plans, {"AAAUSDT": 10.0, "BBBUSDT": 99.0})
    assert _ids(row) == [r.plan.plan_id for r in sel.ranked]
    assert row["plans"][0]["symbol"] == "BBBUSDT"


def test_differing_setup_quality():
    plans = [
        _Plan("low", notes=["planner_entry_quality=60"]),
        _Plan("high", notes=["planner_entry_quality=95"]),
    ]
    row, sel = _row(plans, {"BTCUSDT": 90.0})
    assert _ids(row) == [r.plan.plan_id for r in sel.ranked]
    assert row["plans"][0]["plan_id"] == "high"


def test_differing_spread():
    plans = [
        _Plan("wide", notes=["spread_bps=9.0"]),
        _Plan("tight", notes=["spread_bps=0.5"]),
    ]
    row, sel = _row(plans, {"BTCUSDT": 90.0})
    assert _ids(row) == [r.plan.plan_id for r in sel.ranked]
    assert row["plans"][0]["plan_id"] == "tight"


def test_alphabetical_deterministic_fallback():
    plans = [_Plan("zz", strategy="zeta"), _Plan("aa", strategy="alpha")]
    row, sel = _row(plans, {"BTCUSDT": 90.0})
    assert _ids(row) == [r.plan.plan_id for r in sel.ranked]
    assert row["plans"][0]["strategy"] == "alpha"


# --- ranking keys are the selector's, not a recomputation ------------------


def test_ranking_keys_are_taken_from_the_selector():
    plans = [_Plan("a", notes=["planner_entry_quality=77", "spread_bps=1.25"])]
    row, sel = _row(plans, {"BTCUSDT": 88.5})
    keys = row["plans"][0]["ranking_keys"]
    ranked = sel.ranked[0]
    assert keys["execution_score"] == ranked.execution_score
    assert keys["expectancy"] == ranked.expectancy
    assert keys["setup_quality"] == ranked.setup_quality
    assert keys["liquidity_spread_quality"] == ranked.liquidity_spread_quality


def test_rejected_plans_are_recorded_with_their_reason():
    bad = _Plan("bad")
    bad.verdict = "BLOCKED"
    row, _ = _row([_Plan("ok"), bad], {"BTCUSDT": 90.0})
    assert row["rejected_count"] == 1
    assert row["rejected"][0]["reason"] == "not_executable"
    assert "bad" not in _ids(row)


# --- direction-specific diagnostics ----------------------------------------


def test_selected_direction_entry_quality_is_not_a_maximum():
    notes = ["entry_quality_long=100", "entry_quality_short=40"]
    row, _ = _row([_Plan("s", direction="SHORT", notes=notes)], {"BTCUSDT": 90.0})
    d = row["plans"][0]["diagnostic"]
    assert d["entry_quality_long"] == 100.0
    assert d["entry_quality_short"] == 40.0
    assert d["selected_direction_entry_quality"] == 40.0


def test_selected_direction_entry_quality_for_long():
    notes = ["entry_quality_long=70", "entry_quality_short=100"]
    row, _ = _row([_Plan("l", direction="LONG", notes=notes)], {"BTCUSDT": 90.0})
    assert row["plans"][0]["diagnostic"]["selected_direction_entry_quality"] == 70.0


@pytest.mark.parametrize("notes", [[], ["entry_quality_long=abc"], ["unrelated=1"]])
def test_missing_or_malformed_diagnostics_are_none_not_zero(notes):
    row, _ = _row([_Plan("x", notes=notes)], {"BTCUSDT": 90.0})
    assert row["plans"][0]["diagnostic"]["entry_quality_long"] is None


# --- writing ----------------------------------------------------------------


def test_append_writes_one_json_line_per_cycle(tmp_path):
    path = tmp_path / "ranked_plans.jsonl"
    logger = RankedPlanLogger(path=path)
    sel = _select([_Plan("a"), _Plan("b", symbol="AAAUSDT")], {"BTCUSDT": 9.0, "AAAUSDT": 9.0})
    assert logger.append("s1", "2026-08-10T00:00:00Z", sel) is True
    assert logger.append("s2", "2026-08-10T00:01:00Z", sel) is True
    lines = path.read_text().strip().split("\n")
    assert len(lines) == 2
    parsed = json.loads(lines[0])
    assert parsed["scan_id"] == "s1"
    assert len(parsed["plans"]) == 2


def test_write_failure_never_raises_into_the_caller(tmp_path):
    # A directory where the file should be: open() fails, trading must not.
    path = tmp_path / "blocked.jsonl"
    path.mkdir()
    sel = _select([_Plan("a")], {"BTCUSDT": 9.0})
    assert RankedPlanLogger(path=path).append("s", "t", sel) is False


def test_row_is_json_serialisable_without_custom_encoder():
    row, _ = _row([_Plan("a", notes=["planner_entry_quality=80"])], {"BTCUSDT": 90.0})
    json.loads(json.dumps(row))


# --- participation_score propagation ---------------------------------------
# The value already exists and is authoritative; only the CSV column was empty.


class _MC:
    """Parser under test, without touching the file-writing machinery."""

    @staticmethod
    def parse(notes):
        from telemetry.market_context_logger import MarketContextLogger
        return MarketContextLogger.__new__(MarketContextLogger)._parse_notes(notes)


def test_participation_score_key_value_form_is_parsed():
    """The form the producers actually emit. Was returning '' for all 3422 rows."""
    assert _MC.parse(["participation_score=15.0"])["participation_score"] == 15.0


def test_participation_score_legacy_space_form_still_parsed():
    assert _MC.parse(["participation_score 15.0"])["participation_score"] == 15.0


def test_participation_score_absent_stays_absent():
    assert _MC.parse(["unrelated=1"]).get("participation_score") is None


def test_participation_score_malformed_does_not_crash():
    assert _MC.parse(["participation_score=n/a"])["participation_score"] == ""


def test_participation_score_value_is_taken_verbatim():
    """No rescaling, no clamping, no direction blending -- echo the producer."""
    for raw in ("10.0", "12.34", "15.0", "0.0"):
        assert _MC.parse([f"participation_score={raw}"])["participation_score"] == float(raw)


def test_plan_score_decides_when_planner_entry_quality_is_absent():
    """The complement of the tie case: without the marker, plan.score is key 3."""
    plans = [
        _Plan("t", strategy="trend_continuation", score=108.0),
        _Plan("l", strategy="low_vol_reclaim", score=92.0),
    ]
    row, sel = _row(plans, {"BTCUSDT": 90.0})
    assert _ids(row) == [r.plan.plan_id for r in sel.ranked]
    assert row["plans"][0]["strategy"] == "trend_continuation"
