"""Objective 11 + 12 — real clean-data smoke validation and throughput
benchmark.

Read-only. Never writes into ``data_store/``, never deletes or renames a
segment file, never calls any collector or production code. Run as:

    .venv/bin/python3 -m research.cross_venue.smoke_validate

Environment variables (all optional, default to this repo's own clean-epoch
data roots):

  CROSS_VENUE_BINANCE_DATA_ROOT   default: data_store/research_binance_v2
  CROSS_VENUE_BITGET_DATA_ROOT    default: data_store/research_bitget_v2_clean
  CROSS_VENUE_CLEAN_EPOCH_ID      default: clean-epoch-9a1fb33a8e22
  CROSS_VENUE_MAX_ROWS_PER_SYMBOL default: 2000
  CROSS_VENUE_MAX_SEGMENTS        default: 3 (finalized segments per symbol per venue)

This performs no return-prediction, no PnL, no threshold search. It only
counts how many rows parsed, how they were classified, and how many of the
three frozen state-transition primitives fired. That is instrumentation
validation, not research.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from pathlib import Path

from .contract import SUPPORTED_SYMBOLS
from .continuity import ContinuityStatus
from .provenance import build_provenance
from .streaming import iter_canonical_binance_depth5, iter_canonical_bitget_state
from .transitions import TransitionType, extract_transitions

REJECTED_STATUSES = frozenset({ContinuityStatus.MALFORMED_BOOK, ContinuityStatus.TIMESTAMP_INVALID})
ACCEPTED_STATUSES = frozenset({ContinuityStatus.VALID})
# everything else (VALID_AFTER_SESSION_START, RECONNECT_BOUNDARY, GAP_DETECTED,
# SEQUENCE_UNVERIFIED, STALE_STATE) is usable-with-caveats: "quality-flagged"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def run(
    *,
    binance_root: Path,
    bitget_root: Path,
    clean_epoch_id: str,
    max_rows_per_symbol: int,
    max_segments: int,
) -> dict:
    results: dict = {
        "records_read": 0,
        "records_accepted": 0,
        "records_quality_flagged": 0,
        "records_rejected": 0,
        "continuity_status_counts": Counter(),
        "quality_flag_counts": Counter(),
        "per_symbol_venue": {},
        "transitions": Counter(),
    }

    started = time.monotonic()

    for symbol in SUPPORTED_SYMBOLS:
        for venue, iterator_fn, root, stream_dir in (
            ("binance", iter_canonical_binance_depth5, binance_root, "depth5/segments"),
            ("bitget", iter_canonical_bitget_state, bitget_root, "state/segments"),
        ):
            segments_dir = root / stream_dir
            key = f"{venue}:{symbol}"
            if not segments_dir.is_dir():
                results["per_symbol_venue"][key] = {"error": f"segments dir not found: {segments_dir}"}
                continue

            records = list(
                iterator_fn(
                    segments_dir,
                    symbol,
                    max_segments=max_segments,
                    max_rows=max_rows_per_symbol,
                )
            )

            read = len(records)
            accepted = 0
            flagged = 0
            rejected = 0
            for record in records:
                results["continuity_status_counts"][record.continuity_status.value] += 1
                for flag in record.quality_flags:
                    results["quality_flag_counts"][flag] += 1
                if record.continuity_status in REJECTED_STATUSES:
                    rejected += 1
                elif record.continuity_status in ACCEPTED_STATUSES:
                    accepted += 1
                else:
                    flagged += 1

            results["records_read"] += read
            results["records_accepted"] += accepted
            results["records_quality_flagged"] += flagged
            results["records_rejected"] += rejected
            results["per_symbol_venue"][key] = {
                "read": read,
                "accepted": accepted,
                "quality_flagged": flagged,
                "rejected": rejected,
            }

            for transition in extract_transitions(records):
                results["transitions"][transition.transition_type.value] += 1

    elapsed_s = time.monotonic() - started
    results["elapsed_s"] = elapsed_s
    results["throughput_rows_per_s"] = (
        results["records_read"] / elapsed_s if elapsed_s > 0 else None
    )
    results["continuity_status_counts"] = dict(results["continuity_status_counts"])
    results["quality_flag_counts"] = dict(results["quality_flag_counts"])
    results["transitions"] = dict(results["transitions"])
    for t in TransitionType:
        results["transitions"].setdefault(t.value, 0)

    results["provenance"] = build_provenance(
        clean_epoch_id=clean_epoch_id,
        source_files=(str(binance_root), str(bitget_root)),
        source_schema=("research_binance_v2_depth", "microflow_state_v2"),
        repo_root=_repo_root(),
    ).as_dict()

    return results


def main() -> None:
    binance_root = Path(
        os.environ.get("CROSS_VENUE_BINANCE_DATA_ROOT") or "data_store/research_binance_v2"
    )
    bitget_root = Path(
        os.environ.get("CROSS_VENUE_BITGET_DATA_ROOT") or "data_store/research_bitget_v2_clean"
    )
    clean_epoch_id = os.environ.get("CROSS_VENUE_CLEAN_EPOCH_ID") or "clean-epoch-9a1fb33a8e22"
    max_rows_per_symbol = int(os.environ.get("CROSS_VENUE_MAX_ROWS_PER_SYMBOL") or 2000)
    max_segments = int(os.environ.get("CROSS_VENUE_MAX_SEGMENTS") or 3)

    results = run(
        binance_root=binance_root,
        bitget_root=bitget_root,
        clean_epoch_id=clean_epoch_id,
        max_rows_per_symbol=max_rows_per_symbol,
        max_segments=max_segments,
    )
    print(json.dumps(results, indent=2, sort_keys=True))


if __name__ == "__main__":  # pragma: no cover - process entry
    main()
