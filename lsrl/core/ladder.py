"""E1-E5 ladder geometry, structural stop and risk-based sizing (pure).

Rounding is conservative and explicit:
  * entries (passive limits) round AWAY from the market (LONG down, SHORT up);
  * the stop rounds AWAY from the entries (LONG down, SHORT up) and is then the
    price the risk is SIZED against, so rounding can never increase risk;
  * quantity rounds DOWN to the exchange step and is NEVER raised to reach a
    minimum -- an unreachable minimum is a NO-TRADE, not a bigger position.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from lsrl.core.config import LSRLConfig
from lsrl.core.rounding import floor_to_step, round_to_tick, to_dec
from lsrl.core.types import Direction, SymbolRules


@dataclass(frozen=True, slots=True)
class Geometry:
    direction: Direction
    boundary: float
    extreme: float
    rung_prices: tuple[float, ...]     # E1..E5
    stop: float


@dataclass(frozen=True, slots=True)
class Sizing:
    risk_fraction: float
    risk_budget_usdt: float
    rung_qty: float
    total_qty: float
    planned_loss_usdt: float           # loss from a FULL E1-E5 fill to the stop, before fees
    expected_avg_entry: float
    est_entry_fees_usdt: float
    est_stop_exit_fee_usdt: float
    est_stop_slippage_usdt: float


def build_geometry(direction: Direction, boundary: float, extreme: float, atr1m: float,
                   rules: SymbolRules, cfg: LSRLConfig) -> tuple[Geometry | None, str]:
    sgn = direction.sgn
    # Exact Decimal geometry: float subtraction noise must never move a rung across a tick.
    boundary_d, extreme_d = to_dec(boundary), to_dec(extreme)
    span = sgn * (boundary_d - extreme_d)
    if span <= 0:
        return None, "DEGENERATE_SWEEP_RANGE"
    if span / to_dec(rules.tick_size) < cfg.min_ladder_range_ticks:
        return None, "SWEEP_RANGE_TOO_SMALL_FOR_LADDER"
    mode = "down" if direction is Direction.LONG else "up"
    prices = tuple(round_to_tick(boundary_d - sgn * to_dec(f) * span, rules.tick_size, mode)
                   for f in cfg.ladder_fractions)
    ordered = all(sgn * (a - b) > 0 for a, b in zip(prices, prices[1:]))
    if not ordered:
        return None, "LADDER_RUNGS_NOT_DISTINCT"
    raw_stop = extreme_d - sgn * to_dec(cfg.sl_atr1m_mult) * to_dec(atr1m)
    stop = round_to_tick(raw_stop, rules.tick_size, mode)
    if sgn * (prices[-1] - stop) < rules.tick_size * 0.999:
        return None, "SL_GEOMETRY_INVALID"
    return Geometry(direction, boundary, extreme, prices, stop), ""


def split_tp_quantities(total_qty: float, rules: SymbolRules, cfg: LSRLConfig,
                        *, merge_below_min: bool) -> list[tuple[str, float]]:
    """30/40/30 split of `total_qty` (remainder to TP3). With merge_below_min a
    slice under the exchange minimum is folded into the next target (used only for
    partial ladder fills); without it the caller rejects instead."""
    step = Decimal(str(rules.qty_step))
    total = Decimal(str(total_qty))
    q1 = (total * Decimal(str(cfg.tp_fractions[0])) / step).to_integral_value(rounding=ROUND_DOWN) * step
    q2 = (total * Decimal(str(cfg.tp_fractions[1])) / step).to_integral_value(rounding=ROUND_DOWN) * step
    q3 = total - q1 - q2
    slices = [q1, q2, q3]
    if merge_below_min:
        minimum = Decimal(str(rules.min_qty))
        for i in range(2):
            if slices[i] < minimum:
                slices[i + 1] += slices[i]
                slices[i] = Decimal(0)
    return [(lvl, float(q)) for lvl, q in zip(("TP1", "TP2", "TP3"), slices)]


def size_ladder(geometry: Geometry, nav_usdt: float, risk_fraction: float, rules: SymbolRules,
                cfg: LSRLConfig, *, maker_fee_rate: float, taker_fee_rate: float,
                stop_slippage_bps: float) -> tuple[Sizing | None, str]:
    if nav_usdt <= 0:
        return None, "NAV_UNAVAILABLE"
    if risk_fraction <= 0:
        return None, "RISK_FRACTION_NOT_POSITIVE"
    prices = geometry.rung_prices
    n = len(prices)
    risk_budget = nav_usdt * risk_fraction
    mean_distance = sum(abs(p - geometry.stop) for p in prices) / n
    if mean_distance <= 0:
        return None, "SL_GEOMETRY_INVALID"
    rung_qty = floor_to_step(risk_budget / mean_distance / n, rules.qty_step)
    if rung_qty < rules.min_qty:
        return None, "BELOW_MIN_QTY"
    if any(rung_qty * p < rules.min_notional_usdt for p in prices):
        return None, "BELOW_MIN_NOTIONAL"
    slices = split_tp_quantities(rung_qty * n, rules, cfg, merge_below_min=False)
    if any(q < rules.min_qty for _lvl, q in slices):
        return None, "BELOW_MIN_QTY_TP_SLICE"
    total = rung_qty * n
    planned_loss = sum(rung_qty * abs(p - geometry.stop) for p in prices)
    return Sizing(
        risk_fraction=risk_fraction,
        risk_budget_usdt=risk_budget,
        rung_qty=rung_qty,
        total_qty=total,
        planned_loss_usdt=planned_loss,
        expected_avg_entry=sum(prices) / n,
        est_entry_fees_usdt=sum(rung_qty * p * maker_fee_rate for p in prices),
        est_stop_exit_fee_usdt=total * geometry.stop * taker_fee_rate,
        est_stop_slippage_usdt=total * geometry.stop * stop_slippage_bps / 10_000.0,
    ), ""
