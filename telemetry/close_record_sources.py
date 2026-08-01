"""One authority for which close records may carry money.

Two independent consumers decide real things from the v2 close dataset:
``risk/symbol_expectancy.py`` (per-symbol directional edge) and
``RiskManager._weekly_realized_pnl`` (the WEEKLY_FREEZE_LOSS_PCT kill-switch).
They used to answer "is this row trustworthy?" separately — expectancy filtered
on ``sync_source`` while the weekly meter filtered only on ``event_type``. A
provisional ``position_manager`` row therefore stayed out of expectancy but was
summed into the freeze meter, which is exactly how a ROI percentage written into
a USDT field reached a live kill-switch.

Both now import from here, so the two can no longer drift apart.
"""

from __future__ import annotations

#: Event types that represent a finished, economically meaningful close.
#: ``CLOSE_PROVISIONAL`` and ``CLOSE_QUARANTINED`` are deliberately absent: the
#: first has no exchange-confirmed money yet, the second was retired by
#: migration. Both stay fully readable for audit while counting nowhere.
ECONOMIC_CLOSE_EVENT_TYPES = frozenset({"CLOSE", "POSITION_CLOSED"})

#: A close whose economics came from the exchange rather than local bookkeeping.
#: Anything outside this set is excluded rather than trusted:
#:   * ``position_manager``                     — internal bookkeeping;
#:   * ``unprotected_position_emergency_close`` — internal emergency path;
#:   * numeric/empty values                     — CSV column shift observed on
#:     live rows; admitting them would silently trust mis-parsed records.
#: Simulation output never carries any of these values, which is what keeps
#: simulated and live results from being pooled.
EXCHANGE_CONFIRMED_CLOSE_SOURCES = frozenset({
    "validated_exchange_position_closed_sync",
    "bitget_position_history",
    "bitget_export_backfill",
    "bitget_position_history_manual_backfill_20260712",
})

#: Written when a position is known to be closed but the exchange has not yet
#: supplied realized PnL. Carries ``margin_roi_pct`` for observability and leaves
#: every money column empty, so no consumer can mistake a percentage for USDT.
PROVISIONAL_CLOSE_EVENT_TYPE = "CLOSE_PROVISIONAL"

#: Set by the one-off migration on rows that already stored a percentage as USDT.
QUARANTINED_CLOSE_EVENT_TYPE = "CLOSE_QUARANTINED"


#: Writers that are known *not* to be exchange truth. Kept explicit so display
#: surfaces can drop them by name without also dropping legacy rows that predate
#: the ``sync_source`` column entirely.
NON_EXCHANGE_CLOSE_SOURCES = frozenset({
    "position_manager",
    "unprotected_position_emergency_close",
    "simulated",
    "backtest",
})


def is_economic_close(row: dict) -> bool:
    """True when this row may contribute money to a risk decision.

    Fails closed: an unknown, empty, or numeric ``sync_source`` is not counted.
    Money-critical callers — the weekly freeze meter, expectancy — must never
    guess, because guessing wrong halts live trading on a phantom loss.
    """
    event_type = str(row.get("event_type") or "").strip().upper()
    if event_type not in ECONOMIC_CLOSE_EVENT_TYPES:
        return False
    source = str(row.get("sync_source") or "").strip()
    return source in EXCHANGE_CONFIRMED_CLOSE_SOURCES


def is_displayable_close(row: dict) -> bool:
    """True when a read-only surface may show this row as a money result.

    Deliberately weaker than :func:`is_economic_close`: a dashboard should keep
    rendering historical closes written before ``event_type`` and ``sync_source``
    existed, so *absent* fields are allowed. What is never allowed is a row that
    explicitly declares itself provisional or retired, or one from a writer known
    to carry percentages rather than USDT — those are the rows that would show a
    ROI figure as a P&L.

    Never use this for a risk decision.
    """
    event_type = str(row.get("event_type") or "").strip().upper()
    if event_type and event_type not in ECONOMIC_CLOSE_EVENT_TYPES:
        return False
    return str(row.get("sync_source") or "").strip().lower() not in NON_EXCHANGE_CLOSE_SOURCES


__all__ = [
    "ECONOMIC_CLOSE_EVENT_TYPES",
    "EXCHANGE_CONFIRMED_CLOSE_SOURCES",
    "NON_EXCHANGE_CLOSE_SOURCES",
    "PROVISIONAL_CLOSE_EVENT_TYPE",
    "QUARANTINED_CLOSE_EVENT_TYPE",
    "is_displayable_close",
    "is_economic_close",
]
