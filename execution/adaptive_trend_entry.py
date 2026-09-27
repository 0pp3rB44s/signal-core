"""AdaptiveTrend candidate -> TradePlan -> ExecutionService.execute().

This is the piece the shadow-only modules (adaptive_trend_runtime.py,
adaptive_trend_shadow.py) are deliberately forbidden from ever containing --
see their own AST-verified isolation tests. It exists as a separate,
explicit call site so that boundary stays true: nothing in the pure
signal-evaluation path can ever submit an order, only this orchestration
layer can, and only when the caller (app/runner.py) chooses to invoke it.

Current status: reachable, and live-eligible once ADAPTIVE_TREND_LIVE_ENTRY_ENABLED=true
(execution/execution_service.py's HYBRID SAFE MODE gate recognises
"adaptive_trend_tsmom_v1" via is_trailing_stop_only_strategy() + that flag --
see is_adaptive_trend_live there). That gate is the authoritative live-entry
switch; ADAPTIVE_TREND_RETIRED (checked here and, defensively, in that same
gate) is a separate, later off switch for when the strategy is withdrawn --
neither flag substitutes for the other.
"""

from __future__ import annotations

import logging

from clients.schemas import ExecutionReport, TradePlan
from execution.execution_service import ExecutionService
from strategies.adaptive_trend_plan import build_trade_plan
from strategies.adaptive_trend_tsmom import SignalCandidate, SizingResult

logger = logging.getLogger("adaptive_trend")


def submit_adaptive_trend_entry(
    *, winner: SignalCandidate | None, winner_sizing: SizingResult | None,
    weekly_freeze_active: bool, execution_service: ExecutionService,
    retired: bool = False,
) -> ExecutionReport | None:
    """Build a TradePlan for the ranked winner and route it through the real
    execution path, if -- and only if -- there is a winner with accepted
    sizing AND the weekly freeze is not active AND the strategy is not
    retired. Returns None otherwise (no winner, sizing rejected, frozen, or
    retired -- the same conditions under which route_selected_candidate
    would have logged a rejection-reason shadow record instead of an
    actionable one).

    `weekly_freeze_active` is checked here independently of the caller's own
    ADAPTIVE_TREND_LIVE_ENTRY_ENABLED gate -- the feature flag is not a
    substitute for the freeze, and size_position()/sizing.accepted alone does
    not encode freeze state (see route_selected_candidate's own docstring:
    weekly_freeze_active there only changes shadow-record observability, not
    sizing). Both gates must independently hold for an entry to happen.

    `retired` defaults False and is expected to be passed as
    settings.adaptive_trend_retired by the caller. This is a defensive,
    early check: the authoritative enforcement is execution_service.execute()'s
    own ADAPTIVE_TREND_RETIRED gate (checked before its hybrid-strategy gate,
    with the same explicit skip reason), which fires regardless of whether
    this parameter is ever passed correctly -- so a caller that forgets this
    argument does not reopen new entries.

    Never raises: a failure here must not be allowed to look like "silently
    did nothing" nor crash the caller's cycle. Any exception is logged and
    swallowed, matching the fail-safe contract of every other independent
    block in the scan cycle.
    """
    if winner is None or winner_sizing is None or not winner_sizing.accepted:
        return None
    if retired:
        logger.info("ADAPTIVE_TREND_RETIRED | symbol=%s | stage=entry_call_site", winner.symbol)
        return None
    if weekly_freeze_active:
        logger.info(
            "ADAPTIVE_TREND_ENTRY_BLOCKED_BY_WEEKLY_FREEZE | symbol=%s", winner.symbol
        )
        return None
    try:
        plan: TradePlan = build_trade_plan(winner, winner_sizing)
    except ValueError as exc:
        logger.warning("ADAPTIVE_TREND_PLAN_BUILD_REJECTED | error=%s", exc)
        return None
    try:
        reports = execution_service.execute([plan])
    except Exception as exc:
        logger.exception("ADAPTIVE_TREND_ENTRY_SUBMISSION_FAILED | symbol=%s | error=%s",
                          winner.symbol, exc)
        return None
    if not reports:
        return None
    report = reports[0]
    logger.info(
        "ADAPTIVE_TREND_ENTRY_ATTEMPTED | symbol=%s | status=%s | message=%s",
        winner.symbol, report.status, report.message,
    )
    return report
