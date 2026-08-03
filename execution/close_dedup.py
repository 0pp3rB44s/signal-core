"""Decide whether an economic CLOSE for a lifecycle already exists.

Reconciliation may run more than once for the same position — a retry, a restart
mid-poll, or a later sweep. Writing a second economic row would double-count the
trade in the weekly freeze meter, which is the same failure class as the ROI
percentage that once inflated a week's loss to 7.37%.

Identity is layered, strongest first:

  1. ``position_lifecycle_id``  — our own identifier, unique per lifecycle
  2. exchange ``positionId`` / entry order id  — unique per lifecycle at Bitget
  3. symbol + direction + opened_at second  — last resort, and deliberately
     strict: two real lifecycles on the same symbol and side *can* close in the
     same second, so the composite key uses the OPEN second, not the close
     second, and still refuses to merge rows whose sizes disagree.

Rotated segments count. `_rotate_on_schema_change` moves the active dataset to
`.csv.1` whenever columns are added, so a lifecycle written before a rotation
lives in a different file than its reconciliation. Ignoring those files would
make a duplicate look like a first write.
"""

from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path

from telemetry.close_record_sources import is_economic_close

#: How many rotated segments to consult. Rotation is rare; two is generous.
ROTATED_SEGMENTS = 3


def segment_paths(dataset: Path | str) -> list[Path]:
    """The active dataset plus its rotated siblings, newest first."""
    p = Path(dataset)
    out = [p]
    for i in range(1, ROTATED_SEGMENTS + 1):
        seg = p.with_name(f"{p.name}.{i}")
        if seg.exists():
            out.append(seg)
    return [x for x in out if x.exists()]


def _read(paths: list[Path]) -> list[dict]:
    rows: list[dict] = []
    for p in paths:
        try:
            with p.open("r", newline="", encoding="utf-8", errors="replace") as fh:
                rows.extend(csv.DictReader(fh))
        except Exception:
            continue
    return rows


def lifecycle_keys(row: dict) -> set[tuple]:
    """Every identity this row can be recognised by."""
    keys: set[tuple] = set()
    lid = str(row.get("position_lifecycle_id") or "").strip()
    if lid:
        keys.add(("lifecycle", lid))
    for field in ("exchange_position_id", "exchange_entry_order_id", "exchange_order_id"):
        oid = str(row.get(field) or "").strip()
        if oid:
            keys.add(("order", oid))
    sym = str(row.get("symbol") or "").upper()
    direction = str(row.get("direction") or "").upper()
    opened = str(row.get("opened_at") or "")[:19]
    raw_size = row.get("confirmed_position_size") or row.get("position_size") or ""
    try:
        size = format(Decimal(str(raw_size)).normalize(), "f") if raw_size != "" else ""
    except (InvalidOperation, ValueError):
        size = str(raw_size).strip()
    if sym and direction and opened:
        keys.add(("composite", sym, direction, opened, size))
    return keys


def economic_close_exists(dataset: Path | str, candidate: dict) -> bool:
    """True when an economic CLOSE for this lifecycle is already on disk.

    Only economic rows block a write. A provisional row does not: replacing it
    is the whole point of reconciliation.
    """
    want = lifecycle_keys(candidate)
    if not want:
        return False
    for row in _read(segment_paths(dataset)):
        if not is_economic_close(row):
            continue
        if lifecycle_keys(row) & want:
            return True
    return False


__all__ = ["ROTATED_SEGMENTS", "economic_close_exists", "lifecycle_keys", "segment_paths"]
