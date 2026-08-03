"""Give a bot-initiated close its exchange economics, or nothing at all.

`PositionManager` closes a position the moment it detects one is gone. At that
instant Bitget has not yet published realized PnL, so the close is written as a
provisional row with empty money columns (see `_money_or_none`). On routes where
`bitget_position_history` later writes its own record that gap closes itself.
The dead-trade-timeout route has no such second writer, so five of the first
thirteen trades under 817bc72 never reached the weekly meter or expectancy —
and those are precisely the flat-runners, whose absence biases any later
strategy analysis in the flattering direction.

This module closes that gap by asking the exchange, never by estimating:

  * poll ``/api/v2/mix/position/history-position`` a bounded number of times;
  * match one lifecycle on identity, with an explicit tolerance;
  * copy gross PnL, openFee, closeFee, funding and netProfit verbatim;
  * retire the provisional row so exactly one economic CLOSE survives.

If the exchange does not produce the lifecycle within the budget, the row stays
provisional and non-economic. That is the fail-closed outcome: a missing trade
understates the loss meter, while a fabricated 0.0 would be indistinguishable
from a real break-even trade and would corrupt the switch that halts trading.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from telemetry.close_record_sources import (
    EXCHANGE_CONFIRMED_CLOSE_SOURCES,
    PROVISIONAL_CLOSE_EVENT_TYPE,
)

log = logging.getLogger("close_reconciler")

#: Bitget publishes a closed lifecycle within seconds, but not instantly. Bounded
#: so a permanently absent lifecycle cannot spin: five attempts over ~35s.
POLL_DELAYS_S: tuple[float, ...] = (2.0, 4.0, 8.0, 8.0, 12.0)

#: `opened_at` may differ between our clock and Bitget's by a small amount.
OPENED_AT_TOLERANCE_MS = 5_000

#: Size is compared relatively; Bitget rounds to contract precision.
SIZE_TOLERANCE_REL = 0.01

#: The source we stamp on a reconciled row. Must be in the economic allowlist or
#: the reconciled trade would still count nowhere.
RECONCILED_SOURCE = "bitget_position_history"
assert RECONCILED_SOURCE in EXCHANGE_CONFIRMED_CLOSE_SOURCES


class CloseReconciliationUnavailable(RuntimeError):
    """Exchange did not publish the lifecycle within the polling budget."""


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def match_lifecycle(
    rows: list[dict],
    *,
    symbol: str,
    direction: str,
    opened_at_ms: int | None,
    size: float | None,
) -> dict | None:
    """Pick the one history row that is this lifecycle.

    Identity is deliberately layered. Symbol and side are required — they are
    never ambiguous. Beyond that we prefer the row whose open time is closest to
    ours within tolerance, because two lifecycles on the same symbol and side can
    close within the same second (a scale-out, or two cycles in a row), and a
    looser match would silently merge them into one.
    """
    want_sym = str(symbol or "").upper()
    want_side = str(direction or "").lower()
    cands = [
        r for r in rows
        if str(r.get("symbol") or "").upper() == want_sym
        and str(r.get("holdSide") or "").lower() == want_side
    ]
    if not cands:
        return None

    if size is not None:
        sized = []
        for r in cands:
            got = _f(r.get("closeTotalPos")) or _f(r.get("openTotalPos"))
            if got is None or size <= 0:
                continue
            if abs(got - size) / size <= SIZE_TOLERANCE_REL:
                sized.append(r)
        if not sized:
            # A supplied exchange-confirmed size is part of the lifecycle
            # identity. Falling back to symbol/side after every candidate
            # disagrees can attach another trade's money to this close.
            return None
        cands = sized

    if opened_at_ms is not None:
        within = [
            r for r in cands
            if abs(int(r.get("ctime") or 0) - opened_at_ms) <= OPENED_AT_TOLERANCE_MS
        ]
        if not within:
            return None
        cands = within
        cands.sort(key=lambda r: abs(int(r.get("ctime") or 0) - opened_at_ms))
        return cands[0]

    if len(cands) > 1:
        # No open time to disambiguate and several candidates: refuse rather than
        # guess which lifecycle the money belongs to.
        return None
    return cands[0]


def economics_from_history(row: dict) -> dict:
    """Copy the money out of a history row. No derivation, no defaults."""
    gross = _f(row.get("pnl"))
    net = _f(row.get("netProfit"))
    open_fee = _f(row.get("openFee"))
    close_fee = _f(row.get("closeFee"))
    funding = _f(row.get("totalFunding"))
    if (
        gross is None or net is None or open_fee is None
        or close_fee is None or funding is None
    ):
        raise CloseReconciliationUnavailable(
            "history row lacks a money field: pnl={!r} netProfit={!r} openFee={!r} closeFee={!r} totalFunding={!r}".format(
                row.get("pnl"), row.get("netProfit"), row.get("openFee"),
                row.get("closeFee"), row.get("totalFunding"))
        )
    return {
        "gross_pnl": gross,
        "net_pnl": net,
        "open_fee": abs(open_fee),
        "close_fee": abs(close_fee),
        "fees": abs(open_fee) + abs(close_fee),
        "funding": funding,
        "exit_price": _f(row.get("closeAvgPrice")),
        "entry_price": _f(row.get("openAvgPrice")),
        "size": _f(row.get("closeTotalPos")),
        "position_id": str(row.get("positionId") or ""),
        "sync_source": RECONCILED_SOURCE,
    }


def reconcile_close(
    *,
    symbol: str,
    direction: str,
    opened_at_ms: int | None,
    size: float | None,
    fetch_history: Callable[[], list[dict]],
    sleep: Callable[[float], None] = time.sleep,
    delays: tuple[float, ...] = POLL_DELAYS_S,
) -> dict:
    """Return exchange economics for one just-closed lifecycle.

    Raises `CloseReconciliationUnavailable` when the exchange has not published
    it within the budget. Callers must leave the provisional row alone on that
    path — never substitute zeros.
    """
    last_err: Exception | None = None
    for attempt, delay in enumerate(delays, start=1):
        try:
            rows = fetch_history() or []
        except Exception as exc:  # transport/auth failures are not "no trade"
            last_err = exc
            log.warning(
                "CLOSE_RECONCILE_FETCH_FAILED | %s | attempt=%s/%s | error=%s",
                symbol, attempt, len(delays), exc,
            )
            rows = []
        else:
            hit = match_lifecycle(
                rows, symbol=symbol, direction=direction,
                opened_at_ms=opened_at_ms, size=size,
            )
            if hit is not None:
                econ = economics_from_history(hit)
                log.warning(
                    "CLOSE_RECONCILED_FROM_EXCHANGE | %s | %s | attempt=%s | "
                    "gross=%s fees=%s funding=%s net=%s position_id=%s",
                    symbol, direction, attempt, econ["gross_pnl"], econ["fees"],
                    econ["funding"], econ["net_pnl"], econ["position_id"],
                )
                return econ
        if attempt < len(delays):
            sleep(delay)

    log.critical(
        "CLOSE_RECONCILE_UNAVAILABLE | %s | %s | attempts=%s | "
        "row stays provisional and counts nowhere | last_error=%s",
        symbol, direction, len(delays), last_err,
    )
    raise CloseReconciliationUnavailable(
        f"no exchange lifecycle for {symbol} {direction} after {len(delays)} attempts"
    )


def is_provisional(row: dict) -> bool:
    return str(row.get("event_type") or "").strip().upper() == PROVISIONAL_CLOSE_EVENT_TYPE


__all__ = [
    "CloseReconciliationUnavailable",
    "OPENED_AT_TOLERANCE_MS",
    "POLL_DELAYS_S",
    "RECONCILED_SOURCE",
    "SIZE_TOLERANCE_REL",
    "economics_from_history",
    "is_provisional",
    "match_lifecycle",
    "reconcile_close",
]
