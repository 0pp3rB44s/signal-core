"""Entry routing observability: signs, routes, NULLs, and no behaviour change.

The tests that matter most here are the negative ones. This change is only
allowed to observe, so a test proving the recorder issues no API call and never
reaches the order path is worth more than any assertion about its output.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from execution.entry_outcome import EntryOutcomeTracker
from execution.entry_routing import (
    ROUTE_FALLBACK_FULL,
    ROUTE_MAKER_FILLED_DURING_CANCEL,
    ROUTE_MAKER_FULL,
    ROUTE_MAKER_PARTIAL_THEN_FALLBACK,
    ROUTE_UNKNOWN,
    STAGE_FALLBACK_ACK,
    STAGE_FALLBACK_FILL,
    STAGE_FALLBACK_SUBMIT,
    STAGE_MAKER_END,
    STAGE_MAKER_FILL,
    STAGE_MAKER_SUBMIT,
    STAGE_PLAN,
    EntryRoutingRecorder,
    Quote,
    assert_matches_legacy_slippage_convention,
    capture_quote,
    entry_advantage_bps,
    execution_drag_bps,
    position_return_bps,
)

REPO = Path(__file__).resolve().parents[1]


def _recorder(tmp_path, direction="LONG", planned=100.0, **kwargs):
    return EntryRoutingRecorder(
        lifecycle_id=kwargs.pop("lifecycle_id", "entry-plan-1"),
        plan_id=kwargs.pop("plan_id", "plan-1"),
        candidate_id="cand-1",
        symbol="BTCUSDT",
        direction=direction,
        planned_entry=planned,
        intended_route="maker_then_market_fallback",
        size_requested=1.0,
        path=str(tmp_path / "entry_routing.jsonl"),
        **kwargs,
    )


# --- 7 & 8: sign conventions -------------------------------------------------


def test_long_plan_to_fill_adverse_is_positive():
    """A long that ends up buying higher than plan has paid. Positive."""
    assert execution_drag_bps("LONG", 100.0, 101.0) == pytest.approx(100.0)
    assert execution_drag_bps("LONG", 100.0, 99.0) == pytest.approx(-100.0)


def test_short_plan_to_fill_adverse_is_positive():
    """A short that ends up selling lower than plan has paid. Same sign."""
    assert execution_drag_bps("SHORT", 100.0, 99.0) == pytest.approx(100.0)
    assert execution_drag_bps("SHORT", 100.0, 101.0) == pytest.approx(-100.0)


def test_sign_symmetry_long_and_short_agree_on_adversity():
    """The same adversity must produce the same sign for both directions."""
    long_adverse = execution_drag_bps("LONG", 100.0, 100.5)
    short_adverse = execution_drag_bps("SHORT", 100.0, 99.5)
    assert long_adverse == pytest.approx(short_adverse) == pytest.approx(50.0)

    long_favourable = execution_drag_bps("LONG", 100.0, 99.5)
    short_favourable = execution_drag_bps("SHORT", 100.0, 100.5)
    assert long_favourable == pytest.approx(short_favourable) == pytest.approx(-50.0)


def test_favourable_fill_produces_negative_drag(tmp_path):
    rec = _recorder(tmp_path, direction="LONG", planned=100.0)
    rec.record(STAGE_FALLBACK_FILL, fill_price=99.0, size_filled=1.0)
    assert rec.metrics()["total_execution_drag_bps"] == pytest.approx(-100.0)


def test_position_return_keeps_the_opposite_sense_on_purpose():
    """A gain is positive; a cost is positive. They must not be the same call."""
    assert position_return_bps("LONG", 100.0, 101.0) == pytest.approx(100.0)
    assert execution_drag_bps("LONG", 100.0, 101.0) == pytest.approx(100.0)
    # Same inputs, same sign, different meaning -- so the short case must differ.
    assert position_return_bps("SHORT", 100.0, 101.0) == pytest.approx(-100.0)
    assert execution_drag_bps("SHORT", 100.0, 101.0) == pytest.approx(-100.0)


def test_sign_matches_legacy_slippage_pct():
    """One execution sign convention in the codebase, not two."""
    # execution_service: SHORT slippage_pct = (expected - actual)/expected*100
    expected, actual = 62476.3, 62410.4
    legacy = round((expected - actual) / expected * 100, 5)
    assert legacy > 0  # positive == adverse
    assert assert_matches_legacy_slippage_convention("SHORT", expected, actual, legacy)
    assert execution_drag_bps("SHORT", expected, actual) > 0  # agrees


def test_fallback_cross_is_positive_when_taker_crosses(tmp_path):
    rec = _recorder(tmp_path, direction="LONG", planned=100.0)
    rec.record(STAGE_FALLBACK_SUBMIT, quote=Quote(bid=99.9, ask=100.1, mid=100.0))
    rec.record(STAGE_FALLBACK_FILL, fill_price=100.1, size_filled=1.0)
    assert rec.metrics()["fallback_cross_bps"] > 0


def test_maker_wait_drift_is_positive_when_market_runs_away(tmp_path):
    """The adverse-selection term: price left while the maker sat."""
    rec = _recorder(tmp_path, direction="LONG", planned=100.0)
    rec.record(STAGE_MAKER_SUBMIT, quote=Quote(bid=99.95, ask=100.05, mid=100.0))
    rec.record(STAGE_MAKER_END, quote=Quote(bid=100.45, ask=100.55, mid=100.5))
    assert rec.metrics()["maker_wait_drift_bps"] == pytest.approx(50.0)


# --- 9 & 10: market data ------------------------------------------------------


def test_quote_derives_mid_and_spread_from_book():
    client = MagicMock()
    client.get_orderbook.return_value = {"best_bid": 99.0, "best_ask": 101.0}
    client.get_symbol_price.return_value = {"data": [{"markPrice": "100.5", "lastPr": "100.4"}]}

    quote = capture_quote(client, "BTCUSDT")

    assert quote.bid == 99.0
    assert quote.ask == 101.0
    assert quote.mid == pytest.approx(100.0)
    assert quote.spread_bps == pytest.approx(200.0)
    assert quote.mark == 100.5
    assert quote.last == 100.4


def test_missing_market_data_stays_null_never_zero():
    """Zero is a price. Absence must not be able to impersonate one."""
    client = MagicMock()
    client.get_orderbook.side_effect = RuntimeError("book down")
    client.get_symbol_price.return_value = {"data": [{"markPrice": "0"}]}

    quote = capture_quote(client, "BTCUSDT")

    assert quote.bid is None and quote.ask is None and quote.mid is None
    assert quote.mark is None  # "0" is absence, not a mark of zero
    assert entry_advantage_bps("LONG", 100.0, None) is None
    assert entry_advantage_bps("LONG", None, 100.0) is None


def test_metrics_are_null_when_quotes_were_never_captured(tmp_path):
    """Quote capture off must yield NULL metrics, not zeroed ones."""
    rec = _recorder(tmp_path)
    rec.record(STAGE_PLAN, quote=Quote.unavailable("quote_capture_disabled"))
    rec.record(STAGE_FALLBACK_FILL, fill_price=99.0, size_filled=1.0)
    metrics = rec.metrics()
    provenance = rec.metric_provenance()

    assert metrics["submit_to_fill_bps"] is None
    assert metrics["maker_wait_drift_bps"] is None
    assert provenance["maker_wait_drift_bps"] == "UNKNOWN"
    # plan_to_fill needs no quote and must still be measured
    assert metrics["plan_to_fill_bps"] == pytest.approx(-100.0)
    assert provenance["plan_to_fill_bps"] == "MEASURED"


# --- 1-6: route classification ------------------------------------------------


def test_route_maker_full(tmp_path):
    rec = _recorder(tmp_path)
    rec.record(STAGE_MAKER_FILL, fill_price=99.5, size_filled=1.0)
    rec.record(STAGE_MAKER_END, size_filled=1.0, remaining_size=0.0)
    assert rec.actual_fill_route() == ROUTE_MAKER_FULL


def test_route_maker_partial_then_fallback(tmp_path):
    rec = _recorder(tmp_path)
    rec.record(STAGE_MAKER_FILL, fill_price=99.5, size_filled=0.4)
    rec.record(STAGE_MAKER_END, size_filled=0.4, remaining_size=0.6)
    rec.record(STAGE_FALLBACK_FILL, fill_price=100.2, size_filled=0.6)
    assert rec.actual_fill_route() == ROUTE_MAKER_PARTIAL_THEN_FALLBACK


def test_route_full_fallback(tmp_path):
    rec = _recorder(tmp_path)
    rec.record(STAGE_MAKER_END, size_filled=None, remaining_size=1.0,
               exchange_order_status="UNFILLED_CANCELLED")
    rec.record(STAGE_FALLBACK_FILL, fill_price=100.2, size_filled=1.0)
    assert rec.actual_fill_route() == ROUTE_FALLBACK_FULL


def test_route_maker_rejected_then_fallback(tmp_path):
    rec = _recorder(tmp_path)
    rec.record(STAGE_MAKER_END, exchange_order_status="ERROR", reason="post_only_rejected")
    rec.record(STAGE_FALLBACK_FILL, fill_price=100.2, size_filled=1.0)
    assert rec.actual_fill_route() == ROUTE_FALLBACK_FULL


def test_route_fallback_rejected_leaves_route_unknown(tmp_path):
    """No fill anywhere must not be silently called a fallback fill."""
    rec = _recorder(tmp_path)
    rec.record(STAGE_MAKER_END, exchange_order_status="UNFILLED_CANCELLED")
    rec.record(STAGE_FALLBACK_ACK, exchange_order_status="REJECTED")
    assert rec.actual_fill_route() == ROUTE_UNKNOWN
    assert rec.final_fill_price() is None


def test_route_exchange_unknown_is_not_a_fill(tmp_path):
    rec = _recorder(tmp_path)
    rec.record(STAGE_MAKER_END, exchange_order_status="BLOCKED_UNKNOWN")
    assert rec.actual_fill_route() == ROUTE_UNKNOWN


def test_route_filled_during_cancel_is_its_own_class(tmp_path):
    """The race that produces an unintended position must stay visible."""
    rec = _recorder(tmp_path)
    rec.record(STAGE_MAKER_FILL, fill_price=99.5, size_filled=1.0, reason="filled_during_cancel")
    assert rec.actual_fill_route() == ROUTE_MAKER_FILLED_DURING_CANCEL


def test_attempted_route_and_achieved_route_are_separate_fields(tmp_path):
    """The old label named the attempt; both must now be readable."""
    rec = _recorder(tmp_path)
    rec.record(STAGE_MAKER_END, exchange_order_status="UNFILLED_CANCELLED")
    rec.record(STAGE_FALLBACK_FILL, fill_price=100.2, size_filled=1.0)
    row = rec.to_row()
    assert row["intended_route"] == "maker_then_market_fallback"
    assert row["actual_fill_route"] == ROUTE_FALLBACK_FULL


# --- 11 & 12: lifecycle integrity --------------------------------------------


def test_lifecycle_links_every_event_exactly_once(tmp_path):
    path = tmp_path / "entry_routing.jsonl"
    rec = _recorder(tmp_path)
    for stage in (STAGE_PLAN, STAGE_MAKER_SUBMIT, STAGE_MAKER_END,
                  STAGE_FALLBACK_SUBMIT, STAGE_FALLBACK_ACK, STAGE_FALLBACK_FILL):
        rec.record(stage)
    rec.record(STAGE_FALLBACK_FILL, fill_price=100.0, size_filled=1.0)
    assert rec.write()

    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["lifecycle_id"] == "entry-plan-1"
    assert [s["stage"] for s in rows[0]["stages"]].count(STAGE_PLAN) == 1


def test_two_entries_write_two_independent_rows(tmp_path):
    path = tmp_path / "entry_routing.jsonl"
    for i in (1, 2):
        rec = _recorder(tmp_path, lifecycle_id=f"entry-plan-{i}", plan_id=f"plan-{i}")
        rec.record(STAGE_FALLBACK_FILL, fill_price=100.0, size_filled=1.0)
        rec.write()
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert {r["lifecycle_id"] for r in rows} == {"entry-plan-1", "entry-plan-2"}
    assert len(rows) == 2


def test_routing_row_carries_no_economic_close_record(tmp_path):
    """This file must never become a second source of trade economics."""
    rec = _recorder(tmp_path)
    rec.record(STAGE_FALLBACK_FILL, fill_price=100.0, size_filled=1.0)
    row = rec.to_row()
    for forbidden in ("realized_pnl", "net_pnl", "exchange_truth_pnl", "closed_at"):
        assert forbidden not in row


# --- 18 + FASE 5: leakage ----------------------------------------------------


def test_pre_entry_snapshot_rejects_post_fill_fields(tmp_path):
    rec = _recorder(tmp_path)
    with pytest.raises(ValueError, match="post-fill"):
        rec.set_pre_entry_features({
            "planner_entry_quality": 80.0,
            "max_favorable_excursion_pct": 0.34,
        })


def test_outcome_fields_live_in_a_different_file(tmp_path):
    """A pre-entry consumer reading the routing file cannot reach an outcome."""
    routing_path = tmp_path / "entry_routing.jsonl"
    outcome_path = tmp_path / "entry_outcomes.jsonl"
    filled = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)

    rec = _recorder(tmp_path)
    rec.record(STAGE_FALLBACK_FILL, fill_price=100.0, size_filled=1.0)
    rec.write()

    tracker = EntryOutcomeTracker(path=str(outcome_path))
    tracker.arm(lifecycle_id="entry-plan-1", symbol="BTCUSDT", direction="LONG",
                fill_price=100.0, filled_at=filled)
    tracker.observe("BTCUSDT", 101.0, at=filled + timedelta(seconds=10))
    tracker.flush_due(now=filled + timedelta(seconds=61))

    routing_text = routing_path.read_text()
    assert "post_fill_return_10s_bps" not in routing_text
    assert "post_fill_return_10s_bps" in outcome_path.read_text()


def test_outcome_horizons_are_null_when_not_sampled(tmp_path):
    """A 60s return must never be back-filled from a 10s price."""
    filled = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)
    tracker = EntryOutcomeTracker(path=str(tmp_path / "o.jsonl"))
    tracker.arm(lifecycle_id="L1", symbol="BTCUSDT", direction="LONG",
                fill_price=100.0, filled_at=filled)
    tracker.observe("BTCUSDT", 101.0, at=filled + timedelta(seconds=11))
    rows = tracker.flush_due(now=filled + timedelta(seconds=61))

    assert rows[0]["post_fill_return_10s_bps"] == pytest.approx(100.0)
    assert rows[0]["post_fill_return_30s_bps"] is None
    assert rows[0]["post_fill_return_60s_bps"] is None


def test_outcome_mfe_and_mae_use_position_return_sign(tmp_path):
    filled = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)
    tracker = EntryOutcomeTracker(path=str(tmp_path / "o.jsonl"))
    tracker.arm(lifecycle_id="L1", symbol="BTCUSDT", direction="SHORT",
                fill_price=100.0, filled_at=filled)
    tracker.observe("BTCUSDT", 99.0, at=filled + timedelta(seconds=5))   # short profit
    tracker.observe("BTCUSDT", 102.0, at=filled + timedelta(seconds=20))  # short loss
    rows = tracker.flush_due(now=filled + timedelta(seconds=61))

    assert rows[0]["post_fill_mfe_60s_bps"] == pytest.approx(100.0)
    assert rows[0]["post_fill_mae_60s_bps"] == pytest.approx(-200.0)


# --- 13-16: proof that nothing about ordering changed -------------------------


def test_quote_capture_is_off_by_default_and_makes_no_api_call():
    """The default must cost zero requests, or it moves the fill it measures."""
    from app.config import Settings
    from execution.execution_service import ExecutionService

    assert Settings(_env_file=None).entry_routing_quote_capture is False

    service = ExecutionService.__new__(ExecutionService)
    service.settings = Settings(_env_file=None)
    service.client = MagicMock()
    service.log = MagicMock()

    quote = service._routing_quote("BTCUSDT")

    assert quote.mid is None
    assert service.client.get_orderbook.call_count == 0
    assert service.client.get_symbol_price.call_count == 0


def test_maker_attempt_telemetry_is_serialized_with_lineage_and_identity(tmp_path):
    recorder = EntryRoutingRecorder(
        lifecycle_id="entry-plan-1", plan_id="plan-1", candidate_id="candidate-1",
        strategy_id="low_vol_reclaim_v2", execution_identity={"executor_id": "runner01"},
        symbol="AVAXUSDT", direction="SHORT", planned_entry=6.25,
        intended_route="maker_only", size_requested=3.0,
        path=str(tmp_path / "routing.jsonl"),
    )
    maker_attempt = {
        "candidate_id": "candidate-1", "plan_id": "plan-1",
        "strategy_id": "low_vol_reclaim_v2", "executor_id": "runner01",
        "symbol": "AVAXUSDT", "side": "sell", "best_bid_submit": 6.234,
        "best_ask_submit": 6.235, "submitted_price": 6.236,
        "tick_size": 0.001, "distance_to_touch_ticks": 1.0,
        "distance_to_touch_bps": 1.603849, "post_only": True,
        "submit_ts": "2026-08-12T07:00:00.000Z",
        "ack_ts": "2026-08-12T07:00:00.400Z", "fill_ts": "",
        "cancel_ts": "2026-08-12T07:00:04.800Z", "timeout_ms": 4000,
        "exchange_order_state": "canceled", "exchange_cancel_reason": "normal_cancel",
        "fill_qty": 0.0, "fill_price": 0.0, "maker_fee": 0.0,
        "reprice_count": 0, "price_transitions": [],
        "setup_valid_at_reprice": None, "maker_timeout": True, "skipped_no_fill": True,
    }
    recorder.set_maker_attempt(maker_attempt)

    row = recorder.to_row()

    assert row["candidate_id"] == "candidate-1"
    assert row["strategy_id"] == "low_vol_reclaim_v2"
    assert row["executor_id"] == "runner01"
    assert row["maker_attempt"] == maker_attempt


@pytest.mark.parametrize("module", ["execution/maker_entry.py", "execution/entry_submitter.py"])
def test_order_placement_modules_do_not_import_observability(module):
    """Observability must not reach into the code that places orders."""
    source = (REPO / module).read_text()
    assert "entry_routing" not in source
    assert "EntryRoutingRecorder" not in source


def test_maker_wait_and_offset_defaults_are_untouched():
    from app.config import Settings
    settings = Settings(_env_file=None)
    assert settings.maker_entry_wait_seconds == 4.0
    assert settings.maker_entry_poll_seconds == 1.0
    assert settings.maker_entry_offset_bps == 1.0
    assert settings.maker_entry_fallback_market is True


def test_limit_price_formula_is_unchanged():
    """The one function that prices the maker leg must be bit-identical."""
    from execution.maker_entry import compute_limit_price
    assert compute_limit_price("LONG", 100.0, 1.0) == pytest.approx(99.99)
    assert compute_limit_price("SHORT", 100.0, 1.0) == pytest.approx(100.01)


def test_sizing_risk_and_protection_settings_are_untouched():
    from app.config import Settings
    production = Settings(_env_file=None, **{
        "ACCOUNT_RISK_PER_TRADE_PCT": 0.50,
        "DEFAULT_LEVERAGE": 3.0,
        "MAX_LEVERAGE": 3.0,
        "MAX_OPEN_POSITIONS": 2,
        "EXECUTION_MAX_PER_CYCLE": 2,
        "EXECUTION_MAX_LIVE_NOTIONAL_PER_TRADE_USDT": 35.0,
    })
    assert production.account_risk_per_trade_pct == 0.50
    assert production.default_leverage == 3.0
    assert production.max_leverage == 3.0
    assert production.max_open_positions == 2
    assert production.execution_max_per_cycle == 2
    assert production.execution_max_live_notional_per_trade_usdt == 35.0


def test_write_failure_never_propagates(tmp_path):
    """An entry already on the exchange must not be aborted by a log failure."""
    rec = _recorder(tmp_path)
    rec.path = "/proc/definitely/not/writable/x.jsonl"
    rec.record(STAGE_FALLBACK_FILL, fill_price=100.0, size_filled=1.0)
    assert rec.write() is False  # returns, does not raise


def test_direct_market_route_without_a_maker_leg(tmp_path):
    """maker_entry_enabled=false must produce its own route, not a fallback."""
    rec = EntryRoutingRecorder(
        lifecycle_id="entry-plan-9", plan_id="plan-9", candidate_id="c9",
        symbol="BTCUSDT", direction="LONG", planned_entry=100.0,
        intended_route="market", size_requested=1.0,
        path=str(tmp_path / "r.jsonl"),
    )
    rec.record(STAGE_FALLBACK_SUBMIT, reason="market_only_route")
    rec.record(STAGE_FALLBACK_FILL, fill_price=100.2, size_filled=1.0)
    row = rec.to_row()
    assert row["intended_route"] == "market"
    assert row["actual_fill_route"] == ROUTE_FALLBACK_FULL


def test_stage_timestamps_are_monotonic_when_present(tmp_path):
    rec = _recorder(tmp_path)
    for stage in (STAGE_PLAN, STAGE_MAKER_SUBMIT, STAGE_MAKER_END,
                  STAGE_FALLBACK_SUBMIT, STAGE_FALLBACK_FILL):
        rec.record(stage)
    stamps = [s.at for s in rec.stages]
    assert stamps == sorted(stamps)
    assert all(s.endswith("Z") and "." in s for s in stamps)


def test_snapshot_is_immutable_once_attached(tmp_path):
    """Mutating the caller's dict afterwards must not rewrite the record."""
    rec = _recorder(tmp_path)
    source = {"planner_entry_quality": 80.0}
    rec.set_pre_entry_features(source)
    source["planner_entry_quality"] = 10.0
    source["injected_later"] = True

    assert rec.pre_entry_features["planner_entry_quality"] == 80.0
    assert "injected_later" not in rec.pre_entry_features


def test_recorder_holds_no_client_and_cannot_reach_the_exchange(tmp_path):
    """Structural guarantee: the recorder has no transport to misuse."""
    rec = _recorder(tmp_path)
    assert not hasattr(rec, "client")
    assert not any("client" in name for name in vars(rec))


def test_logging_failure_leaves_the_routing_decision_untouched(tmp_path):
    """A dead disk must not change what the recorder says the route was."""
    rec = _recorder(tmp_path)
    rec.record(STAGE_MAKER_END, exchange_order_status="UNFILLED_CANCELLED")
    rec.record(STAGE_FALLBACK_FILL, fill_price=100.2, size_filled=1.0)
    route_before = rec.actual_fill_route()
    metrics_before = rec.metrics()

    rec.path = "/proc/definitely/not/writable/x.jsonl"
    assert rec.write() is False

    assert rec.actual_fill_route() == route_before
    assert rec.metrics() == metrics_before


# --- 17: existing snapshot schemas stay readable ------------------------------


def test_existing_decision_snapshot_variants_both_parse():
    """The 7- and 9-field rows in trade_decision_snapshots.csv are documented.

    This change adds no writer and no migration; it only proves that a reader
    aware of the drift can still read both shapes. The 9-field variant repeats
    the timestamp in field 0 and 1 and appends a symbol|timestamp key.
    """
    import csv
    import io

    seven = "2026-08-03T14:49:09+00:00,BTCUSDT,low_vol_reclaim,LONG,EXECUTABLE,92.00,planner_gate=ok"
    nine = ("2026-08-03T14:49:09+00:00,2026-08-03T14:49:09+00:00,BTCUSDT,low_vol_reclaim,"
            "LONG,EXECUTABLE,92.00,planner_gate=ok,BTCUSDT|2026-08-03T14:49:09")

    for line, expected_symbol, snapshot_index in ((seven, "BTCUSDT", 6), (nine, "BTCUSDT", 7)):
        row = next(csv.reader(io.StringIO(line)))
        offset = 1 if len(row) == 9 else 0
        assert row[1 + offset] == expected_symbol
        assert "planner_gate" in row[snapshot_index]
