"""LSRL_V1: E1-E5 geometry, structural stop, 0.50% NAV sizing, targets, execution gates."""
from __future__ import annotations

from dataclasses import replace

import pytest

from lsrl.core.config import DEFAULT_CONFIG as CFG
from lsrl.core.ladder import build_geometry, size_ladder, split_tp_quantities
from lsrl.core.targets import rr_to_tp2
from lsrl.core.types import Direction, PlaceEntry, SymbolRules
from tests.lsrl_fixtures import ASK_POOLS, RULES, Harness, long_sweep, mirror

FEES = dict(maker_fee_rate=0.0002, taker_fee_rate=0.0006, stop_slippage_bps=2.0)


def _geom(direction, boundary, extreme, atr1m=0.4, rules=RULES):
    g, why = build_geometry(direction, boundary, extreme, atr1m, rules, CFG)
    assert g is not None, why
    return g


# ------------------------------------------------------------------ geometry
def test_long_ladder_geometry_e1_reclaim_boundary_to_e5_sweep_extreme():
    g = _geom(Direction.LONG, 149.60, 149.20)
    assert g.rung_prices == (149.60, 149.50, 149.40, 149.30, 149.20)      # 0/25/50/75/100% of the range
    assert g.stop == pytest.approx(149.14)                                 # extreme - 0.15*ATR1m, rounded away


def test_long_stop_is_below_the_sweep_extreme_by_015_atr1m_rounded_away():
    g = _geom(Direction.LONG, 149.60, 149.20, atr1m=0.4)
    assert g.stop == pytest.approx(149.14) and g.stop < g.rung_prices[-1]
    g = _geom(Direction.LONG, 149.60, 149.20, atr1m=0.4137)                # raw 149.137945 -> down to the tick
    assert g.stop == pytest.approx(149.137)


def test_short_ladder_is_the_exact_mirror_of_long():
    long_g = _geom(Direction.LONG, 149.60, 149.20, atr1m=0.4)
    short_g = _geom(Direction.SHORT, 150.40, 150.80, atr1m=0.4)
    assert short_g.rung_prices == pytest.approx(tuple(round(300 - p, 3) for p in long_g.rung_prices))
    assert short_g.stop == pytest.approx(round(300 - long_g.stop, 3))
    assert short_g.stop > short_g.rung_prices[-1]                          # above the sweep high


def _oracle(direction: Direction, boundary: str, extreme: str, atr1m: str, tick: str):
    """Independent reference geometry in exact decimal arithmetic (no floats, no engine code)."""
    from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
    b, e, a, t = Decimal(boundary), Decimal(extreme), Decimal(atr1m), Decimal(tick)
    long_ = direction is Direction.LONG
    span = abs(b - e)
    passive = ROUND_FLOOR if long_ else ROUND_CEILING                    # entries: away from the market
    rungs = []
    for f in ("0", "0.25", "0.5", "0.75", "1"):
        raw = b - Decimal(f) * span if long_ else b + Decimal(f) * span
        rungs.append(float((raw / t).to_integral_value(rounding=passive) * t))
    raw_stop = e - Decimal("0.15") * a if long_ else e + Decimal("0.15") * a
    stop = float((raw_stop / t).to_integral_value(rounding=ROUND_FLOOR if long_ else ROUND_CEILING) * t)
    return tuple(rungs), stop


@pytest.mark.parametrize("tick", ["0.001", "0.005", "0.01"])                # real SOLUSDT, a priceEndStep=5 symbol, legacy
@pytest.mark.parametrize("direction,boundary,extreme", [
    (Direction.LONG, "149.600", "149.230"), (Direction.SHORT, "150.400", "150.770")])
def test_off_grid_prices_round_conservatively_at_any_exchange_tick(tick, direction, boundary, extreme):
    """LONG entries round DOWN and the stop DOWN; SHORT entries UP and the stop UP: always away from the market."""
    rules = replace(RULES, tick_size=float(tick))
    g = _geom(direction, float(boundary), float(extreme), atr1m=0.4137, rules=rules)
    want_rungs, want_stop = _oracle(direction, boundary, extreme, "0.4137", tick)
    assert g.rung_prices == pytest.approx(want_rungs, abs=1e-9) and g.stop == pytest.approx(want_stop, abs=1e-9)
    sgn = direction.sgn
    assert all(sgn * (r - w) <= 1e-9 for r, w in zip(g.rung_prices, want_rungs))
    assert sgn * (g.stop - g.rung_prices[-1]) < 0                             # the stop is beyond the last rung


def test_real_tick_values_for_the_worked_example():
    g = _geom(Direction.LONG, 149.60, 149.23, atr1m=0.4137)
    assert g.rung_prices == (149.6, 149.507, 149.415, 149.322, 149.23) and g.stop == pytest.approx(149.167)
    s = _geom(Direction.SHORT, 150.40, 150.77, atr1m=0.4137)
    assert s.rung_prices == (150.4, 150.493, 150.585, 150.678, 150.77) and s.stop == pytest.approx(150.833)


@pytest.mark.parametrize("boundary,extreme,reason", [
    (149.60, 149.60, "DEGENERATE_SWEEP_RANGE"),
    (149.60, 149.70, "DEGENERATE_SWEEP_RANGE"),
    (149.60, 149.598, "SWEEP_RANGE_TOO_SMALL_FOR_LADDER"),               # 2 ticks: E1..E5 would not be distinct
])
def test_degenerate_or_too_small_ladders_are_rejected(boundary, extreme, reason):
    g, why = build_geometry(Direction.LONG, boundary, extreme, 0.4, RULES, CFG)
    assert g is None and why == reason


def test_a_range_of_exactly_the_minimum_number_of_ticks_gives_five_distinct_rungs():
    g = _geom(Direction.LONG, 149.600, 149.596)                             # 4 ticks -> one tick between rungs
    assert g.rung_prices == (149.6, 149.599, 149.598, 149.597, 149.596)


# ------------------------------------------------------------------ sizing: 0.50% NAV
@pytest.mark.parametrize("nav", [50.0, 100.0, 250.0, 1_000.0, 5_000.0, 25_000.0])
@pytest.mark.parametrize("direction,boundary,extreme", [
    (Direction.LONG, 149.60, 149.20), (Direction.LONG, 149.60, 148.60),
    (Direction.SHORT, 150.40, 150.80), (Direction.SHORT, 150.40, 151.40),
])
def test_full_ladder_loss_to_stop_never_exceeds_half_percent_of_nav(nav, boundary, extreme, direction):
    g = _geom(direction, boundary, extreme)
    sizing, why = size_ladder(g, nav, 0.005, RULES, CFG, **FEES)
    if sizing is None:                                     # too small an account: skipped, never enlarged
        assert why in {"BELOW_MIN_QTY", "BELOW_MIN_NOTIONAL", "BELOW_MIN_QTY_TP_SLICE"}
        return
    loss = sum(sizing.rung_qty * abs(p - g.stop) for p in g.rung_prices)
    assert loss <= nav * 0.005 + 1e-9
    assert loss == pytest.approx(sizing.planned_loss_usdt)
    assert sizing.total_qty == pytest.approx(5 * sizing.rung_qty)
    assert sizing.rung_qty >= RULES.min_qty


def test_quantity_is_floored_to_the_step_and_never_rounded_up():
    g = _geom(Direction.LONG, 149.60, 149.20)
    sizing, _ = size_ladder(g, 1_000.0, 0.005, RULES, CFG, **FEES)
    mean_distance = sum(abs(p - g.stop) for p in g.rung_prices) / 5
    unrounded_rung = 1_000.0 * 0.005 / mean_distance / 5
    assert sizing.rung_qty <= unrounded_rung and unrounded_rung - sizing.rung_qty < RULES.qty_step


def test_minimum_quantity_skips_the_setup_instead_of_raising_risk():
    g = _geom(Direction.LONG, 149.60, 149.20)
    sizing, why = size_ladder(g, 10.0, 0.005, RULES, CFG, **FEES)       # NAV 10 -> rung qty ~0.04 < 0.1
    assert sizing is None and why == "BELOW_MIN_QTY"


def test_minimum_notional_skips_the_setup():
    strict = SymbolRules("SOLUSDT", 0.01, 0.1, 0.1, min_notional_usdt=1_000.0)   # rung ~3.8 x 149 = ~566 < 1000
    g = _geom(Direction.LONG, 149.60, 149.20, rules=strict)
    sizing, why = size_ladder(g, 1_000.0, 0.005, strict, CFG, **FEES)
    assert sizing is None and why == "BELOW_MIN_NOTIONAL"


def test_fees_and_slippage_are_reported_separately_from_risk():
    g = _geom(Direction.LONG, 149.60, 149.20)
    sizing, _ = size_ladder(g, 1_000.0, 0.005, RULES, CFG, **FEES)
    assert sizing.est_entry_fees_usdt > 0 and sizing.est_stop_exit_fee_usdt > 0
    assert sizing.est_stop_slippage_usdt > 0
    assert sizing.planned_loss_usdt <= sizing.risk_budget_usdt         # fees are NOT inside the 0.50%


def test_expected_weighted_entry_is_the_mean_of_the_rungs():
    g = _geom(Direction.LONG, 149.60, 149.20)
    sizing, _ = size_ladder(g, 1_000.0, 0.005, RULES, CFG, **FEES)
    assert sizing.expected_avg_entry == pytest.approx(149.40)


# ------------------------------------------------------------------ take-profit split
def test_tp_quantities_split_30_40_30_with_remainder_to_tp3():
    parts = dict(split_tp_quantities(18.5, RULES, CFG, merge_below_min=False))
    assert parts["TP1"] == pytest.approx(5.5) and parts["TP2"] == pytest.approx(7.4)
    assert parts["TP3"] == pytest.approx(5.6)
    assert sum(parts.values()) == pytest.approx(18.5)


def test_partial_fill_slices_below_the_minimum_are_merged_forward():
    # 0.3 -> 30/40/30 floors to 0.0 / 0.1 / 0.2: the empty TP1 slice folds into TP2 (0.1 is the minimum)
    parts = dict(split_tp_quantities(0.3, RULES, CFG, merge_below_min=True))
    assert (parts["TP1"], parts["TP2"]) == (0.0, pytest.approx(0.1)) and parts["TP3"] == pytest.approx(0.2)
    # 0.2 -> 0.0 / 0.0 / 0.2: both under-minimum slices fold forward into TP3
    parts = dict(split_tp_quantities(0.2, RULES, CFG, merge_below_min=True))
    assert parts["TP1"] == 0.0 and parts["TP2"] == 0.0 and parts["TP3"] == pytest.approx(0.2)


# ------------------------------------------------------------------ engine-level gates
def test_engine_skips_a_setup_when_nav_cannot_carry_the_minimum_size():
    h = Harness().run(long_sweep(nav=10.0))
    assert h.engine.setup is None and h.intents == []
    assert "BELOW_MIN_QTY" in h.rejected()


def test_setup_is_skipped_when_nav_is_unknown_or_stale():
    assert "NAV_UNAVAILABLE" in Harness().run(long_sweep(nav=None)).rejected()


def test_sizing_records_the_planned_loss_at_half_percent_nav():
    h = Harness().run(long_sweep(nav=1_000.0))
    sizing = h.engine.setup.sizing
    assert sizing["risk_budget_usdt"] == pytest.approx(5.0)
    assert sizing["planned_loss_usdt"] <= 5.0 + 1e-9


def test_tp2_below_one_and_a_half_r_is_rejected():
    strict = replace(CFG, min_rr_to_tp2=6.0)                # scenario RR is ~4.8
    h = Harness(cfg=strict).run(long_sweep())
    assert h.engine.setup is None and "RR_TO_TP2_BELOW_MIN" in h.rejected()


def test_rr_to_tp2_is_measured_from_the_weighted_average_entry():
    assert rr_to_tp2(Direction.LONG, 149.40, 149.13, 150.70) == pytest.approx((150.70 - 149.40) / 0.27)
    assert rr_to_tp2(Direction.SHORT, 150.60, 150.87, 149.30) == pytest.approx((150.60 - 149.30) / 0.27)


def test_recorded_expected_r_meets_the_minimum_on_an_accepted_setup():
    h = Harness().run(long_sweep())
    assert h.engine.setup.expected_rr_tp2 >= 1.5


def test_targets_are_the_three_nearest_pre_existing_opposite_pools():
    h = Harness().run(long_sweep())
    prices = [t["price"] for t in h.engine.setup.targets]
    assert prices == [150.30, 150.70, 151.10]                # near edges of the ASK pools
    assert [t["level"] for t in h.engine.setup.targets] == ["TP1", "TP2", "TP3"]


def test_fewer_than_three_opposite_pools_is_a_no_trade():
    h = Harness().run(long_sweep(ask_pools=ASK_POOLS[:2]))
    assert h.engine.setup is None and "INSUFFICIENT_OPPOSITE_POOLS" in h.rejected()


def test_target_pools_must_be_pre_existing():
    h = Harness().run(long_sweep(ask_pools_first_seen_s=-100))         # first seen 160s before decision
    assert h.engine.setup is None and "INSUFFICIENT_OPPOSITE_POOLS" in h.rejected()


def test_wide_spread_fails_the_execution_gate():
    h = Harness().run(long_sweep(spread=0.20))                          # ~13 bps > 7.5 bps
    assert h.engine.setup is None and "SPREAD_GATE" in h.rejected()


def test_rung_that_would_cross_the_book_is_skipped_never_chased():
    close_asks = ((150.20, 5, 40.0), (150.60, 5, 40.0), (151.00, 5, 40.0))
    path = ((10, 150.0), (20, 149.85), (30, 149.70), (40, 149.30), (50, None), (60, 149.55))
    h = Harness().run(long_sweep(path=path, ask_pools=close_asks))
    s = h.engine.setup
    assert s is not None
    assert s.rungs[1].status == "SKIPPED" and s.rungs[1].reject_reason == "WOULD_CROSS_NO_CHASE"
    assert [i.rung for i in h.intents_of(PlaceEntry)] == [2, 3, 4, 5]


def test_short_setup_is_the_exact_mirror_of_the_long_setup():
    long_h = Harness().run(long_sweep())
    short_h = Harness().run(mirror(long_sweep()))
    ls, ss = long_h.engine.setup, short_h.engine.setup
    assert ss is not None and ss.direction is Direction.SHORT
    assert [round(300 - p, 3) for p in ls.geometry["rungs"]] == pytest.approx(ss.geometry["rungs"])
    assert round(300 - ls.geometry["stop"], 3) == pytest.approx(ss.geometry["stop"])
    assert [round(300 - t["price"], 3) for t in ls.targets] == pytest.approx([t["price"] for t in ss.targets])
    assert ls.sizing["total_qty"] == ss.sizing["total_qty"]
    assert ls.expected_rr_tp2 == pytest.approx(ss.expected_rr_tp2)
    assert [(i.rung, i.qty) for i in long_h.intents_of(PlaceEntry)] == \
           [(i.rung, i.qty) for i in short_h.intents_of(PlaceEntry)]
