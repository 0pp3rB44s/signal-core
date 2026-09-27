"""Deterministic reproduction of the Round-1-observed timing edge: two same-cluster symbols where one's
entry FILL and the other's candidate EVALUATION share the exact same millisecond timestamp, and the outcome
depends on which symbol's step is processed first within that tied instant.

Fully synthetic (no network, no live data) so this is reproducible byte-for-byte on every run -
TUNING_ROUND1_VALIDATION_2026-09-27.md's real SUIUSDT/SEIUSDT occurrence showed the mechanism exists; this
isolates it under total control, with both processing orders exercised explicitly, so the order-dependence
itself is demonstrated rather than merely inferred from one real occurrence.

No PnL analysis. This is a risk-accounting/ordering audit only.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import Settings
from candidate_lifecycle import deterministic_candidate_id
from clients.schemas import StrategyCandidate, SweepDetection
from lsrl.adapters.riskgate import CentralRiskGate
from lsrl.core.types import BarClosed, DepthSnapshot, NavEvent, SymbolRules
from risk.risk_manager import RiskManager

from funnel.core.config import FunnelConfig
from funnel.core.engine import FunnelEngine
from funnel.tools.tuning_round1_ab import OPEN_POSITIONS_PATH, _write_open_positions

H, M15 = 3600_000, 900_000
RULES = {
    "SOLUSDT": SymbolRules(symbol="SOLUSDT", tick_size=0.001, qty_step=0.01, min_qty=0.01, min_notional_usdt=5.0),
    "AVAXUSDT": SymbolRules(symbol="AVAXUSDT", tick_size=0.001, qty_step=0.01, min_qty=0.01, min_notional_usdt=5.0),
}  # both L1_BETA


def _feed_history(engine: FunnelEngine, *, t0: int, start_price: float) -> tuple[int, float]:
    price = start_price
    for i in range(25):
        o, price = price, price + 0.05
        ots = t0 + i * H
        engine.on_event(BarClosed(t_ms=ots + H, tf_s=3600, open_ts_ms=ots, close_ts_ms=ots + H, open=o,
                                  high=max(o, price) + 0.05, low=min(o, price) - 0.05, close=price, volume=1000.0))
    t_conf_end = t0 + 25 * H
    for i in range(25):
        o, price = price, price + 0.02
        ots = t_conf_end + i * M15
        engine.on_event(BarClosed(t_ms=ots + M15, tf_s=900, open_ts_ms=ots, close_ts_ms=ots + M15, open=o,
                                  high=max(o, price) + 0.03, low=min(o, price) - 0.03, close=price, volume=500.0))
    t_now = t_conf_end + 25 * M15
    engine.on_event(DepthSnapshot(t_ms=t_now, exchange_ts_ms=t_now, bids=((price - 0.01, 10.0),),
                                  asks=((price + 0.01, 10.0),)))
    engine.on_event(NavEvent(t_ms=t_now, nav_usdt=100.0, source="test"))
    return t_now, price


def _synthetic_candidate(engine: FunnelEngine, symbol: str, t_now: int, price: float) -> StrategyCandidate:
    market = engine._market_snapshot(t_now)
    assert market is not None
    candle_open_ts = t_now - M15
    cid = deterministic_candidate_id("low_vol_reclaim", symbol, "LONG", candle_open_ts)
    detection = SweepDetection(side="bid", swept_level=price - 0.5, sweep_extreme=price - 0.6,
                               reclaim_level=price - 0.4, entry_hint=price, invalidation=price - 0.6,
                               displacement_pct=0.5, bars_since_sweep=1, volume_ratio_on_sweep=1.5,
                               local_range_size_pct=0.3, reason_flags=[])
    return StrategyCandidate(candidate_id=cid, candidate_candle_open_timestamp_ms=candle_open_ts, symbol=symbol,
                             strategy="low_vol_reclaim", direction="LONG", primary_granularity="15m",
                             confirmation_granularity="1h", market=market,
                             detection=detection, notes=["synthetic_test_fixture=true"])


def build_engine(symbol: str, risk_manager: RiskManager, settings: Settings) -> FunnelEngine:
    gate = CentralRiskGate(risk_manager, settings)
    eng = FunnelEngine(FunnelConfig(symbol=symbol), RULES[symbol], risk_gate=gate, settings=settings)
    eng.begin_session(None)
    return eng


def reproduce(*, process_first: str) -> dict[str, Any]:
    """process_first: 'SOLUSDT' or 'AVAXUSDT' - which symbol's step (fill for SOL, evaluation for AVAX,
    both landing at the same t_ms) is processed first."""
    settings = Settings(MAX_CLUSTER_EXPOSURE_PCT=40.0)
    risk_manager = RiskManager(settings)
    _write_open_positions([])
    t0 = 1_700_000_000_000 - (1_700_000_000_000 % H)

    sol = build_engine("SOLUSDT", risk_manager, settings)
    avax = build_engine("AVAXUSDT", risk_manager, settings)
    t_now_sol, price_sol = _feed_history(sol, t0=t0, start_price=100.0)
    t_now_avax, price_avax = _feed_history(avax, t0=t0, start_price=100.0)
    assert t_now_sol == t_now_avax

    timeline: list[dict[str, Any]] = []

    def record(stage: str, **kw: Any) -> None:
        timeline.append({"stage": stage, **kw})

    # --- Step A (t = t_now): SOLUSDT's candidate is detected and accepted; PlaceEntry emitted, not yet filled.
    sol_candidate = _synthetic_candidate(sol, "SOLUSDT", t_now_sol, price_sol)
    sol._detect = lambda market: (sol_candidate, "SYNTHETIC_TEST_OVERRIDE")
    record("market_event", t_ms=t_now_sol, symbol="SOLUSDT", note="candle closes, decision cycle runs")
    open_before = json.loads(OPEN_POSITIONS_PATH.read_text())["data"] if OPEN_POSITIONS_PATH.exists() else []
    record("risk_manager_state_before_sol_eval", t_ms=t_now_sol, open_positions=open_before)
    sol._run_decision_cycle(t_now_sol)
    record("candidate_evaluation", t_ms=t_now_sol, symbol="SOLUSDT", accepted=sol.setup is not None,
          setup_state=sol.setup.state if sol.setup else None)
    _write_open_positions([])  # SOL is PENDING_ENTRY, not OPEN yet - nothing to reserve under current semantics
    record("order_acceptance", t_ms=t_now_sol, symbol="SOLUSDT", entry_price=sol.setup.entry_price,
          state="PENDING_ENTRY (order placed, not yet filled)")

    # --- Step B (t = t_now + 900000): SOLUSDT's entry FILLS on this bar; AVAXUSDT's NEXT candidate is
    # ALSO evaluated at this exact same t_ms - the tied instant.
    t_fill = t_now_sol + M15
    from lsrl.adapters.sim import SimExchange
    sim_sol = SimExchange(RULES["SOLUSDT"], sol.cfg)
    for oe in sim_sol.apply(sol._intents, t_now_sol):
        sol.on_event(oe)
    bar_sol = BarClosed(t_ms=t_fill, tf_s=900, open_ts_ms=t_now_sol, close_ts_ms=t_fill,
                        open=price_sol, high=price_sol, low=sol.setup.entry_price - 0.01,
                        close=sol.setup.entry_price, volume=300.0)

    # Refresh AVAX's depth/NAV so its snapshot at t_fill isn't stale (only SOL's own clock advanced via its
    # own bar feed above; AVAX needs its own fresh tick to be evaluated at this same instant).
    avax.on_event(DepthSnapshot(t_ms=t_fill - 1, exchange_ts_ms=t_fill - 1, bids=((price_avax - 0.01, 10.0),),
                                asks=((price_avax + 0.01, 10.0),)))
    avax.on_event(NavEvent(t_ms=t_fill - 1, nav_usdt=100.0, source="test"))
    avax_candidate = _synthetic_candidate(avax, "AVAXUSDT", t_fill, price_avax)
    avax._detect = lambda market: (avax_candidate, "SYNTHETIC_TEST_OVERRIDE")

    def do_sol_fill() -> None:
        fills = sim_sol.entry_fills(bar_sol)
        for f in fills:
            sol.on_event(f)
        record("fill", t_ms=t_fill, symbol="SOLUSDT", filled=bool(fills))
        _write_open_positions([r for r in [
            {"symbol": "SOLUSDT", "status": "OPEN", "position_notional_usdt": sol.setup.entry_qty * sol.setup.entry_price}
        ] if sol.setup is not None and sol.setup.state == "IN_POSITION"])
        record("position_state_update", t_ms=t_fill, symbol="SOLUSDT",
              state=sol.setup.state if sol.setup else None,
              open_positions_file=json.loads(OPEN_POSITIONS_PATH.read_text())["data"])

    def do_avax_eval() -> None:
        open_now = json.loads(OPEN_POSITIONS_PATH.read_text())["data"] if OPEN_POSITIONS_PATH.exists() else []
        record("risk_manager_state_before_avax_eval", t_ms=t_fill, open_positions=open_now)
        avax._run_decision_cycle(t_fill)
        notional = (avax.setup.entry_qty * avax.setup.entry_price) if avax.setup is not None else None
        record("second_candidate_evaluation", t_ms=t_fill, symbol="AVAXUSDT",
              accepted=avax.setup is not None, notional_usdt=notional)
        record("second_acceptance_or_rejection", t_ms=t_fill, symbol="AVAXUSDT",
              result="ACCEPTED" if avax.setup is not None else "REJECTED", notional_usdt=notional)

    if process_first == "SOLUSDT":
        do_sol_fill()
        do_avax_eval()
    else:
        do_avax_eval()
        do_sol_fill()

    avax_notional = (avax.setup.entry_qty * avax.setup.entry_price) if avax.setup is not None else None
    _write_open_positions([])
    return {"process_first": process_first, "timeline": timeline,
           "avax_accepted": avax.setup is not None, "avax_notional_usdt": avax_notional,
           "sol_state": sol.setup.state if sol.setup else None}


def main() -> None:
    result_fill_first = reproduce(process_first="SOLUSDT")     # SOL's fill recorded before AVAX is evaluated
    result_eval_first = reproduce(process_first="AVAXUSDT")     # AVAX evaluated before SOL's same-instant fill is recorded
    print(json.dumps({
        "SOLUSDT_FILL_PROCESSED_FIRST": {"avax_accepted": result_fill_first["avax_accepted"],
                                        "avax_notional_usdt": result_fill_first["avax_notional_usdt"]},
        "AVAXUSDT_EVAL_PROCESSED_FIRST": {"avax_accepted": result_eval_first["avax_accepted"],
                                         "avax_notional_usdt": result_eval_first["avax_notional_usdt"]},
        "ORDER_DEPENDENCE_ON_ACCEPTANCE": result_fill_first["avax_accepted"] != result_eval_first["avax_accepted"],
        "ORDER_DEPENDENCE_ON_SIZE": result_fill_first["avax_notional_usdt"] != result_eval_first["avax_notional_usdt"],
        "timeline_fill_first": result_fill_first["timeline"],
        "timeline_eval_first": result_eval_first["timeline"],
    }, indent=2, default=str))


if __name__ == "__main__":
    main()
