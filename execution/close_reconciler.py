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
import math
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
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
#: Retained as the *forward* half of the observation window below, and still
#: exported because callers and tests refer to it by name.
OPENED_AT_TOLERANCE_MS = 5_000

#: EXCHANGE EVENT TIME vs LOCAL OBSERVATION TIME.
#:
#: A history row's `ctime`/`utime` are the moments Bitget opened and closed the
#: position — exchange event time. Our `opened_at`/`closed_at` are the moments
#: *we* confirmed those facts — local observation time. Observation necessarily
#: trails the event: we cannot see a position before it exists. The lag is the
#: maker wait (`MAKER_ENTRY_WAIT_SECONDS`) plus fill-confirmation polling, and
#: production measurements on 2026-08-08 put it at 0.7-2.7 s for 43 of 45
#: lifecycles, 5.678 s for one and 20.828 s for another — both maker entries.
#: The old symmetric ±5 s window refused exactly those two, leaving real money
#: unreconciled and the startup gate blocking new entries.
#:
#: So the window is deliberately ASYMMETRIC:
#:
#:   backward (exchange event before our observation) -- the physically expected
#:   direction, and legitimately slow. Bounded at two minutes: 5.8x the worst
#:   lag ever measured, while the nearest WRONG candidate in the same production
#:   data sat 244,103,578 ms (2.8 days) away. Roughly three orders of magnitude
#:   of headroom on both sides.
#:
#:   forward (exchange event AFTER our observation) -- physically impossible for
#:   a genuine observation, so it is only ever host clock skew. Kept at the
#:   original 5 s: wide enough to absorb ordinary drift, tight enough that a
#:   badly skewed clock cannot silently drop the true candidate and promote a
#:   wrong one to "unique".
#:
#: Widening the backward bound does NOT weaken the guarantee. The guarantee is
#: uniqueness, not proximity: two survivors still raise `AmbiguousLifecycle`.
#: A wider window can only turn a refusal into an ambiguity — never a refusal
#: into a wrong match.
OPEN_OBSERVATION_MAX_MS = 120_000
CLOSE_OBSERVATION_MAX_MS = 120_000

#: Size is compared relatively; Bitget rounds to contract precision.
SIZE_TOLERANCE_REL = 0.01

#: The source we stamp on a reconciled row. Must be in the economic allowlist or
#: the reconciled trade would still count nowhere.
RECONCILED_SOURCE = "bitget_position_history"
assert RECONCILED_SOURCE in EXCHANGE_CONFIRMED_CLOSE_SOURCES


class CloseReconciliationUnavailable(RuntimeError):
    """Exchange did not publish the lifecycle within the polling budget."""


class AmbiguousLifecycle(CloseReconciliationUnavailable):
    """More than one exchange lifecycle satisfies the available identity."""


@dataclass(frozen=True)
class ExchangeCloseEconomics(Mapping[str, Any]):
    gross_pnl: float
    open_fee: float
    close_fee: float
    funding: float
    net_profit: float
    exchange_position_id: str
    symbol: str
    side: str
    open_time: int
    close_time: int
    size: float
    open_price: float
    close_price: float
    sync_source: str = RECONCILED_SOURCE

    @property
    def fees(self) -> float:
        return abs(self.open_fee) + abs(self.close_fee)

    def _mapping(self) -> dict[str, Any]:
        # Compatibility aliases are read-only and keep old callers from
        # reinterpreting netProfit as generic gross PnL.
        return {
            "gross_pnl": self.gross_pnl,
            "open_fee": abs(self.open_fee),
            "close_fee": abs(self.close_fee),
            "funding": self.funding,
            "net_profit": self.net_profit,
            "net_pnl": self.net_profit,
            "fees": self.fees,
            "exchange_position_id": self.exchange_position_id,
            "position_id": self.exchange_position_id,
            "symbol": self.symbol,
            "side": self.side,
            "open_time": self.open_time,
            "close_time": self.close_time,
            "size": self.size,
            "open_price": self.open_price,
            "entry_price": self.open_price,
            "close_price": self.close_price,
            "exit_price": self.close_price,
            "sync_source": self.sync_source,
        }

    def __getitem__(self, key: str) -> Any:
        return self._mapping()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._mapping())

    def __len__(self) -> int:
        return len(self._mapping())


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _observation_lag_ok(event_ms: Any, observed_ms: int, backward_max: int) -> bool:
    """True when `observed_ms` can be an observation of the event at `event_ms`.

    `lag = observed - event`. Positive means we saw it after it happened, which
    is the only physically possible order and may be slow (see
    `OPEN_OBSERVATION_MAX_MS`). Negative means the exchange stamped the event
    after we claim to have seen it, which can only be clock skew and is allowed
    just `OPENED_AT_TOLERANCE_MS`.
    """
    try:
        event = int(event_ms or 0)
    except (TypeError, ValueError):
        return False
    if event <= 0:
        return False
    lag = observed_ms - event
    return -OPENED_AT_TOLERANCE_MS <= lag <= backward_max


def match_lifecycle(
    rows: list[dict],
    *,
    symbol: str,
    direction: str,
    opened_at_ms: int | None,
    size: float | None,
    exchange_position_id: str | None = None,
    closed_at_ms: int | None = None,
) -> dict | None:
    """Pick the one history row that is this lifecycle.

    Identity is deliberately layered. Symbol and side are required — they are
    never ambiguous. A unique exchange identifier resolves outright. The
    composite fallback only matches when exactly one row satisfies every
    tolerance; distance inside the window is never a tie-breaker, because two
    lifecycles on one symbol and side can close in the same second and picking
    the nearer one would be a guess. Historically we preferred the row whose
    open time was closest to
    ours within tolerance, because two lifecycles on the same symbol and side can
    close within the same second (a scale-out, or two cycles in a row), and a
    looser match would silently merge them into one.

    The composite route filters, in order: symbol, side, a positive size within
    `SIZE_TOLERANCE_REL`, the open observation window, and — when the caller
    supplies `closed_at_ms` — the close observation window. Exactly one survivor
    matches; none returns ``None``; more than one raises `AmbiguousLifecycle`.
    Every filter can only shrink the candidate set, so each added axis is
    strictly safer than omitting it.
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

    wanted_position_id = str(exchange_position_id or "").strip()
    if wanted_position_id:
        exact = [r for r in cands if str(r.get("positionId") or "").strip() == wanted_position_id]
        if len(exact) > 1:
            raise AmbiguousLifecycle("duplicate exchange positionId in position history")
        return exact[0] if exact else None

    if opened_at_ms is None or size is None or size <= 0:
        # Without a strong exchange id the fallback identity is deliberately
        # composite. Dropping either open time or size would allow a different
        # lifecycle on the same symbol/side to inherit this money.
        return None

    sized = []
    for r in cands:
        got = _f(r.get("closeTotalPos")) or _f(r.get("openTotalPos"))
        if got is None:
            continue
        if abs(got - size) / size <= SIZE_TOLERANCE_REL:
            sized.append(r)
    if not sized:
        return None
    cands = sized

    # Open axis: exchange `ctime` (event) against our `opened_at` (observation).
    within = [
        r for r in cands
        if _observation_lag_ok(r.get("ctime"), opened_at_ms, OPEN_OBSERVATION_MAX_MS)
    ]
    if not within:
        return None
    cands = within

    # Close axis: exchange `utime` against our `closed_at`. A second, independent
    # time axis, applied only when the caller has an observed close — recovery
    # reads it off the provisional row. It can only ever REMOVE candidates, so a
    # caller without it is exactly as safe as before, just less discriminating.
    if closed_at_ms is not None:
        closed_within = [
            r for r in cands
            if _observation_lag_ok(r.get("utime"), closed_at_ms, CLOSE_OBSERVATION_MAX_MS)
        ]
        if not closed_within:
            return None
        cands = closed_within

    if len(cands) > 1:
        # Distance inside the tolerance window is NOT a tie-breaker. Two rows
        # that both satisfy symbol, side, size and open-time tolerance are
        # indistinguishable evidence: picking the nearer one is a guess, and a
        # wrong guess attaches one lifecycle's money to another. Preferring the
        # closest was exactly the misattribution this fails closed on.
        raise AmbiguousLifecycle(
            "multiple lifecycle rows satisfy the composite fallback: "
            + ", ".join(
                "positionId={} ctime={} size={}".format(
                    r.get("positionId"), r.get("ctime"),
                    r.get("closeTotalPos") or r.get("openTotalPos"),
                )
                for r in cands
            )
        )
    return cands[0]


def economics_from_history(row: dict) -> ExchangeCloseEconomics:
    """Copy the money out of a history row. No derivation, no defaults."""
    gross = _f(row.get("pnl"))
    net = _f(row.get("netProfit"))
    open_fee = _f(row.get("openFee"))
    close_fee = _f(row.get("closeFee"))
    funding = _f(row.get("totalFunding"))
    required_numbers = {
        "pnl": gross,
        "netProfit": net,
        "openFee": open_fee,
        "closeFee": close_fee,
        "totalFunding": funding,
        "closeTotalPos": _f(row.get("closeTotalPos")),
        "openAvgPrice": _f(row.get("openAvgPrice")),
        "closeAvgPrice": _f(row.get("closeAvgPrice")),
    }
    missing = [name for name, value in required_numbers.items() if value is None]
    symbol = str(row.get("symbol") or "").upper()
    side = str(row.get("holdSide") or "").lower()
    position_id = str(row.get("positionId") or "").strip()
    try:
        open_time = int(row.get("ctime"))
        close_time = int(row.get("utime"))
    except (TypeError, ValueError):
        open_time = close_time = 0
    if missing or not symbol or side not in {"long", "short"} or not position_id or open_time <= 0 or close_time <= 0:
        raise CloseReconciliationUnavailable(
            f"history row lacks required economics/identity fields: missing={missing} "
            f"symbol={symbol!r} side={side!r} position_id={position_id!r} times={open_time}/{close_time}"
        )
    try:
        expected = (
            Decimal(str(gross))
            - abs(Decimal(str(open_fee)))
            - abs(Decimal(str(close_fee)))
            + Decimal(str(funding))
        )
        actual = Decimal(str(net))
    except InvalidOperation as exc:
        raise CloseReconciliationUnavailable("invalid decimal economics") from exc
    if abs(expected - actual) > Decimal("0.0000001"):
        log.critical(
            "CLOSE_ECONOMICS_MISMATCH | position_id=%s | expected=%s | netProfit=%s",
            position_id, expected, actual,
        )
        raise CloseReconciliationUnavailable(
            f"exchange economics mismatch: formula={expected} netProfit={actual}"
        )
    return ExchangeCloseEconomics(
        gross_pnl=gross,
        open_fee=abs(open_fee),
        close_fee=abs(close_fee),
        funding=funding,
        net_profit=net,
        exchange_position_id=position_id,
        symbol=symbol,
        side=side,
        open_time=open_time,
        close_time=close_time,
        size=required_numbers["closeTotalPos"],
        open_price=required_numbers["openAvgPrice"],
        close_price=required_numbers["closeAvgPrice"],
    )


def reconcile_close(
    *,
    symbol: str,
    direction: str,
    opened_at_ms: int | None,
    size: float | None,
    exchange_position_id: str | None = None,
    closed_at_ms: int | None = None,
    fetch_history: Callable[[], list[dict]],
    sleep: Callable[[float], None] = time.sleep,
    delays: tuple[float, ...] = POLL_DELAYS_S,
) -> ExchangeCloseEconomics:
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
                exchange_position_id=exchange_position_id,
                closed_at_ms=closed_at_ms,
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
    "AmbiguousLifecycle",
    "ExchangeCloseEconomics",
    "CLOSE_OBSERVATION_MAX_MS",
    "OPENED_AT_TOLERANCE_MS",
    "OPEN_OBSERVATION_MAX_MS",
    "POLL_DELAYS_S",
    "RECONCILED_SOURCE",
    "SIZE_TOLERANCE_REL",
    "economics_from_history",
    "is_provisional",
    "match_lifecycle",
    "reconcile_close",
]
