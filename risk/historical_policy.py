from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from enum import Enum

from clients.schemas import MarketSnapshot, RiskVerdict, StrategyCandidate, StrategyScore


class ResearchRiskMode(str, Enum):
    PRODUCTION = "PRODUCTION"
    HISTORICAL_STRUCTURAL_ONLY = "HISTORICAL_STRUCTURAL_ONLY"
    HISTORICAL_CONSERVATIVE_PROXY = "HISTORICAL_CONSERVATIVE_PROXY"


@dataclass(frozen=True)
class HistoricalProxyConfig:
    minimum_volume_ratio: float = 0.50
    maximum_candle_range_pct: float = 5.0
    maximum_volatility_rank: float = 90.0
    minimum_reward_cost_multiple: float = 2.0

    def canonical_payload(self) -> dict[str, float]:
        return {
            "maximum_candle_range_pct": self.maximum_candle_range_pct,
            "maximum_volatility_rank": self.maximum_volatility_rank,
            "minimum_reward_cost_multiple": self.minimum_reward_cost_multiple,
            "minimum_volume_ratio": self.minimum_volume_ratio,
        }

    @property
    def configuration_hash(self) -> str:
        encoded = json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class HistoricalGateDecision:
    allowed: bool
    reasons: tuple[str, ...]
    proxy_values: dict[str, float]


_UNAVAILABLE_ORDERBOOK_NOTES = {
    "orderbook_available=false",
    "orderbook_risk_off=true",
    "risk_off_reason=orderbook_context_unavailable",
}


def historical_candidate(candidate: StrategyCandidate) -> StrategyCandidate:
    """Remove only unavailable live-orderbook markers at the explicit research boundary."""
    market_notes = [note for note in candidate.market.notes if str(note).lower() not in _UNAVAILABLE_ORDERBOOK_NOTES]
    market_notes.extend(("historical_orderbook_unavailable=true", "backtest synthetic snapshot"))
    market = replace(candidate.market, notes=market_notes)
    return replace(candidate, market=market)


def configured_round_trip_cost_bps(execution_config) -> float:
    entry_fee = execution_config.maker_fee_bps if execution_config.entry_type.upper() == "LIMIT" else execution_config.taker_fee_bps
    return float(
        2.0 * execution_config.spread_bps
        + execution_config.entry_slippage_bps
        + execution_config.exit_slippage_bps
        + entry_fee
        + execution_config.taker_fee_bps
    )


def evaluate_conservative_proxy(
    candidate: StrategyCandidate,
    execution_config,
    config: HistoricalProxyConfig,
) -> HistoricalGateDecision:
    entry = float(getattr(candidate.detection, "entry_hint", 0.0) or getattr(candidate.detection, "close", 0.0) or 0.0)
    stop = float(getattr(candidate.detection, "invalidation", 0.0) or getattr(candidate.detection, "breakout_level", 0.0) or 0.0)
    risk_per_unit = abs(entry - stop)
    tp1_reward_bps = (risk_per_unit * 0.8 / entry * 10_000.0) if entry > 0 else 0.0
    round_trip_cost_bps = configured_round_trip_cost_bps(execution_config)
    required_reward_bps = round_trip_cost_bps * config.minimum_reward_cost_multiple
    values = {
        "volume_ratio": float(candidate.market.primary.volume_ratio_20 or 0.0),
        "candle_range_pct": float(candidate.market.primary.range_pct or 0.0),
        "volatility_rank": float(candidate.market.volatility_rank or 0.0),
        "tp1_reward_bps": tp1_reward_bps,
        "round_trip_cost_bps": round_trip_cost_bps,
        "required_reward_bps": required_reward_bps,
    }
    reasons: list[str] = []
    if values["volume_ratio"] < config.minimum_volume_ratio:
        reasons.append("proxy blocked: relative candle volume below frozen floor")
    if values["candle_range_pct"] > config.maximum_candle_range_pct:
        reasons.append("proxy blocked: signal candle range above frozen extreme-range ceiling")
    if values["volatility_rank"] > config.maximum_volatility_rank:
        reasons.append("proxy blocked: volatility rank above frozen ceiling")
    if values["tp1_reward_bps"] < required_reward_bps:
        reasons.append("proxy blocked: TP1 reward below frozen round-trip-cost multiple")
    return HistoricalGateDecision(not reasons, tuple(reasons), values)


def blocked_proxy_verdict(base: RiskVerdict, decision: HistoricalGateDecision) -> RiskVerdict:
    return RiskVerdict(
        allowed=False,
        status="BLOCKED",
        reasons=list(base.reasons) + list(decision.reasons),
        account_risk_pct=base.account_risk_pct,
        leverage=base.leverage,
        max_open_positions=base.max_open_positions,
    )
