"""Hostile: ExecutionService must never finish a close by itself.

`execute()` marked any local OPEN row whose symbol had vanished from Bitget as
`CLOSED_SYNCED` and saved it. No recorder, no economics, no dedup, no
provisional row, no recovery hook. Both services share
`state/executed_trades.json`, so this took the position away from
`PositionManager.sync` -- the one path that *is* wired to the shared recorder --
before it could ever see it. Whichever ran first won, and when `execute()` won,
that close's economics were gone for good.

There are now exactly two outcomes for a vanished position, both owned by
PositionManager: the recorder accepts and decides the state, or it refuses and
the row stays provisional for recovery. These tests pin that there is no third.
"""

from __future__ import annotations

import ast
import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from execution.closed_lifecycle_recorder import record_known_economics_close
from execution.close_reconciler import economics_from_history, is_provisional
from telemetry.close_record_sources import is_economic_close

from tests.test_entry_path_audit import _live_settings, _plan, _service
from tests.test_position_lifecycle_safety import _manager, _position, _snapshot

REPO = Path(__file__).resolve().parents[1]
OPEN_MS = 1_785_700_000_000
DATASET = "logs/trade_dataset_v2.csv"


# ── the source contract ─────────────────────────────────────────────────────

def _execute_source() -> str:
    source = (REPO / "execution" / "execution_service.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "execute":
            return "\n".join(source.splitlines()[node.lineno - 1:node.end_lineno])
    raise AssertionError("ExecutionService.execute not found")


def test_execute_never_assigns_a_closed_status():
    """No literal close status may be written anywhere inside execute()."""
    tree = ast.parse(_execute_source().strip())
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Constant):
            continue
        if str(node.value.value) not in {"CLOSED", "CLOSED_SYNCED"}:
            continue
        for target in node.targets:
            if isinstance(target, ast.Subscript):
                offenders.append(ast.unparse(node))
    assert offenders == [], f"execute() finishes a close on its own: {offenders}"


def test_execute_calls_no_dataset_writer():
    body = _execute_source()
    for forbidden in (
        "append_exchange_truth_close",
        "_append_provisional_close_dataset_row",
        "reconcile_closed_lifecycle",
        "record_known_economics_close",
    ):
        assert forbidden not in body, (
            f"execute() reaches {forbidden} directly; close economics belong to "
            "PositionManager"
        )


# ── the live behaviour ──────────────────────────────────────────────────────

def _row(symbol: str = "SOLUSDT") -> dict:
    return {
        "symbol": symbol, "status": "OPEN", "direction": "LONG",
        "position_lifecycle_id": f"life-{symbol}",
        "opened_at": "2026-08-01T10:00:00+00:00",
        "confirmed_position_size": 0.001,
        "exchange_avg_entry": 100.0,
    }


def test_execute_leaves_a_vanished_position_open_for_the_recorder(monkeypatch):
    """The proven defect: exchange says flat, local says OPEN."""
    service = _service(monkeypatch)
    service.store.save([_row()])
    service.client.get_all_positions.return_value = {"data": []}

    service.execute([])

    saved = service.store.load(default=[])
    assert [r["status"] for r in saved] == ["OPEN"], (
        "execute() retired the position without economics"
    )
    assert "closed_at" not in saved[0]
    assert "sync_reason" not in saved[0]


def test_execute_writes_nothing_to_the_close_dataset(monkeypatch):
    service = _service(monkeypatch)
    service.store.save([_row()])
    service.client.get_all_positions.return_value = {"data": []}

    service.execute([])

    assert not Path(DATASET).exists() or _dataset_rows() == [], (
        "execute() wrote a close row"
    )


def _dataset_rows() -> list[dict]:
    path = Path(DATASET)
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8", errors="replace") as fh:
        return list(csv.DictReader(fh))


def test_execute_still_uses_exchange_truth_for_capacity(monkeypatch):
    """Deferring the retirement must not let a stale row consume a slot."""
    service = _service(monkeypatch)                # LIVE pins MAX_OPEN_POSITIONS=2
    # two stale local rows: under the old behaviour they were retired here, so
    # leaving them OPEN must still not consume the two exchange slots
    service.store.save([_row("SOLUSDT"), _row("ETHUSDT")])
    service.client.get_all_positions.return_value = {"data": []}

    reports = service.execute([_plan("BTCUSDT")])

    blocked = [r for r in reports if "max open positions" in (r.message or "")]
    assert blocked == [], "a stale local row consumed an exchange slot"


# ── PositionManager remains the single owner ────────────────────────────────

def history_row(position_id: str = "P-1") -> dict:
    return {
        "symbol": "BTCUSDT", "holdSide": "long", "ctime": OPEN_MS,
        "utime": OPEN_MS + 600_000, "openTotalPos": "1.0", "closeTotalPos": "1.0",
        "openAvgPrice": "100.0", "closeAvgPrice": "101.0", "pnl": "1.0",
        "netProfit": "0.88", "openFee": "-0.06", "closeFee": "-0.06",
        "totalFunding": "0", "positionId": position_id,
    }


def disappeared(position: dict):
    manager = _manager(open_positions=[])
    open_ms = int(
        datetime.fromisoformat(position["opened_at"].replace("Z", "+00:00")).timestamp() * 1000
    )
    manager.client.get_position_history.return_value = {
        "data": {"list": [dict(history_row(), ctime=open_ms, utime=open_ms + 600_000)]}
    }
    manager.store.save([position])
    return manager


def test_recorder_runs_exactly_once_and_writes_one_economic_close():
    manager = disappeared(_position())
    manager.sync([_snapshot(price=101.0)])

    saved = manager.store.load(default=[])[0]
    assert saved["status"] == "CLOSED_SYNCED"
    assert saved["close_economics_outcome"] == "RECORDED"
    assert len([r for r in _dataset_rows() if is_economic_close(r)]) == 1


def test_recorder_already_is_idempotent():
    manager = disappeared(_position())
    manager.sync([_snapshot(price=101.0)])
    assert len([r for r in _dataset_rows() if is_economic_close(r)]) == 1

    restarted = disappeared(_position())          # crash before state save
    restarted.sync([_snapshot(price=101.0)])

    assert restarted.store.load(default=[])[0]["close_economics_outcome"] == "ALREADY"
    assert len([r for r in _dataset_rows() if is_economic_close(r)]) == 1


def test_recorder_blocked_leaves_a_provisional_row(monkeypatch):
    import execution.closed_lifecycle_recorder as recorder

    monkeypatch.setattr(
        recorder, "economic_close_status",
        lambda *a, **k: recorder.DedupOutcome.BLOCKED_UNREADABLE,
    )
    manager = disappeared(_position())
    manager.sync([_snapshot(price=101.0)])

    saved = manager.store.load(default=[])[0]
    assert saved["close_economics_outcome"] == "BLOCKED_UNREADABLE"
    assert [r for r in _dataset_rows() if is_economic_close(r)] == []
    assert [r for r in _dataset_rows() if is_provisional(r)] != [], (
        "a refused write must still leave the trade findable"
    )


def test_no_third_outcome_exists(tmp_path):
    """Every recorder verdict is either a success or leaves work for recovery."""
    dataset = tmp_path / "trade_dataset_v2.csv"
    economics = economics_from_history(history_row())
    position = {"symbol": "BTCUSDT", "direction": "LONG", "position_lifecycle_id": "LC-1"}

    outcomes = {
        record_known_economics_close(
            position=position, economics=economics, dataset_path=str(dataset),
            write_economic_close=lambda p, e: None,
        )
        for _ in range(1)
    }
    assert outcomes <= {"RECORDED", "ALREADY", "BLOCKED_UNREADABLE", "NO_IDENTITY"}


# ── crash / restart / second recovery ───────────────────────────────────────

def test_crash_before_state_save_then_recovery_writes_no_duplicate():
    manager = disappeared(_position())
    manager.sync([_snapshot(price=101.0)])
    assert len([r for r in _dataset_rows() if is_economic_close(r)]) == 1

    # state never reached disk: the position is OPEN again after restart
    manager.store.save([_position()])
    manager.sync([_snapshot(price=101.0)])
    assert len([r for r in _dataset_rows() if is_economic_close(r)]) == 1

    for _ in range(2):
        manager.recover_provisional_close_rows(limit=20)
    assert len([r for r in _dataset_rows() if is_economic_close(r)]) == 1
