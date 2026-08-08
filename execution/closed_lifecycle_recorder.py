"""Turn a confirmed bot close into exactly one economic CLOSE, or leave it open.

This is the wiring between `close_futures_position_full` (which proves the
exchange is flat) and `close_reconciler` (which fetches what the lifecycle was
worth). Every bot-initiated close route funnels through it:

    execution_service.py:1534   fail-safe close
    position_manager.py:782     residual cleanup
    position_manager.py:1176    tp3 close-all
    position_manager.py:1404    dead-trade timeout
    bitget_rest.py:70           emergency flatten

They all call `close_futures_position_full`, which only reports
``status="CLOSED"`` after re-reading the position and finding remaining size 0.
Anything short of that is `CLOSE_FULL_POSITION_REMAINS`, and this module refuses
to record economics for it — a local CLOSED before exchange flatness is how a
position gets forgotten while it is still live.

The ordering is deliberate:

    exchange flat (verified)  ->  provisional row  ->  reconcile  ->  economic row

The provisional row exists so a close is never invisible, even if the process
dies before Bitget publishes the lifecycle. `recover_provisional_closes` sweeps
those up later, bounded and idempotent.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from execution.close_dedup import (
    DedupOutcome,
    SegmentUnreadable,
    economic_close_status,
    read_segment,
    segment_paths,
)
from execution.close_reconciler import (
    AmbiguousLifecycle,
    CloseReconciliationUnavailable,
    economics_from_history,
    is_provisional,
    match_lifecycle,
    reconcile_close,
)

log = logging.getLogger("closed_lifecycle_recorder")

#: `close_futures_position_full` uses this exact status for "exchange is flat".
EXCHANGE_FLAT_STATUS = "CLOSED"

#: A recovery sweep must not walk the whole history forever.
MAX_RECOVERY_PER_SWEEP = 20


def exchange_confirmed_flat(close_result: Any) -> bool:
    """True only when the client proved remaining size is 0.

    `close_futures_position_full` sets ``status="CLOSED"`` after a fresh
    position re-read; every other outcome (``CLOSE_FULL_POSITION_REMAINS``, a
    transport error, a bare API acknowledgement) means we do not know the
    position is gone. An API response alone is never enough.
    """
    if not isinstance(close_result, dict):
        return False
    status = str(close_result.get("status") or "").strip().upper()
    flatness = str(close_result.get("flatness") or "").strip().upper()
    remaining = _f(close_result.get("remaining_size"))
    return status in {EXCHANGE_FLAT_STATUS, "NO_POSITION"} and flatness == "FLAT" and remaining == 0.0


def _emit_outcome_telemetry(
    position: dict,
    economics: Any,
    *,
    source: str,
    log_: logging.Logger | None = None,
) -> None:
    """Research telemetry, fired after the economic close is already on disk.

    Placed after ``write_economic_close`` on purpose. It inherits that call's
    dedup -- a duplicate close is refused before reaching here, and a
    provisional close never gets this far -- so one lifecycle yields exactly one
    outcome row, written at the moment its economics became authoritative.

    Nothing it does may reach the caller. The close is already recorded; a
    research row failing to write is not a reason to change what happens next,
    and the import is local so a broken telemetry module cannot stop this file
    from loading in a close path.
    """
    logger = log_ or log
    try:
        from execution.entry_outcome import emit_close_outcome

        emit_close_outcome(position, economics, source=source, log=logger)
    except Exception as exc:  # noqa: BLE001 - telemetry is never load-bearing
        logger.warning(
            "ENTRY_OUTCOME_TELEMETRY_SKIPPED | %s | source=%s | error=%s",
            str(position.get("symbol") or "UNKNOWN"), source, exc,
        )


def record_closed_lifecycle(
    *,
    position: dict,
    close_result: Any,
    dataset_path: str,
    fetch_history: Callable[[], list[dict]],
    write_economic_close: Callable[[dict, dict], None],
    retire_provisional: Callable[[dict], None] | None = None,
    reconcile: Callable[..., dict] = reconcile_close,
) -> str:
    """Record one confirmed close. Returns the outcome for logging/tests.

    Outcomes:
      ``NOT_FLAT``      exchange did not confirm remaining size 0; nothing written
      ``ALREADY``       an economic CLOSE for this lifecycle already exists
      ``RECONCILED``    exchange economics written, provisional retired
      ``PROVISIONAL``   exchange has not published yet; row stays non-economic
      ``BLOCKED_UNREADABLE``  the dataset could not be read, so whether an
                        economic CLOSE already exists is unknown; nothing
                        written, and the row stays provisional for a later sweep
    """
    symbol = str(position.get("symbol") or "").upper()
    lifecycle = str(position.get("position_lifecycle_id") or "")

    if not exchange_confirmed_flat(close_result):
        log.error(
            "CLOSE_NOT_EXCHANGE_FLAT | %s | lifecycle=%s | status=%r | "
            "no economic close recorded",
            symbol, lifecycle or "UNKNOWN",
            (close_result or {}).get("status") if isinstance(close_result, dict) else close_result,
        )
        return "NOT_FLAT"

    dedup = economic_close_status(dataset_path, position)
    if dedup is DedupOutcome.BLOCKED_UNREADABLE:
        # Storage could not be read, so we cannot tell a first write from a
        # second one. Refuse both: the provisional row survives and the recovery
        # sweep retries once the segment is readable again.
        log.critical(
            "CLOSE_DEDUP_UNCERTAIN | %s | lifecycle=%s | no economic close recorded | "
            "row stays PROVISIONAL",
            symbol, lifecycle or "UNKNOWN",
        )
        return "BLOCKED_UNREADABLE"
    if dedup is DedupOutcome.FOUND:
        log.info(
            "CLOSE_ALREADY_RECONCILED | %s | lifecycle=%s | second write refused",
            symbol, lifecycle or "UNKNOWN",
        )
        return "ALREADY"

    try:
        econ = reconcile(
            symbol=symbol,
            direction=str(position.get("direction") or ""),
            opened_at_ms=position.get("opened_at_ms"),
            size=_f(position.get("confirmed_position_size") or position.get("position_size")),
            exchange_position_id=position.get("exchange_position_id"),
            fetch_history=fetch_history,
        )
    except CloseReconciliationUnavailable as exc:
        # Position really is closed; we simply do not know its economics yet.
        log.critical(
            "CLOSE_ECONOMICS_PENDING | %s | lifecycle=%s | reason=%s | "
            "row stays PROVISIONAL and counts in no risk decision",
            symbol, lifecycle or "UNKNOWN", exc,
        )
        return "PROVISIONAL"

    write_economic_close(position, econ)
    _emit_outcome_telemetry(position, econ, source="reconciled")
    if retire_provisional is not None:
        retire_provisional(position)
    log.warning(
        "CLOSE_ECONOMICS_RECORDED | %s | lifecycle=%s | net=%s | fees=%s | funding=%s",
        symbol, lifecycle or "UNKNOWN", econ["net_pnl"], econ["fees"], econ["funding"],
    )
    return "RECONCILED"


def record_known_economics_close(
    *,
    position: dict,
    economics: Any,
    dataset_path: str,
    write_economic_close: Callable[[dict, Any], None],
    log_: logging.Logger | None = None,
) -> str:
    """Record a close whose exchange economics are already in hand.

    The exchange-disappearance path fetches position-history itself, so there is
    nothing left to reconcile — but that is no reason for it to be the one close
    path that writes straight to the dataset. Routing it here puts every close
    behind the same dedup, and, more importantly, gives the caller an *outcome*.
    The direct writer returned None, so a refused write was invisible: the
    position was retired as CLOSED_SYNCED while nothing economic and nothing
    provisional reached the dataset, and no sweep would ever look for it again.

    Outcomes:
      ``RECORDED``            written
      ``ALREADY``             an economic CLOSE for this lifecycle already exists
      ``BLOCKED_UNREADABLE``  dedup could not read storage; nothing written
      ``NO_IDENTITY``         nothing to dedup on; nothing written
    """
    logger = log_ or log
    symbol = str(position.get("symbol") or "").upper()

    candidate = dict(position)
    if isinstance(economics, Mapping):
        for source, target in (
            ("exchange_position_id", "exchange_position_id"),
            ("symbol", "symbol"),
            ("open_time", "exchange_open_time"),
            ("size", "confirmed_position_size"),
        ):
            value = economics.get(source)
            if value not in (None, ""):
                candidate[target] = value

    lifecycle = str(candidate.get("position_lifecycle_id") or "")
    position_id = str(candidate.get("exchange_position_id") or "").strip()
    if not position_id and not lifecycle:
        # Without an identity a second run cannot recognise this row, so writing
        # it would make a duplicate indistinguishable from a first write.
        logger.critical(
            "CLOSE_ECONOMICS_NO_IDENTITY | %s | economics carry no exchange position id | "
            "nothing written",
            symbol or "UNKNOWN",
        )
        return "NO_IDENTITY"

    dedup = economic_close_status(dataset_path, candidate)
    if dedup is DedupOutcome.BLOCKED_UNREADABLE:
        logger.critical(
            "CLOSE_ECONOMICS_DEDUP_UNCERTAIN | %s | lifecycle=%s | position_id=%s | "
            "no economic close recorded",
            symbol or "UNKNOWN", lifecycle or "UNKNOWN", position_id or "UNKNOWN",
        )
        return "BLOCKED_UNREADABLE"
    if dedup is DedupOutcome.FOUND:
        logger.info(
            "CLOSE_ALREADY_RECONCILED | %s | lifecycle=%s | position_id=%s | second write refused",
            symbol or "UNKNOWN", lifecycle or "UNKNOWN", position_id or "UNKNOWN",
        )
        return "ALREADY"

    write_economic_close(position, economics)
    _emit_outcome_telemetry(position, economics, source="known_economics", log_=logger)
    logger.warning(
        "CLOSE_ECONOMICS_RECORDED | %s | lifecycle=%s | position_id=%s | source=known_economics",
        symbol or "UNKNOWN", lifecycle or "UNKNOWN", position_id or "UNKNOWN",
    )
    return "RECORDED"


def reconcile_fail_safe_close(
    *,
    lifecycle_identity: dict | None,
    close_result: Any,
    dataset_path: str,
    fetch_history: Callable[[], list[dict]],
    write_economic_close: Callable[[dict, dict], None],
    log_: logging.Logger | None = None,
    reconcile: Callable[..., dict] = reconcile_close,
) -> str:
    """Record economics for a close made by the entry flow's fail-safe.

    Separate entry point from `record_closed_lifecycle` only because the caller
    holds an identity dict rather than a position dict — the fail-safe fires
    before PositionManager has ever seen the position. Everything downstream is
    the same code path, so there is no second copy of the reconciliation or
    dataset rules living in ExecutionService.

    Returns ``NO_IDENTITY`` when the position was never confirmed on the
    exchange, which is the one case a position-based caller cannot hit.
    """
    lg = log_ or log
    if not lifecycle_identity or not lifecycle_identity.get("opened_at_ms"):
        # Without the exchange's own open time, the only way to find this
        # lifecycle in position-history would be symbol+side — and two
        # lifecycles on one symbol and side can close in the same second.
        lg.critical(
            "FAIL_SAFE_CLOSE_NO_IDENTITY | symbol=%s | side=%s | size=%s | "
            "available=%s | no economic close recorded",
            (lifecycle_identity or {}).get("symbol"),
            (lifecycle_identity or {}).get("hold_side"),
            (lifecycle_identity or {}).get("confirmed_position_size"),
            sorted((lifecycle_identity or {}).keys()) or "none",
        )
        return "NO_IDENTITY"

    return record_closed_lifecycle(
        position=dict(lifecycle_identity),
        close_result=close_result,
        dataset_path=dataset_path,
        fetch_history=fetch_history,
        write_economic_close=write_economic_close,
        reconcile=reconcile,
    )


def _parse_cursor(value: Any) -> tuple | None:
    """Turn a persisted cursor back into a comparable key, or None.

    Anything unrecognisable means "start from the beginning" — a lost cursor
    costs one pass of fairness, never a skipped row.
    """
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        return (int(value[0]), str(value[1]), str(value[2]), int(value[3]))
    except (TypeError, ValueError):
        return None


def _serialize_cursor(key: tuple | None) -> list | None:
    return list(key) if key is not None else None


def _select_fair_window(
    unresolved: list[dict], *, limit: int, cursor: tuple | None,
) -> tuple[list[dict], tuple | None]:
    """Pick this sweep's batch, resuming after the cursor and wrapping around.

    Returns the batch and the cursor for the next sweep. When everything fits in
    one batch the cursor is cleared: there is nothing to rotate past, and keeping
    a stale key would make the next sweep start in the middle for no reason.
    """
    if limit <= 0 or not unresolved:
        return [], None
    ordered = sorted(unresolved, key=_oldest_key)
    if len(ordered) <= limit:
        return ordered, None

    start = 0
    if cursor is not None:
        start = next(
            (i for i, row in enumerate(ordered) if _oldest_key(row) > cursor),
            0,  # cursor is at or past the end: wrap to the oldest row
        )
    end = start + limit
    window = ordered[start:end] if end <= len(ordered) else ordered[start:] + ordered[: end - len(ordered)]
    return window, _oldest_key(window[-1])


def recover_provisional_closes(
    *,
    provisional_rows: list[dict],
    dataset_path: str,
    fetch_history: Callable[[], list[dict]],
    write_economic_close: Callable[[dict, dict], None],
    retire_provisional: Callable[[dict], None] | None = None,
    reconcile: Callable[..., dict] = reconcile_close,
    limit: int = MAX_RECOVERY_PER_SWEEP,
    cursor: Any = None,
    load_blocked: bool = False,
) -> dict:
    """Fill in economics for closes that were provisional at the time.

    Runs at startup and on a periodic sweep, so a lifecycle Bitget published
    late — or one whose process died mid-poll — is not lost forever. Bounded by
    `limit`, and idempotent: a lifecycle that already has an economic CLOSE is
    skipped without touching the exchange.

    Selection rotates. Taking `unresolved[:limit]` off an oldest-first list looks
    fair and is not: a row the exchange can no longer produce — too old for the
    history window — never resolves, stays oldest, and holds its slot forever.
    Twenty of those starve every newer row behind them permanently. So the sweep
    resumes after the row it last attempted and wraps around, which keeps the
    order deterministic and the batch bounded while guaranteeing every row is
    reached within one full pass.

    `cursor` is the `next_cursor` of the previous sweep. Losing it costs
    fairness for one pass, never correctness, so a crash between the write and
    the cursor save simply repeats a window that dedup then skips.
    """
    stats = {
        "seen": 0, "skipped": 0, "recovered": 0, "still_pending": 0,
        "ambiguous": 0, "blocked": False, "unresolved_total": 0,
        "next_cursor": _serialize_cursor(_parse_cursor(cursor)),
    }

    if load_blocked:
        # The row list is known to be incomplete, so nothing here can be trusted:
        # not the dedup verdicts, not "unresolved_total", and certainly not a
        # cursor advance past rows we never saw. Refuse the whole sweep and let
        # the startup gate keep new entries out until storage is readable.
        stats["blocked"] = True
        log.critical(
            "CLOSE_RECOVERY_ABORTED_LOAD_UNREADABLE | visible_rows=%s | "
            "0 economic writes | 0 retires | cursor unchanged",
            len(provisional_rows),
        )
        return stats

    unresolved: list[dict] = []
    for row in sorted(provisional_rows, key=_oldest_key):
        dedup = economic_close_status(dataset_path, row)
        if dedup is DedupOutcome.BLOCKED_UNREADABLE:
            # One unreadable segment invalidates the sweep, not just this row:
            # every remaining candidate would be deduped against the same
            # unknown dataset. Abort before anything is written.
            stats["blocked"] = True
            log.critical(
                "CLOSE_RECOVERY_ABORTED_DEDUP_UNCERTAIN | symbol=%s | lifecycle=%s | "
                "0 economic writes | sweep retries when storage is readable",
                row.get("symbol") or "UNKNOWN",
                row.get("position_lifecycle_id") or "UNKNOWN",
            )
            return stats
        if dedup is DedupOutcome.FOUND:
            stats["skipped"] += 1
        else:
            unresolved.append(row)
    stats["unresolved_total"] = len(unresolved)
    selected, next_cursor = _select_fair_window(
        unresolved, limit=max(0, limit), cursor=_parse_cursor(cursor),
    )
    stats["next_cursor"] = _serialize_cursor(next_cursor)
    if not selected:
        return stats
    try:
        history_rows = fetch_history()
        if not isinstance(history_rows, list):
            raise CloseReconciliationUnavailable("position history response is not a list")
    except Exception as exc:
        stats["blocked"] = True
        stats["still_pending"] = len(selected)
        log.critical("CLOSE_RECOVERY_HISTORY_UNKNOWN | selected=%s | error=%s", len(selected), exc)
        return stats

    for row in selected:
        stats["seen"] += 1
        try:
            hit = match_lifecycle(
                history_rows,
                symbol=str(row.get("symbol") or "").upper(),
                direction=str(row.get("direction") or ""),
                opened_at_ms=_opened_at_ms(row),
                size=_f(row.get("confirmed_position_size") or row.get("position_size")),
                exchange_position_id=row.get("exchange_position_id"),
            )
            if hit is None:
                raise CloseReconciliationUnavailable("no unambiguous lifecycle match")
            econ = economics_from_history(hit)
        except (CloseReconciliationUnavailable, AmbiguousLifecycle) as exc:
            stats["still_pending"] += 1
            # Ambiguity is not the same failure as "the exchange has not
            # published it yet": it means several lifecycles fit and picking one
            # would be a guess. Counted separately so a caller can refuse on it
            # by name instead of reading it out of a lumped pending total.
            if isinstance(exc, AmbiguousLifecycle):
                stats["ambiguous"] += 1
            log.critical(
                "CLOSE_RECOVERY_PENDING | symbol=%s | opened_at=%s | ambiguous=%s | error=%s",
                row.get("symbol"), row.get("opened_at"),
                isinstance(exc, AmbiguousLifecycle), exc,
            )
            continue
        write_economic_close(row, econ)
        if retire_provisional is not None:
            retire_provisional(row)
        stats["recovered"] += 1
    log.warning("CLOSE_ECONOMICS_RECOVERY | %s", stats)
    return stats


@dataclass(frozen=True)
class ProvisionalLoad:
    """What the loader could actually establish about the dataset.

    ``blocked`` is the whole point. A segment that exists but cannot be read
    used to be logged and skipped, so its pending rows simply vanished from the
    result. The sweep then reported ``unresolved_total=0``, the startup verdict
    read COMPLETE, and the bot opened new positions while closes it had never
    seen were sitting in an unreadable file. An incomplete list must announce
    that it is incomplete.
    """

    rows: list[dict]
    blocked: bool
    unreadable_segments: tuple[str, ...] = ()
    error_types: tuple[str, ...] = ()
    segment_paths: tuple[str, ...] = ()


def load_provisional_rows(dataset_path: str) -> ProvisionalLoad:
    """Read provisional rows from the active dataset and every numeric rotation.

    Uses the same strict reader as close-dedup, so both agree on what corruption
    means: permission and I/O errors, decode failures, malformed CSV, a missing
    or invalid header, absent required columns and truncated rows are all
    unreadable. An absent optional rotation is not — its emptiness is provable.
    """
    try:
        paths = segment_paths(dataset_path)
    except OSError as exc:
        log.critical(
            "CLOSE_RECOVERY_SEGMENT_DISCOVERY_FAILED | dataset=%s | error=%s | "
            "provisional rows UNKNOWN | recovery blocked",
            dataset_path, type(exc).__name__,
        )
        return ProvisionalLoad(
            rows=[], blocked=True,
            unreadable_segments=(str(dataset_path),),
            error_types=(type(exc).__name__,),
        )

    rows: list[dict] = []
    unreadable: list[str] = []
    error_types: list[str] = []
    for path in paths:
        try:
            segment = read_segment(path)
        except SegmentUnreadable as exc:
            unreadable.append(exc.path)
            error_types.append(exc.reason)
            log.critical(
                "CLOSE_RECOVERY_SEGMENT_UNREADABLE | path=%s | reason=%s | "
                "pending rows in this segment are UNKNOWN | recovery blocked",
                exc.path, exc.reason,
            )
            continue
        for index, row in enumerate(segment, start=2):
            if is_provisional(row):
                row["_recovery_segment"] = str(path)
                row["_recovery_line"] = index
                rows.append(row)

    return ProvisionalLoad(
        rows=rows,
        blocked=bool(unreadable),
        unreadable_segments=tuple(unreadable),
        error_types=tuple(error_types),
        segment_paths=tuple(str(p) for p in paths),
    )


def _opened_at_ms(row: dict) -> int | None:
    for name in ("exchange_open_time", "opened_at_ms", "open_time"):
        value = row.get(name)
        if value not in (None, ""):
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return None
    value = row.get("opened_at")
    if value:
        try:
            return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
        except (TypeError, ValueError):
            return None
    return None


def _oldest_key(row: dict) -> tuple:
    return (
        _opened_at_ms(row) or 2**63 - 1,
        str(row.get("timestamp") or ""),
        str(row.get("_recovery_segment") or ""),
        int(row.get("_recovery_line") or 0),
    )


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "EXCHANGE_FLAT_STATUS",
    "reconcile_fail_safe_close",
    "MAX_RECOVERY_PER_SWEEP",
    "exchange_confirmed_flat",
    "recover_provisional_closes",
    "ProvisionalLoad",
    "load_provisional_rows",
    "record_closed_lifecycle",
]
