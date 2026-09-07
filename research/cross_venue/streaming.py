"""Objective 12 — bounded-memory, streaming access to clean-epoch segment
files.

Every function here is a generator. Nothing reads a whole segment file, or
the whole set of segment files, into a list before yielding — the largest
retained object at any time is either one parsed JSON row or, in the
canonical-record generators, one row plus the single previous canonical
record needed for continuity/transition comparison. This module never opens
a file for writing and never deletes or moves anything under
``data_store/`` — it is read-only by construction (only ``open(path, "rb")``
/ ``gzip.open(path, "rt")`` calls appear below).
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Iterator, Optional

from .canonical import (
    CanonicalBookState,
    canonicalize_binance_depth5_row,
    canonicalize_bitget_state_row,
)


def _open_segment(path: Path):
    if path.suffix == ".gz" or path.name.endswith(".gz.tmp"):
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, "rt", encoding="utf-8")


def iter_segment_rows(path: Path) -> Iterator[dict]:
    """Yield one dict per JSON line in a single segment file.

    A malformed trailing line (possible only in an active ``.tmp`` segment
    that is being written concurrently) is silently skipped rather than
    raising — this reader must never crash on, or block, a collector that is
    still appending to the file it is reading.
    """
    with _open_segment(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def list_finalized_segments(segments_dir: Path) -> list[Path]:
    """Segment filenames embed an ISO-8601-like timestamp
    (``microflow-<UTC timestamp>Z.jsonl.gz``), so lexicographic sort is
    chronological. ``.tmp`` files (the currently-open, not-yet-rotated
    segment) are excluded by default so a bounded sample only ever reads
    data the collector has already finished writing."""
    return sorted(p for p in segments_dir.glob("*.jsonl.gz") if not p.name.endswith(".tmp"))


def iter_rows_for_symbol(
    segments_dir: Path,
    symbol: str,
    *,
    max_segments: Optional[int] = None,
    max_rows: Optional[int] = None,
) -> Iterator[dict]:
    """Stream rows for one symbol across (optionally bounded) finalized
    segments, oldest first."""
    symbol = symbol.upper()
    segments = list_finalized_segments(segments_dir)
    if max_segments is not None:
        segments = segments[-max_segments:]
    yielded = 0
    for path in segments:
        for row in iter_segment_rows(path):
            if str(row.get("symbol", "")).upper() != symbol:
                continue
            yield row
            yielded += 1
            if max_rows is not None and yielded >= max_rows:
                return


def iter_canonical_binance_depth5(
    segments_dir: Path,
    symbol: str,
    *,
    max_segments: Optional[int] = None,
    max_rows: Optional[int] = None,
) -> Iterator[CanonicalBookState]:
    prev: CanonicalBookState | None = None
    for row in iter_rows_for_symbol(
        segments_dir, symbol, max_segments=max_segments, max_rows=max_rows
    ):
        record = canonicalize_binance_depth5_row(row, prev)
        prev = record
        yield record


def iter_canonical_bitget_state(
    segments_dir: Path,
    symbol: str,
    *,
    max_segments: Optional[int] = None,
    max_rows: Optional[int] = None,
) -> Iterator[CanonicalBookState]:
    prev: CanonicalBookState | None = None
    for row in iter_rows_for_symbol(
        segments_dir, symbol, max_segments=max_segments, max_rows=max_rows
    ):
        record = canonicalize_bitget_state_row(row, prev)
        prev = record
        yield record
