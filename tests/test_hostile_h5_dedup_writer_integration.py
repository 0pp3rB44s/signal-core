"""Hostile: the real writers must stop when dedup cannot answer.

A typed enum is worthless if the production writer keeps treating anything that
is not FOUND as permission to append. These tests drive the actual
`record_closed_lifecycle`, `recover_provisional_closes` and
`TradeDatasetV2Logger.append_close` against an unreadable dataset and count the
economic rows that reach disk.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from execution.close_reconciler import reconcile_close
from execution.closed_lifecycle_recorder import (
    record_closed_lifecycle,
    recover_provisional_closes,
)
from telemetry.close_record_sources import is_economic_close
from telemetry.trade_logger import TradeDatasetV2Logger

from tests.test_hostile_h5_dedup_unreadable_segments import (
    HEADER,
    OPEN_MS,
    block_read,
    candidate,
    econ_row,
    write_csv,
)

SYMBOL = "BTCUSDT"


def history_row() -> dict:
    return {
        "symbol": SYMBOL, "holdSide": "long", "ctime": OPEN_MS,
        "utime": OPEN_MS + 600_000, "openTotalPos": "0.001",
        "closeTotalPos": "0.001", "openAvgPrice": "62900.0",
        "closeAvgPrice": "62950.0", "pnl": "-0.03619",
        "netProfit": "-0.06631471", "openFee": "-0.01505149",
        "closeFee": "-0.01507321", "totalFunding": "0", "positionId": "P1",
    }


def flat_close() -> dict:
    return {"status": "CLOSED", "flatness": "FLAT", "remaining_size": 0.0}


def position() -> dict:
    row = candidate()
    row["opened_at"] = "2026-08-01T10:00:00+00:00"
    return row


def economic_rows(dataset: Path) -> list[dict]:
    """Every economic CLOSE currently on disk, read without the dedup layer."""
    if not dataset.exists():
        return []
    with dataset.open("r", newline="", encoding="utf-8", errors="replace") as fh:
        return [r for r in csv.DictReader(fh) if is_economic_close(r)]


@pytest.fixture
def dataset(tmp_path):
    return tmp_path / "trade_dataset_v2.csv"


def run_recorder(dataset: Path, written: list) -> str:
    def write(pos: dict, econ: dict) -> None:
        written.append(econ)
        rows = []
        if dataset.exists():
            with dataset.open("r", newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
        row = econ_row(pos.get("position_lifecycle_id"), pos.get("exchange_position_id"))
        row["net_pnl"] = str(econ["net_pnl"])
        row["fees"] = str(econ["fees"])
        write_csv(dataset, rows + [row])

    return record_closed_lifecycle(
        position=position(),
        close_result=flat_close(),
        dataset_path=str(dataset),
        fetch_history=lambda: [history_row()],
        write_economic_close=write,
        reconcile=lambda **kw: reconcile_close(sleep=lambda _: None, **kw),
    )


# ── H10: the central recorder refuses on BLOCKED ────────────────────────────

def test_h10_recorder_blocked_writes_nothing(dataset, monkeypatch):
    write_csv(dataset, [])                     # header only: no economic rows yet
    before = len(economic_rows(dataset))
    block_read(monkeypatch, dataset, PermissionError(13, "denied"))

    written: list = []
    outcome = run_recorder(dataset, written)

    assert outcome == "BLOCKED_UNREADABLE"
    assert written == [], "no economic CLOSE may be produced on an unknown dataset"
    monkeypatch.undo()
    assert len(economic_rows(dataset)) == before, "economic counter must not move"


def test_h10_blocked_is_not_reported_as_already(dataset, monkeypatch):
    """BLOCKED must not masquerade as the idempotent success path."""
    write_csv(dataset, [])
    block_read(monkeypatch, dataset, PermissionError(13, "denied"))
    assert run_recorder(dataset, []) not in {"ALREADY", "RECONCILED"}


def test_h10_real_writer_refuses_to_append(dataset, monkeypatch):
    """`TradeDatasetV2Logger.append_close` is the thing that actually writes."""
    write_csv(dataset, [])
    logger = TradeDatasetV2Logger(str(dataset))
    block_read(monkeypatch, dataset, PermissionError(13, "denied"))

    trade = position()
    trade.update({"strategy": "test", "closed_at": "2026-08-01T11:00:00+00:00",
                  "sync_source": "bitget_position_history"})
    logger.append_close(trade=trade, result="tp", pnl=-0.05, quality={})

    monkeypatch.undo()
    assert economic_rows(dataset) == [], "writer appended despite unreadable storage"


def test_h10_recovery_sweep_aborts_and_writes_nothing(dataset, monkeypatch):
    write_csv(dataset, [])
    provisional = position()
    provisional["event_type"] = "CLOSE_PROVISIONAL"
    block_read(monkeypatch, dataset, PermissionError(13, "denied"))

    written: list = []
    stats = recover_provisional_closes(
        provisional_rows=[provisional],
        dataset_path=str(dataset),
        fetch_history=lambda: [history_row()],
        write_economic_close=lambda pos, econ: written.append(econ),
        limit=20,
    )

    assert stats["blocked"] is True
    assert stats["recovered"] == 0
    assert stats["skipped"] == 0, "an unreadable dataset is not proof the row is done"
    assert written == []


# ── H7: recovery once storage is repaired ───────────────────────────────────

def test_h7_recovers_after_repair_without_duplicating(dataset, monkeypatch):
    write_csv(dataset, [])

    # 1. storage broken -> blocked, nothing written
    block_read(monkeypatch, dataset, PermissionError(13, "denied"))
    written: list = []
    assert run_recorder(dataset, written) == "BLOCKED_UNREADABLE"
    assert written == []

    # 2. storage repaired -> exactly one economic write
    monkeypatch.undo()
    assert len(economic_rows(dataset)) == 0
    assert run_recorder(dataset, written) == "RECONCILED"
    assert len(written) == 1
    assert written[0]["net_pnl"] == pytest.approx(-0.06631471)
    assert len(economic_rows(dataset)) == 1

    # 3. a third attempt finds it and refuses to duplicate
    assert run_recorder(dataset, written) == "ALREADY"
    assert len(written) == 1
    assert len(economic_rows(dataset)) == 1
