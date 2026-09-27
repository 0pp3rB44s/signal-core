"""B1 outcome linkage: one research row per economically closed lifecycle.

The wiring sits after ``write_economic_close``, so most of what needs proving is
that it inherits the close path's guarantees rather than inventing its own: no
row on a refused duplicate, no row on a provisional close, one row when truth
finally arrives, and a dead disk that changes nothing about the close.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from execution.close_reconciler import CloseReconciliationUnavailable
from execution.closed_lifecycle_recorder import (
    record_closed_lifecycle,
    record_known_economics_close,
)
from execution.entry_outcome import CLOSE_OUTCOME_PATH, build_close_outcome, emit_close_outcome

OPEN_MS = 1_785_700_000_000
FLAT = {"status": "CLOSED", "flatness": "FLAT", "remaining_size": 0.0}
FIELDS = ["event_type", "symbol", "direction", "opened_at", "net_pnl", "fees",
          "sync_source", "position_lifecycle_id", "confirmed_position_size"]


def hist(pid="P1"):
    return {"symbol": "TRXUSDT", "holdSide": "short", "ctime": OPEN_MS,
            "utime": OPEN_MS + 5_400_000, "openTotalPos": "77", "closeTotalPos": "77",
            "openAvgPrice": "0.32579", "closeAvgPrice": "0.32626", "pnl": "-0.03619",
            "netProfit": "-0.06631471", "openFee": "-0.01505149",
            "closeFee": "-0.01507321", "totalFunding": "0", "positionId": pid}


def pos(lifecycle="pos-abc"):
    return {
        "symbol": "TRXUSDT", "direction": "SHORT", "position_lifecycle_id": lifecycle,
        "confirmed_position_size": "77", "opened_at_ms": OPEN_MS,
        "opened_at": "2026-08-03T06:01:36", "plan_id": "plan-1", "candidate_id": "cand-1",
        "planned_avg_entry": 0.32700, "exchange_avg_entry": 0.32579,
        "max_favorable_excursion_pct": 0.21, "max_adverse_excursion_pct": -0.34,
        "trade_duration_seconds": 5400.0, "closed_reason": "stop_loss",
        "entry_via": "maker_then_market_fallback",
    }


def dataset(tmp_path, rows=()):
    path = tmp_path / "trade_dataset_v2.csv"
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in FIELDS})
    return path


class Sink:
    def __init__(self):
        self.written = []

    def write(self, position, econ):
        self.written.append((position, econ))


def run_close(tmp_path, monkeypatch, outcome_path, *, fetch=None, position=None, rows=()):
    """Run the real close path with the outcome file redirected to tmp."""
    import execution.entry_outcome as outcome_module

    real_emit = outcome_module.emit_close_outcome
    monkeypatch.setattr(
        outcome_module, "emit_close_outcome",
        lambda p, e, **kw: real_emit(p, e, **{**kw, "path": str(outcome_path)}),
    )
    sink = Sink()
    result = record_closed_lifecycle(
        position=position or pos(),
        close_result=FLAT,
        dataset_path=str(dataset(tmp_path, rows)),
        fetch_history=fetch or (lambda: [hist()]),
        write_economic_close=sink.write,
        retire_provisional=lambda _p: None,
        reconcile=lambda **kw: __import__(
            "execution.close_reconciler", fromlist=["x"]
        ).reconcile_close(sleep=lambda _: None, **kw),
    )
    return result, sink


def rows_in(path):
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


# --- 1 & 2: linkage ---------------------------------------------------------


def test_exchange_confirmed_close_produces_one_linked_outcome(tmp_path, monkeypatch):
    out_path = tmp_path / "entry_outcomes.jsonl"
    result, sink = run_close(tmp_path, monkeypatch, out_path)

    assert result == "RECONCILED"
    assert len(sink.written) == 1

    rows = rows_in(out_path)
    assert len(rows) == 1
    assert rows[0]["lifecycle_id"] == "pos-abc"
    assert rows[0]["outcome_source"] == "reconciled"


def test_outcome_carries_authoritative_economics_not_a_recomputation(tmp_path, monkeypatch):
    out_path = tmp_path / "entry_outcomes.jsonl"
    _result, sink = run_close(tmp_path, monkeypatch, out_path)

    _position, econ = sink.written[0]
    row = rows_in(out_path)[0]

    assert row["net_pnl"] == pytest.approx(float(econ["net_pnl"]))
    assert row["fees"] == pytest.approx(float(econ["fees"]))
    assert row["gross_pnl"] == pytest.approx(float(econ["gross_pnl"]))
    assert row["funding"] == pytest.approx(float(econ["funding"]))


def test_outcome_links_pre_entry_snapshot_by_lifecycle_id(tmp_path):
    """The join key must be the same string on both sides."""
    from execution.entry_routing import EntryRoutingRecorder

    routing_path = tmp_path / "entry_routing.jsonl"
    recorder = EntryRoutingRecorder(
        lifecycle_id="pos-abc", plan_id="plan-1", candidate_id="cand-1",
        symbol="TRXUSDT", direction="SHORT", planned_entry=0.327,
        intended_route="market", size_requested=77.0, path=str(routing_path),
    )
    recorder.record("fallback_fill", fill_price=0.32579, size_filled=77.0)
    recorder.write()

    outcome = build_close_outcome(pos(), {"net_pnl": -0.066}, "reconciled")
    routing_row = rows_in(routing_path)[0]

    assert outcome["lifecycle_id"] == routing_row["lifecycle_id"] == "pos-abc"


def test_outcome_execution_drag_uses_the_adverse_positive_convention():
    """SHORT filled below plan is adverse, so drag must be positive."""
    row = build_close_outcome(pos(), {}, "reconciled")
    assert row["actual_execution_drag_bps"] > 0
    assert row["actual_plan_to_fill_bps"] == row["actual_execution_drag_bps"]


# --- 3, 4, 5: idempotency ---------------------------------------------------


def test_duplicate_close_writes_no_second_outcome(tmp_path, monkeypatch):
    """The upstream dedup refuses the write; telemetry must not run anyway."""
    out_path = tmp_path / "entry_outcomes.jsonl"
    existing = {"event_type": "CLOSE", "symbol": "TRXUSDT", "direction": "SHORT",
                "opened_at": "2026-08-03T06:01:36", "net_pnl": "-0.066", "fees": "0.03",
                "sync_source": "bitget_position_history", "position_lifecycle_id": "pos-abc",
                "confirmed_position_size": "77"}

    result, sink = run_close(tmp_path, monkeypatch, out_path, rows=[existing])

    assert result == "ALREADY"
    assert sink.written == []
    assert rows_in(out_path) == []


def test_recovery_close_is_idempotent_across_runs(tmp_path, monkeypatch):
    """Second pass over an already-reconciled lifecycle adds nothing."""
    out_path = tmp_path / "entry_outcomes.jsonl"
    first, _sink = run_close(tmp_path, monkeypatch, out_path)
    assert first == "RECONCILED"
    assert len(rows_in(out_path)) == 1

    written_row = rows_in(out_path)[0]
    existing = {"event_type": "CLOSE", "symbol": "TRXUSDT", "direction": "SHORT",
                "opened_at": "2026-08-03T06:01:36", "net_pnl": "-0.066", "fees": "0.03",
                "sync_source": "bitget_position_history", "position_lifecycle_id": "pos-abc",
                "confirmed_position_size": "77"}
    second, _ = run_close(tmp_path, monkeypatch, out_path, rows=[existing])

    assert second == "ALREADY"
    assert rows_in(out_path) == [written_row]


def test_provisional_close_writes_no_outcome_then_upgrade_writes_exactly_one(
    tmp_path, monkeypatch
):
    """No economics yet means no research row; the row appears when truth does."""
    out_path = tmp_path / "entry_outcomes.jsonl"

    provisional, sink = run_close(
        tmp_path, monkeypatch, out_path,
        fetch=lambda: (_ for _ in ()).throw(CloseReconciliationUnavailable("pending")),
    )
    assert provisional == "PROVISIONAL"
    assert sink.written == []
    assert rows_in(out_path) == []

    upgraded, sink2 = run_close(tmp_path, monkeypatch, out_path)
    assert upgraded == "RECONCILED"
    assert len(sink2.written) == 1
    assert len(rows_in(out_path)) == 1


def test_known_economics_path_also_emits_exactly_one(tmp_path, monkeypatch):
    import execution.entry_outcome as outcome_module

    out_path = tmp_path / "entry_outcomes.jsonl"
    real_emit = outcome_module.emit_close_outcome
    monkeypatch.setattr(
        outcome_module, "emit_close_outcome",
        lambda p, e, **kw: real_emit(p, e, **{**kw, "path": str(out_path)}),
    )
    sink = Sink()
    result = record_known_economics_close(
        position=pos(), economics={"net_pnl": -0.066, "fees": 0.03, "gross_pnl": -0.036,
                                   "funding": 0.0, "exchange_position_id": "P1"},
        dataset_path=str(dataset(tmp_path)), write_economic_close=sink.write,
    )

    assert result == "RECORDED"
    rows = rows_in(out_path)
    assert len(rows) == 1
    assert rows[0]["outcome_source"] == "known_economics"


# --- 6: failure isolation ---------------------------------------------------


def test_outcome_write_failure_leaves_the_close_identical(tmp_path, monkeypatch):
    """A dead telemetry disk must not change the close outcome or its economics."""
    import execution.entry_outcome as outcome_module

    healthy_path = tmp_path / "ok.jsonl"
    healthy_result, healthy_sink = run_close(tmp_path, monkeypatch, healthy_path)

    real_emit = outcome_module.emit_close_outcome
    monkeypatch.setattr(
        outcome_module, "emit_close_outcome",
        lambda p, e, **kw: real_emit(
            p, e, **{**kw, "path": "/proc/definitely/not/writable/x.jsonl"}
        ),
    )
    broken_result, broken_sink = run_close(tmp_path, monkeypatch, tmp_path / "unused.jsonl")

    assert broken_result == healthy_result == "RECONCILED"
    assert len(broken_sink.written) == len(healthy_sink.written) == 1
    assert broken_sink.written[0][1] == healthy_sink.written[0][1]


def test_outcome_telemetry_raising_cannot_escape_the_close(tmp_path, monkeypatch):
    """Even a telemetry module that throws must not reach the close path."""
    import execution.entry_outcome as outcome_module

    def explode(*_args, **_kwargs):
        raise RuntimeError("telemetry is on fire")

    monkeypatch.setattr(outcome_module, "emit_close_outcome", explode)
    sink = Sink()
    result = record_closed_lifecycle(
        position=pos(), close_result=FLAT, dataset_path=str(dataset(tmp_path)),
        fetch_history=lambda: [hist()], write_economic_close=sink.write,
        retire_provisional=lambda _p: None,
        reconcile=lambda **kw: __import__(
            "execution.close_reconciler", fromlist=["x"]
        ).reconcile_close(sleep=lambda _: None, **kw),
    )

    assert result == "RECONCILED"
    assert len(sink.written) == 1


def test_emit_refuses_a_row_it_could_never_join():
    assert emit_close_outcome({"symbol": "X"}, {}, source="reconciled", log=MagicMock()) is False


# --- 7-10: separation, defaults, absent models ------------------------------


def test_outcome_row_is_not_written_into_the_pre_entry_file(tmp_path):
    from execution.entry_routing import EntryRoutingRecorder

    routing_path = tmp_path / "entry_routing.jsonl"
    recorder = EntryRoutingRecorder(
        lifecycle_id="pos-abc", plan_id="p", candidate_id="c", symbol="TRXUSDT",
        direction="SHORT", planned_entry=0.327, intended_route="market",
        size_requested=77.0, path=str(routing_path),
    )
    recorder.record("fallback_fill", fill_price=0.32579, size_filled=77.0)
    recorder.write()

    text = routing_path.read_text()
    for outcome_field in ("net_pnl", "gross_pnl", "exit_reason", "hold_duration_seconds"):
        assert outcome_field not in text


def test_pre_entry_snapshot_still_rejects_outcome_fields(tmp_path):
    from execution.entry_routing import EntryRoutingRecorder

    recorder = EntryRoutingRecorder(
        lifecycle_id="L", plan_id="p", candidate_id="c", symbol="X", direction="LONG",
        planned_entry=1.0, intended_route="market", size_requested=1.0,
        path=str(tmp_path / "r.jsonl"),
    )
    with pytest.raises(ValueError, match="post-fill"):
        recorder.set_pre_entry_features({"net_pnl": -0.1})


def test_default_config_makes_zero_extra_api_calls_per_entry():
    from app.config import Settings
    from execution.execution_service import ExecutionService

    settings = Settings(_env_file=None)
    assert settings.entry_routing_quote_capture is False

    service = ExecutionService.__new__(ExecutionService)
    service.settings = settings
    service.client = MagicMock()
    service.log = MagicMock()

    for _ in range(5):
        service._routing_quote("BTCUSDT")

    assert service.client.method_calls == []


def test_expected_drag_and_expected_move_remain_absent():
    from execution.entry_snapshot import economic_hurdle_observability

    snapshot = economic_hurdle_observability(
        {"tp1_move_bps": 45.1, "spread_bps": 2.4, "estimated_roundtrip_fee_bps": 12.0}
    )
    assert snapshot["historical_expected_execution_drag_bps"] is None
    assert snapshot["expected_favorable_move_bps"] is None
    assert snapshot["expected_move_model_version"] == "NONE"


def test_close_outcome_path_is_separate_from_routing_path():
    from execution.entry_routing import EntryRoutingRecorder

    default_routing = EntryRoutingRecorder(
        lifecycle_id="L", plan_id="p", candidate_id="c", symbol="X",
        direction="LONG", planned_entry=1.0, intended_route="market", size_requested=1.0,
    ).path
    assert CLOSE_OUTCOME_PATH != default_routing
