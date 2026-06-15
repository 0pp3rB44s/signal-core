from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MarketRegime = Literal["trend", "breakout", "grind", "chop", "volatile", "compression", "pre_expansion", "exhaustion"]
StrategyType = Literal["momentum_breakout", "momentum_breakdown", "trend_continuation", "liquidity_sweep_reversal", "low_vol_reclaim"]


@dataclass
class AdaptiveTPContext:
    symbol: str
    strategy: StrategyType
    direction: str
    entry: float
    stop_loss: float
    atr_pct: float
    volatility_rank: float
    volume_ratio: float
    tp1_hit_rate: float
    tp3_hit_rate: float
    missed_tp1_to_sl_rate: float
    market_regime: MarketRegime


@dataclass
class AdaptiveTPResult:
    tp1_rr: float
    tp2_rr: float
    tp3_rr: float
    tp1_size_pct: float
    tp2_size_pct: float
    tp3_size_pct: float
    reasoning: list[str]


class AdaptiveTPEngine:
    @staticmethod
    def _normalize_sizes(tp1: float, tp2: float, tp3: float) -> tuple[float, float, float]:
        total = float(tp1 + tp2 + tp3)
        if total <= 0:
            return 50.0, 30.0, 20.0

        tp1_n = round((tp1 / total) * 100.0, 2)
        tp2_n = round((tp2 / total) * 100.0, 2)
        tp3_n = round(100.0 - tp1_n - tp2_n, 2)
        return tp1_n, tp2_n, tp3_n

    @staticmethod
    def _clamp_rr(value: float, minimum: float = 0.45, maximum: float = 5.0) -> float:
        return max(minimum, min(maximum, float(value)))

    def build(self, ctx: AdaptiveTPContext) -> AdaptiveTPResult:
        tp1_rr = 0.9
        tp2_rr = 1.6
        tp3_rr = 2.4

        tp1_size_pct = 50.0
        tp2_size_pct = 30.0
        tp3_size_pct = 20.0

        reasoning: list[str] = []

        if ctx.volatility_rank < 8:
            tp1_rr *= 0.85
            tp2_rr *= 0.90
            tp3_rr *= 0.90
            reasoning.append("low volatility compression")

        if ctx.strategy in ("momentum_breakout", "momentum_breakdown") and ctx.volume_ratio >= 1.5 and ctx.volatility_rank >= 15:
            tp2_rr *= 1.10
            tp3_rr *= 1.20
            reasoning.append("strong breakout expansion")

        if ctx.strategy == "trend_continuation":
            tp1_rr *= 0.85
            tp2_rr *= 0.95
            tp1_size_pct = 60.0
            tp2_size_pct = 25.0
            tp3_size_pct = 15.0
            reasoning.append("continuation protection bias")

        if ctx.strategy == "trend_continuation" and str(ctx.direction or "").upper() == "SHORT":
            tp1_rr *= 0.92
            tp2_rr *= 0.90
            tp3_rr *= 0.85
            tp1_size_pct = max(tp1_size_pct, 65.0)
            tp2_size_pct = min(tp2_size_pct, 25.0)
            tp3_size_pct = min(tp3_size_pct, 10.0)
            reasoning.append("short continuation defensive TP profile")

        if ctx.strategy == "low_vol_reclaim":
            # Single-TP low-vol reclaim must align with the planner's 1.00R expectancy rule.
            # Do not generate 0.55-0.60R targets that are rejected later or create poor expectancy.
            tp1_rr = 1.00
            tp2_rr = 1.00
            tp3_rr = 1.50

            tp1_size_pct = 100.0
            tp2_size_pct = 0.0
            tp3_size_pct = 0.0
            reasoning.append("low vol reclaim single TP 1.00R expectancy profile")

        if ctx.market_regime in {"compression", "pre_expansion"}:
            tp1_rr *= 0.90
            tp2_rr *= 0.95
            tp1_size_pct = max(tp1_size_pct, 60.0)
            reasoning.append("compression/pre-expansion protection bias")

        if ctx.missed_tp1_to_sl_rate >= 0.35:
            tp1_rr *= 0.80
            tp1_size_pct = 65.0
            reasoning.append("high missed TP1 rate")

        if ctx.tp3_hit_rate >= 0.30:
            tp3_rr *= 1.15
            tp3_size_pct += 5.0
            tp1_size_pct -= 5.0
            reasoning.append("runner environment confirmed")

        if ctx.strategy == "low_vol_reclaim":
            tp1_rr = 1.00
            tp2_rr = min(tp2_rr, 1.00)
            tp3_rr = min(tp3_rr, 1.50)

        tp1_rr = self._clamp_rr(tp1_rr)
        tp2_rr = self._clamp_rr(tp2_rr)
        tp3_rr = self._clamp_rr(tp3_rr)
        tp1_size_pct, tp2_size_pct, tp3_size_pct = self._normalize_sizes(
            tp1_size_pct,
            tp2_size_pct,
            tp3_size_pct,
        )

        return AdaptiveTPResult(
            tp1_rr=round(tp1_rr, 2),
            tp2_rr=round(tp2_rr, 2),
            tp3_rr=round(tp3_rr, 2),
            tp1_size_pct=round(tp1_size_pct, 2),
            tp2_size_pct=round(tp2_size_pct, 2),
            tp3_size_pct=round(tp3_size_pct, 2),
            reasoning=reasoning,
        )
