"""The TP engine must never build targets its own planner gates reject.

Live 2026-07-06: 78 GO momentum candidates produced 0 executions because the
engine built TP1 at 0.8-0.9R while the planner hard-gates rr_to_tp1 >= 1.00
and stop <= 1.2x TP1 distance. These tests pin the geometry contract.
"""

import itertools

from execution.adaptive_tp_engine import AdaptiveTPContext, AdaptiveTPEngine


STRATEGIES = ["momentum_breakout", "momentum_breakdown", "trend_continuation", "liquidity_sweep_reversal", "low_vol_reclaim"]
REGIMES = ["trend", "breakout", "grind", "chop", "volatile", "compression", "pre_expansion", "exhaustion"]


def _ctx(strategy: str, regime: str, direction: str = "LONG", vol_rank: float = 5.0, volume_ratio: float = 0.8) -> AdaptiveTPContext:
    return AdaptiveTPContext(
        symbol="BTCUSDT",
        strategy=strategy,  # type: ignore[arg-type]
        direction=direction,
        entry=100.0,
        stop_loss=99.0,
        atr_pct=0.5,
        volatility_rank=vol_rank,
        volume_ratio=volume_ratio,
        tp1_hit_rate=0.0,
        tp3_hit_rate=0.0,
        missed_tp1_to_sl_rate=0.5,  # worst case: drukt tp1 verder omlaag
        market_regime=regime,  # type: ignore[arg-type]
    )


def test_tp1_never_below_planner_gate_floor_for_any_combination():
    engine = AdaptiveTPEngine()
    for strategy, regime, direction in itertools.product(STRATEGIES, REGIMES, ("LONG", "SHORT")):
        result = engine.build(_ctx(strategy, regime, direction))
        assert result.tp1_rr >= 1.0, (
            f"{strategy}/{regime}/{direction}: tp1_rr={result.tp1_rr} < 1.0 — "
            "planner rejects this plan on rr_to_tp1 and risk-shape by construction"
        )
        # stop <= 1.2x tp1 volgt automatisch uit tp1_rr >= 1.0 (verhouding 1/tp1_rr <= 1.0)
        assert 1.0 / result.tp1_rr <= 1.2


def test_reclaim_keeps_its_own_130r_profile():
    engine = AdaptiveTPEngine()
    result = engine.build(_ctx("low_vol_reclaim", "compression"))
    assert result.tp1_rr == 1.30
    assert result.tp1_size_pct == 100.0


def test_multi_tp_ladder_stays_ordered():
    engine = AdaptiveTPEngine()
    for strategy in ("momentum_breakout", "trend_continuation"):
        result = engine.build(_ctx(strategy, "compression"))
        assert result.tp1_rr < result.tp2_rr <= result.tp3_rr or result.tp2_rr < result.tp3_rr
        assert result.tp2_rr >= result.tp1_rr + 0.10
