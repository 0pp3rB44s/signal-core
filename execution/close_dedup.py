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

A segment we cannot read is not an empty segment. Swallowing the error and
carrying on made dedup fail *open*: an unreadable active dataset produced zero
rows, no key matched, and the caller wrote a second economic CLOSE — double
counting realized PnL in the very meter that decides whether to keep trading.
So reading is strict, and an uninterpretable segment yields
`DedupOutcome.BLOCKED_UNREADABLE`: no write, the row stays provisional, and
recovery may retry once storage is healthy. Absent files are not blockers —
their emptiness is provable.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime
from enum import Enum
from pathlib import Path

from telemetry.close_record_sources import is_economic_close

log = logging.getLogger("close_dedup")
OPEN_TIME_TOLERANCE_MS = 5_000
SIZE_TOLERANCE_REL = 0.01


class DedupOutcome(str, Enum):
    """What the dataset can prove about a lifecycle.

    ``NOT_FOUND`` is the only outcome that permits an economic write, and it
    means every relevant segment was read *and understood*. Absence of evidence
    is not evidence of absence: a segment we could not parse becomes
    ``BLOCKED_UNREADABLE``, never an implicit "no row here".
    """

    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    BLOCKED_UNREADABLE = "BLOCKED_UNREADABLE"


class SegmentStatus(str, Enum):
    ABSENT = "ABSENT"
    READABLE = "READABLE"
    UNREADABLE = "UNREADABLE"


class SegmentUnreadable(RuntimeError):
    """A segment exists but could not be interpreted well enough to trust."""

    def __init__(self, path: Path | str, reason: str, cause: BaseException | None = None):
        self.path = str(path)
        self.reason = reason
        self.cause = cause
        detail = f" ({type(cause).__name__}: {cause})" if cause is not None else ""
        super().__init__(f"{self.path}: {reason}{detail}")


#: Without these a row cannot be classified as economic, nor matched to a
#: lifecycle. A segment lacking them is not an empty dataset — it is an
#: uninterpretable one, and pretending otherwise is how a duplicate gets written.
REQUIRED_COLUMNS = frozenset({"event_type", "symbol", "direction"})

_MISSING = object()
_EXTRA_KEY = "__close_dedup_extra_fields__"


def segment_paths(dataset: Path | str) -> list[Path]:
    """Every relevant segment: the active dataset plus its numeric rotations.

    "Relevant" is exactly the active file and `<name>.<int>` siblings, with no
    ceiling on the rotation number — `_rotate_on_schema_change` can push a
    lifecycle arbitrarily far back, and a fixed limit would silently drop old
    evidence. Non-numeric neighbours (`.tmp`, `.bak`, `.csv.gz`) are deliberately
    excluded: they are not authoritative, so their absence blocks nothing.

    Raises `OSError` when the directory itself cannot be enumerated; callers must
    treat that as uncertainty, not as an empty segment list.
    """
    p = Path(dataset)
    rotated: list[tuple[int, Path]] = []
    for seg in p.parent.glob(f"{p.name}.*"):
        suffix = seg.name.removeprefix(f"{p.name}.")
        if suffix.isdigit():
            rotated.append((int(suffix), seg))
    out = [p] + [seg for _, seg in sorted(rotated)]
    return [x for x in out if x.exists()]


def segment_status(path: Path | str) -> SegmentStatus:
    """Classify one segment without keeping its rows."""
    p = Path(path)
    try:
        if not p.exists():
            return SegmentStatus.ABSENT
    except OSError:
        return SegmentStatus.UNREADABLE
    try:
        read_segment(p)
    except SegmentUnreadable:
        return SegmentStatus.UNREADABLE
    return SegmentStatus.READABLE


def read_segment(path: Path | str) -> list[dict]:
    """Return every row of one segment, or refuse.

    Decoding is strict on purpose. The previous `errors="replace"` turned a
    corrupted byte into U+FFFD and carried on, so a mangled lifecycle id simply
    failed to match and the caller concluded "no economic close exists".
    """
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as exc:  # PermissionError and IsADirectoryError included
        raise SegmentUnreadable(p, "io_error", exc) from exc

    if not raw.strip():
        # A zero-byte or whitespace-only file provably holds no rows. That is
        # certainty, not corruption, so it must not block a first write.
        return []

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SegmentUnreadable(p, "decode_error", exc) from exc
    if "\x00" in text:
        raise SegmentUnreadable(p, "nul_byte")

    reader = csv.DictReader(io.StringIO(text), restkey=_EXTRA_KEY, restval=_MISSING)
    try:
        rows = list(reader)
        header = reader.fieldnames
    except csv.Error as exc:
        raise SegmentUnreadable(p, "csv_parse_error", exc) from exc

    if not header:
        raise SegmentUnreadable(p, "no_header")
    absent = REQUIRED_COLUMNS.difference(header)
    if absent:
        raise SegmentUnreadable(p, "missing_columns:" + ",".join(sorted(absent)))

    for index, row in enumerate(rows, start=2):
        if row.get(_EXTRA_KEY) is not None:
            raise SegmentUnreadable(p, f"row_{index}_has_more_fields_than_header")
        for column in REQUIRED_COLUMNS:
            if row.get(column) is _MISSING:
                raise SegmentUnreadable(p, f"row_{index}_truncated_at_{column}")
    return rows


def read_all_segments(dataset: Path | str) -> tuple[list[dict], list[str]]:
    """All rows across relevant segments, plus the ones we could not read.

    A non-empty second element means the dataset's contents are UNKNOWN and no
    economic write may proceed — regardless of what the readable segments said.
    """
    try:
        paths = segment_paths(dataset)
    except OSError as exc:
        log.critical(
            "CLOSE_DEDUP_SEGMENT_DISCOVERY_FAILED | dataset=%s | error=%s: %s | dedup=BLOCKED_UNREADABLE",
            dataset, type(exc).__name__, exc,
        )
        return [], [f"{dataset}: discovery_failed ({type(exc).__name__})"]

    rows: list[dict] = []
    unreadable: list[str] = []
    for path in paths:
        try:
            rows.extend(read_segment(path))
        except SegmentUnreadable as exc:
            segment_type = "active" if Path(path) == Path(dataset) else "rotation"
            log.critical(
                "CLOSE_DEDUP_SEGMENT_UNREADABLE | path=%s | segment=%s | reason=%s | "
                "error=%s | dedup=BLOCKED_UNREADABLE",
                exc.path, segment_type, exc.reason,
                type(exc.cause).__name__ if exc.cause is not None else "none",
            )
            unreadable.append(str(exc))
    return rows, unreadable


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


def _matches_economic(rows: list[dict], candidate: dict) -> bool:
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


def economic_close_status(dataset: Path | str, candidate: dict) -> DedupOutcome:
    """Whether an economic CLOSE for this lifecycle is already on disk.

    Only economic rows block a write; a provisional row does not, since
    replacing it is the whole point of reconciliation.

    Uncertainty outranks a hit. If any relevant segment is unreadable the answer
    is `BLOCKED_UNREADABLE` even when a readable rotation happens to contain a
    match — the caller must stop either way, and reporting FOUND would claim a
    completeness the read never had.
    """
    rows, unreadable = read_all_segments(dataset)
    if unreadable:
        log.critical(
            "CLOSE_DEDUP_BLOCKED | symbol=%s | lifecycle=%s | unreadable_segments=%s | "
            "existing economic row UNKNOWN | economic write refused",
            candidate.get("symbol") or "UNKNOWN",
            candidate.get("position_lifecycle_id") or "UNKNOWN",
            "; ".join(unreadable),
        )
        return DedupOutcome.BLOCKED_UNREADABLE
    economic = [row for row in rows if is_economic_close(row)]
    return DedupOutcome.FOUND if _matches_economic(economic, candidate) else DedupOutcome.NOT_FOUND


def economic_close_exists(dataset: Path | str, candidate: dict) -> bool:
    """Fail-closed boolean view of `economic_close_status`.

    `BLOCKED_UNREADABLE` reports True so that any caller which has not been
    taught the typed contract still refuses to write. Callers that must
    distinguish "already recorded" from "cannot tell" — every production writer
    does — should use `economic_close_status` instead.
    """
    return economic_close_status(dataset, candidate) is not DedupOutcome.NOT_FOUND


def _matches_provisional(rows: list[dict], candidate: dict) -> bool:
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


def provisional_close_status(dataset: Path | str, candidate: dict) -> DedupOutcome:
    """Whether this lifecycle already has a durable provisional marker."""
    rows, unreadable = read_all_segments(dataset)
    if unreadable:
        log.critical(
            "CLOSE_DEDUP_BLOCKED | symbol=%s | lifecycle=%s | unreadable_segments=%s | "
            "existing provisional row UNKNOWN | write refused",
            candidate.get("symbol") or "UNKNOWN",
            candidate.get("position_lifecycle_id") or "UNKNOWN",
            "; ".join(unreadable),
        )
        return DedupOutcome.BLOCKED_UNREADABLE
    provisional = [
        row for row in rows
        if str(row.get("event_type") or "").strip().upper() == "CLOSE_PROVISIONAL"
    ]
    return DedupOutcome.FOUND if _matches_provisional(provisional, candidate) else DedupOutcome.NOT_FOUND


def provisional_close_exists(dataset: Path | str, candidate: dict) -> bool:
    """Fail-closed boolean view of `provisional_close_status`."""
    return provisional_close_status(dataset, candidate) is not DedupOutcome.NOT_FOUND


__all__ = [
    "OPEN_TIME_TOLERANCE_MS",
    "REQUIRED_COLUMNS",
    "SIZE_TOLERANCE_REL",
    "DedupOutcome",
    "SegmentStatus",
    "SegmentUnreadable",
    "economic_close_exists",
    "economic_close_status",
    "provisional_close_exists",
    "provisional_close_status",
    "lifecycle_keys",
    "read_all_segments",
    "read_segment",
    "segment_paths",
    "segment_status",
]
