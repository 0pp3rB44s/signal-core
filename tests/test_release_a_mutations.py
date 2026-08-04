from __future__ import annotations

import csv
import logging
from contextlib import nullcontext
from pathlib import Path

import pytest

from app.runner import StartupRunner
from clients.bitget_account_client import (
    BitgetAccountClientMixin,
    PositionReadbackState,
    PositionReadbackUnknown,
)
from clients.bitget_order_client import BitgetOrderClientMixin
from clients.bitget_rest import BitgetRestClient
from execution.close_dedup import economic_close_exists
from execution.close_reconciler import (
    CloseReconciliationUnavailable,
    economics_from_history,
    match_lifecycle,
)
from execution.closed_lifecycle_recorder import (
    exchange_confirmed_flat,
    load_provisional_rows,
    recover_provisional_closes,
)
from execution.position_manager import PositionManager
from scripts.audit_provisional_close_migration import audit
from telemetry.trade_logger import MoneyFieldError, append_exchange_truth_close

OPEN_MS = 1_785_700_000_000


def history(*, pid="P1", open_ms=OPEN_MS, size="0.0003", side="short"):
    return {
        "symbol": "BTCUSDT", "holdSide": side, "ctime": open_ms,
        "utime": open_ms + 600_000, "openTotalPos": size, "closeTotalPos": size,
        "openAvgPrice": "62900", "closeAvgPrice": "62950", "pnl": "-0.03619000",
        "openFee": "-0.01505149", "closeFee": "-0.01507321",
        "totalFunding": "0", "netProfit": "-0.06631471", "positionId": pid,
    }


def identity(**overrides):
    base = {
        "symbol": "BTCUSDT", "direction": "SHORT", "hold_side": "short",
        "opened_at_ms": OPEN_MS, "opened_at": "2026-08-03T06:01:36+00:00",
        "confirmed_position_size": 0.0003, "position_lifecycle_id": "life-1",
    }
    base.update(overrides)
    return base


class ReadbackClient(BitgetAccountClientMixin):
    def __init__(self, responses):
        self.settings = type("Settings", (), {"bitget_product_type": "USDT-FUTURES"})()
        self.log = logging.getLogger("readback-test")
        self.responses = list(responses)

    def _request(self, *args, **kwargs):
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class CloseClient(BitgetOrderClientMixin, BitgetAccountClientMixin):
    def __init__(self, responses, events):
        self.settings = type("Settings", (), {"bitget_product_type": "USDT-FUTURES"})()
        self.log = logging.getLogger("close-test")
        self.responses = list(responses)
        self.events = events

    def _request(self, method, path, **kwargs):
        if method == "GET":
            self.events.append("readback")
            value = self.responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        raise AssertionError("unexpected request")

    def close_futures_position(self, **kwargs):
        self.events.append("close")
        return {"code": "00000"}

    def cancel_all_futures_tpsl_orders(self, **kwargs):
        self.events.append("cleanup")
        return {"status": "OK"}


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = sorted({key for row in rows for key in row} | {
        "event_type", "symbol", "direction", "opened_at", "confirmed_position_size",
        "position_lifecycle_id", "sync_source", "net_pnl",
    })
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_m1_get_error_is_unknown_and_never_zero():
    client = ReadbackClient([RuntimeError("transport")])
    result = client.read_position_state("BTCUSDT", "short")
    assert result.state is PositionReadbackState.UNKNOWN and result.size is None
    client = ReadbackClient([RuntimeError("transport")])
    with pytest.raises(PositionReadbackUnknown):
        client._live_position_size_for_symbol("BTCUSDT", "short")


def test_m2_unknown_or_bare_closed_is_not_flat():
    assert not exchange_confirmed_flat({"status": "CLOSED"})
    assert not exchange_confirmed_flat({
        "status": "READBACK_UNKNOWN", "flatness": "UNKNOWN", "remaining_size": None,
    })
    malformed = ReadbackClient([{"data": {"not": "a list"}}]).read_position_state(
        "BTCUSDT", "short"
    )
    assert malformed.state is PositionReadbackState.UNKNOWN
    manager = PositionManager.__new__(PositionManager)
    manager.log = logging.getLogger("unprotected-negative-test")
    manager.client = type("Client", (), {
        "close_futures_position_full": lambda self, **kwargs: {
            "status": "CLOSED", "flatness": "UNKNOWN", "remaining_size": None,
        }
    })()
    assert manager._close_unprotected_position(identity(), "protection_repair_failed") is False


def test_m3_and_m4_post_readback_precedes_cleanup():
    events: list[str] = []
    client = CloseClient([
        {"data": [{"symbol": "BTCUSDT", "holdSide": "short", "total": "0.0003"}]},
        {"data": []},
    ], events)
    result = client.close_futures_position_full("BTCUSDT", "SHORT")
    assert result["status"] == "CLOSED"
    assert events == ["readback", "close", "readback", "cleanup"]


def test_m5_and_m6_real_writer_preserves_literal_net_once(tmp_path):
    path = tmp_path / "dataset.csv"
    econ = economics_from_history(history())
    append_exchange_truth_close(
        position=identity(), economics=econ, close_reason="fail_safe_close", dataset_path=path,
    )
    with path.open(newline="", encoding="utf-8") as handle:
        row = list(csv.DictReader(handle))[-1]
    assert row["gross_pnl"] == "-0.03619"
    assert row["fees"] == "0.0301247"
    assert row["net_pnl"] == "-0.06631471"


def test_emergency_orchestrator_consumes_each_identity():
    manager = PositionManager.__new__(PositionManager)
    manager.client = type("Client", (), {"emergency_flatten_all": lambda self: {
        "status": "OK", "closed": [
            {"symbol": "BTCUSDT", "result": {"status": "CLOSED", "flatness": "FLAT", "remaining_size": 0.0},
             "lifecycle_identity": identity()},
            {"symbol": "ETHUSDT", "result": {"status": "CLOSED", "flatness": "FLAT", "remaining_size": 0.0},
             "lifecycle_identity": identity(symbol="ETHUSDT", position_lifecycle_id="life-2")},
        ]
    }})()
    seen: list[str] = []
    manager._append_provisional_close_dataset_row = lambda **kwargs: seen.append(kwargs["position"]["symbol"])
    manager.reconcile_closed_lifecycle = lambda position, result, reason: "RECONCILED"
    result = manager.emergency_flatten_all()
    assert seen == ["BTCUSDT", "ETHUSDT"]
    assert [x["recording"] for x in result["recording_outcomes"]] == ["RECONCILED", "RECONCILED"]


def test_m7_real_emergency_production_caller_consumes_each_identity(monkeypatch, capsys):
    from scripts import emergency_flatten

    calls: list[str] = []
    fake_manager = type("Manager", (), {
        "emergency_flatten_all": lambda self: calls.append("orchestrator") or {
            "status": "OK", "positions_found": 2, "closed": [{}, {}], "errors": [],
            "recording_outcomes": [
                {"symbol": "BTCUSDT", "recording": "RECONCILED"},
                {"symbol": "ETHUSDT", "recording": "PROVISIONAL"},
            ],
        }
    })()
    monkeypatch.setattr(emergency_flatten, "get_settings", lambda: object())
    monkeypatch.setattr(emergency_flatten, "PositionManager", lambda settings: fake_manager)
    assert emergency_flatten.main(["--confirm-emergency-flatten"]) == 0
    assert calls == ["orchestrator"]
    assert '"recording_outcomes"' in capsys.readouterr().out


def test_m8_emergency_unknown_hold_side_is_not_mapped_to_short():
    client = BitgetRestClient.__new__(BitgetRestClient)
    client.log = logging.getLogger("emergency-test")
    client.get_all_positions = lambda: {"data": [{
        "symbol": "BTCUSDT", "holdSide": "mystery", "total": "1",
    }]}
    client.close_futures_position_full = lambda **kwargs: pytest.fail("must not close unknown side")
    result = client.emergency_flatten_all()
    assert result["closed"] == [] and result["status"] == "PARTIAL_ERROR"


def test_m9_startup_recovery_runs_before_real_execution_call(monkeypatch):
    events: list[str] = []
    runner = StartupRunner.__new__(StartupRunner)
    runner.log = logging.getLogger("runner-test")
    runner._startup_close_recovery_complete = False
    # A fully accounted sweep: nothing blocked, nothing pending, nothing
    # ambiguous, and no row left unattempted. `{"blocked": False}` alone is an
    # unknown state now, not a clean one -- see the startup-recovery gate.
    clean_sweep = {
        "seen": 0, "skipped": 0, "recovered": 0, "still_pending": 0,
        "ambiguous": 0, "blocked": False, "unresolved_total": 0,
    }
    runner.position_manager = type("PM", (), {
        "recover_provisional_close_rows": lambda self: events.append("recovery") or clean_sweep
    })()
    runner.execution_service = type("ES", (), {
        "execute": lambda self, plans: events.append("execute") or ["ok"]
    })()
    monkeypatch.setattr("app.runner.trading_state_lock", lambda: nullcontext())
    assert runner._execute_selected_plans([object()]) == ["ok"]
    assert events == ["recovery", "execute"]


def test_startup_recovery_transport_failure_blocks_execution(monkeypatch):
    events: list[str] = []
    runner = StartupRunner.__new__(StartupRunner)
    runner.log = logging.getLogger("runner-block-test")
    runner._startup_close_recovery_complete = False
    runner.position_manager = type("PM", (), {
        "recover_provisional_close_rows": lambda self: events.append("recovery") or {"blocked": True}
    })()
    runner.execution_service = type("ES", (), {
        "execute": lambda self, plans: events.append("execute") or ["unsafe"]
    })()
    monkeypatch.setattr("app.runner.trading_state_lock", lambda: nullcontext())
    assert runner._execute_selected_plans([object()]) == []
    assert events == ["recovery"]


def test_m10_and_m11_resolved_filtering_precedes_limit_and_rotated_is_loaded(tmp_path):
    active = tmp_path / "trade_dataset_v2.csv"
    rotated = tmp_path / "trade_dataset_v2.csv.1"
    resolved_rows: list[dict] = []
    for index in range(25):
        resolved_rows.extend([
            {"event_type": "CLOSE_PROVISIONAL", **identity(
                position_lifecycle_id=f"resolved-{index}", opened_at_ms=OPEN_MS + 10_000 + index,
                opened_at="2026-08-03T06:02:00+00:00",
            )},
            {"event_type": "CLOSE", "sync_source": "bitget_position_history",
             **identity(position_lifecycle_id=f"resolved-{index}", opened_at_ms=OPEN_MS + 10_000 + index)},
        ])
    write_csv(active, resolved_rows)
    old = {"event_type": "CLOSE_PROVISIONAL", **identity(position_lifecycle_id="old")}
    write_csv(rotated, [old])
    provisionals = load_provisional_rows(str(active)).rows
    written: list[dict] = []
    stats = recover_provisional_closes(
        provisional_rows=provisionals,
        dataset_path=str(active),
        fetch_history=lambda: [history()],
        write_economic_close=lambda position, economics: written.append(position),
        limit=1,
    )
    assert stats["skipped"] == 25 and stats["recovered"] == 1
    assert written[0]["position_lifecycle_id"] == "old"
    assert written[0]["_recovery_segment"].endswith(".csv.1")


def test_m12_and_m15_active_dedup_blocks_second_real_writer(tmp_path):
    path = tmp_path / "dataset.csv"
    econ = economics_from_history(history())
    for _ in range(2):
        append_exchange_truth_close(
            position=identity(), economics=econ, close_reason="dead_trade_timeout", dataset_path=path,
        )
    with path.open(newline="", encoding="utf-8") as handle:
        economic = [row for row in csv.DictReader(handle) if row["event_type"] == "CLOSE"]
    assert len(economic) == 1


def test_m13_open_time_is_required_for_lifecycle_match():
    assert match_lifecycle(
        [history(open_ms=OPEN_MS + 60_000)], symbol="BTCUSDT", direction="SHORT",
        opened_at_ms=OPEN_MS, size=0.0003,
    ) is None
    assert match_lifecycle(
        [history(size="0.0003")], symbol="BTCUSDT", direction="SHORT",
        opened_at_ms=OPEN_MS, size=0.5,
    ) is None


def test_m14_missing_money_is_never_zero():
    row = history()
    row["openFee"] = ""
    with pytest.raises(CloseReconciliationUnavailable):
        economics_from_history(row)


def test_migration_audit_is_read_only_without_apply(tmp_path):
    path = tmp_path / "dataset.csv"
    write_csv(path, [{"event_type": "CLOSE_PROVISIONAL", **identity()}])
    before = path.read_bytes()
    result = audit(dataset=str(path), history=[history()], apply=False)
    assert result["safe"] == 1 and result["applied"] == 0
    assert result["writes_enabled"] is False and path.read_bytes() == before


def test_rotated_economic_row_blocks_duplicate(tmp_path):
    active = tmp_path / "dataset.csv"
    active.write_text("event_type,symbol\n", encoding="utf-8")
    rotated = tmp_path / "dataset.csv.7"
    write_csv(rotated, [{
        "event_type": "CLOSE", "sync_source": "bitget_position_history", **identity(),
    }])
    assert economic_close_exists(active, identity())
