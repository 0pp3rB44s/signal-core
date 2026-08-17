"""TrendFlow V1 — correctness and safety.

The tests that matter most are the ones that would have caught the mistakes this project already
paid for: signals computed on unsettled data, targets marked hit by price action from before the
position existed, and a stop distance quietly shrunk to justify a bigger position.
"""

from __future__ import annotations

import pytest

from strategies.trendflow_v1 import (Candle, CostModel, DataQualityError, TrendFlowSpec,
                                     atr, cost_gate, ema, evaluate, percentile_rank,
                                     size_position, volume_ratio)
from strategies.trendflow_lifecycle import (LifecycleError, PortfolioState, RiskBreakers,
                                            TargetTracker)

SPEC = TrendFlowSpec()
H = 3_600_000


def _mid_band_history(current=0.004):
    """History placing `current` at roughly the 60th percentile — inside the P30-P95 band."""
    return [current * (0.4 + 0.02 * i) for i in range(100)]


def _c(i, close, *, high=None, low=None, vol=1000.0, closed=True, tf=H):
    return Candle(open_time_ms=i * tf, close_time_ms=(i + 1) * tf,
                  open=close, high=high if high is not None else close * 1.002,
                  low=low if low is not None else close * 0.998,
                  close=close, volume=vol, closed=closed)


def _trend(n=80, start=100.0, step=0.05, vol=1000.0, tf=H):
    """A realistic uptrend: a steady advance that stays near its own EMA20.

    A perfectly straight ramp is *overextended* by construction — price sits many ATR above the
    mean — so it would be rejected by the anti-chase rule, correctly. Real trends pull back.
    """
    out = [_c(i, start + i * step, vol=vol, tf=tf) for i in range(n)]
    out[-1] = _c(n - 1, start + (n - 1) * step, vol=vol * 1.5, tf=tf)  # volume confirms the move
    return out


def _down(n=80, start=150.0, step=0.05, vol=1000.0, tf=H):
    out = [_c(i, start - i * step, vol=vol, tf=tf) for i in range(n)]
    out[-1] = _c(n - 1, start - (n - 1) * step, vol=vol * 1.5, tf=tf)
    return out


# --- 1-4 indicators -----------------------------------------------------------


def test_ema_matches_the_standard_recursion():
    values = [1.0] * 20 + [2.0]
    out = ema(values, 20)
    k = 2 / 21
    assert out[0] == pytest.approx(1.0)
    assert out[-1] == pytest.approx(2.0 * k + 1.0 * (1 - k))


def test_ema_refuses_insufficient_history():
    with pytest.raises(DataQualityError):
        ema([1.0] * 5, 20)


def test_atr_is_positive_and_needs_period_plus_one():
    assert atr(_trend(20), 14) > 0
    with pytest.raises(DataQualityError):
        atr(_trend(10), 14)


def test_volume_ratio_uses_the_median_of_prior_closed_candles():
    candles = _trend(30, vol=100.0)
    candles[-1] = _c(29, candles[-1].close, vol=250.0)
    assert volume_ratio(candles, 20) == pytest.approx(2.5)


def test_percentile_rank_bounds():
    assert percentile_rank([1, 2, 3, 4], 0) == 0.0
    assert percentile_rank([1, 2, 3, 4], 99) == 100.0


# --- 5-7 qualification --------------------------------------------------------


def test_a_clean_uptrend_qualifies_long():
    sig = evaluate(_trend(), _trend(60, step=0.2, tf=4 * H), SPEC, atr_pct_history=_mid_band_history())
    assert sig.direction == "LONG"
    assert sig.score >= SPEC.score_threshold


def test_a_clean_downtrend_qualifies_short():
    sig = evaluate(_down(), _down(60, step=0.2, tf=4 * H), SPEC, atr_pct_history=_mid_band_history())
    assert sig.direction == "SHORT"


def test_a_flat_market_qualifies_neither():
    flat = [_c(i, 100.0) for i in range(80)]
    sig = evaluate(flat, [_c(i, 100.0, tf=4 * H) for i in range(60)], SPEC,
                   atr_pct_history=_mid_band_history())
    assert sig.direction is None


def test_score_components_are_logged_separately():
    sig = evaluate(_trend(), _trend(60, step=0.2, tf=4 * H), SPEC, atr_pct_history=_mid_band_history())
    assert set(sig.components) == {"structure_1h", "structure_4h", "momentum", "volume", "volatility"}
    assert sum(sig.components.values()) == pytest.approx(sig.score)


def test_score_is_bounded_at_100():
    sig = evaluate(_trend(), _trend(60, step=0.2, tf=4 * H), SPEC, atr_pct_history=_mid_band_history())
    assert 0.0 <= sig.score <= 100.0


# --- 1 candle-close enforcement ----------------------------------------------


def test_an_open_candle_is_refused():
    candles = _trend()
    candles[-1] = _c(len(candles) - 1, candles[-1].close, closed=False)
    with pytest.raises(DataQualityError, match="open candle"):
        evaluate(candles, _trend(60, step=0.2, tf=4 * H), SPEC)


def test_short_history_is_refused():
    with pytest.raises(DataQualityError):
        evaluate(_trend(20), _trend(60, step=0.2, tf=4 * H), SPEC)


# --- 8-10 filters -------------------------------------------------------------


def test_overextension_blocks_the_entry():
    """Isolated from the volatility gate: a spike raises ATR too, and a 25% bar genuinely *is* a
    shock, so the history is scaled to keep the regime check passing and leave only the extension
    rule under test."""
    candles = _trend()
    spiked = candles[-1].close * 1.25
    candles[-1] = _c(len(candles) - 1, spiked, high=spiked, low=spiked * 0.99, vol=1500.0)
    from strategies.trendflow_v1 import atr as _atr
    atr_pct_now = _atr(candles, SPEC.atr_period) / candles[-1].close
    sig = evaluate(candles, _trend(60, step=0.2, tf=4 * H), SPEC,
                   atr_pct_history=_mid_band_history(atr_pct_now))
    assert sig.direction is None and sig.block_reason == "WAIT_OVEREXTENDED"


def test_low_volatility_blocks_the_entry():
    sig = evaluate(_trend(), _trend(60, step=0.2, tf=4 * H), SPEC,
                   atr_pct_history=[1.0] * 100)  # current atr_pct far below history
    assert sig.block_reason in ("NO_TRADE_LOW_VOL", "SCORE_BELOW_THRESHOLD")


def test_volatility_shock_blocks_the_entry():
    sig = evaluate(_trend(), _trend(60, step=0.2, tf=4 * H), SPEC,
                   atr_pct_history=[1e-9] * 100)  # current atr_pct above every historical value
    assert sig.block_reason in ("NO_TRADE_SHOCK", "SCORE_BELOW_THRESHOLD")


def test_thin_volume_blocks_the_entry():
    candles = _trend(vol=1000.0)
    candles[-1] = _c(len(candles) - 1, candles[-1].close, vol=100.0)
    sig = evaluate(candles, _trend(60, step=0.2, tf=4 * H), SPEC, atr_pct_history=_mid_band_history())
    assert sig.direction is None


# --- 11-12 cost gate ----------------------------------------------------------


def test_cost_gate_requires_three_times_the_round_trip():
    cost = CostModel(taker_fee_bps=6.0, maker_fee_bps=2.0, spread_bps=1.0, slippage_bps=1.0)
    rt = cost.round_trip_bps()
    assert rt == pytest.approx(16.0)
    ok, detail = cost_gate(expected_opportunity_bps=rt * 3.0, cost=cost, spec=SPEC)
    assert ok and detail["cost_multiple"] == pytest.approx(3.0)
    assert not cost_gate(expected_opportunity_bps=rt * 2.99, cost=cost, spec=SPEC)[0]


def test_taker_economics_are_used_unless_maker_is_proven():
    taker = CostModel(6.0, 2.0, 1.0, 1.0).round_trip_bps()
    maker = CostModel(6.0, 2.0, 1.0, 1.0, assume_taker=False).round_trip_bps()
    assert taker > maker
    assert CostModel(6.0, 2.0, 1.0, 1.0).assume_taker is True


def test_cost_gate_logs_every_component():
    _, detail = cost_gate(expected_opportunity_bps=100.0,
                          cost=CostModel(6.0, 2.0, 1.0, 1.0), spec=SPEC)
    assert set(detail) == {"estimated_cost_bps", "expected_opportunity_bps",
                           "cost_multiple", "required_multiple"}


# --- 13-14 sizing -------------------------------------------------------------


def _size(**kw):
    base = dict(direction="LONG", entry_price=100.0, atr14=2.0, equity_usdt=10_000.0,
                spec=SPEC, min_qty=0.001, qty_step=0.001, min_notional=5.0)
    base.update(kw)
    return size_position(**base)


def test_risk_per_trade_is_the_configured_fraction_of_equity():
    r = _size()
    assert r.refused_reason is None
    assert r.risk_usdt <= 10_000.0 * 0.0025 * 1.0001
    assert r.stop_price == pytest.approx(100.0 - 1.5 * 2.0)


def test_a_wider_stop_yields_a_smaller_position_never_a_wider_risk():
    tight, wide = _size(atr14=1.0), _size(atr14=4.0)
    assert wide.quantity < tight.quantity
    assert wide.risk_usdt <= 10_000.0 * 0.0025 * 1.0001


def test_rounding_can_only_reduce_risk():
    r = _size(qty_step=1.0)
    if r.refused_reason is None:
        assert r.risk_usdt <= 10_000.0 * 0.0025 * 1.0001


def test_exchange_minimums_refuse_rather_than_shrink_the_stop():
    r = _size(min_qty=1e9)
    assert r.quantity == 0.0 and r.refused_reason == "BELOW_EXCHANGE_MINIMUM_QTY"
    assert r.stop_price == pytest.approx(100.0 - 1.5 * 2.0)


def test_min_notional_is_enforced():
    assert _size(min_notional=1e9).refused_reason == "BELOW_EXCHANGE_MIN_NOTIONAL"


@pytest.mark.parametrize("bad", [dict(entry_price=0), dict(atr14=0), dict(equity_usdt=0),
                                 dict(direction="SIDEWAYS")])
def test_invalid_sizing_inputs_refuse(bad):
    assert _size(**bad).quantity == 0.0


# --- 15-16 portfolio ----------------------------------------------------------


def test_portfolio_risk_cap_limits_the_last_position():
    r = _size(committed_risk_pct=0.45)
    assert r.risk_usdt <= 10_000.0 * 0.0005 * 1.0001


def test_portfolio_risk_exhausted_refuses():
    assert _size(committed_risk_pct=0.50).refused_reason == "PORTFOLIO_RISK_EXHAUSTED"


def test_max_two_positions():
    assert PortfolioState(("LONG", "SHORT"), 0.4).can_open("LONG", SPEC) == "MAX_POSITIONS"
    assert PortfolioState(("LONG",), 0.25).can_open("SHORT", SPEC) is None


def test_correlated_same_side_positions_are_capped():
    """BTC/ETH/SOL move together; three longs is one leveraged bet, not three trades."""
    assert PortfolioState(("LONG", "LONG"), 0.4).can_open("LONG", SPEC) is not None


# --- 17-20 lifecycle ----------------------------------------------------------


def _tracker(direction="LONG", entry=100.0, stop=97.0, fill_ms=1_000_000):
    return TargetTracker(direction=direction, entry_price=entry, fill_timestamp_ms=fill_ms,
                         initial_stop=stop, spec=SPEC)


def test_tp1_closes_a_quarter_and_moves_the_stop_to_break_even():
    t = _tracker()
    assert "TP1_HIT" in t.observe(price=103.0, timestamp_ms=1_000_001)
    assert t.remaining_pct == pytest.approx(75.0)
    assert t.stop_price == pytest.approx(100.0)


def test_tp2_requires_tp1_first_and_closes_another_quarter():
    t = _tracker()
    t.observe(price=103.0, timestamp_ms=1_000_001)
    assert "TP2_HIT" in t.observe(price=106.0, timestamp_ms=1_000_002)
    assert t.remaining_pct == pytest.approx(50.0)


def test_the_runner_trails_after_tp2():
    t = _tracker()
    t.observe(price=103.0, timestamp_ms=1_000_001)
    t.observe(price=106.0, timestamp_ms=1_000_002)
    before = t.stop_price
    t.observe(price=120.0, timestamp_ms=1_000_003, atr14=2.0)
    assert t.stop_price > before


def test_the_trail_never_widens():
    t = _tracker()
    t.observe(price=103.0, timestamp_ms=1_000_001)
    t.observe(price=106.0, timestamp_ms=1_000_002)
    t.observe(price=120.0, timestamp_ms=1_000_003, atr14=2.0)
    high_water = t.stop_price
    t.observe(price=108.0, timestamp_ms=1_000_004, atr14=2.0)
    assert t.stop_price == pytest.approx(high_water)


def test_short_trail_mirrors_and_never_widens():
    t = _tracker("SHORT", 100.0, 103.0)
    t.observe(price=97.0, timestamp_ms=1_000_001)
    t.observe(price=94.0, timestamp_ms=1_000_002)
    t.observe(price=80.0, timestamp_ms=1_000_003, atr14=2.0)
    low_water = t.stop_price
    t.observe(price=92.0, timestamp_ms=1_000_004, atr14=2.0)
    assert t.stop_price == pytest.approx(low_water)


# --- 30 PB-2 regression -------------------------------------------------------


def test_pre_entry_price_action_can_never_mark_a_target_hit():
    """The PB-2 regression, stated exactly as the defect was.

    A candle whose high cleared TP1 *before* the fill must not advance target state.
    """
    t = _tracker(fill_ms=2_000_000)
    with pytest.raises(LifecycleError, match="PB-2"):
        t.observe(price=999.0, timestamp_ms=1_999_999)   # a pre-entry high far beyond TP1
    assert t.tp1_hit is False
    assert t.remaining_pct == pytest.approx(100.0)


def test_an_observation_exactly_at_the_fill_is_still_refused():
    t = _tracker(fill_ms=2_000_000)
    with pytest.raises(LifecycleError):
        t.observe(price=999.0, timestamp_ms=2_000_000)


def test_post_entry_price_action_does_mark_the_target_hit():
    t = _tracker(fill_ms=2_000_000)
    assert "TP1_HIT" in t.observe(price=103.0, timestamp_ms=2_000_001)
    assert t.tp1_hit is True


def test_observations_may_not_go_backwards():
    t = _tracker(fill_ms=1_000)
    t.observe(price=101.0, timestamp_ms=5_000)
    with pytest.raises(LifecycleError):
        t.observe(price=102.0, timestamp_ms=4_000)


def test_a_tracker_cannot_be_built_without_a_real_fill_timestamp():
    with pytest.raises(LifecycleError):
        TargetTracker(direction="LONG", entry_price=100.0, fill_timestamp_ms=0,
                      initial_stop=97.0, spec=SPEC)


# --- 21-22 exits --------------------------------------------------------------


def test_trend_invalidation_exit_on_closed_candle():
    t = _tracker()
    assert t.trend_invalidated(closed_1h_close=99.0, ema20_1h=100.0) is True
    assert t.trend_invalidated(closed_1h_close=101.0, ema20_1h=100.0) is False


def test_short_invalidation_mirrors():
    t = _tracker("SHORT", 100.0, 103.0)
    assert t.trend_invalidated(closed_1h_close=101.0, ema20_1h=100.0) is True


def test_max_hold_is_seventy_two_hours_not_ten_minutes():
    t = _tracker(fill_ms=1)
    assert t.max_hold_exceeded(1 + 71 * H) is False
    assert t.max_hold_exceeded(1 + 72 * H) is True


# --- 23-25 breakers -----------------------------------------------------------


def _breakers(**kw):
    base = dict(spec=SPEC, day_start_equity=10_000.0, week_start_equity=10_000.0)
    base.update(kw)
    return RiskBreakers(**base)


def test_daily_halt_at_one_percent():
    b = _breakers()
    b.record_trade(-99.0)
    assert b.entries_blocked() is None
    b.record_trade(-2.0)
    assert b.entries_blocked() == "TREND_FLOW_DAILY_HALT"


def test_weekly_halt_at_three_percent():
    b = _breakers(day_start_equity=1e9)
    b.record_trade(-301.0)
    assert b.entries_blocked() == "TREND_FLOW_WEEKLY_HALT"


def test_three_consecutive_losses_halt_entries():
    b = _breakers()
    for _ in range(3):
        b.record_trade(-1.0)
    assert b.entries_blocked() == "TREND_FLOW_CONSECUTIVE_LOSS_HALT"


def test_a_winner_resets_the_consecutive_loss_counter():
    b = _breakers()
    b.record_trade(-1.0); b.record_trade(-1.0); b.record_trade(+1.0)
    assert b.consecutive_losses == 0
    assert b.entries_blocked() is None


def test_breakers_gate_entries_only_and_never_exits():
    """A risk breaker that could block an exit would trap capital in a losing position."""
    import inspect
    source = inspect.getsource(RiskBreakers)
    assert "entries_blocked" in source
    for exit_word in ("close_position", "cancel_stop", "block_exit", "suppress_exit"):
        assert exit_word not in source


# --- 28-29 data quality -------------------------------------------------------


def test_malformed_candle_geometry_is_refused():
    candles = _trend()
    candles[-1] = Candle(80 * H, 81 * H, 100, low_ := 90.0, 110.0, 100.0, 10.0)  # high < low
    with pytest.raises(DataQualityError):
        evaluate(candles, _trend(60, step=0.2, tf=4 * H), SPEC)


def test_unordered_candles_are_refused():
    candles = _trend()
    candles[-1], candles[-2] = candles[-2], candles[-1]
    with pytest.raises(DataQualityError, match="ordered"):
        evaluate(candles, _trend(60, step=0.2, tf=4 * H), SPEC)


# --- 31-32 isolation ----------------------------------------------------------


def test_strategy_id_is_distinct_from_microflow():
    assert SPEC.spec_id.startswith("trendflow-v1")
    assert "microflow" not in SPEC.spec_id


def test_universe_is_the_three_liquid_symbols_only():
    assert SPEC.symbols == ("BTCUSDT", "ETHUSDT", "SOLUSDT")


def test_trendflow_does_not_import_microflow_direction_logic():
    """MicroFlow may gate execution quality. It may never set or reverse direction."""
    from pathlib import Path
    repo = Path(__file__).resolve().parents[1]
    for module in ("strategies/trendflow_v1.py", "strategies/trendflow_lifecycle.py"):
        source = (repo / module).read_text()
        assert "from microflow" not in source
        assert "import microflow" not in source


def test_both_sides_qualifying_is_an_invariant_violation_not_a_coin_flip():
    import strategies.trendflow_v1 as tf
    sig = tf.TrendFlowSignal(None, 90.0, {"long": 90.0, "short": 90.0}, {},
                             "INVARIANT_VIOLATION_BOTH_SIDES")
    assert sig.direction is None and sig.is_entry is False


# --- gaps found by mutation testing ------------------------------------------


def test_tighten_refuses_any_stop_that_would_widen_risk():
    """Direct test of the stop ratchet.

    The trailing test above cannot catch a widening bug on its own: the trail is derived from
    `max_favourable_price`, which never retreats, so the computed candidate never widens either.
    The ratchet itself has to be exercised directly.
    """
    t = _tracker()                     # LONG, entry 100, stop 97
    assert t._tighten(98.0) is True and t.stop_price == pytest.approx(98.0)
    assert t._tighten(95.0) is False, "a lower stop on a LONG widens risk and must be refused"
    assert t.stop_price == pytest.approx(98.0)

    s = _tracker("SHORT", 100.0, 103.0)
    assert s._tighten(102.0) is True and s.stop_price == pytest.approx(102.0)
    assert s._tighten(105.0) is False, "a higher stop on a SHORT widens risk and must be refused"
    assert s.stop_price == pytest.approx(102.0)


def test_both_sides_qualifying_yields_no_trade(monkeypatch):
    """If scoring is ever corrupted into passing both sides, the answer is NO TRADE.

    This cannot arise from valid data — the conditions are mutually exclusive — so the branch is
    driven directly. A strategy that silently picks one side when its own logic contradicts itself
    is worse than one that refuses.
    """
    import strategies.trendflow_v1 as tf
    monkeypatch.setattr(tf, "_score", lambda *a, **k: (100.0, {"forced": 100.0}))
    sig = tf.evaluate(_trend(), _trend(60, step=0.2, tf=4 * H), SPEC,
                      atr_pct_history=_mid_band_history())
    assert sig.direction is None
    assert sig.block_reason == "INVARIANT_VIOLATION_BOTH_SIDES"
