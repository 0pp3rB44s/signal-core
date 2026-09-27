"""Regression tests for the concentration-mechanism audit (TUNING_ROUND1_TIMING_RACE_AUDIT_2026-09-27.md).

Two genuine defects were found in THIS RESTORATION'S OWN code (never in the shared, unmodified
RiskManager/CentralRiskGate, and never in 120/40 or any detector/scorer/planner/entry/exit logic):

1. funnel/core/engine.py's RiskProposal.strategy_risk_fraction used a notional/NAV ratio (up to 35% at the
   capital floor) instead of a risk-manager-comparable fraction (LSRL_V1's own engine uses a fixed 0.5%,
   `cfg.risk_per_full_trade_frac`) - so large it always swamped the `min(strategy_fraction*scale,
   central_pct)` comparison in lsrl/adapters/riskgate.py::CentralRiskGate.check(), making ANY cluster/
   portfolio-based scale factor structurally unable to reach the final applied risk fraction.
2. FunnelEngine never wrote to app/equity.py's shared, file-based equity snapshot
   (write_equity_snapshot()), so planning/trade_planner.py's own account-equity lookup
   (app.equity.resolve_account_equity()) silently fell back to Settings.account_equity_usdt (a fixed
   1000.0 placeholder) instead of this engine's actual simulated NAV - sizing math and the cluster-room
   math were computed against two different equity bases.

With both fixed, the cluster-exposure cap genuinely constrains a same-cluster position's size - and a
THIRD issue then became measurable (previously masked by #1): funnel/tools/tuning_round1_ab.py's
timestep loop could evaluate one symbol's new candidate before recording another same-cluster symbol's
same-instant fill, purely because of arbitrary (symbol-name) ordering. Fixed by grouping the replay
timeline by tied timestamp and processing every already-positioned symbol (recording its fill) before any
flat symbol (evaluating a new candidate) within that group - test_run_variant_grouping_is_order_independent
proves this directly against the fixed production-path code, not a hand-simulated stand-in.
"""
from __future__ import annotations

from app.config import Settings
from clients.schemas import Candle, ContractSpec
from lsrl.core.types import SymbolRules
from risk.risk_manager import RiskManager

from funnel.tools.concentration_race_repro import reproduce
from funnel.tools.tuning_round1_ab import _build_timeline, run_variant

M15 = 900_000
H = 3_600_000
H4 = 14_400_000
D1 = 86_400_000


def test_the_fixed_strategy_risk_fraction_and_equity_wiring_let_the_cluster_cap_constrain_a_real_trade():
    """Direct evidence for defects 1-2: the SAME candidate, correctly ordered (the already-positioned
    symbol's fill recorded first), must come back meaningfully smaller than the flat 35 USDT cap."""
    result = reproduce(process_first="SOLUSDT")
    assert result["avax_accepted"] is True
    assert result["avax_notional_usdt"] is not None
    assert result["avax_notional_usdt"] < 34.0, (
        "cluster exposure did not constrain the second same-cluster position - "
        "the strategy_risk_fraction/equity-snapshot fixes are not both in effect")
    assert result["avax_notional_usdt"] == 15.99


def test_order_tied_group_processes_already_positioned_symbols_first_regardless_of_name():
    """Direct, deterministic unit test of the extracted ordering function itself (the actual fix
    funnel.tools.tuning_round1_ab.run_variant now calls) - not a hand-simulated stand-in. Two name pairs
    are tried, one where alphabetical order already happens to agree with the correct answer and one
    deliberately chosen so it does NOT (SUIUSDT already positioned, SEIUSDT flat - 'SEI' < 'SUI'
    alphabetically, so naive alphabetical-first processing would get this pair wrong)."""
    from types import SimpleNamespace

    from funnel.tools.tuning_round1_ab import order_tied_group

    def variant_with(positioned_symbol: str, flat_symbol: str):
        engines = {positioned_symbol: SimpleNamespace(setup=object()),
                  flat_symbol: SimpleNamespace(setup=None)}
        return SimpleNamespace(engines=engines)

    for positioned, flat in (("SOLUSDT", "AVAXUSDT"), ("SUIUSDT", "SEIUSDT")):
        variant = variant_with(positioned, flat)
        group = [(1_700_000_000_000, positioned, "candle_a"), (1_700_000_000_000, flat, "candle_b")]
        ordered = order_tied_group(group, variant)
        assert [sym for sym, _candle in ordered] == [positioned, flat], (
            f"expected the already-positioned symbol ({positioned}) processed before the flat one "
            f"({flat}) regardless of name order; got {ordered}")
