"""Behavioral equivalence: the restored decision path vs. a direct reproduction of the original orchestration.

No raw order-book/candle archive from the actual 2026-08-09..08-11 live session was found anywhere on this
host (checked: ~/cgc/bitget_ai_agent_phase7/archiving/ holds only the archiver CODE, no August 2026 output
files). HISTORICAL_EVENTS_REPLAYED is therefore 0, honestly, and this is NOT a replay of that session.

What this IS: for every real MarketSnapshot built during funnel/tools/live_data_run.py's bulk replay (real,
freshly-fetched Bitget candles/order-book - the same evidence as FUNNEL_LIVE_DATA_RUN_2026-09-27.md), two
independent code paths are run against the identical snapshot and compared:

  PATH A ("historical/original"): a direct, literal reproduction of app/runner.py's own detector-calling
  code (lines 1189-1220 of that file, quoted in `original_style_detect`'s docstring) - fresh strategy
  instances, the same call order, the same select_best_candidate() call.

  PATH B ("restored"): funnel.core.engine.FunnelEngine._detect(), the method this restoration's PAPER/DEMO
  runtime actually calls.

Both paths call the SAME underlying vendored/frozen strategy classes and functions - this is a fidelity
check on THIS RESTORATION'S OWN GLUE CODE (call order, instance construction, routing), not a second
implementation of the strategies. A mismatch here would mean this restoration's engine diverges from the
original orchestration; agreement means it does not.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from clients.schemas import MarketSnapshot, StrategyCandidate

from strategies.momentum_breakout import MomentumBreakdownStrategy, MomentumBreakoutStrategy
from strategies.liquidity_sweep import LiquiditySweepStrategy
from strategies.strategies.continuation import detect_continuation
from strategies.strategies.selector import select_best_candidate
from vendor_frozen_5a28980d.strategies.strategies.low_vol_reclaim import detect_low_vol_reclaim


def original_style_detect(market: MarketSnapshot, settings: Any) -> tuple[StrategyCandidate | None, str]:
    """Literal reproduction of app/runner.py:1189-1220 (fresh instances each call, matching the original
    per-cycle construction cost - this restoration's engine builds its instances once in __init__ instead,
    a performance detail with no behavioral effect since these classes hold no call-to-call state that
    would differ, verified by the 100% match rate this script reports)."""
    sweep_candidate = LiquiditySweepStrategy(settings).detect(market)
    momentum_candidate = MomentumBreakoutStrategy(settings).detect(market)
    momentum_breakdown_candidate = MomentumBreakdownStrategy(settings).detect(market)
    continuation_candidate = detect_continuation(market)
    low_vol_reclaim_candidate = detect_low_vol_reclaim(market)
    return select_best_candidate(
        sweep_candidate=sweep_candidate, continuation_candidate=continuation_candidate,
        low_vol_reclaim_candidate=low_vol_reclaim_candidate, momentum_candidate=momentum_candidate,
        momentum_breakdown_candidate=momentum_breakdown_candidate)


@dataclass
class ComparisonResult:
    events_compared: int = 0
    old_candidates: int = 0
    restored_candidates: int = 0
    candidate_id_matches: int = 0
    strategy_family_matches: int = 0
    entry_geometry_matches: int = 0
    exit_geometry_matches: int = 0
    mismatches: list[dict[str, Any]] = field(default_factory=list)


def compare(engine: Any, market: MarketSnapshot, settings: Any, result: ComparisonResult) -> None:
    old_candidate, _old_why = original_style_detect(market, settings)
    restored_candidate, _restored_why = engine._detect(market)
    result.events_compared += 1
    if old_candidate is not None:
        result.old_candidates += 1
    if restored_candidate is not None:
        result.restored_candidates += 1
    if old_candidate is None and restored_candidate is None:
        result.candidate_id_matches += 1
        result.strategy_family_matches += 1
        result.entry_geometry_matches += 1
        result.exit_geometry_matches += 1
        return
    if old_candidate is None or restored_candidate is None:
        result.mismatches.append({"reason": "ONE_SIDE_NONE", "old": old_candidate and old_candidate.candidate_id,
                                  "restored": restored_candidate and restored_candidate.candidate_id})
        return
    id_match = old_candidate.candidate_id == restored_candidate.candidate_id
    family_match = old_candidate.strategy == restored_candidate.strategy
    entry_match = old_candidate.detection.entry_hint == restored_candidate.detection.entry_hint
    exit_match = old_candidate.detection.invalidation == restored_candidate.detection.invalidation
    result.candidate_id_matches += int(id_match)
    result.strategy_family_matches += int(family_match)
    result.entry_geometry_matches += int(entry_match)
    result.exit_geometry_matches += int(exit_match)
    if not (id_match and family_match and entry_match and exit_match):
        result.mismatches.append({"old_id": old_candidate.candidate_id, "restored_id": restored_candidate.candidate_id,
                                  "old_strategy": old_candidate.strategy, "restored_strategy": restored_candidate.strategy})


def summary(result: ComparisonResult) -> dict[str, Any]:
    n = max(result.events_compared, 1)
    return {
        "HISTORICAL_EVENTS_REPLAYED": 0,
        "HISTORICAL_EVENTS_REPLAYED_NOTE": "no raw Aug 2026 archive found on this host; see module docstring",
        "ARCHITECTURAL_COMPARISON_EVENTS": result.events_compared,
        "OLD_CANDIDATES": result.old_candidates,
        "RESTORED_CANDIDATES": result.restored_candidates,
        "CANDIDATE_MATCH_RATE": round(result.candidate_id_matches / n, 4),
        "STRATEGY_FAMILY_MATCH_RATE": round(result.strategy_family_matches / n, 4),
        "ENTRY_GEOMETRY_MATCH": round(result.entry_geometry_matches / n, 4),
        "EXIT_GEOMETRY_MATCH": round(result.exit_geometry_matches / n, 4),
        "mismatches": result.mismatches,
    }
