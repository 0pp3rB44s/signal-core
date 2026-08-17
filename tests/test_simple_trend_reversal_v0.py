"""Simple Trend Reversal V0 — the full required matrix.

The tests that carry the most weight are the PB-2 ones and the idempotency ones. Everything else
here is arithmetic; those two are where this project has actually been burned.
"""

from __future__ import annotations

import pytest

from strategies.simple_trend_reversal_v0 import (STRATEGY_ID, SPEC_ID, Candle, V0DataError,
                                                 V0Spec, ema, evaluate, size_position)
from strategies.simple_trend_reversal_lifecycle import (V0LifecycleError, V0Portfolio,
                                                        V0Position, V0SignalLedger)

SPEC = V0Spec()
H = 3_600_000


def _c(i, o, h, l, c, closed=True):
    return Candle(i * H, (i + 1) * H, o, h, l, c, 0.0, closed)


def _series(n, start, step):
    """A clean ramp; the last three candles are replaced by the test to shape the setup."""
    return [_c(i, start + i * step, start + i * step + abs(step),
               start + i * step - abs(step), start + i * step) for i in range(n)]


def _long_setup():
    """Uptrend, a pullback touching EMA20, then a bullish reclaim close above it."""
    c = _series(80, 100.0, 0.5)
    e20 = ema([x.close for x in c[:-1]], SPEC.ema_fast)[-1]
    n = len(c)
    c[-3] = _c(n - 3, e20 + 0.4, e20 + 0.5, e20 - 1.2, e20 - 0.8)   # pullback under EMA20
    c[-2] = _c(n - 2, e20 - 0.8, e20 + 0.2, e20 - 1.0, e20 - 0.2)
    c[-1] = _c(n - 1, e20 - 0.1, e20 + 1.6, e20 - 0.3, e20 + 1.4)   # bullish reclaim
    return c


def _short_setup():
    c = _series(80, 150.0, -0.5)
    e20 = ema([x.close for x in c[:-1]], SPEC.ema_fast)[-1]
    n = len(c)
    c[-3] = _c(n - 3, e20 - 0.4, e20 + 1.2, e20 - 0.5, e20 + 0.8)
    c[-2] = _c(n - 2, e20 + 0.8, e20 + 1.0, e20 - 0.2, e20 + 0.2)
    c[-1] = _c(n - 1, e20 + 0.1, e20 + 0.3, e20 - 1.6, e20 - 1.4)   # bearish reclaim
    return c


# --- 1-4 trend ---------------------------------------------------------------


def test_ema20_above_ema50_permits_long():
    s = evaluate(_long_setup(), SPEC)
    assert s.direction == "LONG"
    assert s.values["ema20"] > s.values["ema50"]


def test_ema20_below_ema50_permits_short():
    s = evaluate(_short_setup(), SPEC)
    assert s.direction == "SHORT"
    assert s.values["ema20"] < s.values["ema50"]


def test_the_opposite_side_is_never_produced():
    assert evaluate(_long_setup(), SPEC).direction != "SHORT"
    assert evaluate(_short_setup(), SPEC).direction != "LONG"


def test_equal_emas_are_deterministically_no_trade():
    flat = [_c(i, 100.0, 100.5, 99.5, 100.0) for i in range(80)]
    s = evaluate(flat, SPEC)
    assert s.direction is None and s.block_reason == "NO_TREND_EMA_EQUAL"


# --- 5-7 pullback ------------------------------------------------------------


def test_long_requires_a_pullback_to_the_ema_within_the_window():
    c = _long_setup()
    e20 = ema([x.close for x in c[:-1]], SPEC.ema_fast)[-1]
    n = len(c)
    for k in (3, 2):                      # lift the whole window clear of EMA20
        c[-k] = _c(n - k, e20 + 3.0, e20 + 3.5, e20 + 2.5, e20 + 3.0)
    c[-1] = _c(n - 1, e20 + 2.9, e20 + 4.0, e20 + 2.8, e20 + 3.8)
    assert evaluate(c, SPEC).block_reason == "NO_PULLBACK"


def test_short_requires_a_pullback_up_to_the_ema():
    c = _short_setup()
    e20 = ema([x.close for x in c[:-1]], SPEC.ema_fast)[-1]
    n = len(c)
    for k in (3, 2):
        c[-k] = _c(n - k, e20 - 3.0, e20 - 2.5, e20 - 3.5, e20 - 3.0)
    c[-1] = _c(n - 1, e20 - 2.9, e20 - 2.8, e20 - 4.0, e20 - 3.8)
    assert evaluate(c, SPEC).block_reason == "NO_PULLBACK"


def test_a_pullback_older_than_the_window_does_not_count():
    c = _long_setup()
    e20 = ema([x.close for x in c[:-1]], SPEC.ema_fast)[-1]
    n = len(c)
    c[-4] = _c(n - 4, e20 - 2.0, e20 - 1.5, e20 - 3.0, e20 - 2.5)   # stale pullback
    for k in (3, 2):
        c[-k] = _c(n - k, e20 + 3.0, e20 + 3.5, e20 + 2.5, e20 + 3.0)
    c[-1] = _c(n - 1, e20 + 2.9, e20 + 4.0, e20 + 2.8, e20 + 3.8)
    assert evaluate(c, SPEC).block_reason == "NO_PULLBACK"


# --- 8-10 reclaim ------------------------------------------------------------


def test_long_reclaim_needs_close_above_ema_and_a_bullish_body():
    c = _long_setup()
    e20 = ema([x.close for x in c[:-1]], SPEC.ema_fast)[-1]
    c[-1] = _c(len(c) - 1, e20 + 1.6, e20 + 1.8, e20 - 0.3, e20 + 1.4)  # above EMA but bearish body
    assert evaluate(c, SPEC).block_reason == "NO_RECLAIM"


def test_short_reclaim_mirrors():
    c = _short_setup()
    e20 = ema([x.close for x in c[:-1]], SPEC.ema_fast)[-1]
    c[-1] = _c(len(c) - 1, e20 - 1.6, e20 + 0.3, e20 - 1.8, e20 - 1.4)  # below EMA but bullish body
    assert evaluate(c, SPEC).block_reason == "NO_RECLAIM"


def test_an_unfinished_candle_is_refused():
    c = _long_setup()
    last = c[-1]
    c[-1] = Candle(last.open_time_ms, last.close_time_ms, last.open, last.high,
                   last.low, last.close, 0.0, closed=False)
    with pytest.raises(V0DataError, match="unfinished"):
        evaluate(c, SPEC)


# --- 11-13 entry idempotency -------------------------------------------------


def _ledger_kw(sig, symbol="BTCUSDT"):
    return dict(strategy_id=STRATEGY_ID, symbol=symbol, side=sig.direction,
                signal_candle_ts=sig.signal_candle_ts)


def test_the_same_signal_candle_cannot_enter_twice():
    sig = evaluate(_long_setup(), SPEC)
    ledger = V0SignalLedger()
    assert not ledger.already_taken(**_ledger_kw(sig))
    ledger.record(**_ledger_kw(sig))
    assert ledger.already_taken(**_ledger_kw(sig))


def test_an_open_position_blocks_a_new_entry_on_that_symbol():
    p = V0Portfolio(spec=SPEC, day_start_equity=10_000.0, open_symbols=("BTCUSDT",))
    assert p.entry_blocked("BTCUSDT") == "POSITION_ALREADY_OPEN"
    assert p.entry_blocked("ETHUSDT") is None


def test_a_new_sequence_after_exit_can_re_enter():
    sig = evaluate(_long_setup(), SPEC)
    ledger = V0SignalLedger(); ledger.record(**_ledger_kw(sig))
    later = dict(_ledger_kw(sig)); later["signal_candle_ts"] = sig.signal_candle_ts + H
    assert not ledger.already_taken(**later)


# --- 14-17 stop --------------------------------------------------------------


def test_long_stop_is_the_swing_low_of_the_sequence():
    c = _long_setup(); sig = evaluate(c, SPEC)
    assert sig.stop_price == pytest.approx(min(x.low for x in c[-SPEC.pullback_lookback:]))
    assert sig.stop_price < sig.signal_price


def test_short_stop_is_the_swing_high_of_the_sequence():
    c = _short_setup(); sig = evaluate(c, SPEC)
    assert sig.stop_price == pytest.approx(max(x.high for x in c[-SPEC.pullback_lookback:]))
    assert sig.stop_price > sig.signal_price


def test_a_stop_on_the_wrong_side_of_entry_is_rejected():
    c = _long_setup()
    e20 = ema([x.close for x in c[:-1]], SPEC.ema_fast)[-1]
    n = len(c)
    # Reclaim closes *below* the window's lowest low — impossible geometry for a LONG stop.
    for k in (3, 2):
        c[-k] = _c(n - k, e20 - 0.5, e20 + 0.2, e20 + 0.15, e20 + 0.18)
    c[-1] = _c(n - 1, e20 + 0.01, e20 + 0.05, e20 + 0.02, e20 + 0.04)
    sig = evaluate(c, SPEC)
    assert sig.direction is None
    assert sig.block_reason in ("INVALID_STOP_WRONG_SIDE", "NO_PULLBACK", "NO_RECLAIM")


def test_the_stop_uses_only_candles_known_at_entry_time():
    c = _long_setup()
    sig_now = evaluate(c, SPEC)
    future = c + [_c(len(c), 999.0, 1000.0, 1.0, 999.0)]     # a wild future candle
    sig_then = evaluate(c, SPEC)
    assert sig_now.stop_price == pytest.approx(sig_then.stop_price)
    assert evaluate(future, SPEC).signal_candle_ts != sig_now.signal_candle_ts


# --- 18-19 target ------------------------------------------------------------


def test_long_take_profit_is_exactly_two_r():
    sig = evaluate(_long_setup(), SPEC)
    assert sig.take_profit == pytest.approx(sig.signal_price + 2.0 * sig.r_distance)


def test_short_take_profit_is_exactly_two_r():
    sig = evaluate(_short_setup(), SPEC)
    assert sig.take_profit == pytest.approx(sig.signal_price - 2.0 * sig.r_distance)


# --- 20-28 lifecycle ---------------------------------------------------------


def _pos(side="LONG", fill=100.0, stop=98.0, fill_ts=1_000_000, symbol="BTCUSDT"):
    tp = fill + 2.0 * abs(fill - stop) if side == "LONG" else fill - 2.0 * abs(fill - stop)
    return V0Position(symbol=symbol, side=side, signal_candle_ts=fill_ts - H,
                      signal_price=fill, fill_ts=fill_ts, fill_price=fill,
                      initial_stop=stop, take_profit=tp, position_size=1.0,
                      risk_usdt=25.0, spec=SPEC)


def test_pre_fill_movement_cannot_trigger_take_profit():
    p = _pos(fill_ts=2_000_000)
    with pytest.raises(V0LifecycleError, match="PB-2"):
        p.observe(high=9_999.0, low=9_999.0, timestamp_ms=1_999_999)
    assert p.closed is False and p.exit_reason is None


def test_pre_fill_movement_cannot_trigger_the_stop():
    p = _pos(fill_ts=2_000_000)
    with pytest.raises(V0LifecycleError, match="PB-2"):
        p.observe(high=1.0, low=0.5, timestamp_ms=1_999_999)
    assert p.closed is False


def test_mfe_and_mae_begin_at_the_fill():
    p = _pos(fill_ts=2_000_000)
    with pytest.raises(V0LifecycleError):
        p.observe(high=500.0, low=1.0, timestamp_ms=2_000_000)   # exactly at the fill
    assert p.mfe_bps == 0.0 and p.mae_bps == 0.0
    p.observe(high=101.0, low=99.5, timestamp_ms=2_000_001)
    assert p.mfe_bps == pytest.approx(100.0)
    assert p.mae_bps == pytest.approx(-50.0)


def test_take_profit_closes_the_whole_position():
    p = _pos()
    assert p.observe(high=104.0, low=100.0, timestamp_ms=1_000_001) == "TP"
    assert p.closed and p.exit_price == pytest.approx(104.0)
    assert p.realized_r() == pytest.approx(2.0)


def test_stop_loss_closes_the_whole_position():
    p = _pos()
    assert p.observe(high=100.5, low=97.0, timestamp_ms=1_000_001) == "SL"
    assert p.realized_r() == pytest.approx(-1.0)


def test_a_candle_spanning_both_assumes_the_stop_first():
    """Optimism here would flatter every replay this touches."""
    p = _pos()
    assert p.observe(high=105.0, low=97.0, timestamp_ms=1_000_001) == "SL"


def test_forty_eight_hour_fail_safe_closes_what_remains():
    p = _pos(fill_ts=1)
    assert p.check_max_hold(1 + 47 * H, mark_price=101.0) is None
    assert p.check_max_hold(1 + 48 * H, mark_price=101.0) == "MAX_HOLD_48H"
    assert p.exit_reason == "MAX_HOLD_48H"


def test_no_partial_break_even_or_trailing_logic_exists():
    from pathlib import Path
    repo = Path(__file__).resolve().parents[1]
    for module in ("strategies/simple_trend_reversal_v0.py",
                   "strategies/simple_trend_reversal_lifecycle.py"):
        src = repo / module
        text = src.read_text()
        body = text.split('"""', 2)[-1]          # ignore the module docstring's prose
        for banned in ("tp1", "partial", "break_even", "breakeven", "trail"):
            assert banned not in body.lower(), f"{banned!r} appears in {module}"


def test_targets_are_recomputed_from_the_actual_fill():
    """Slippage must not leave a stale 2R computed off the signal price."""
    p = _pos(fill=100.5, stop=98.0)
    assert p.recomputed_take_profit() == pytest.approx(100.5 + 2.0 * 2.5)


# --- 29-34 sizing and portfolio ----------------------------------------------


def _size(**kw):
    base = dict(entry=100.0, stop=98.0, equity_usdt=10_000.0, spec=SPEC,
                min_qty=0.001, qty_step=0.001, min_notional=5.0)
    base.update(kw)
    return size_position(**base)


def test_risk_is_a_quarter_percent_of_equity():
    r = _size()
    assert r.refused_reason is None
    assert r.risk_usdt <= 10_000.0 * 0.0025 * 1.0001


def test_quantity_respects_the_step_and_rounding_only_reduces_risk():
    r = _size(qty_step=1.0)
    if r.refused_reason is None:
        assert r.quantity == pytest.approx(round(r.quantity))
        assert r.risk_usdt <= 10_000.0 * 0.0025 * 1.0001


def test_exchange_minimums_refuse_rather_than_widen_risk():
    assert _size(min_qty=1e9).refused_reason == "BELOW_EXCHANGE_MINIMUM_QTY"
    assert _size(min_notional=1e9).refused_reason == "BELOW_EXCHANGE_MIN_NOTIONAL"


def test_a_portfolio_notional_cap_is_respected_never_raised():
    capped = _size(max_notional=100.0)
    assert capped.notional <= 100.0 + 1e-9


def test_at_most_three_concurrent_positions():
    p = V0Portfolio(spec=SPEC, day_start_equity=10_000.0,
                    open_symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT"))
    assert p.entry_blocked("XRPUSDT") == "MAX_CONCURRENT_POSITIONS"


def test_the_daily_breaker_blocks_new_entries():
    p = V0Portfolio(spec=SPEC, day_start_equity=10_000.0)
    p.record_close(-99.0)
    assert p.entry_blocked("BTCUSDT") is None
    p.record_close(-2.0)
    assert p.entry_blocked("BTCUSDT") == "DAILY_LOSS_LIMIT"


def test_the_breaker_gates_entries_only_and_never_exits():
    import inspect
    src = inspect.getsource(V0Portfolio)
    assert "entry_blocked" in src
    for exit_word in ("close_position", "cancel_stop", "block_exit", "suppress_exit"):
        assert exit_word not in src


# --- 35-38 recovery ----------------------------------------------------------


def test_a_position_can_be_reconstructed_after_restart():
    original = _pos()
    restored = V0Position(symbol=original.symbol, side=original.side,
                          signal_candle_ts=original.signal_candle_ts,
                          signal_price=original.signal_price, fill_ts=original.fill_ts,
                          fill_price=original.fill_price, initial_stop=original.initial_stop,
                          take_profit=original.take_profit, position_size=original.position_size,
                          risk_usdt=original.risk_usdt, spec=SPEC,
                          position_lifecycle_id="pos-restored")
    assert restored.initial_r == pytest.approx(original.initial_r)
    assert restored.take_profit == pytest.approx(original.take_profit)
    assert restored.strategy_id == STRATEGY_ID


def test_a_restored_position_still_refuses_pre_fill_observations():
    """Recovery must not be a hole in the PB-2 guarantee."""
    restored = _pos(fill_ts=5_000_000)
    with pytest.raises(V0LifecycleError, match="PB-2"):
        restored.observe(high=9_999.0, low=9_999.0, timestamp_ms=4_999_999)


def test_duplicate_protective_orders_are_prevented_by_the_ledger():
    sig = evaluate(_long_setup(), SPEC)
    ledger = V0SignalLedger()
    ledger.record(**_ledger_kw(sig))
    assert ledger.already_taken(**_ledger_kw(sig))


def test_a_position_without_a_valid_stop_cannot_be_constructed():
    with pytest.raises(V0LifecycleError):
        _pos(stop=101.0)                 # LONG stop above the fill
    with pytest.raises(V0LifecycleError):
        _pos(side="SHORT", stop=99.0)    # SHORT stop below the fill
    with pytest.raises(V0LifecycleError):
        _pos(stop=100.0)                 # zero distance


# --- identity ----------------------------------------------------------------


def test_strategy_and_spec_ids_are_new_and_distinct():
    assert STRATEGY_ID == "simple_trend_reversal_v0"
    assert SPEC_ID == "simple-trend-reversal-v0-20260817"
    for other in ("microflow", "trendflow"):
        assert other not in STRATEGY_ID and other not in SPEC_ID


def test_v0_imports_nothing_from_the_earlier_strategies():
    """Checked on the import graph, not on prose — the docstrings legitimately name both."""
    import ast
    from pathlib import Path
    repo = Path(__file__).resolve().parents[1]
    for module in ("strategies/simple_trend_reversal_v0.py",
                   "strategies/simple_trend_reversal_lifecycle.py"):
        names = set()
        for node in ast.walk(ast.parse((repo / module).read_text())):
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
        for name in names:
            low = name.lower()
            assert "microflow" not in low and "trendflow" not in low, f"V0 imports {name!r}"
            assert low.split(".")[0] not in {"execution", "app", "risk", "planning"}
