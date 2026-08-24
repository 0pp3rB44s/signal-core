"""ATR trailing-stop evaluation for an OPEN adaptive_trend_tsmom_v1 position.

No existing MicroFlow mechanism to reuse here -- MicroFlow manages stops via
discrete TP1/breakeven/profit-lock events, not a continuous per-candle ATR
ratchet, so this is genuinely new logic. It is built the same way as every
other module in this rollout: pure functions over already-tested primitives
(closed_only/unprocessed_since from adaptive_trend_candles, compute_atr/
update_trailing_stop from adaptive_trend_tsmom), with all exchange I/O left
to the caller.

Design choice that removes an entire class of restart risk: this module
does NOT persist a rolling ATR state. Every evaluation re-fetches the
candles it needs (via the caller, same `fetch_6h_candles` the signal path
already uses) and recomputes ATR fresh from real exchange data. The only
state that must survive a restart is two plain values already naturally
persisted on the local position record: `last_processed_close_ms` and the
current stop (which the exchange itself is the source of truth for, via the
open position's own `stopLoss` field) -- there is no separate "ATR history"
that can drift out of sync with reality.

Idempotency is structural, not a promise: `evaluate_trail` calls
`unprocessed_since` exactly like the signal path does, so evaluating the
same closed candle twice is a no-op by construction (NO_NEW_CANDLE), the
same guarantee already proven for signal evaluation in
test_adaptive_trend_runtime.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from strategies.adaptive_trend_candles import check_data_health, closed_only, unprocessed_since
from strategies.adaptive_trend_tsmom import (
    ATR_MULT,
    ATR_PERIOD,
    Candle6h,
    Side,
    compute_atr,
    update_trailing_stop,
)


class TrailOutcome(str, Enum):
    NO_NEW_CANDLE = "NO_NEW_CANDLE"           # same candle already processed -- idempotent no-op
    DATA_UNHEALTHY = "DATA_UNHEALTHY"          # cannot compute ATR safely -- do not touch the stop
    UNCHANGED = "UNCHANGED"                    # new candle processed, ratchet did not improve
    UPDATED = "UPDATED"                        # new candle processed, stop tightened


@dataclass(slots=True)
class TrailDecision:
    outcome: TrailOutcome
    new_stop: float | None = None
    last_processed_close_ms: int | None = None
    candle_close: float | None = None
    atr: float | None = None
    reason: str = ""


def evaluate_trail(
    *,
    symbol: str,
    side: Side,
    current_stop: float,
    candles: list[Candle6h],
    now_ms: int,
    last_processed_close_ms: int | None,
) -> TrailDecision:
    """One trailing-stop evaluation for one open position.

    Never mutates exchange or local state -- returns a decision the caller
    applies. `current_stop` must be the EXCHANGE's own reported stop (not a
    locally-cached value), so a restart naturally reconciles: whatever
    Bitget says the stop is right now is where the ratchet continues from,
    never from a stale local record and never reset to the initial stop.
    """
    closed = closed_only(candles, now_ms=now_ms)
    health = check_data_health(closed, now_ms=now_ms, max_staleness_ms=6 * 60 * 60 * 1000 + 30 * 60 * 1000,
                                min_candles=ATR_PERIOD + 1)
    if not health.ok:
        return TrailDecision(outcome=TrailOutcome.DATA_UNHEALTHY, reason=health.reason,
                              last_processed_close_ms=last_processed_close_ms)

    new_candles = unprocessed_since(closed, last_processed_close_ms=last_processed_close_ms)
    if not new_candles:
        return TrailDecision(outcome=TrailOutcome.NO_NEW_CANDLE,
                              last_processed_close_ms=last_processed_close_ms)

    latest_close_ms = new_candles[-1].close_ms
    upto_latest = [c for c in closed if c.close_ms <= latest_close_ms]
    atr = compute_atr(upto_latest, period=ATR_PERIOD)
    if atr is None or atr <= 0:
        return TrailDecision(outcome=TrailOutcome.DATA_UNHEALTHY, reason="atr_unavailable",
                              last_processed_close_ms=last_processed_close_ms)

    latest_close = upto_latest[-1].close
    candidate_stop = update_trailing_stop(current_stop, latest_close, atr, side, ATR_MULT)

    if candidate_stop == current_stop:
        return TrailDecision(outcome=TrailOutcome.UNCHANGED, new_stop=current_stop,
                              last_processed_close_ms=latest_close_ms,
                              candle_close=latest_close, atr=atr)

    return TrailDecision(outcome=TrailOutcome.UPDATED, new_stop=candidate_stop,
                          last_processed_close_ms=latest_close_ms,
                          candle_close=latest_close, atr=atr)
