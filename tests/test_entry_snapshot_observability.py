"""B1 pre-entry snapshot: units that say what they are, and no future in them.

The volatility tests are the load-bearing ones. `volatility_rank` is not being
fixed in B1, so the only thing standing between a later re-threshold and a
repeat of the same unit confusion is a test that states, in numbers, what the
legacy field actually holds.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

from execution.entry_snapshot import (
    EXPECTED_MOVE_MODEL_VERSION,
    FIELD_AVAILABILITY,
    VOLATILITY_RANK_LEGACY_SEMANTICS,
    atr_bps_from_percent,
    economic_hurdle_observability,
    missingness,
    volatility_observability,
)


# --- 10: atr_bps ------------------------------------------------------------


def test_atr_bps_converts_percent_to_basis_points():
    """atr_percent is (range/close)*100, so 0.18 means 0.18% means 18 bps."""
    assert atr_bps_from_percent(0.18) == pytest.approx(18.0)
    assert atr_bps_from_percent(0.51) == pytest.approx(51.0)
    assert atr_bps_from_percent(1.0) == pytest.approx(100.0)


def test_atr_bps_is_null_for_absent_or_invalid_input():
    for value in (None, "", "abc", -1.0, float("nan")):
        assert atr_bps_from_percent(value) is None


# --- 9: legacy field untouched ----------------------------------------------


def test_legacy_volatility_rank_function_is_not_modified():
    """B1 must not change how the live gate computes its input."""
    from market_features.engine import volatility_rank

    # The exact production behaviour, pinned by value, not by reading the source.
    assert volatility_rank(0.18) == pytest.approx(18.0)
    assert volatility_rank(0.51) == pytest.approx(51.0)
    assert volatility_rank(0) == 0.0
    assert volatility_rank(None) == 0.0
    assert volatility_rank(7.0) == pytest.approx(7.0)      # the >5 branch
    assert volatility_rank(200.0) == pytest.approx(100.0)  # clamp


def test_legacy_rank_equals_atr_bps_in_every_observed_range():
    """The documented equivalence, asserted rather than asserted-in-prose.

    Observed production range is 11-51. Across it the legacy 'rank' and ATR in
    basis points are the same number, which is why a threshold of 55 set as if
    it were a percentile rejected nothing.
    """
    from market_features.engine import volatility_rank

    for atr_percent in (0.11, 0.18, 0.25, 0.35, 0.51):
        assert volatility_rank(atr_percent) == pytest.approx(atr_bps_from_percent(atr_percent))


def test_volatility_snapshot_states_its_own_semantics():
    market = MagicMock()
    market.primary.atr_percent = 0.18
    market.volatility_rank = 18.0

    snapshot = volatility_observability(market)

    assert snapshot["atr_percent_raw"] == pytest.approx(0.18)
    assert snapshot["atr_bps"] == pytest.approx(18.0)
    assert snapshot["volatility_rank_legacy"] == pytest.approx(18.0)
    assert "NOT percentile rank" in snapshot["volatility_rank_legacy_semantics"]
    assert snapshot["volatility_rank_legacy_semantics"] == VOLATILITY_RANK_LEGACY_SEMANTICS


# --- economic hurdle --------------------------------------------------------


def test_hurdle_snapshot_records_current_gate_verbatim():
    notes = {
        "tp1_move_bps": 45.1,
        "spread_bps": 2.4,
        "estimated_roundtrip_fee_bps": 12.0,
        "minimum_net_edge_buffer_bps": 4.0,
        "minimum_tp1_move_bps": 30.4,
    }
    snapshot = economic_hurdle_observability(notes)

    assert snapshot["tp1_move_bps"] == pytest.approx(45.1)
    assert snapshot["minimum_tp1_move_bps"] == pytest.approx(30.4)
    assert snapshot["hurdle_margin_bps"] == pytest.approx(14.7)


def test_hurdle_snapshot_adds_drag_to_the_research_cost_only():
    """The production gate's own numbers must not move; the research one may."""
    notes = {"tp1_move_bps": 45.1, "spread_bps": 2.4,
             "estimated_roundtrip_fee_bps": 12.0, "minimum_tp1_move_bps": 30.4}

    without = economic_hurdle_observability(notes)
    with_drag = economic_hurdle_observability(notes, historical_execution_drag_bps=15.25)

    assert without["minimum_tp1_move_bps"] == with_drag["minimum_tp1_move_bps"]
    assert without["estimated_all_in_cost_bps"] == pytest.approx(14.4)
    assert with_drag["estimated_all_in_cost_bps"] == pytest.approx(29.65)


def test_expected_move_is_explicitly_absent_not_invented():
    snapshot = economic_hurdle_observability({})
    assert snapshot["expected_favorable_move_bps"] is None
    assert snapshot["expected_move_model_version"] == EXPECTED_MOVE_MODEL_VERSION == "NONE"


# --- 7: data quality --------------------------------------------------------


def test_missingness_separates_expected_from_defective():
    complete = {field: 1.0 for field in FIELD_AVAILABILITY}
    for field, availability in FIELD_AVAILABILITY.items():
        if availability == "UNKNOWN_ALLOWED":
            complete[field] = None

    report = missingness(complete)

    assert report["missing_unexpected"] == []
    assert "expected_favorable_move_bps" in report["missing_expected"]


def test_missingness_flags_a_field_that_should_always_be_there():
    snapshot = {field: 1.0 for field in FIELD_AVAILABILITY}
    snapshot["strategy"] = None

    assert "strategy" in missingness(snapshot)["missing_unexpected"]


# --- 14/15: observability cannot act, execution unchanged -------------------


@pytest.mark.parametrize("module_name", ["execution.entry_snapshot", "execution.entry_routing",
                                         "execution.entry_outcome"])
def test_observability_modules_cannot_place_or_cancel_anything(module_name):
    """No order verb may appear anywhere in the observability layer."""
    import importlib

    source = inspect.getsource(importlib.import_module(module_name))
    for forbidden in ("place_futures", "cancel_futures", "close_futures",
                      "place-order", "cancel-plan-order", "place_position_tpsl"):
        assert forbidden not in source, f"{module_name} references {forbidden}"


def test_snapshot_builder_never_raises_on_hostile_input():
    """A research field must not be able to abort an entry."""
    class Hostile:
        def __getattr__(self, name):
            raise RuntimeError("no attributes for you")

    # volatility_observability reads through getattr with defaults; a hostile
    # object must yield NULLs, not an exception reaching the caller.
    snapshot = volatility_observability(MagicMock(primary=None, volatility_rank=None))
    assert snapshot["atr_bps"] is None

    assert economic_hurdle_observability({})["tp1_move_bps"] is None
    assert missingness({})["missing_unexpected"]


def test_pre_entry_features_survives_a_malformed_plan():
    """A broken note degrades the snapshot; it must not reach the order path."""
    from execution.execution_service import ExecutionService

    plan = MagicMock()
    plan.strategy = "low_vol_reclaim"
    plan.direction = "LONG"
    plan.symbol = "BTCUSDT"
    plan.score = 92.0
    plan.risk_reward_ratio = 1.3
    plan.notes = ["atr_percent=not_a_number | volatility_rank=oops"]
    plan.reasons = []

    features = ExecutionService._pre_entry_features(plan)

    assert features["atr_bps"] is None
    assert features["volatility_rank_legacy"] is None
    assert features["symbol"] == "BTCUSDT"


def test_outcome_named_plan_note_is_dropped_not_raised(tmp_path):
    """A note named like an outcome must not abort an entry already decided."""
    from execution.entry_routing import EntryRoutingRecorder

    recorder = EntryRoutingRecorder(
        lifecycle_id="L1", plan_id="p1", candidate_id="c1", symbol="BTCUSDT",
        direction="LONG", planned_entry=100.0, intended_route="market",
        size_requested=1.0, log=MagicMock(), path=str(tmp_path / "r.jsonl"),
    )

    attached = recorder.safe_set_pre_entry_features(
        {"planner_entry_quality": 80.0, "net_pnl": -0.13}
    )

    assert attached is False  # a leak was detected
    assert recorder.pre_entry_features == {"planner_entry_quality": 80.0}
    assert "net_pnl" not in recorder.pre_entry_features


def test_pre_entry_features_builder_adds_research_fields_without_outcomes():
    from clients.schemas import TradePlan
    from execution.execution_service import ExecutionService

    plan = MagicMock(spec=TradePlan)
    plan.strategy = "low_vol_reclaim"
    plan.direction = "LONG"
    plan.symbol = "BTCUSDT"
    plan.score = 92.0
    plan.risk_reward_ratio = 1.3
    plan.notes = ["atr_percent=0.18 | volatility_rank=18.0 | spread_bps=2.4"]
    plan.reasons = ["tp1_move_bps=45.1 | minimum_tp1_move_bps=30.4 | estimated_roundtrip_fee_bps=12.0"]

    features = ExecutionService._pre_entry_features(plan)

    assert features["atr_bps"] == pytest.approx(18.0)
    assert features["volatility_rank_legacy"] == pytest.approx(18.0)
    assert features["hurdle_margin_bps"] == pytest.approx(14.7)
    assert features["expected_favorable_move_bps"] is None
    assert "missingness" in features
    for outcome in ("net_pnl", "realized_pnl", "max_favorable_excursion_pct", "closed_at"):
        assert outcome not in features
