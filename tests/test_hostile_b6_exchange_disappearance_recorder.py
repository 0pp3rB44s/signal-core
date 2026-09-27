"""Hostile: the exchange-disappearance close must go through the recorder.

When Bitget stops reporting a position, `PositionManager.sync` fetched
position-history itself and then called `append_exchange_truth_close` directly —
the last close path that wrote to the dataset without passing the shared
recorder. Two consequences:

  * idempotency rested on one layer instead of on the shared contract, so it was
    only ever as good as whatever `append_close` happened to check;
  * the writer returns None, so a *refused* write was invisible. The position was
    still retired as CLOSED_SYNCED with nothing economic and nothing provisional
    on disk, and no recovery sweep would ever look for that trade again.

These tests drive the real `PositionManager.sync()` with a mocked exchange edge,
the real reconciler, the real recorder, the real dedup and the real writer, and
count the rows that actually land in the dataset.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from execution.closed_lifecycle_recorder import record_known_economics_close
from execution.close_reconciler import economics_from_history
from telemetry.close_record_sources import is_economic_close

from tests.test_position_lifecycle_safety import (
    _manager,
    _position,
    _snapshot,
    _v2_close_rows,
    _v2_provisional_rows,
)

SYMBOL = "BTCUSDT"
OPEN_MS = 1_785_700_000_000
DATASET = "logs/trade_dataset_v2.csv"


def history_row(position_id: str = "P-XYZ", opened_at: str | None = None) -> dict:
    """One closed lifecycle as Bitget publishes it."""
    open_ms = OPEN_MS
    if opened_at:
        open_ms = int(
            datetime.fromisoformat(opened_at.replace("Z", "+00:00")).timestamp() * 1000
        )
    return {
        "symbol": SYMBOL, "holdSide": "long", "ctime": open_ms,
        "utime": open_ms + 600_000, "openTotalPos": "1.0", "closeTotalPos": "1.0",
        "openAvgPrice": "100.0", "closeAvgPrice": "101.0", "pnl": "1.0",
        "netProfit": "0.88", "openFee": "-0.06", "closeFee": "-0.06",
        "totalFunding": "0", "positionId": position_id,
    }


def disappeared_manager(position: dict, history: list[dict] | None = None):
    """Exchange reports nothing open; position-history holds the lifecycle."""
    manager = _manager(open_positions=[])
    rows = history if history is not None else [history_row(opened_at=position["opened_at"])]
    manager.client.get_position_history.return_value = {"data": {"list": rows}}
    manager.store.save([position])
    return manager


def economic_rows() -> list[dict]:
    path = Path(DATASET)
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8", errors="replace") as fh:
        return [r for r in csv.DictReader(fh) if is_economic_close(r)]


# ── the path really does produce economics now ──────────────────────────────

def test_disappearance_writes_exactly_one_economic_close():
    position = _position()
    manager = disappeared_manager(position)

    manager.sync([_snapshot(price=101.0)])

    saved = manager.store.load(default=[])[0]
    assert saved["status"] == "CLOSED_SYNCED"
    assert saved["close_economics_outcome"] == "RECORDED"
    assert len(economic_rows()) == 1
    assert economic_rows()[0]["exchange_position_id"] == "P-XYZ"


# ── H17: crash straight after the CSV append ────────────────────────────────

def test_h17_crash_after_append_then_restart_writes_no_duplicate():
    """State was never saved, so the restart replays the identical close."""
    position = _position()
    manager = disappeared_manager(position)
    manager.sync([_snapshot(price=101.0)])
    assert len(economic_rows()) == 1

    # restart: the dataset survived the crash, the position state did not
    restarted = disappeared_manager(_position())
    restarted.sync([_snapshot(price=101.0)])

    assert len(economic_rows()) == 1, "the replayed close wrote a second economic row"
    assert restarted.store.load(default=[])[0]["close_economics_outcome"] == "ALREADY"


# ── H18: crash before the state save ────────────────────────────────────────

def test_h18_crash_before_state_save_leaves_one_economics():
    position = _position()
    manager = disappeared_manager(position)
    manager.sync([_snapshot(price=101.0)])
    rows_after_first = economic_rows()
    assert len(rows_after_first) == 1

    # simulate the state file never reaching disk: the position is OPEN again
    manager.store.save([_position()])
    manager.sync([_snapshot(price=101.0)])

    rows = economic_rows()
    assert len(rows) == 1
    assert rows[0]["net_pnl"] == rows_after_first[0]["net_pnl"]


# ── H19: two recovery runs ──────────────────────────────────────────────────

def test_h19_two_recovery_runs_keep_one_economics():
    manager = disappeared_manager(_position())
    manager.sync([_snapshot(price=101.0)])

    writer = manager  # the same service exposes the recovery sweep
    for _ in range(2):
        writer.recover_provisional_close_rows(limit=20)

    assert len(economic_rows()) == 1


# ── H20: recorder already has an economic row ───────────────────────────────

def test_h20_existing_economic_row_blocks_a_second_write(tmp_path):
    """Drive the recorder directly: an existing row means no write at all."""
    dataset = tmp_path / "trade_dataset_v2.csv"
    economics = economics_from_history(history_row())
    position = {"symbol": SYMBOL, "direction": "LONG", "strategy": "t",
                "position_lifecycle_id": "LC-1", "closed_reason": "exchange_position_closed"}

    from telemetry.trade_logger import append_exchange_truth_close

    writes: list = []

    def write(pos, econ):
        writes.append(econ)
        append_exchange_truth_close(position=pos, economics=econ,
                                    close_reason="exchange_position_closed",
                                    dataset_path=str(dataset))

    first = record_known_economics_close(
        position=position, economics=economics, dataset_path=str(dataset),
        write_economic_close=write)
    second = record_known_economics_close(
        position=position, economics=economics, dataset_path=str(dataset),
        write_economic_close=write)

    assert first == "RECORDED"
    assert second == "ALREADY"
    assert len(writes) == 1


def test_h20_unreadable_dataset_blocks_the_write(tmp_path):
    dataset = tmp_path / "trade_dataset_v2.csv"
    dataset.write_bytes(b"event_type,symbol,direction\nCLOSE,BTC\x00USDT,LONG\n")
    writes: list = []

    outcome = record_known_economics_close(
        position={"symbol": SYMBOL, "direction": "LONG", "position_lifecycle_id": "LC-1"},
        economics=economics_from_history(history_row()),
        dataset_path=str(dataset),
        write_economic_close=lambda pos, econ: writes.append(econ),
    )

    assert outcome == "BLOCKED_UNREADABLE"
    assert writes == []


def test_h20_no_identity_blocks_the_write(tmp_path):
    dataset = tmp_path / "trade_dataset_v2.csv"
    writes: list = []
    economics = dict(economics_from_history(history_row()))
    economics["exchange_position_id"] = ""

    outcome = record_known_economics_close(
        position={"symbol": SYMBOL, "direction": "LONG"},
        economics=economics,
        dataset_path=str(dataset),
        write_economic_close=lambda pos, econ: writes.append(econ),
    )

    assert outcome == "NO_IDENTITY"
    assert writes == []


# ── the refused write must not vanish ───────────────────────────────────────

def test_refused_economic_write_falls_back_to_a_provisional_row(monkeypatch):
    """A refusal used to retire the position with nothing on disk at all."""
    import execution.closed_lifecycle_recorder as recorder

    monkeypatch.setattr(
        recorder, "economic_close_status",
        lambda *a, **k: recorder.DedupOutcome.BLOCKED_UNREADABLE,
    )

    manager = disappeared_manager(_position())
    manager.sync([_snapshot(price=101.0)])

    saved = manager.store.load(default=[])[0]
    assert saved["status"] == "CLOSED_SYNCED"
    assert saved["close_economics_outcome"] == "BLOCKED_UNREADABLE"
    assert economic_rows() == [], "nothing economic may be written on uncertainty"
    assert len(_v2_provisional_rows()) == 1, "the trade must stay findable by recovery"
