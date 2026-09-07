"""Offline, synthetic-data exercise of the Objective 11/12 smoke-validation
entry point (research/cross_venue/smoke_validate.py).

This test suite must never depend on network access or the real remote
clean-epoch host — it builds tiny synthetic segment trees under tmp_path so
the mission's automated test suite stays fully self-contained. The *real*
clean-data validation (against the actual production clean epoch) is a
separate, manually-run, one-time step reported in
reports/CANONICAL_CROSS_VENUE_RESEARCH_LAYER_2026-09-07.md — not something a
CI-style pytest run should attempt against live infrastructure.
"""

import gzip
import json
from pathlib import Path

from research.cross_venue.smoke_validate import run


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _binance_depth_row(symbol, t, final_update_id, mono, bid=100.0, ask=100.1):
    return {
        "symbol": symbol,
        "connection_id": "conn-1",
        "transaction_time_ms": t,
        "receive_monotonic_ms": mono,
        "schema_version": "research_binance_v2_depth",
        "bids": [[str(bid), "1.0"], [str(bid - 0.01), "2.0"]],
        "asks": [[str(ask), "1.0"], [str(ask + 0.01), "2.0"]],
        "first_update_id": final_update_id - 5,
        "final_update_id": final_update_id,
        "prev_final_update_id": final_update_id - 10,
    }


def _bitget_state_row(symbol, t, mono, bid=100.0, ask=100.1, sequence_valid=True):
    return {
        "symbol": symbol,
        "schema_version": "microflow_state_v2",
        "timestamp_exchange": t,
        "receive_monotonic_ms": mono,
        "freshness": {"connection_id": "conn-1", "sequence_valid": sequence_valid},
        "book": {
            "bid_levels": [[bid, 1.0], [bid - 0.01, 2.0]],
            "ask_levels": [[ask, 1.0], [ask + 0.01, 2.0]],
        },
    }


def test_smoke_run_reads_and_classifies_synthetic_rows(tmp_path):
    binance_root = tmp_path / "binance"
    bitget_root = tmp_path / "bitget"

    _write(
        binance_root / "depth5" / "segments" / "microflow-20260907T010000000000Z.jsonl.gz",
        [
            _binance_depth_row("BTCUSDT", 1_000, 100, 1.0, bid=100.0, ask=100.1),
            _binance_depth_row("BTCUSDT", 1_100, 110, 2.0, bid=100.5, ask=100.6),  # bid+ask moved -> transitions
            _binance_depth_row("BTCUSDT", 1_200, 999, 3.0, bid=100.5, ask=100.6),  # broken prev_final_update_id -> gap
        ],
    )
    _write(
        bitget_root / "state" / "segments" / "microflow-20260907T010000000000Z.jsonl.gz",
        [
            _bitget_state_row("BTCUSDT", 1_000, 1.0, bid=100.0, ask=100.1),
            _bitget_state_row("BTCUSDT", 1_100, 2.0, bid=100.0, ask=100.2, sequence_valid=False),
        ],
    )

    results = run(
        binance_root=binance_root,
        bitget_root=bitget_root,
        clean_epoch_id="test-epoch",
        max_rows_per_symbol=100,
        max_segments=5,
    )

    assert results["records_read"] == 5  # 3 binance BTC + 2 bitget BTC (other 11 symbols find no dir)
    assert results["records_accepted"] >= 1
    assert results["records_rejected"] == 0
    assert results["transitions"]["BEST_BID_PRICE_CHANGE"] >= 1
    assert results["transitions"]["BEST_ASK_PRICE_CHANGE"] >= 1
    assert "IMBALANCE_SIGN_CHANGE" in results["transitions"]
    assert results["continuity_status_counts"].get("GAP_DETECTED", 0) >= 1
    assert results["provenance"]["normalization_contract_version"] == "2026-09-07-v1"
    assert results["provenance"]["CLEAN_EPOCH_ID"] == "test-epoch"
    assert results["throughput_rows_per_s"] is None or results["throughput_rows_per_s"] > 0


def test_smoke_run_reports_missing_segment_dirs_without_crashing(tmp_path):
    # No segment directories exist at all for any symbol; must not raise.
    results = run(
        binance_root=tmp_path / "nowhere_binance",
        bitget_root=tmp_path / "nowhere_bitget",
        clean_epoch_id="test-epoch",
        max_rows_per_symbol=10,
        max_segments=1,
    )
    assert results["records_read"] == 0
    assert all("error" in v for v in results["per_symbol_venue"].values())


def test_smoke_run_counts_malformed_book_as_rejected(tmp_path):
    binance_root = tmp_path / "binance"
    bitget_root = tmp_path / "bitget"
    row = _binance_depth_row("ETHUSDT", 1_000, 100, 1.0)
    row["bids"] = []  # force MISSING_BID_SIDE -> MALFORMED_BOOK -> rejected
    _write(
        binance_root / "depth5" / "segments" / "microflow-20260907T010000000000Z.jsonl.gz",
        [row],
    )

    results = run(
        binance_root=binance_root,
        bitget_root=bitget_root,
        clean_epoch_id="test-epoch",
        max_rows_per_symbol=10,
        max_segments=1,
    )
    assert results["per_symbol_venue"]["binance:ETHUSDT"]["rejected"] == 1
    assert results["quality_flag_counts"]["MISSING_BID_SIDE"] == 1
