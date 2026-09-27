"""Adapter: the LSRL engine's RiskGate -> the project's CENTRAL RiskManager.

This module (unlike lsrl/core) is allowed to import project code. It builds the
canonical StrategyCandidate for an LSRL proposal, runs the unmodified central
`RiskManager.evaluate()` with a fixed frozen score (the same convention as
MicroFlow: the score is not part of a frozen-policy strategy), and translates the
verdict into a RiskDecision. Central limits only ever REDUCE LSRL risk:

  * verdict.account_risk_pct (percent) caps the risk fraction (probe / session multipliers);
  * the leverage-derived notional ceiling and, in LIVE, the flat per-trade notional cap
    scale the risk fraction down so the planned notional fits (never up).

The evaluated verdict is retained per setup so the live executor can record the
mandatory risk authorization for each order against exactly this verdict.
"""
from __future__ import annotations

from typing import Any

from candidate_lifecycle import deterministic_candidate_id
from clients.schemas import (ContractSpec, MarketSnapshot, RiskVerdict, StrategyCandidate, StrategyScore,
                             SweepDetection, TimeframeSnapshot)
from lsrl.core.config import STRATEGY_ID
from lsrl.core.gates import RiskDecision, RiskProposal


def build_candidate(proposal: RiskProposal) -> StrategyCandidate:
    """The canonical risk-schema view of an LSRL proposal (mirrors microflow's adapter)."""
    d = proposal.direction.value
    mid = sum(proposal.entry_prices) / len(proposal.entry_prices)
    tf = TimeframeSnapshot(symbol=proposal.symbol, granularity="lsrl_1m", latest_close=mid, change_pct=0.0,
                           range_pct=0.0, volume_ratio_20=0.0, ema20=mid, ema50=mid, trend="neutral",
                           candles=[], closed_candle_timestamp_ms=proposal.t_ms,
                           as_of_timestamp_ms=proposal.t_ms)
    market = MarketSnapshot(
        symbol=proposal.symbol,
        contract=ContractSpec(symbol=proposal.symbol, product_type="USDT-FUTURES", quote_coin="USDT",
                              base_coin=proposal.symbol.removesuffix("USDT"), status="live",
                              min_trade_num=None, size_multiplier=None, price_place=None,
                              volume_24h_usdt=None, change_pct_24h=None, raw={}),
        primary=tf, confirmation=tf, alignment="neutral", score_hint=100.0, notes=[])
    detection = SweepDetection(side=d, swept_level=proposal.entry_prices[0], sweep_extreme=proposal.entry_prices[-1],
                               reclaim_level=proposal.entry_prices[0], entry_hint=mid,
                               invalidation=proposal.stop, displacement_pct=0.0, bars_since_sweep=0,
                               volume_ratio_on_sweep=0.0, local_range_size_pct=0.0, reason_flags=[])
    return StrategyCandidate(
        candidate_id=deterministic_candidate_id(STRATEGY_ID, proposal.symbol, d, proposal.t_ms),
        candidate_candle_open_timestamp_ms=proposal.t_ms, symbol=proposal.symbol, strategy=STRATEGY_ID,
        direction=d, primary_granularity="lsrl_1m", confirmation_granularity="lsrl_1m", market=market,
        detection=detection,
        notes=[f"lsrl_setup_id={proposal.setup_id}", f"spread_bps={proposal.spread_bps:.4f}",
               "risk_schema_adapter=lsrl_v1"])


FROZEN_SCORE = StrategyScore(total=100.0, breakdown={"frozen_lsrl_v1": 100.0}, verdict="GO", reasons=[])


class CentralRiskGate:
    SAFETY = 0.999          # the central exposure gates reject at >= their limit: stay strictly under

    def __init__(self, risk_manager: Any, settings: Any) -> None:
        self.risk_manager = risk_manager
        self.settings = settings
        self.verdicts: dict[str, tuple[RiskVerdict, StrategyCandidate]] = {}

    def _ceiling(self, proposal: RiskProposal) -> tuple[float | None, list[str]]:
        """Largest notional the central limits leave room for (None = cannot be determined).

        Uses the same settings the RiskManager's own gates use: leverage, MAX_TOTAL_EXPOSURE_PCT,
        MAX_CLUSTER_EXPOSURE_PCT and (in LIVE) the flat per-trade cap. The RiskManager still
        arbitrates the final proposal; this only lets LSRL size DOWN into the room that exists
        instead of always being vetoed for a risk-derived notional that is too large."""
        rm, s, nav = self.risk_manager, self.settings, proposal.nav_usdt
        notes: list[str] = []
        leverage = min(float(getattr(s, "default_leverage", 5.0)), float(getattr(s, "max_leverage", 5.0)),
                       float(getattr(rm, "SAFE_ALPHA_MAX_LEVERAGE", 8)))
        ceiling = nav * leverage
        notes.append(f"leverage_ceiling={ceiling:.2f}")
        positions = rm._load_open_positions()
        if positions is None:
            return None, ["open_positions_unreadable"]
        total_open = sum(float(p.get("position_notional_usdt") or 0.0) for p in positions)
        total_room = nav * float(getattr(s, "max_total_exposure_pct", 200.0)) / 100.0 - total_open
        ceiling = min(ceiling, total_room * self.SAFETY)
        notes.append(f"portfolio_room={total_room:.2f}")
        cluster = rm._cluster_for_symbol(proposal.symbol)
        if cluster:
            in_cluster = sum(float(p.get("position_notional_usdt") or 0.0) for p in positions
                             if rm._cluster_for_symbol(str(p.get("symbol") or "")) == cluster)
            cluster_room = nav * float(getattr(s, "max_cluster_exposure_pct", 120.0)) / 100.0 - in_cluster
            ceiling = min(ceiling, cluster_room * self.SAFETY)
            notes.append(f"cluster_room={cluster_room:.2f}")
        flat = float(getattr(s, "execution_max_live_notional_per_trade_usdt", 0.0) or 0.0)
        if getattr(s, "is_live_execution", False) is True and flat > 0:
            ceiling = min(ceiling, flat)
            notes.append(f"live_flat_notional_cap={flat}")
        return ceiling, notes

    def check(self, proposal: RiskProposal) -> RiskDecision:
        candidate = build_candidate(proposal)
        ceiling, notes = self._ceiling(proposal)
        scale = 1.0
        if ceiling is not None and ceiling > 0 and proposal.planned_notional_usdt > ceiling:
            scale = ceiling / proposal.planned_notional_usdt
            notes.append(f"risk_scaled_to_fit_central_limits={scale:.4f}")
        verdict = self.risk_manager.evaluate(
            candidate, FROZEN_SCORE, observed_equity=proposal.nav_usdt,
            proposed_notional_usdt=proposal.planned_notional_usdt * scale)
        self.verdicts[proposal.setup_id] = (verdict, candidate)
        if not verdict.allowed:
            return RiskDecision(False, tuple(verdict.reasons), None, "RiskManager")
        applied = min(proposal.strategy_risk_fraction * scale, float(verdict.account_risk_pct) / 100.0)
        return RiskDecision(True, (*verdict.reasons, f"central_risk_pct={verdict.account_risk_pct}", *notes),
                            applied, "RiskManager")

    def take_verdict(self, setup_id: str) -> tuple[RiskVerdict, StrategyCandidate] | None:
        return self.verdicts.pop(setup_id, None)
