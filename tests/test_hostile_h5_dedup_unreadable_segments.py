"""Hostile: an unreadable dataset segment must never look like an empty one.

`_read` used to wrap every segment in `except Exception: continue`. A dataset
the process could not open therefore contributed zero rows, no identity key
matched, and `economic_close_exists` returned False — so the caller wrote a
*second* economic CLOSE for a lifecycle that already had one. That double-counts
realized PnL in `RiskManager._weekly_realized_pnl`, the meter behind the weekly
freeze kill-switch, which is the same failure class as the ROI-as-USDT bug.

These tests hold the whole chain to the fail-closed rule: uncertainty blocks the
write, absence does not. They drive the real dedup, the real recorder and the
real `TradeDatasetV2Logger` writer, so a helper that returns a nice enum while
the writer still appends cannot pass.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from execution.close_dedup import (
    DedupOutcome,
    SegmentStatus,
    economic_close_exists,
    economic_close_status,
    segment_status,
)

HEADER = [
    "event_type", "symbol", "direction", "sync_source", "position_lifecycle_id",
    "exchange_position_id", "opened_at", "confirmed_position_size", "net_pnl", "fees",
]

OPEN_MS = 1_785_700_000_000


def econ_row(lifecycle: str = "L1", position_id: str = "P1") -> dict:
    return {
        "event_type": "CLOSE",
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "sync_source": "bitget_position_history",
        "position_lifecycle_id": lifecycle,
        "exchange_position_id": position_id,
        "opened_at": "2026-08-01T10:00:00+00:00",
        "confirmed_position_size": "0.001",
        "net_pnl": "-0.05",
        "fees": "0.03",
    }


def candidate(lifecycle: str = "L1", position_id: str = "P1") -> dict:
    return {
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "position_lifecycle_id": lifecycle,
        "exchange_position_id": position_id,
        "opened_at_ms": OPEN_MS,
        "confirmed_position_size": 0.001,
    }


def write_csv(path: Path, rows: list[dict], header: list[str] = HEADER) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in header})


def block_read(monkeypatch, target: Path, exc: BaseException) -> None:
    """Make exactly one path fail to open, leaving every other read intact."""
    real = Path.read_bytes

    def fake(self):
        if Path(self) == Path(target):
            raise exc
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", fake)


@pytest.fixture
def dataset(tmp_path):
    return tmp_path / "trade_dataset_v2.csv"


# ── H1: unreadable active segment ───────────────────────────────────────────

def test_h1_unreadable_active_segment_blocks(dataset, monkeypatch):
    write_csv(dataset, [econ_row()])
    block_read(monkeypatch, dataset, PermissionError(13, "Permission denied"))

    assert economic_close_status(dataset, candidate("L2", "P2")) is DedupOutcome.BLOCKED_UNREADABLE
    assert segment_status(dataset) is SegmentStatus.UNREADABLE
    # the fail-closed bool view must not report "free to write" either
    assert economic_close_exists(dataset, candidate("L2", "P2")) is True


def test_h1_io_error_blocks(dataset, monkeypatch):
    write_csv(dataset, [econ_row()])
    block_read(monkeypatch, dataset, OSError(5, "Input/output error"))
    assert economic_close_status(dataset, candidate("L2", "P2")) is DedupOutcome.BLOCKED_UNREADABLE


# ── H2: unreadable rotation ─────────────────────────────────────────────────

def test_h2_unreadable_rotation_blocks(dataset, monkeypatch):
    write_csv(dataset, [econ_row("OTHER", "OTHER")])          # readable, no match
    rotation = dataset.with_name(dataset.name + ".1")
    write_csv(rotation, [econ_row()])
    block_read(monkeypatch, rotation, PermissionError(13, "denied"))

    assert economic_close_status(dataset, candidate()) is DedupOutcome.BLOCKED_UNREADABLE


# ── H3: malformed CSV ───────────────────────────────────────────────────────

def test_h3_nul_bytes_block(dataset):
    dataset.write_bytes(b"event_type,symbol,direction\nCLOSE,BTC\x00USDT,LONG\n")
    assert economic_close_status(dataset, candidate()) is DedupOutcome.BLOCKED_UNREADABLE


def test_h3_undecodable_bytes_block(dataset):
    dataset.write_bytes(b"event_type,symbol,direction\nCLOSE,\xff\xfe\xfd,LONG\n")
    assert economic_close_status(dataset, candidate()) is DedupOutcome.BLOCKED_UNREADABLE


def test_h3_truncated_row_blocks(dataset):
    # fewer fields than the header: the lifecycle columns silently vanish
    dataset.write_text(
        "event_type,symbol,direction,position_lifecycle_id\nCLOSE,BTCUSDT\n",
        encoding="utf-8",
    )
    assert economic_close_status(dataset, candidate()) is DedupOutcome.BLOCKED_UNREADABLE


def test_h3_overlong_row_blocks(dataset):
    dataset.write_text(
        "event_type,symbol,direction\nCLOSE,BTCUSDT,LONG,EXTRA,MORE\n", encoding="utf-8"
    )
    assert economic_close_status(dataset, candidate()) is DedupOutcome.BLOCKED_UNREADABLE


# ── H4: missing required schema ─────────────────────────────────────────────

def test_h4_missing_required_columns_block(dataset):
    # perfectly readable text, but nothing here can classify or identify a row
    dataset.write_text("alpha,beta,gamma\n1,2,3\n", encoding="utf-8")
    assert economic_close_status(dataset, candidate()) is DedupOutcome.BLOCKED_UNREADABLE


def test_h4_header_missing_one_identity_column_blocks(dataset):
    dataset.write_text("event_type,symbol\nCLOSE,BTCUSDT\n", encoding="utf-8")
    assert economic_close_status(dataset, candidate()) is DedupOutcome.BLOCKED_UNREADABLE


def test_h4_headerless_file_blocks(dataset):
    # data lines with no header row: the first record is consumed as the header,
    # so every subsequent row is misaligned and nothing can be identified
    dataset.write_text("CLOSE,BTCUSDT,LONG\nCLOSE,ETHUSDT,SHORT\n", encoding="utf-8")
    assert economic_close_status(dataset, candidate()) is DedupOutcome.BLOCKED_UNREADABLE


def test_h4_whitespace_only_file_is_provably_empty(dataset):
    # deliberate: a blank file holds no rows, which is certainty rather than
    # corruption, so it must not block a first write
    dataset.write_text("\n\n", encoding="utf-8")
    assert economic_close_status(dataset, candidate()) is DedupOutcome.NOT_FOUND


# ── H5: absent segments are not blockers ────────────────────────────────────

def test_h5_absent_rotation_allows_normal_semantics(dataset):
    write_csv(dataset, [econ_row("OTHER", "OTHER")])
    rotation = dataset.with_name(dataset.name + ".1")
    write_csv(rotation, [econ_row()])
    assert not dataset.with_name(dataset.name + ".2").exists()

    assert economic_close_status(dataset, candidate()) is DedupOutcome.FOUND
    assert economic_close_status(dataset, candidate("L9", "P9")) is DedupOutcome.NOT_FOUND
    assert segment_status(dataset.with_name(dataset.name + ".2")) is SegmentStatus.ABSENT


def test_h5_absent_dataset_allows_first_write(dataset):
    assert not dataset.exists()
    assert economic_close_status(dataset, candidate()) is DedupOutcome.NOT_FOUND


def test_h5_empty_file_allows_first_write(dataset):
    dataset.write_bytes(b"")
    assert economic_close_status(dataset, candidate()) is DedupOutcome.NOT_FOUND


def test_h5_non_numeric_neighbours_are_not_authoritative(dataset):
    write_csv(dataset, [econ_row("OTHER", "OTHER")])
    # a backup and a temp file must neither be read as truth nor block on garbage
    dataset.with_name(dataset.name + ".bak").write_bytes(b"\xff\xfe garbage")
    dataset.with_name(dataset.name + ".tmp").write_bytes(b"\x00\x00")
    assert economic_close_status(dataset, candidate()) is DedupOutcome.NOT_FOUND


# ── H6: readable rotation holding the lifecycle ─────────────────────────────

def test_h6_match_in_readable_rotation_is_found(dataset):
    write_csv(dataset, [econ_row("OTHER", "OTHER")])
    write_csv(dataset.with_name(dataset.name + ".1"), [econ_row()])
    assert economic_close_status(dataset, candidate()) is DedupOutcome.FOUND


def test_h6_deep_rotation_is_not_silently_dropped(dataset):
    write_csv(dataset, [econ_row("OTHER", "OTHER")])
    for i in (1, 2, 3, 4, 5):
        write_csv(dataset.with_name(f"{dataset.name}.{i}"), [econ_row(f"X{i}", f"Y{i}")])
    write_csv(dataset.with_name(f"{dataset.name}.7"), [econ_row()])
    assert economic_close_status(dataset, candidate()) is DedupOutcome.FOUND


# ── H8: one corrupt segment poisons the whole answer ────────────────────────

def test_h8_one_corrupt_segment_is_not_not_found(dataset):
    write_csv(dataset, [econ_row("OTHER", "OTHER")])            # readable, no match
    dataset.with_name(dataset.name + ".1").write_bytes(b"\x00\x00\x00")

    status = economic_close_status(dataset, candidate())
    assert status is not DedupOutcome.NOT_FOUND
    assert status is DedupOutcome.BLOCKED_UNREADABLE


# ── H9: corrupt active outranks a match in a rotation ───────────────────────

def test_h9_corrupt_active_blocks_even_when_rotation_matches(dataset, monkeypatch):
    write_csv(dataset, [econ_row()])
    write_csv(dataset.with_name(dataset.name + ".1"), [econ_row()])
    block_read(monkeypatch, dataset, PermissionError(13, "denied"))

    # a rotation does contain the lifecycle, but the active file was never
    # examined, so the honest answer is "unknown", not "already recorded"
    assert economic_close_status(dataset, candidate()) is DedupOutcome.BLOCKED_UNREADABLE
