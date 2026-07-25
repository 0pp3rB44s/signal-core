"""NON-PRODUCTION forward-paper smoke strategy.

This module exists for one purpose: to prove that the paper execution lifecycle
can open, manage, close, persist, restore and report a position. It is an
engineering test harness, **not** a trading edge, and every record it produces is
tagged with :data:`SMOKE_STRATEGY_NAME` so it can never be mistaken for research
data.

Safety model:
  * disabled by default (``FORWARD_PAPER_SMOKE_STRATEGY_ENABLED``);
  * force-disabled by ``Settings.enforce_forward_paper_only`` unless the runtime
    is strict forward-paper-only with execution disabled;
  * emits only a :class:`TradePlan`, which in forward-paper mode is consumed by
    ``ForwardPaperService`` — it cannot reach an order-placing code path;
  * prices come from the live snapshot, so fills are realistic.
"""

from __future__ import annotations

import logging

from app.config import Settings
from candidate_lifecycle import deterministic_candidate_id, deterministic_plan_id
from clients.schemas import MarketSnapshot, TradePlan

SMOKE_STRATEGY_NAME = "SMOKE_TEST_NON_PRODUCTION"

logger = logging.getLogger("forward_paper_smoke")


def smoke_plan(settings: Settings, snapshot: MarketSnapshot) -> TradePlan | None:
    """Build one deterministic executable paper plan from a live snapshot.

    Returns ``None`` unless the smoke strategy is enabled and the snapshot is the
    configured smoke symbol with a usable closed candle. Identity is derived from
    the closed-candle timestamp, so re-running the same candle reproduces the same
    candidate_id and the event store dedupes it instead of opening a second trade.
    """
    if not settings.forward_paper_smoke_strategy_enabled:
        return None
    if not settings.forward_paper_only:
        # Defence in depth: config already forces this off, never trust it alone.
        return None
    if str(snapshot.symbol).upper() != str(settings.forward_paper_smoke_symbol).upper():
        return None

    fill = float(getattr(snapshot.primary, "latest_close", 0.0) or 0.0)
    candle_timestamp = int(getattr(snapshot.primary, "closed_candle_timestamp_ms", 0) or 0)
    if fill <= 0 or candle_timestamp <= 0:
        return None

    direction = "LONG"
    stop_pct = abs(float(settings.forward_paper_smoke_stop_pct)) / 100.0
    target_pct = abs(float(settings.forward_paper_smoke_target_pct)) / 100.0
    if stop_pct <= 0 or target_pct <= 0:
        return None

    stop_loss = fill * (1.0 - stop_pct)
    take_profit = fill * (1.0 + target_pct)
    candidate_id = deterministic_candidate_id(
        SMOKE_STRATEGY_NAME, snapshot.symbol, direction, candle_timestamp
    )
    plan = TradePlan(
        candidate_id=candidate_id,
        candidate_candle_open_timestamp_ms=candle_timestamp,
        plan_id=deterministic_plan_id(candidate_id),
        symbol=snapshot.symbol,
        strategy=SMOKE_STRATEGY_NAME,
        direction=direction,
        verdict="EXECUTABLE",
        score=100.0,
        entry_prices=[fill],
        stop_loss=stop_loss,
        take_profits=[take_profit],
        risk_reward_ratio=target_pct / stop_pct,
        account_risk_pct=0.0,
        leverage=1.0,
        position_notional_usdt=float(settings.forward_paper_smoke_notional_usdt),
        notes=[
            "NON_PRODUCTION_SMOKE_STRATEGY",
            "lifecycle_validation_only",
            f"smoke_stop_pct={stop_pct * 100:.3f}",
            f"smoke_target_pct={target_pct * 100:.3f}",
        ],
        reasons=["forward_paper_lifecycle_smoke_test"],
    )
    logger.warning(
        "SMOKE_STRATEGY_PLAN | NON_PRODUCTION | %s | fill=%.6f | sl=%.6f | tp=%.6f | candle=%s",
        plan.symbol, fill, stop_loss, take_profit, candle_timestamp,
    )
    return plan
