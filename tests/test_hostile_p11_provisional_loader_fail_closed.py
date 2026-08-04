"""Hostile: an unreadable segment must block startup, not disappear quietly.

`load_provisional_rows` read the active dataset and its rotations with
`errors="replace"` and `except OSError: continue`. A segment that existed but
could not be read therefore contributed no rows at all. The sweep then reported
`unresolved_total=0`, the startup verdict read COMPLETE, and the bot opened new
positions while closes it had never seen sat in an unreadable file.

Close-dedup was already strict about exactly the same segments, so two readers
of one dataset disagreed on what corruption means. They now share one reader.

These tests drive the real loader, the real sweep, the real startup verdict and
the real execution gate, and assert on whether `ExecutionService.execute` is
reachable at all.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from app.runner import StartupRunner
from execution.closed_lifecycle_recorder import (
    ProvisionalLoad,
    load_provisional_rows,
    recover_provisional_closes,
)

OPEN_MS = 1_785_700_000_000

HEADER = (
    "event_type,symbol,direction,sync_source,position_lifecycle_id,"
    "exchange_position_id,opened_at,opened_at_ms,confirmed_position_size,net_pnl,fees\n"
)


def provisional_line(lifecycle="LC-1", symbol="BTCUSDT", open_ms=OPEN_MS) -> str:
    return (
        f"CLOSE_PROVISIONAL,{symbol},LONG,position_manager,{lifecycle},,"
        f"2026-08-01T10:00:00+00:00,{open_ms},0.001,,\n"
    )


@pytest.fixture
def dataset(tmp_path):
    return tmp_path / "trade_dataset_v2.csv"


def rotation(dataset: Path, index: int = 1) -> Path:
    return dataset.with_name(f"{dataset.name}.{index}")


def deny(path: Path):
    os.chmod(path, 0o000)
    return path


@pytest.fixture(autouse=True)
def _restore_permissions(tmp_path):
    yield
    for path in tmp_path.rglob("*"):
        if path.is_file():
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass


# ── the gate, driven end to end ─────────────────────────────────────────────

class Runner:
    """The real startup gate over a real sweep over the real loader."""

    _ensure_startup_close_recovery = StartupRunner._ensure_startup_close_recovery
    _startup_recovery_verdict = StartupRunner._startup_recovery_verdict
    _execute_selected_plans = StartupRunner._execute_selected_plans

    def __init__(self, dataset: Path):
        self.log = logging.getLogger("runner")
        self._startup_close_recovery_complete = False
        self.executed: list = []
        self.written: list = []
        self.dataset = dataset
        outer = self

        def sweep(_self, limit: int = 20):
            load = load_provisional_rows(str(outer.dataset))
            return recover_provisional_closes(
                provisional_rows=load.rows,
                dataset_path=str(outer.dataset),
                fetch_history=lambda: [history_row()],
                write_economic_close=lambda row, econ: outer.written.append(econ),
                limit=limit,
                load_blocked=load.blocked,
            )

        self.position_manager = type("PM", (), {"recover_provisional_close_rows": sweep})()
        self.execution_service = type("ES", (), {
            "execute": lambda _s, plans: (outer.executed.append(plans) or ["REPORT"]),
        })()


def history_row() -> dict:
    return {
        "symbol": "BTCUSDT", "holdSide": "long", "ctime": OPEN_MS,
        "utime": OPEN_MS + 600_000, "openTotalPos": "0.001", "closeTotalPos": "0.001",
        "openAvgPrice": "100.0", "closeAvgPrice": "101.0", "pnl": "-0.03619",
        "netProfit": "-0.06631471", "openFee": "-0.01505149",
        "closeFee": "-0.01507321", "totalFunding": "0", "positionId": "P-1",
    }


def gate_blocks(dataset: Path) -> Runner:
    runner = Runner(dataset)
    runner._execute_selected_plans(["PLAN-A"])
    return runner


def assert_startup_blocked(dataset: Path):
    load = load_provisional_rows(str(dataset))
    assert load.blocked is True, "loader reported a complete list it did not have"

    runner = gate_blocks(dataset)
    assert runner.executed == [], "execution was reached with pending closes unknown"
    assert runner._startup_close_recovery_complete is False
    assert runner.written == [], "economics were written from an incomplete view"


# ── H-P11-1: pending row hidden in an unreadable rotation ───────────────────

def test_hp11_1_unreadable_rotation_hides_a_pending_row_and_blocks(dataset):
    dataset.write_text(HEADER, encoding="utf-8")               # active: empty
    rotation(dataset).write_text(HEADER + provisional_line("LC-hidden"), encoding="utf-8")
    deny(rotation(dataset))

    load = load_provisional_rows(str(dataset))
    assert load.rows == [], "the hidden row is genuinely invisible -- that is the trap"
    assert_startup_blocked(dataset)


# ── H-P11-2 / 3 / 4 / 5 / 6: every unreadable shape ─────────────────────────

def test_hp11_2_unreadable_active_segment_blocks(dataset):
    dataset.write_text(HEADER + provisional_line(), encoding="utf-8")
    deny(dataset)
    assert_startup_blocked(dataset)


def test_hp11_3_malformed_active_csv_is_not_an_empty_list(dataset):
    dataset.write_bytes(b"event_type,symbol,direction\nCLOSE_PROVISIONAL,BTC\x00USDT,LONG\n")
    assert_startup_blocked(dataset)


def test_hp11_4_malformed_rotation_blocks_while_active_is_readable(dataset):
    dataset.write_text(HEADER + provisional_line("LC-visible"), encoding="utf-8")
    rotation(dataset).write_bytes(b"\x00\x00\x00")
    assert_startup_blocked(dataset)


def test_hp11_5_missing_required_schema_blocks(dataset):
    dataset.write_text("alpha,beta,gamma\n1,2,3\n", encoding="utf-8")
    assert_startup_blocked(dataset)


def test_hp11_6_decode_failure_blocks(dataset):
    dataset.write_bytes(b"event_type,symbol,direction\nCLOSE_PROVISIONAL,\xff\xfe\xfd,LONG\n")
    assert_startup_blocked(dataset)


def test_hp11_6_truncated_row_blocks(dataset):
    dataset.write_text(
        "event_type,symbol,direction,position_lifecycle_id\nCLOSE_PROVISIONAL,BTCUSDT\n",
        encoding="utf-8",
    )
    assert_startup_blocked(dataset)


# ── H-P11-7 / 8: absence and emptiness are not corruption ───────────────────

def test_hp11_7_absent_rotation_is_not_a_blocker(dataset):
    dataset.write_text(HEADER + provisional_line(), encoding="utf-8")
    assert not rotation(dataset, 2).exists()

    load = load_provisional_rows(str(dataset))
    assert load.blocked is False
    assert [r["position_lifecycle_id"] for r in load.rows] == ["LC-1"]

    runner = Runner(dataset)
    runner._execute_selected_plans(["PLAN-A"])
    assert runner.written, "a readable pending row must still be recovered"


def test_hp11_8_provably_empty_file_is_not_a_blocker(dataset):
    dataset.write_bytes(b"")
    load = load_provisional_rows(str(dataset))
    assert load.blocked is False and load.rows == []

    runner = Runner(dataset)
    runner._execute_selected_plans(["PLAN-A"])
    assert runner.executed == [["PLAN-A"]], "nothing pending, nothing unreadable -- may trade"


def test_hp11_8_absent_dataset_is_not_a_blocker(dataset):
    assert not dataset.exists()
    assert load_provisional_rows(str(dataset)).blocked is False
    runner = Runner(dataset)
    runner._execute_selected_plans(["PLAN-A"])
    assert runner.executed == [["PLAN-A"]]


# ── H-P11-9: repair releases the gate, once ─────────────────────────────────

def test_hp11_9_startup_recovers_after_storage_repair(dataset):
    dataset.write_text(HEADER, encoding="utf-8")
    rot = rotation(dataset)
    rot.write_text(HEADER + provisional_line("LC-hidden"), encoding="utf-8")
    deny(rot)

    runner = Runner(dataset)
    runner._execute_selected_plans(["PLAN-A"])
    assert runner.executed == []
    assert runner.written == []

    os.chmod(rot, 0o600)                                   # storage repaired

    runner._execute_selected_plans(["PLAN-A"])
    assert len(runner.written) == 1, "the previously hidden row must now be recovered"
    assert runner.executed == [["PLAN-A"]], "exactly one execution after repair"

    runner._execute_selected_plans(["PLAN-B"])
    assert len(runner.written) == 1, "no duplicate economic write"


# ── H-P11-10: a blocked load changes nothing at all ─────────────────────────

def test_hp11_10_blocked_load_writes_nothing_and_leaves_the_cursor(dataset):
    dataset.write_text(HEADER + provisional_line(), encoding="utf-8")
    rotation(dataset).write_bytes(b"\x00")

    load = load_provisional_rows(str(dataset))
    written: list = []
    retired: list = []
    cursor_in = [OPEN_MS, "", "", 5]

    stats = recover_provisional_closes(
        provisional_rows=load.rows,
        dataset_path=str(dataset),
        fetch_history=lambda: [history_row()],
        write_economic_close=lambda row, econ: written.append(econ),
        retire_provisional=lambda row: retired.append(row),
        cursor=cursor_in,
        load_blocked=load.blocked,
    )

    assert stats["blocked"] is True
    assert written == [] and retired == []
    assert stats["recovered"] == 0 and stats["skipped"] == 0
    assert stats["next_cursor"] == cursor_in, "the cursor advanced over rows nobody saw"


def test_hp11_10_next_sweep_after_repair_recovers_exactly_once(dataset):
    dataset.write_text(HEADER + provisional_line(), encoding="utf-8")
    corrupt = rotation(dataset)
    corrupt.write_bytes(b"\x00")

    runner = Runner(dataset)
    runner._execute_selected_plans(["PLAN-A"])
    assert runner.written == []

    corrupt.unlink()
    runner._execute_selected_plans(["PLAN-A"])
    assert len(runner.written) == 1


# ── H-P11-11: uncertainty outranks a visible match ──────────────────────────

def test_hp11_11_visible_row_does_not_excuse_an_unreadable_rotation(dataset):
    dataset.write_text(HEADER + provisional_line("LC-visible"), encoding="utf-8")
    rot = rotation(dataset)
    rot.write_text(HEADER, encoding="utf-8")
    deny(rot)

    load = load_provisional_rows(str(dataset))
    assert [r["position_lifecycle_id"] for r in load.rows] == ["LC-visible"]
    assert load.blocked is True, "one readable segment must not excuse an unreadable one"
    assert_startup_blocked(dataset)


# ── H-P11-12: the typed contract itself ─────────────────────────────────────

def test_hp11_12_loader_reports_what_it_could_not_read(dataset):
    dataset.write_text(HEADER, encoding="utf-8")
    corrupt = rotation(dataset)
    corrupt.write_bytes(b"\x00\x00")

    load = load_provisional_rows(str(dataset))
    assert isinstance(load, ProvisionalLoad)
    assert load.blocked is True
    assert str(corrupt) in load.unreadable_segments
    assert load.error_types, "the failure reason must be reported, not just the path"
    assert str(dataset) in load.segment_paths


def test_hp11_12_order_submission_is_unreachable_while_storage_is_unreadable(dataset):
    """The gate is the only scanner entry to ExecutionService."""
    dataset.write_bytes(b"\x00")
    runner = Runner(dataset)
    reports = runner._execute_selected_plans(["PLAN-A", "PLAN-B"])
    assert reports == []
    assert runner.executed == [], "ExecutionService.execute was reached"
