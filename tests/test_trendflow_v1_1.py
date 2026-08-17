"""TrendFlow V1.1 — the horizon-mismatch correction.

The tests that matter here are the ones asserting what V1.1 *removed*: a 1H pullback must no longer
close a position, and a symbol must not be re-enterable until a new closed 4H candle exists. Those
two rules are the entire architecture correction; everything else is carried over.
"""

from __future__ import annotations

import pytest

from strategies.trendflow_v1 import Candle, CostModel, DataQualityError, cost_gate, size_position
from strategies.trendflow_v1_1 import (SPEC_ID, TrendFlowV11Spec, classify_4h, evaluate,
                                       trend_invalidated_4h)
from strategies.trendflow_lifecycle import LifecycleError, TrendRunnerTracker

SPEC = TrendFlowV11Spec()
H = 3_600_000
H4 = 4 * H


def _c(i, close, *, high=None, low=None, vol=1000.0, tf=H, closed=True):
    return Candle(i * tf, (i + 1) * tf, close,
                  high if high is not None else close * 1.002,
                  low if low is not None else close * 0.998,
                  close, vol, closed)


def _up(n=80, start=100.0, step=0.05, tf=H, vol=1000.0):
    out = [_c(i, start + i * step, tf=tf, vol=vol) for i in range(n)]
    out[-1] = _c(n - 1, start + (n - 1) * step, tf=tf, vol=vol * 1.5)
    return out


def _dn(n=80, start=150.0, step=0.05, tf=H, vol=1000.0):
    out = [_c(i, start - i * step, tf=tf, vol=vol) for i in range(n)]
    out[-1] = _c(n - 1, start - (n - 1) * step, tf=tf, vol=vol * 1.5)
    return out


def _hist(current=0.004):
    return [current * (0.4 + 0.02 * i) for i in range(100)]


# --- the trend is defined on 4H, with three interpretable states ---------------


def test_four_hour_uptrend_is_classified_up():
    regime, _, conds = classify_4h(_up(60, step=0.2, tf=H4), SPEC)
    assert regime == "UPTREND" and all(conds.values())


def test_four_hour_downtrend_is_classified_down():
    assert classify_4h(_dn(60, step=0.2, tf=H4), SPEC)[0] == "DOWNTREND"


def test_a_flat_four_hour_is_no_trend():
    flat = [_c(i, 100.0, tf=H4) for i in range(60)]
    assert classify_4h(flat, SPEC)[0] == "NO_TREND"


def test_there_is_no_weighted_score_any_more():
    """V1.1 replaces the 0-100 score with three states. The score must be gone, not hidden."""
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "strategies/trendflow_v1_1.py").read_text()
    for gone in ("weight_1h_structure", "score_threshold", "_score("):
        assert gone not in source, f"{gone!r} survives in V1.1"


def test_no_four_hour_trend_blocks_regardless_of_one_hour():
    flat4 = [_c(i, 100.0, tf=H4) for i in range(60)]
    state = evaluate(_up(), flat4, SPEC, atr_pct_history=_hist())
    assert state.direction is None and state.block_reason == "NO_4H_TREND"


# --- 1H is entry timing only --------------------------------------------------


def test_one_hour_times_the_entry_inside_a_qualified_four_hour_trend():
    state = evaluate(_up(), _up(60, step=0.2, tf=H4), SPEC, atr_pct_history=_hist())
    assert state.regime == "UPTREND" and state.direction == "LONG"


def test_a_one_hour_pullback_blocks_entry_but_does_not_flip_the_regime():
    candles = _up()
    pulled = candles[-1].close * 0.97
    candles[-1] = _c(len(candles) - 1, pulled, vol=1500.0)
    state = evaluate(candles, _up(60, step=0.2, tf=H4), SPEC, atr_pct_history=_hist())
    assert state.regime == "UPTREND"          # the trend is intact
    assert state.direction is None            # we simply do not enter here
    assert state.block_reason in ("WAIT_1H_TIMING", "WAIT_OVEREXTENDED")


def test_short_side_mirrors_exactly():
    state = evaluate(_dn(), _dn(60, step=0.2, tf=H4), SPEC, atr_pct_history=_hist())
    assert state.regime == "DOWNTREND" and state.direction == "SHORT"


# --- the central correction ---------------------------------------------------


def test_the_one_hour_invalidation_exit_is_gone():
    """V1 exited on a closed 1H close through EMA20. That rule must not exist in V1.1."""
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "strategies/trendflow_v1_1.py").read_text()
    assert "ema20_1h" not in source.split("def trend_invalidated_4h")[1]
    assert "trend_invalidated_4h" in source


def test_trend_invalidation_requires_closed_four_hour_evidence():
    assert trend_invalidated_4h("LONG", closed_4h_close=99.0, ema20_4h=100.0) is True
    assert trend_invalidated_4h("LONG", closed_4h_close=101.0, ema20_4h=100.0) is False
    assert trend_invalidated_4h("SHORT", closed_4h_close=101.0, ema20_4h=100.0) is True


# --- profit management: TP1 only, then a 2.0 ATR runner -----------------------


def _tracker(direction="LONG", entry=100.0, stop=97.0, fill_ms=1_000_000):
    return TrendRunnerTracker(direction=direction, entry_price=entry,
                              fill_timestamp_ms=fill_ms, initial_stop=stop, spec=SPEC)


def test_tp1_takes_a_quarter_and_leaves_a_seventy_five_percent_runner():
    t = _tracker()
    assert "TP1_HIT" in t.observe(price=103.0, timestamp_ms=1_000_001)
    assert t.remaining_pct == pytest.approx(75.0)
    assert t.stop_price == pytest.approx(100.0)


def test_there_is_no_tp2_in_v1_1():
    t = _tracker()
    t.observe(price=103.0, timestamp_ms=1_000_001)
    t.observe(price=106.0, timestamp_ms=1_000_002)
    assert t.remaining_pct == pytest.approx(75.0), "a second partial must not fire"
    assert not hasattr(t, "tp2_hit")


def test_the_trail_is_two_atr_and_arms_only_after_tp1():
    t = _tracker()
    t.observe(price=101.0, timestamp_ms=1_000_001, atr14=2.0)
    assert t.stop_price == pytest.approx(97.0), "no trailing before TP1"
    t.observe(price=103.0, timestamp_ms=1_000_002, atr14=2.0)   # TP1
    t.observe(price=120.0, timestamp_ms=1_000_003, atr14=2.0)
    assert t.stop_price == pytest.approx(120.0 - 2.0 * 2.0)


def test_the_trail_never_widens_on_either_side():
    t = _tracker()
    t.observe(price=103.0, timestamp_ms=1_000_001)
    t.observe(price=120.0, timestamp_ms=1_000_002, atr14=2.0)
    high_water = t.stop_price
    t.observe(price=108.0, timestamp_ms=1_000_003, atr14=2.0)
    assert t.stop_price == pytest.approx(high_water)

    s = _tracker("SHORT", 100.0, 103.0)
    s.observe(price=97.0, timestamp_ms=1_000_001)
    s.observe(price=80.0, timestamp_ms=1_000_002, atr14=2.0)
    low_water = s.stop_price
    s.observe(price=92.0, timestamp_ms=1_000_003, atr14=2.0)
    assert s.stop_price == pytest.approx(low_water)


# --- PB-2 carries over --------------------------------------------------------


def test_pre_entry_price_action_can_never_mark_tp1_hit():
    t = _tracker(fill_ms=2_000_000)
    with pytest.raises(LifecycleError, match="PB-2"):
        t.observe(price=999.0, timestamp_ms=1_999_999)
    assert t.tp1_hit is False and t.remaining_pct == pytest.approx(100.0)


def test_post_entry_price_action_does_mark_tp1_hit():
    t = _tracker(fill_ms=2_000_000)
    assert "TP1_HIT" in t.observe(price=103.0, timestamp_ms=2_000_001)


def test_max_hold_is_still_seventy_two_hours():
    t = _tracker(fill_ms=1)
    assert t.max_hold_exceeded(1 + 71 * H) is False
    assert t.max_hold_exceeded(1 + 72 * H) is True


# --- carried-over invariants --------------------------------------------------


def test_closed_candle_enforcement_survives():
    candles = _up()
    candles[-1] = _c(len(candles) - 1, candles[-1].close, closed=False)
    with pytest.raises(DataQualityError, match="open candle"):
        evaluate(candles, _up(60, step=0.2, tf=H4), SPEC)


def test_cost_gate_and_taker_assumption_survive():
    cost = CostModel(6.0, 2.0, 1.0, 1.0)
    assert cost.assume_taker is True
    assert not cost_gate(expected_opportunity_bps=cost.round_trip_bps() * 2.9,
                         cost=cost, spec=SPEC)[0]


def test_risk_sizing_survives_unchanged():
    r = size_position(direction="LONG", entry_price=100.0, atr14=2.0, equity_usdt=10_000.0,
                      spec=SPEC, min_qty=0.001, qty_step=0.001, min_notional=5.0)
    assert r.refused_reason is None
    assert r.risk_usdt <= 10_000.0 * 0.0025 * 1.0001
    assert r.stop_price == pytest.approx(97.0)


def test_spec_id_is_distinct_from_v1_and_microflow():
    assert SPEC_ID == "trendflow-v1.1-20260817"
    assert SPEC.spec_id != "trendflow-v1-20260817"
    assert "microflow" not in SPEC.spec_id


def test_v1_1_does_not_import_microflow():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "strategies/trendflow_v1_1.py").read_text()
    assert "microflow" not in source.lower()


def test_one_hour_timing_is_required_and_is_the_reason_for_the_block():
    """Isolated from the extension rule.

    The pullback test above accepts either block reason, so it cannot prove the timing check is
    even consulted. Here the price sits *below* EMA20 while comfortably inside the extension
    limit, so only the timing rule can reject it.
    """
    from strategies.trendflow_v1 import ema as _ema
    candles = _up()
    e20 = _ema([c.close for c in candles[:-1]], SPEC.ema_fast)[-1]
    # Just below EMA20, and therefore trivially inside the 1.5 ATR extension limit.
    candles[-1] = _c(len(candles) - 1, e20 * 0.9995, vol=1500.0)
    state = evaluate(candles, _up(60, step=0.2, tf=H4), SPEC, atr_pct_history=_hist())
    assert state.regime == "UPTREND"
    assert state.direction is None
    assert state.block_reason == "WAIT_1H_TIMING"
