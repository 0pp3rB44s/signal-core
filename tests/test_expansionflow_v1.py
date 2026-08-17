"""ExpansionFlow V1 — the full pre-registered test matrix."""

from __future__ import annotations

import pytest

from strategies.expansionflow_v1 import (STRATEGY_ID, SPEC_ID, BookSnapshot, Candle,
                                         ExpansionDataError, ExpansionSpec, Fees,
                                         available_move_bps, book_confirms, breakout, direction,
                                         economic_gate, opportunity, retest, size_position,
                                         structural_stop)
from strategies.expansionflow_lifecycle import (ExpansionLifecycleError, ExpansionPortfolio,
                                                ExpansionPosition, MakerOrder)

SPEC = ExpansionSpec()
FEES = Fees(maker_bps=2.0, taker_bps=6.0, slippage_bps=1.0)
H = 3_600_000
M15 = 900_000


def _c(i, o, h, l, c, tf=H, closed=True):
    return Candle(i * tf, (i + 1) * tf, o, h, l, c, 0.0, closed)


def _flat_1h(n=130, price=100.0, rng=0.5):
    return [_c(i, price, price + rng, price - rng, price) for i in range(n)]


def _expanding_1h(n=130, price=100.0):
    """Quiet baseline then a genuine expansion in the recent bars."""
    out = [_c(i, price, price + 0.3, price - 0.3, price) for i in range(n - 20)]
    p = price
    for i in range(n - 20, n):
        p += 1.2
        out.append(_c(i, p - 1.0, p + 2.2, p - 1.4, p + 1.6))
    return out


# --- opportunity --------------------------------------------------------------


def test_low_atr_ratio_is_rejected():
    ok, values, reason = opportunity(_flat_1h(), SPEC, FEES)
    assert not ok and reason in ("NO_EXPANSION", "ATR_BELOW_FRICTION_MULTIPLE")


def test_missing_authenticated_fees_fails_closed():
    ok, _, reason = opportunity(_expanding_1h(), SPEC, None)
    assert not ok and reason == "NO_AUTHENTICATED_FEES"


def test_atr_must_clear_four_times_friction():
    """A high ratio on a tiny absolute ATR is still not an opportunity."""
    fat = Fees(maker_bps=200.0, taker_bps=400.0, slippage_bps=50.0)
    ok, values, reason = opportunity(_expanding_1h(), SPEC, fat)
    assert not ok and reason == "ATR_BELOW_FRICTION_MULTIPLE"
    assert values["atr_bps"] < SPEC.atr_friction_multiple * values["friction_bps"]


def test_a_qualifying_expansion_passes():
    ok, values, reason = opportunity(_expanding_1h(), SPEC, FEES)
    assert ok and reason is None
    assert values["atr_ratio"] >= SPEC.atr_ratio_min


def test_friction_assumes_a_taker_exit():
    """The protective stop prioritises capital, so pricing the exit as maker would be fiction."""
    assert FEES.maker_first_round_trip_bps() == pytest.approx(2.0 + 6.0 + 1.0)


# --- direction ----------------------------------------------------------------


def _uptrend_1h(n=130):
    """A realistic uptrend: bar range well above the per-bar drift.

    A clean staircase is *overextended* by construction — price sits many ATR above its own EMA —
    and would be correctly rejected. Real trends carry noise.
    """
    out = []
    p = 100.0
    for i in range(n):
        p += 0.08
        out.append(_c(i, p - 0.50, p + 0.55, p - 0.60, p + 0.40))
    return out


def _downtrend_1h(n=130):
    out = []
    p = 150.0
    for i in range(n):
        p -= 0.08
        out.append(_c(i, p + 0.50, p + 0.60, p - 0.55, p - 0.40))
    return out


def test_long_context_is_detected():
    side, values, reason = direction(_uptrend_1h(), SPEC)
    assert side == "LONG" and reason is None
    assert values["ema20_1h"] > values["ema50_1h"]


def test_short_context_is_detected():
    side, _, _ = direction(_downtrend_1h(), SPEC)
    assert side == "SHORT"


def test_flat_ema_gives_no_direction():
    side, _, reason = direction(_flat_1h(), SPEC)
    assert side is None and reason in ("NO_DIRECTIONAL_CONTEXT", "OVEREXTENDED")


def test_overextension_is_rejected():
    c = _uptrend_1h()
    last = c[-1]
    spike = last.close * 1.20
    c[-1] = _c(len(c) - 1, last.open, spike, last.low, spike)
    side, _, reason = direction(c, SPEC)
    assert side is None and reason == "OVEREXTENDED"


def test_a_weak_close_inside_the_bar_is_rejected():
    """Condition 4: the close must sit in the upper 60% of the range for a long."""
    c = _uptrend_1h()
    last = c[-1]
    c[-1] = _c(len(c) - 1, last.open, last.close + 2.0, last.close - 0.05, last.close)
    side, values, reason = direction(c, SPEC)
    assert values["close_position"] < SPEC.close_position_min
    assert side is None


# --- breakout -----------------------------------------------------------------


def _15m_base(n=40, price=100.0, rng=0.4):
    return [_c(i, price, price + rng, price - rng, price, tf=M15) for i in range(n)]


def test_a_valid_breakout_is_detected():
    c = _15m_base()
    high = max(x.high for x in c[-21:-1])
    c[-1] = _c(len(c) - 1, high, high + 1.0, high - 0.2, high + 0.9, tf=M15)
    level, values, reason = breakout(c, "LONG", SPEC)
    assert level is not None and reason is None
    assert SPEC.breakout_min_range_atr <= values["breakout_range_atr"] <= SPEC.breakout_max_range_atr


def test_no_breakout_when_the_level_is_not_cleared():
    c = _15m_base()
    assert breakout(c, "LONG", SPEC)[2] == "NO_BREAKOUT"


def test_an_insufficient_range_breakout_is_rejected():
    c = _15m_base()
    high = max(x.high for x in c[-21:-1])
    c[-1] = _c(len(c) - 1, high + 0.01, high + 0.03, high, high + 0.02, tf=M15)
    assert breakout(c, "LONG", SPEC)[2] == "BREAKOUT_RANGE_TOO_SMALL"


def test_a_volatility_shock_breakout_is_rejected():
    c = _15m_base()
    high = max(x.high for x in c[-21:-1])
    c[-1] = _c(len(c) - 1, high - 3.0, high + 8.0, high - 4.0, high + 7.0, tf=M15)
    assert breakout(c, "LONG", SPEC)[2] == "VOLATILITY_SHOCK"


# --- retest -------------------------------------------------------------------


def test_a_valid_long_retest_is_accepted():
    level, atr15 = 100.0, 1.0
    bar = _c(0, 99.9, 100.5, 99.85, 100.3, tf=M15)
    got, reason = retest([bar], "LONG", level, atr15, SPEC)
    assert got is bar and reason is None


def test_a_valid_short_retest_is_accepted():
    level, atr15 = 100.0, 1.0
    bar = _c(0, 100.1, 100.15, 99.5, 99.7, tf=M15)
    got, reason = retest([bar], "SHORT", level, atr15, SPEC)
    assert got is bar and reason is None


def test_the_wrong_candle_colour_is_rejected():
    bar = _c(0, 100.4, 100.5, 99.85, 100.1, tf=M15)   # touches, closes above, but bearish
    assert retest([bar], "LONG", 100.0, 1.0, SPEC)[0] is None


def test_a_failure_to_reclaim_is_rejected():
    bar = _c(0, 99.5, 99.9, 99.2, 99.8, tf=M15)       # never closes back above the level
    assert retest([bar], "LONG", 100.0, 1.0, SPEC)[0] is None


def test_the_setup_expires_after_four_candles():
    bars = [_c(i, 99.5, 99.9, 99.2, 99.8, tf=M15) for i in range(4)]
    got, reason = retest(bars, "LONG", 100.0, 1.0, SPEC)
    assert got is None and reason == "RETEST_EXPIRED"


def test_before_four_candles_the_setup_is_merely_waiting():
    bars = [_c(0, 99.5, 99.9, 99.2, 99.8, tf=M15)]
    assert retest(bars, "LONG", 100.0, 1.0, SPEC)[1] == "AWAITING_RETEST"


# --- order book: confirmation only -------------------------------------------


def test_agreeing_book_confirms():
    assert book_confirms(BookSnapshot(0.3, 0.5, 1), "LONG") == (True, None)
    assert book_confirms(BookSnapshot(-0.3, -0.5, 1), "SHORT") == (True, None)


def test_a_disagreeing_book_blocks():
    ok, reason = book_confirms(BookSnapshot(-0.3, 0.5, 1), "LONG")
    assert not ok and reason == "BOOK_DISAGREES"


def test_stale_or_absent_book_fails_closed():
    assert book_confirms(None, "LONG")[1] == "BOOK_STALE_OR_UNAVAILABLE"
    assert book_confirms(BookSnapshot(0.3, 0.5, 1, stale=True), "LONG")[1] == "BOOK_STALE_OR_UNAVAILABLE"


def test_the_book_can_never_create_a_trade_on_its_own():
    """book_confirms returns a veto, never a side."""
    import inspect
    src = inspect.getsource(book_confirms)
    assert "LONG" in src and "return" in src
    assert book_confirms(BookSnapshot(0.9, 9.9, 1), "SHORT")[0] is False


# --- structural stop ----------------------------------------------------------


def test_the_long_stop_sits_below_the_sequence_low_with_a_buffer():
    rb = _c(0, 100.0, 100.5, 99.5, 100.2, tf=M15)
    since = [_c(1, 100.0, 100.6, 99.2, 100.1, tf=M15)]
    stop = structural_stop("LONG", rb, since, atr15=1.0, spec=SPEC)
    assert stop == pytest.approx(99.2 - 0.15)


def test_the_short_stop_mirrors():
    rb = _c(0, 100.0, 100.5, 99.5, 99.8, tf=M15)
    since = [_c(1, 100.0, 100.9, 99.4, 99.9, tf=M15)]
    stop = structural_stop("SHORT", rb, since, atr15=1.0, spec=SPEC)
    assert stop == pytest.approx(100.9 + 0.15)


# --- economic gate ------------------------------------------------------------


def test_too_small_a_stop_is_rejected():
    ok, reason, _ = economic_gate(risk_bps=5.0, move_bps=500.0, friction_bps=9.0, spec=SPEC)
    assert not ok and reason == "RISK_TOO_SMALL"


def test_too_large_a_stop_is_rejected():
    ok, reason, _ = economic_gate(risk_bps=70.0, move_bps=500.0, friction_bps=9.0, spec=SPEC)
    assert not ok and reason == "RISK_TOO_LARGE"


def test_insufficient_move_relative_to_risk_blocks():
    ok, reason, _ = economic_gate(risk_bps=20.0, move_bps=39.0, friction_bps=9.0, spec=SPEC)
    assert not ok and reason == "MOVE_BELOW_RISK_MULTIPLE"


def test_insufficient_move_relative_to_friction_blocks():
    ok, reason, _ = economic_gate(risk_bps=10.0, move_bps=35.0, friction_bps=9.0, spec=SPEC)
    assert not ok and reason == "MOVE_BELOW_FRICTION_MULTIPLE"


def test_a_qualifying_trade_passes_both_limbs():
    ok, reason, detail = economic_gate(risk_bps=20.0, move_bps=60.0, friction_bps=9.0, spec=SPEC)
    assert ok and reason is None
    assert detail["move_to_risk"] >= 2.0 and detail["move_to_friction"] >= 4.0


# --- execution ----------------------------------------------------------------


def _order(**kw):
    base = dict(symbol="BTCUSDT", side="LONG", desired_price=100.0, quantity=1.0,
                created_ms=0, spec=SPEC, atr15=1.0)
    base.update(kw)
    return MakerOrder(**base)


def test_entries_must_be_post_only():
    with pytest.raises(ExpansionLifecycleError, match="post-only"):
        _order(post_only=False)


def test_an_order_that_would_cross_is_identified():
    o = _order(desired_price=101.0)
    assert o.would_cross(best_bid=100.5, best_ask=100.9) is True
    assert _order(desired_price=100.0).would_cross(best_bid=100.5, best_ask=100.9) is False


def test_the_ttl_expires_the_order():
    o = _order()
    assert o.expired(SPEC.order_ttl_seconds * 1000 - 1) is False
    assert o.expired(SPEC.order_ttl_seconds * 1000) is True


def test_exactly_one_reprice_is_permitted():
    o = _order()
    assert o.reprice(new_price=100.05, now_ms=1000, setup_still_valid=True) is True
    assert o.reprices == 1
    assert o.reprice(new_price=100.06, now_ms=2000, setup_still_valid=True) is False
    assert o.status == "CANCELLED" and o.cancel_reason == "MAX_REPRICES"


def test_a_reprice_beyond_tolerance_cancels_instead():
    o = _order()
    assert o.reprice(new_price=100.5, now_ms=1000, setup_still_valid=True) is False
    assert o.cancel_reason == "REPRICE_BEYOND_TOLERANCE"


def test_an_invalidated_setup_cancels_rather_than_repricing():
    o = _order()
    assert o.reprice(new_price=100.05, now_ms=1000, setup_still_valid=False) is False
    assert o.cancel_reason == "SETUP_INVALIDATED"


def test_the_second_ttl_is_shorter():
    o = _order()
    o.reprice(new_price=100.05, now_ms=1000, setup_still_valid=True)
    assert o.ttl_seconds == SPEC.reprice_ttl_seconds


def test_there_is_no_taker_fallback_anywhere():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "strategies/expansionflow_lifecycle.py").read_text()
    body = src.split('"""', 2)[-1]
    for banned in ("taker_fallback", "market_order", "cross_the_spread", "force_fill"):
        assert banned not in body.lower()


# --- lifecycle ----------------------------------------------------------------


def _pos(side="LONG", fill=100.0, stop=98.0, fill_ts=1_000_000):
    return ExpansionPosition(symbol="BTCUSDT", side=side, setup_ts=fill_ts - M15,
                             fill_ts=fill_ts, fill_price=fill, quantity=1.0,
                             initial_stop=stop, spec=SPEC)


def test_pre_fill_data_can_never_touch_the_lifecycle():
    p = _pos(fill_ts=2_000_000)
    with pytest.raises(ExpansionLifecycleError, match="PB-2"):
        p.observe(high=9_999.0, low=9_999.0, timestamp_ms=1_999_999)
    with pytest.raises(ExpansionLifecycleError, match="PB-2"):
        p.observe(high=1.0, low=0.5, timestamp_ms=2_000_000)
    assert p.closed is False and p.mfe_bps == 0.0 and p.mae_bps == 0.0


def test_mfe_and_mae_are_measured_from_the_fill():
    p = _pos(fill_ts=1)
    p.observe(high=101.0, low=99.5, timestamp_ms=2)
    assert p.mfe_bps == pytest.approx(100.0)
    assert p.mae_bps == pytest.approx(-50.0)


def test_the_target_is_two_r_from_the_actual_fill():
    p = _pos(fill=100.5, stop=98.0)
    assert p.target == pytest.approx(100.5 + 2.0 * 2.5)


def test_the_target_closes_the_position():
    p = _pos()
    assert p.observe(high=104.0, low=100.0, timestamp_ms=1_000_001) == "TARGET_2R"
    assert p.realized_r() == pytest.approx(2.0)


def test_the_stop_closes_the_position():
    p = _pos()
    assert p.observe(high=100.5, low=97.0, timestamp_ms=1_000_001) == "SL"
    assert p.realized_r() == pytest.approx(-1.0)


def test_a_bar_spanning_both_resolves_as_the_stop():
    assert _pos().observe(high=105.0, low=97.0, timestamp_ms=1_000_001) == "SL"


def test_the_forty_eight_hour_fail_safe_closes_the_position():
    p = _pos(fill_ts=1)
    assert p.check_max_hold(1 + 47 * H, mark_price=101.0) is None
    assert p.check_max_hold(1 + 48 * H, mark_price=101.0) == "MAX_HOLD_48H"


def test_no_partial_break_even_or_trailing_exists_in_v1():
    from pathlib import Path
    repo = Path(__file__).resolve().parents[1]
    for module in ("strategies/expansionflow_v1.py", "strategies/expansionflow_lifecycle.py"):
        body = (repo / module).read_text().split('"""', 2)[-1]
        for banned in ("tp1", "partial_close", "break_even", "breakeven", "trailing_stop"):
            assert banned not in body.lower(), f"{banned} in {module}"


def test_a_restored_position_still_refuses_pre_fill_observations():
    p = _pos(fill_ts=5_000_000)
    p.position_lifecycle_id = "pos-restored"
    with pytest.raises(ExpansionLifecycleError, match="PB-2"):
        p.observe(high=9_999.0, low=9_999.0, timestamp_ms=4_999_999)


def test_a_position_without_a_valid_stop_cannot_exist():
    with pytest.raises(ExpansionLifecycleError):
        _pos(stop=101.0)
    with pytest.raises(ExpansionLifecycleError):
        _pos(side="SHORT", stop=99.0)


# --- risk ---------------------------------------------------------------------


def _size(**kw):
    base = dict(entry=100.0, stop=98.0, equity_usdt=10_000.0, spec=SPEC,
                min_qty=0.001, qty_step=0.001, min_notional=5.0)
    base.update(kw)
    return size_position(**base)


def test_risk_is_a_quarter_percent_of_equity():
    r = _size()
    assert r.refused_reason is None and r.risk_usdt <= 10_000.0 * 0.0025 * 1.0001


def test_leverage_cannot_raise_account_risk():
    """Risk is derived from the stop distance alone; no leverage term exists in sizing."""
    import inspect
    assert "leverage" not in inspect.getsource(size_position).lower()


def test_rounding_can_only_reduce_risk():
    r = _size(qty_step=1.0)
    if r.refused_reason is None:
        assert r.risk_usdt <= 10_000.0 * 0.0025 * 1.0001


def test_exchange_minimums_refuse_rather_than_widen_risk():
    assert _size(min_qty=1e9).refused_reason == "BELOW_EXCHANGE_MINIMUM_QTY"
    assert _size(min_notional=1e9).refused_reason == "BELOW_EXCHANGE_MIN_NOTIONAL"


def test_portfolio_risk_cap_limits_and_then_refuses():
    assert _size(committed_risk_pct=0.40).risk_usdt <= 10_000.0 * 0.0010 * 1.0001
    assert _size(committed_risk_pct=0.50).refused_reason == "PORTFOLIO_RISK_EXHAUSTED"


def test_at_most_two_concurrent_positions():
    p = ExpansionPortfolio(spec=SPEC, day_start_equity=10_000.0,
                           open_symbols=("BTCUSDT", "ETHUSDT"))
    assert p.entry_blocked("SOLUSDT") == "MAX_CONCURRENT_POSITIONS"


def test_one_position_per_symbol():
    p = ExpansionPortfolio(spec=SPEC, day_start_equity=10_000.0, open_symbols=("BTCUSDT",))
    assert p.entry_blocked("BTCUSDT") == "POSITION_ALREADY_OPEN"


def test_the_daily_breaker_blocks_new_entries():
    p = ExpansionPortfolio(spec=SPEC, day_start_equity=10_000.0)
    p.record_close(-99.0)
    assert p.entry_blocked("BTCUSDT") is None
    p.record_close(-2.0)
    assert p.entry_blocked("BTCUSDT") == "DAILY_LOSS_LIMIT"


def test_the_breaker_gates_entries_only():
    import inspect
    src = inspect.getsource(ExpansionPortfolio)
    assert "entry_blocked" in src
    for w in ("close_position", "cancel_stop", "block_exit"):
        assert w not in src


# --- identity -----------------------------------------------------------------


def test_identity_is_new_and_isolated():
    assert STRATEGY_ID == "expansionflow_v1" and SPEC_ID == "expansionflow-v1-20260817"
    import ast
    from pathlib import Path
    repo = Path(__file__).resolve().parents[1]
    for module in ("strategies/expansionflow_v1.py", "strategies/expansionflow_lifecycle.py"):
        for node in ast.walk(ast.parse((repo / module).read_text())):
            name = None
            if isinstance(node, ast.Import):
                name = ",".join(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                name = node.module or ""
            if name:
                low = name.lower()
                for other in ("microflow", "trendflow", "simple_trend"):
                    assert other not in low, f"{module} imports {name}"
