"""adaptive_trend_tsmom_v1 -- TradePlan construction for the existing,
already-proven execution pipeline.

`execution.execution_service.ExecutionService.execute()` operates on a
generic `TradePlan` (clients/schemas.py) regardless of which strategy
produced it: selection, cooldowns, Bitget-truth reconciliation, and
protection placement (`client.place_futures_protection_orders`, which SETS a
position's protection rather than stacking orders -- a re-attempt reconciles
instead of duplicating) are all strategy-agnostic. This module's only job is
turning a SignalCandidate + SizingResult into a valid TradePlan, reusing the
identity scheme (candidate_lifecycle.identity) every other strategy already
uses -- which is what makes "the same 6H candle evaluated twice produces the
identical candidate_id/plan_id" a structural guarantee rather than a promise
this module has to keep on its own.

AdaptiveTrend's exit model is trailing-stop-only: `take_profits=[]` here is a
first-class, already-supported configuration (execution_service.py guards
every `take_profits[0]`/`take_profits[1]` access with `if ... else None`), so
this does not require any new code in the entry/protection path itself.

Scope boundary carried over from the runtime module: this file constructs a
TradePlan and can prove it is valid and idempotent, but nothing in this
change set calls `ExecutionService.execute()` with it yet -- see
adaptive_trend_runtime.route_selected_candidate, which still routes every
candidate to shadow only. Wiring this adapter into a live execute() call is
explicitly deferred until the ATR trailing-stop mechanism (which has no
existing analog to reuse and is not part of this change) exists to manage
the position afterward.
"""

from __future__ import annotations

from candidate_lifecycle.identity import (
    deterministic_candidate_id,
    deterministic_plan_id,
)
from clients.schemas import TradePlan
from strategies.adaptive_trend_tsmom import (
    STRATEGY_VERSION,
    SignalCandidate,
    SizingResult,
)

STRATEGY_NAME = "adaptive_trend_tsmom_v1"


def build_trade_plan(candidate: SignalCandidate, sizing: SizingResult) -> TradePlan:
    """One TradePlan for one accepted, sized AdaptiveTrend candidate.

    Raises ValueError if `sizing.accepted` is False -- callers must check
    that themselves first (mirrors every other sizing-gated path in this
    project: a rejected sizing result never reaches plan construction).
    """
    if not sizing.accepted:
        raise ValueError(f"cannot build a plan from rejected sizing: {sizing.rejection_reason}")

    # candidate_candle_open_timestamp is the OPEN of the signal candle, not
    # its close -- deterministic_candidate_id's own contract (see
    # candidate_lifecycle/identity.py). SignalCandidate carries the CLOSE
    # timestamp (when the decision became legal to act on), so the open is
    # derived by subtracting one 6H timeframe width.
    six_h_ms = 6 * 60 * 60 * 1000
    candle_open_ms = candidate.signal_candle_close_ms - six_h_ms

    candidate_id = deterministic_candidate_id(
        strategy=STRATEGY_NAME,
        symbol=candidate.symbol,
        direction=candidate.side.value,
        candidate_candle_open_timestamp=candle_open_ms,
    )
    plan_id = deterministic_plan_id(candidate_id)

    stop = sizing.stop_distance
    entry = candidate.close
    stop_price = (entry - stop) if candidate.side.value == "LONG" else (entry + stop)

    return TradePlan(
        candidate_id=candidate_id,
        candidate_candle_open_timestamp_ms=candle_open_ms,
        plan_id=plan_id,
        symbol=candidate.symbol,
        strategy=STRATEGY_NAME,
        direction=candidate.side.value,
        verdict="EXECUTABLE",
        score=candidate.mom_strength,
        entry_prices=[entry],
        stop_loss=stop_price,
        # Trailing-stop-only exit model: no fixed take-profit levels. This is
        # a supported, guarded configuration in execution_service.py, not a
        # placeholder that needs a follow-up change.
        take_profits=[],
        risk_reward_ratio=0.0,  # not applicable -- exit is trailing, not a fixed R target
        account_risk_pct=sizing.effective_risk_pct * 100.0,  # TradePlan uses percent, not fraction
        leverage=sizing.leverage,
        position_notional_usdt=sizing.rounded_notional,
        notes=[
            f"strategy_version={STRATEGY_VERSION}",
            f"mom={candidate.mom:.6f}",
            f"atr={candidate.atr:.8f}",
            f"mom_strength={candidate.mom_strength:.4f}",
            f"initial_stop={stop_price:.8f}",
            f"risk_usdt={sizing.effective_risk_usdt:.8f}",
        ],
        reasons=["adaptive_trend_tsmom_v1: momentum threshold crossed, ATR trailing-stop exit"],
    )
