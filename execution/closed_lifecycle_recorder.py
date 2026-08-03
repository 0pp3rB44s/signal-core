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
from typing import Any, Callable

from execution.close_dedup import economic_close_exists
from execution.close_reconciler import (
    CloseReconciliationUnavailable,
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
    return str(close_result.get("status") or "").strip().upper() == EXCHANGE_FLAT_STATUS


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

    if economic_close_exists(dataset_path, position):
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
    stats = {"seen": 0, "skipped": 0, "recovered": 0, "still_pending": 0}
    for row in provisional_rows[:limit]:
        stats["seen"] += 1
        if economic_close_exists(dataset_path, row):
            stats["skipped"] += 1
            continue
        try:
            econ = reconcile(
                symbol=str(row.get("symbol") or "").upper(),
                direction=str(row.get("direction") or ""),
                opened_at_ms=row.get("opened_at_ms"),
                size=_f(row.get("confirmed_position_size") or row.get("position_size")),
                fetch_history=fetch_history,
            )
        except CloseReconciliationUnavailable:
            stats["still_pending"] += 1
            continue
        write_economic_close(row, econ)
        if retire_provisional is not None:
            retire_provisional(row)
        stats["recovered"] += 1
    log.warning("CLOSE_ECONOMICS_RECOVERY | %s", stats)
    return stats


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "EXCHANGE_FLAT_STATUS",
    "MAX_RECOVERY_PER_SWEEP",
    "exchange_confirmed_flat",
    "recover_provisional_closes",
    "record_closed_lifecycle",
]
