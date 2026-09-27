"""Micro-live capital FEASIBILITY: what the frozen sizing does at each capital in the band. It recommends NOTHING and chooses NOTHING.

    .venv/bin/python -m lsrl.tools.capital_feasibility [--price 150] [--json]

The owner selects the capital (100-150 USDT) in the release policy. This tool shows, using the strategy's OWN frozen sizing (`lsrl.core.ladder.size_ladder`,
not a copy of it), what each capital can trade: the risk budget is 0.50 % of NAV, the rung quantity is rounded DOWN to the exchange step and is NEVER raised to
reach the exchange minimum - a setup whose budget cannot buy the minimum quantity is NO-TRADE (BELOW_MIN_QTY / BELOW_MIN_NOTIONAL / BELOW_MIN_QTY_TP_SLICE), not a
bigger position. So a small capital does not "take more risk to fit": it simply trades fewer, wider setups, and this table says which.

Geometry used (a table, not a forecast): the sweep range `span` from the reclaim boundary to the extreme; ATR(1m) sets the stop gap (0.15 x ATR beyond the extreme);
the five rungs sit at 0/25/50/75/100 % of the span.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from lsrl.capital import CAPITAL_MAX_USDT, CAPITAL_MIN_USDT, EXPOSURE_TO_CAPITAL_MAX, RISK_FRACTION
from lsrl.core.config import DEFAULT_CONFIG
from lsrl.core.ladder import build_geometry, size_ladder
from lsrl.core.types import Direction, SymbolRules

#: SOLUSDT constraints as recorded in the prospective segments' headers (tick 0.001, step/min qty 0.1, min notional 5 USDT).
SOL_RULES = SymbolRules(symbol="SOLUSDT", tick_size=0.001, qty_step=0.1, min_qty=0.1, min_notional_usdt=5.0)
NAVS = (100.0, 110.0, 125.0, 140.0, 150.0)
SPANS = (0.2, 0.4, 0.8, 1.6, 3.2)
ATR1M = 0.15


def evaluate(nav: float, price: float, span: float, atr1m: float = ATR1M, rules: SymbolRules = SOL_RULES) -> dict[str, Any]:
    geometry, why = build_geometry(Direction.LONG, price, price - span, atr1m, rules, DEFAULT_CONFIG)
    row: dict[str, Any] = {"nav_usdt": nav, "sweep_span_usdt": span, "atr1m": atr1m}
    if geometry is None:
        return {**row, "status": "NO_TRADE", "reason": why}
    sizing, why = size_ladder(geometry, nav, RISK_FRACTION, rules, DEFAULT_CONFIG, maker_fee_rate=DEFAULT_CONFIG.assumed_maker_fee_rate,
                              taker_fee_rate=DEFAULT_CONFIG.assumed_taker_fee_rate, stop_slippage_bps=DEFAULT_CONFIG.assumed_stop_slippage_bps)
    mean_distance = sum(abs(p - geometry.stop) for p in geometry.rung_prices) / len(geometry.rung_prices)
    row["mean_rung_to_stop_usdt"] = round(mean_distance, 4)
    if sizing is None:
        return {**row, "status": "NO_TRADE", "reason": why}
    notional = sizing.total_qty * sizing.expected_avg_entry
    return {**row, "status": "TRADE", "reason": "", "rung_qty": sizing.rung_qty, "total_qty": round(sizing.total_qty, 6),
            "risk_budget_usdt": round(sizing.risk_budget_usdt, 4), "planned_loss_usdt": round(sizing.planned_loss_usdt, 4),
            "total_notional_usdt": round(notional, 2), "exposure_multiple_of_nav": round(notional / nav, 3),
            "within_5x_exposure_ceiling": notional <= nav * EXPOSURE_TO_CAPITAL_MAX}


def table(price: float = 150.0, navs: tuple[float, ...] = NAVS, spans: tuple[float, ...] = SPANS) -> dict[str, Any]:
    rows = [evaluate(nav, price, span) for nav in navs for span in spans]
    widest = {}
    for nav in navs:                                    # the widest mean rung-to-stop distance the minimum quantity still allows, from the same rule
        budget = nav * RISK_FRACTION
        widest[str(nav)] = round(budget / (len(DEFAULT_CONFIG.ladder_fractions) * SOL_RULES.min_qty), 4)
    return {"tool": "lsrl.tools.capital_feasibility", "price_usdt": price, "band_usdt": [CAPITAL_MIN_USDT, CAPITAL_MAX_USDT],
            "risk_fraction": RISK_FRACTION, "exposure_ceiling_multiple": EXPOSURE_TO_CAPITAL_MAX, "rules": SOL_RULES.__dict__ if hasattr(SOL_RULES, "__dict__")
            else {"tick": SOL_RULES.tick_size, "qty_step": SOL_RULES.qty_step, "min_qty": SOL_RULES.min_qty, "min_notional": SOL_RULES.min_notional_usdt},
            "widest_mean_rung_to_stop_usdt_by_nav": widest, "rows": rows,
            "OWNER_APPROVAL_REQUIRED": "this table recommends no capital; the owner selects it in the release policy",
            "NEVER_INCREASES_RISK": all(r.get("planned_loss_usdt", 0) <= r.get("risk_budget_usdt", 1e18) + 1e-9 for r in rows)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--price", type=float, default=150.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    t = table(args.price)
    if args.json:
        print(json.dumps(t, indent=1, sort_keys=True))
        return 0
    print(f"SOLUSDT @ {args.price}: risk = {RISK_FRACTION:.2%} of NAV, quantity rounded DOWN and never raised to a minimum; exposure ceiling {EXPOSURE_TO_CAPITAL_MAX:.0f}x NAV")
    print(f"{'NAV':>6} {'span':>6} {'meanDist':>9}  {'status':<8} {'rung':>5} {'total':>6} {'loss':>6} {'notional':>9} {'xNAV':>6}  note")
    for r in t["rows"]:
        print(f"{r['nav_usdt']:>6.0f} {r['sweep_span_usdt']:>6.2f} {r.get('mean_rung_to_stop_usdt', float('nan')):>9.3f}  {r['status']:<8} "
              f"{r.get('rung_qty', 0):>5.1f} {r.get('total_qty', 0):>6.1f} {r.get('planned_loss_usdt', 0):>6.3f} {r.get('total_notional_usdt', 0):>9.2f} "
              f"{r.get('exposure_multiple_of_nav', 0):>6.2f}  "
              f"{r['reason'] or ('ABOVE the 5x exposure ceiling: the central gate scales the ladder DOWN' if r.get('within_5x_exposure_ceiling') is False else '')}")
    print("widest tradeable mean rung-to-stop distance (USDT) by NAV:", t["widest_mean_rung_to_stop_usdt_by_nav"])
    print("OWNER_APPROVAL_REQUIRED: this table recommends no capital.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
