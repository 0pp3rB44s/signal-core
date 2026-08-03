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
import logging
from datetime import datetime
from pathlib import Path

from telemetry.close_record_sources import is_economic_close

log = logging.getLogger("close_dedup")
OPEN_TIME_TOLERANCE_MS = 5_000
SIZE_TOLERANCE_REL = 0.01


def segment_paths(dataset: Path | str) -> list[Path]:
    """The active dataset plus its rotated siblings, newest first."""
    p = Path(dataset)
    rotated: list[tuple[int, Path]] = []
    for seg in p.parent.glob(f"{p.name}.*"):
        suffix = seg.name.removeprefix(f"{p.name}.")
        if suffix.isdigit():
            rotated.append((int(suffix), seg))
    out = [p] + [seg for _, seg in sorted(rotated)]
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
    size = str(row.get("confirmed_position_size") or row.get("position_size") or "").strip()
    if sym and direction and opened:
        keys.add(("composite", sym, direction, opened, size))
    return keys


def _side(row: dict) -> str:
    value = str(row.get("direction") or row.get("side") or row.get("hold_side") or "").upper()
    return {"LONG": "LONG", "SHORT": "SHORT"}.get(value, "")


def _open_ms(row: dict) -> int | None:
    for field in ("exchange_open_time", "opened_at_ms", "open_time"):
        value = row.get(field)
        if value not in (None, ""):
            try:
                parsed = int(float(value))
                return parsed if parsed > 0 else None
            except (TypeError, ValueError):
                return None
    value = row.get("opened_at")
    if value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return int(parsed.timestamp() * 1000)
        except (TypeError, ValueError):
            return None
    return None


def _size(row: dict) -> float | None:
    for field in ("confirmed_position_size", "position_size", "exchange_truth_size", "size"):
        value = row.get(field)
        if value not in (None, ""):
            try:
                parsed = float(value)
                return parsed if parsed > 0 else None
            except (TypeError, ValueError):
                return None
    return None


def _composite_matches(existing: dict, candidate: dict) -> bool:
    if str(existing.get("symbol") or "").upper() != str(candidate.get("symbol") or "").upper():
        return False
    if not _side(existing) or _side(existing) != _side(candidate):
        return False
    existing_open, candidate_open = _open_ms(existing), _open_ms(candidate)
    existing_size, candidate_size = _size(existing), _size(candidate)
    if None in (existing_open, candidate_open, existing_size, candidate_size):
        return False
    if abs(existing_open - candidate_open) > OPEN_TIME_TOLERANCE_MS:
        return False
    return abs(existing_size - candidate_size) / candidate_size <= SIZE_TOLERANCE_REL


def economic_close_exists(dataset: Path | str, candidate: dict) -> bool:
    """True when an economic CLOSE for this lifecycle is already on disk.

    Only economic rows block a write. A provisional row does not: replacing it
    is the whole point of reconciliation.
    """
    rows = [row for row in _read(segment_paths(dataset)) if is_economic_close(row)]
    position_id = str(candidate.get("exchange_position_id") or candidate.get("position_id") or "").strip()
    if position_id and any(str(row.get("exchange_position_id") or "").strip() == position_id for row in rows):
        return True
    lifecycle_id = str(candidate.get("position_lifecycle_id") or "").strip()
    if lifecycle_id and any(str(row.get("position_lifecycle_id") or "").strip() == lifecycle_id for row in rows):
        return True
    order_id = str(
        candidate.get("exchange_order_id")
        or candidate.get("exchange_entry_order_id")
        or ""
    ).strip()
    if order_id:
        hits = [row for row in rows if order_id in {
            str(row.get("exchange_order_id") or "").strip(),
            str(row.get("exchange_entry_order_id") or "").strip(),
        }]
        if hits:
            return True
    composite = [row for row in rows if _composite_matches(row, candidate)]
    if len(composite) > 1:
        log.critical(
            "CLOSE_DEDUP_AMBIGUOUS_COMPOSITE | symbol=%s | side=%s | matches=%s | write_blocked=True",
            candidate.get("symbol"), _side(candidate), len(composite),
        )
        return True
    return len(composite) == 1


def provisional_close_exists(dataset: Path | str, candidate: dict) -> bool:
    """True when this lifecycle already has a durable provisional marker."""
    rows = [
        row for row in _read(segment_paths(dataset))
        if str(row.get("event_type") or "").strip().upper() == "CLOSE_PROVISIONAL"
    ]
    position_id = str(candidate.get("exchange_position_id") or "").strip()
    if position_id and any(str(row.get("exchange_position_id") or "").strip() == position_id for row in rows):
        return True
    lifecycle_id = str(candidate.get("position_lifecycle_id") or "").strip()
    if lifecycle_id and any(str(row.get("position_lifecycle_id") or "").strip() == lifecycle_id for row in rows):
        return True
    order_id = str(candidate.get("exchange_order_id") or candidate.get("exchange_entry_order_id") or "").strip()
    if order_id and any(order_id in {
        str(row.get("exchange_order_id") or "").strip(),
        str(row.get("exchange_entry_order_id") or "").strip(),
    } for row in rows):
        return True
    return any(_composite_matches(row, candidate) for row in rows)


__all__ = [
    "OPEN_TIME_TOLERANCE_MS",
    "SIZE_TOLERANCE_REL",
    "economic_close_exists",
    "provisional_close_exists",
    "lifecycle_keys",
    "segment_paths",
]
