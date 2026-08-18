"""Futures depth5@100ms: proven to deliver on a raw single-stream probe (81 frames/12s)."""
from __future__ import annotations

import gzip
import json

import pytest

from research.collectors.binance_collector import BinanceResearchCollector

# Captured live from wss://fstream.binance.com/ws/btcusdt@depth5@100ms
DEPTH = {"e": "depthUpdate", "E": 1787086950523, "T": 1787086950521, "s": "BTCUSDT",
         "ps": "BTCUSDT", "U": 11315836334271, "u": 11315836340974, "pu": 11315836334106,
         "b": [["64569.20", "5.896"], ["64569.10", "0.009"]],
         "a": [["64569.30", "1.200"], ["64569.40", "0.500"]]}


@pytest.fixture()
def collector(tmp_path):
    c = BinanceResearchCollector(symbols=("BTCUSDT",), data_dir=tmp_path)
    yield c
    c.close()


def _rows(collector, name):
    for writer in (collector.depth_writer,):
        writer.finalize()
    out = []
    for path in sorted((collector.data_dir / name / "segments").glob("*.jsonl*")):
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt") as fh:
            out += [json.loads(line) for line in fh if line.strip()]
    return out


def test_depth_subscribed_on_futures_socket(collector):
    assert "btcusdt@depth5@100ms" in collector.stream_path()


def test_depth_frame_recorded_with_book_levels(collector):
    assert collector.handle_message(json.dumps(DEPTH)) == 1
    row = _rows(collector, "depth5")[0]
    assert row["bids"] == DEPTH["b"] and row["asks"] == DEPTH["a"]
    assert row["first_update_id"] == DEPTH["U"]
    assert row["final_update_id"] == DEPTH["u"]
    assert row["prev_final_update_id"] == DEPTH["pu"]
    assert collector.stream_health("depth") == "HEALTHY"


def test_depth_dedupes_on_final_update_id(collector):
    assert collector.handle_message(json.dumps(DEPTH)) == 1
    assert collector.handle_message(json.dumps(DEPTH)) == 0
    assert collector.duplicates == 1


def test_depth_dedupe_uses_final_update_id_not_payload_fingerprint(collector):
    """Same final update id, different levels -- must still be treated as the same event.

    A fingerprint fallback would let this slip through: proves `u` is actually the key.
    """
    assert collector.handle_message(json.dumps(DEPTH)) == 1
    changed = dict(DEPTH, b=[["99999.00", "1.0"]])  # same u, different book levels
    assert collector.handle_message(json.dumps(changed)) == 0
    assert collector.duplicates == 1


def test_depth_health_independent_of_book(collector):
    collector.handle_message(json.dumps(DEPTH))
    assert collector.stream_health("depth") == "HEALTHY"
    assert collector.stream_health("book") == "NO_DELIVERY_PROVEN"


def test_malformed_depth_frame_isolated(collector):
    bad = json.dumps({"e": "depthUpdate", "s": "BTCUSDT"})  # missing U/u/b/a
    assert collector.handle_message(bad) == 1  # still writes with nulls; no crash
    assert collector.parse_failures == 0
