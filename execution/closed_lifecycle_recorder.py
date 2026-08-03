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
import csv
from datetime import datetime
from typing import Any, Callable

from execution.close_dedup import DedupOutcome, economic_close_status, segment_paths
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
    if retire_provisional is not None:
        retire_provisional(position)
    log.warning(
        "CLOSE_ECONOMICS_RECORDED | %s | lifecycle=%s | net=%s | fees=%s | funding=%s",
        symbol, lifecycle or "UNKNOWN", econ["net_pnl"], econ["fees"], econ["funding"],
    )
    return "RECONCILED"


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


def recover_provisional_closes(
    *,
    provisional_rows: list[dict],
    dataset_path: str,
    fetch_history: Callable[[], list[dict]],
    write_economic_close: Callable[[dict, dict], None],
    retire_provisional: Callable[[dict], None] | None = None,
    reconcile: Callable[..., dict] = reconcile_close,
    limit: int = MAX_RECOVERY_PER_SWEEP,
) -> dict:
    """Fill in economics for closes that were provisional at the time.

    Runs at startup and on a periodic sweep, so a lifecycle Bitget published
    late — or one whose process died mid-poll — is not lost forever. Bounded by
    `limit`, and idempotent: a lifecycle that already has an economic CLOSE is
    skipped without touching the exchange.
    """
    stats = {
        "seen": 0, "skipped": 0, "recovered": 0, "still_pending": 0,
        "blocked": False, "unresolved_total": 0,
    }
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
    selected = unresolved[:max(0, limit)]
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
            log.critical(
                "CLOSE_RECOVERY_PENDING | symbol=%s | opened_at=%s | error=%s",
                row.get("symbol"), row.get("opened_at"), exc,
            )
            continue
        write_economic_close(row, econ)
        if retire_provisional is not None:
            retire_provisional(row)
        stats["recovered"] += 1
    log.warning("CLOSE_ECONOMICS_RECOVERY | %s", stats)
    return stats


def load_provisional_rows(dataset_path: str) -> list[dict]:
    """Read provisional rows from the active dataset and every numeric rotation."""
    rows: list[dict] = []
    for path in segment_paths(dataset_path):
        try:
            with path.open("r", newline="", encoding="utf-8", errors="replace") as handle:
                for index, row in enumerate(csv.DictReader(handle), start=2):
                    if is_provisional(row):
                        row["_recovery_segment"] = str(path)
                        row["_recovery_line"] = index
                        rows.append(row)
        except OSError as exc:
            log.warning("CLOSE_RECOVERY_SEGMENT_READ_FAILED | path=%s | error=%s", path, exc)
    return rows


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
    "load_provisional_rows",
    "record_closed_lifecycle",
]
