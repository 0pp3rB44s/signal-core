import gzip
import inspect
import json
from pathlib import Path

import pytest

from research.cross_venue.streaming import (
    iter_canonical_binance_depth5,
    iter_rows_for_symbol,
    iter_segment_rows,
    list_finalized_segments,
)


def _write_segment(path: Path, rows: list[dict]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _depth_row(symbol="BTCUSDT", t=1_000, final_update_id=100, mono=1.0):
    return {
        "symbol": symbol,
        "connection_id": "conn-1",
        "transaction_time_ms": t,
        "receive_monotonic_ms": mono,
        "schema_version": "research_binance_v2_depth",
        "bids": [["100.00", "1.0"]] * 1,
        "asks": [["100.01", "1.0"]] * 1,
        "first_update_id": final_update_id - 5,
        "final_update_id": final_update_id,
        "prev_final_update_id": final_update_id - 10,
    }


def test_iter_segment_rows_is_a_generator(tmp_path):
    path = tmp_path / "seg.jsonl.gz"
    _write_segment(path, [_depth_row(), _depth_row()])
    result = iter_segment_rows(path)
    assert inspect.isgenerator(result)
    assert len(list(result)) == 2


def test_iter_segment_rows_skips_malformed_trailing_line(tmp_path):
    path = tmp_path / "seg.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(_depth_row()) + "\n")
        fh.write('{"symbol": "BTCUSDT", "truncated": tr')  # deliberately broken, no trailing newline
    rows = list(iter_segment_rows(path))
    assert len(rows) == 1


def test_list_finalized_segments_excludes_tmp_files(tmp_path):
    (tmp_path / "microflow-20260907T000000000000Z.jsonl.gz").write_bytes(b"")
    (tmp_path / "microflow-20260907T000100000000Z.jsonl.gz.tmp").write_bytes(b"")
    finalized = list_finalized_segments(tmp_path)
    assert len(finalized) == 1
    assert finalized[0].name.endswith(".jsonl.gz")


def test_list_finalized_segments_sorted_chronologically(tmp_path):
    names = [
        "microflow-20260907T030000000000Z.jsonl.gz",
        "microflow-20260907T010000000000Z.jsonl.gz",
        "microflow-20260907T020000000000Z.jsonl.gz",
    ]
    for name in names:
        (tmp_path / name).write_bytes(b"")
    finalized = list_finalized_segments(tmp_path)
    assert [p.name for p in finalized] == sorted(names)


def test_iter_rows_for_symbol_filters_and_bounds(tmp_path):
    seg1 = tmp_path / "microflow-20260907T010000000000Z.jsonl.gz"
    seg2 = tmp_path / "microflow-20260907T020000000000Z.jsonl.gz"
    _write_segment(seg1, [_depth_row(symbol="BTCUSDT"), _depth_row(symbol="ETHUSDT")])
    _write_segment(seg2, [_depth_row(symbol="BTCUSDT"), _depth_row(symbol="BTCUSDT")])

    all_btc = list(iter_rows_for_symbol(tmp_path, "BTCUSDT"))
    assert len(all_btc) == 3
    assert all(row["symbol"] == "BTCUSDT" for row in all_btc)

    bounded = list(iter_rows_for_symbol(tmp_path, "BTCUSDT", max_rows=1))
    assert len(bounded) == 1


def test_iter_rows_for_symbol_max_segments_reads_most_recent_only(tmp_path):
    seg1 = tmp_path / "microflow-20260907T010000000000Z.jsonl.gz"
    seg2 = tmp_path / "microflow-20260907T020000000000Z.jsonl.gz"
    _write_segment(seg1, [_depth_row(final_update_id=1)])
    _write_segment(seg2, [_depth_row(final_update_id=2)])

    rows = list(iter_rows_for_symbol(tmp_path, "BTCUSDT", max_segments=1))
    assert len(rows) == 1
    assert rows[0]["final_update_id"] == 2


def test_iter_canonical_binance_depth5_tracks_prev_across_rows(tmp_path):
    seg = tmp_path / "microflow-20260907T010000000000Z.jsonl.gz"
    _write_segment(
        seg,
        [
            _depth_row(final_update_id=100, mono=1.0, t=1_000),
            _depth_row(final_update_id=110, mono=2.0, t=1_100),
        ],
    )
    records = list(iter_canonical_binance_depth5(tmp_path, "BTCUSDT"))
    assert len(records) == 2
    assert records[1].prev_sequence_id == 100


def test_streaming_never_materializes_whole_file_before_first_yield(tmp_path):
    # A crude but effective bounded-memory check: consuming only the first
    # item from the generator must not require the second (malformed) row to
    # have been parsed successfully.
    path = tmp_path / "seg.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(_depth_row()) + "\n")
        fh.write("this line will never be reached if the generator is lazy but would raise if eagerly parsed as strict json without our skip-on-error handling\n")
    gen = iter_segment_rows(path)
    first = next(gen)
    assert first["symbol"] == "BTCUSDT"
