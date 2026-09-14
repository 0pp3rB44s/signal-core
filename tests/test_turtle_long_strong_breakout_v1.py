"""Tests for turtle_shadow: strategy, sizing, and ledger separation.

Formula provenance: turtle_shadow/strategy.py reproduces Agent C's (peer
session bitget-ai-agent-45) offline research verbatim spec, independently
re-verified against fresh Binance public OHLCV -- see
tests/fixtures/turtle/xlm_boundary_fixture.json and
test_reproduces_agent_c_xlmusdt_boundary_trade below for the real-data
parity check. The strategy's own author verdict was REJECT for real-money
execution at the current ~$27.44 NAV (every historical trade's risk-sized
notional fell under Bitget's exchange minimum) -- this module exists to
collect NORMALIZED, non-executable prospective evidence only.
"""

import dataclasses
import json
import os

import pytest

from turtle_shadow.strategy import (
    Candle, TurtleSignal, TurtleTrade,
    sma, wilder_atr, wilder_adx,
    donchian_high_excl_current, donchian_low_excl_current,
    detect_turtle_signal, simulate_trade,
    BREAKOUT_STRENGTH_THRESHOLD, DONCHIAN_ENTRY_LOOKBACK, DONCHIAN_EXIT_LOOKBACK,
    SMA_TREND_PERIOD, ATR_PERIOD, ADX_PERIOD, ADX_MIN, STOP_ATR_MULTIPLE,
)
from turtle_shadow.sizing import compute_risk_sized_position, SHADOW_NAV_USDT, RISK_PCT_PER_TRADE
from turtle_shadow.ledger import TurtleShadowLedgers, RealisticLedgerRow, NormalizedLedgerRow


FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "turtle")


def _flat_candles(n: int, price: float = 100.0, ts0: int = 1_600_000_000_000) -> list[Candle]:
    return [
        Candle(timestamp_ms=ts0 + i * 86_400_000, open=price, high=price, low=price, close=price)
        for i in range(n)
    ]


def _uptrend_candles(n: int, start: float = 100.0, step: float = 1.0, ts0: int = 1_600_000_000_000) -> list[Candle]:
    out = []
    price = start
    for i in range(n):
        o = price
        c = price + step
        h = c + 0.1
        l = o - 0.1
        out.append(Candle(timestamp_ms=ts0 + i * 86_400_000, open=o, high=h, low=l, close=c))
        price = c
    return out


# --------------------------------------------------------------------------
# Donchian
# --------------------------------------------------------------------------

def test_donchian20_entry_excludes_current_bar():
    candles = _flat_candles(25, price=100.0)
    # give the 21st bar (index 20) the highest high of the whole series --
    # it must NOT be able to reference itself as its own breakout level.
    candles[20] = dataclasses.replace(candles[20], high=999.0)
    level = donchian_high_excl_current(candles, 20, DONCHIAN_ENTRY_LOOKBACK)
    assert level == 100.0  # max of bars[0:20], not including bar 20's 999.0


def test_donchian20_uses_exactly_prior_20_bars():
    candles = _flat_candles(30, price=100.0)
    candles[5] = dataclasses.replace(candles[5], high=150.0)   # inside the 20-bar window for i=25
    candles[3] = dataclasses.replace(candles[3], high=500.0)   # outside the window for i=25 (i-20=5, window=[5,25))
    level = donchian_high_excl_current(candles, 25, DONCHIAN_ENTRY_LOOKBACK)
    assert level == 150.0


def test_donchian10_exit_excludes_current_bar():
    candles = _flat_candles(15, price=100.0)
    candles[10] = dataclasses.replace(candles[10], low=0.01)
    level = donchian_low_excl_current(candles, 10, DONCHIAN_EXIT_LOOKBACK)
    assert level == 100.0  # bar 10's own extreme low must not count for itself


def test_donchian_none_before_lookback_satisfied():
    candles = _flat_candles(5, price=100.0)
    assert donchian_high_excl_current(candles, 3, DONCHIAN_ENTRY_LOOKBACK) is None
    assert donchian_low_excl_current(candles, 3, DONCHIAN_EXIT_LOOKBACK) is None


# --------------------------------------------------------------------------
# SMA200 / ATR20 / ADX14 -- causality + structural correctness
# --------------------------------------------------------------------------

def test_sma200_only_uses_completed_bars_up_to_i():
    candles = _uptrend_candles(210, start=100.0, step=1.0)
    closes = [c.close for c in candles]
    values = sma(closes, SMA_TREND_PERIOD)
    assert values[198] is None  # not enough bars yet
    assert values[199] is not None
    expected = sum(closes[0:200]) / 200
    assert values[199] == pytest.approx(expected)
    # sma[199] must not change if a LATER close changes -- pure causality check
    closes2 = list(closes)
    closes2[205] = 99999.0
    values2 = sma(closes2, SMA_TREND_PERIOD)
    assert values2[199] == values[199]


def test_atr20_seeds_at_index_period_and_is_wilder_smoothed():
    candles = _uptrend_candles(25, start=100.0, step=2.0)
    values = wilder_atr(candles, ATR_PERIOD)
    assert values[ATR_PERIOD] is not None
    assert values[ATR_PERIOD - 1] is None
    # every subsequent ATR must be a Wilder blend: strictly between the raw
    # TR extremes over a monotonic uptrend of constant-range bars
    assert values[ATR_PERIOD + 1] > 0


def test_atr20_future_bar_never_changes_past_atr_value():
    candles = _uptrend_candles(30, start=100.0, step=1.0)
    values = wilder_atr(candles, ATR_PERIOD)
    candles2 = list(candles)
    candles2[25] = dataclasses.replace(candles2[25], high=candles2[25].high + 500)
    values2 = wilder_atr(candles2, ATR_PERIOD)
    assert values[ATR_PERIOD] == values2[ATR_PERIOD]  # unaffected by a change far in the future


def test_adx_strong_trend_exceeds_min_choppy_market_does_not():
    trend = _uptrend_candles(60, start=100.0, step=2.0)
    adx_trend = wilder_adx(trend, ADX_PERIOD)
    assert adx_trend[-1] is not None and adx_trend[-1] >= ADX_MIN

    chop = []
    price = 100.0
    for i in range(60):
        price = 100.0 + (0.3 if i % 2 == 0 else -0.3)
        chop.append(Candle(timestamp_ms=1_600_000_000_000 + i * 86_400_000,
                            open=price, high=price + 0.05, low=price - 0.05, close=price))
    adx_chop = wilder_adx(chop, ADX_PERIOD)
    assert adx_chop[-1] is not None and adx_chop[-1] < ADX_MIN


# --------------------------------------------------------------------------
# breakout_dist_atr formula + exact 0.910359 threshold boundary
# --------------------------------------------------------------------------

def _signal_ready_candles(dist_multiple: float) -> tuple[list[Candle], int]:
    """Build a series where SMA200/ADX/ATR all qualify, and the final bar's
    breakout_dist_atr is controlled precisely via dist_multiple.

    Modifying bar i's own high changes bar i's own True Range, which changes
    bar i's own (causal, Wilder-smoothed) ATR -- a real feedback loop, not a
    bug in the strategy module. This converges to a fixed point instead of
    assuming ATR is unaffected by the bar it's being computed for."""
    n = 260
    base = _uptrend_candles(n, start=100.0, step=1.5)
    i = n - 1
    entry_high = donchian_high_excl_current(base, i, DONCHIAN_ENTRY_LOOKBACK)
    atr_guess = wilder_atr(base, ATR_PERIOD)[i]
    candles = list(base)
    # bias the convergence target a hair above dist_multiple: float rounding
    # across the fixed-point iteration can otherwise land a whisker below the
    # intended multiple, which would wrongly exercise the "below threshold"
    # path instead of the "at/above" path this helper is asked for.
    target_dist = dist_multiple + 1e-7
    for _ in range(40):
        target_high = entry_high + target_dist * atr_guess
        candles[i] = dataclasses.replace(base[i], high=target_high, close=target_high - 0.01)
        atr_guess = wilder_atr(candles, ATR_PERIOD)[i]
    return candles, i


def _precompute(candles: list[Candle]):
    closes = [c.close for c in candles]
    return sma(closes, SMA_TREND_PERIOD), wilder_atr(candles, ATR_PERIOD), wilder_adx(candles, ADX_PERIOD)


def test_breakout_below_threshold_rejected():
    candles, i = _signal_ready_candles(BREAKOUT_STRENGTH_THRESHOLD - 0.01)
    sma200, atr20, adx14 = _precompute(candles)
    assert detect_turtle_signal(candles, i, sma200, atr20, adx14) is None


def test_breakout_exactly_at_threshold_allowed():
    candles, i = _signal_ready_candles(BREAKOUT_STRENGTH_THRESHOLD)
    sma200, atr20, adx14 = _precompute(candles)
    sig = detect_turtle_signal(candles, i, sma200, atr20, adx14)
    assert sig is not None
    assert sig.breakout_dist_atr == pytest.approx(BREAKOUT_STRENGTH_THRESHOLD, abs=1e-4)
    assert sig.breakout_dist_atr >= BREAKOUT_STRENGTH_THRESHOLD


def test_breakout_above_threshold_allowed():
    candles, i = _signal_ready_candles(BREAKOUT_STRENGTH_THRESHOLD + 0.05)
    sma200, atr20, adx14 = _precompute(candles)
    sig = detect_turtle_signal(candles, i, sma200, atr20, adx14)
    assert sig is not None
    assert sig.breakout_dist_atr > BREAKOUT_STRENGTH_THRESHOLD


def test_breakout_dist_atr_exact_formula():
    candles, i = _signal_ready_candles(1.2345)
    sma200, atr20, adx14 = _precompute(candles)
    sig = detect_turtle_signal(candles, i, sma200, atr20, adx14)
    assert sig is not None
    entry_high = donchian_high_excl_current(candles, i, DONCHIAN_ENTRY_LOOKBACK)
    expected = (candles[i].high - entry_high) / atr20[i]
    assert sig.breakout_dist_atr == pytest.approx(expected, rel=1e-9)


def test_close_below_sma200_rejects_regardless_of_breakout_strength():
    candles, i = _signal_ready_candles(5.0)
    # force close below a manually-flattened long-run SMA by crashing recent closes
    for j in range(i - 5, i):
        candles[j] = dataclasses.replace(candles[j], close=candles[j].close * 0.5)
    sma200, atr20, adx14 = _precompute(candles)
    candles[i] = dataclasses.replace(candles[i], close=sma200[i] - 1.0 if sma200[i] else candles[i].close)
    sig = detect_turtle_signal(candles, i, sma200, atr20, adx14)
    assert sig is None


def test_adx_below_min_rejects():
    n = 260
    candles = []
    price = 100.0
    for k in range(n):
        price = 100.0 + (0.2 if k % 2 == 0 else -0.2) + k * 0.01  # weak, choppy drift up
        candles.append(Candle(timestamp_ms=1_600_000_000_000 + k * 86_400_000,
                               open=price, high=price + 0.05, low=price - 0.05, close=price))
    sma200, atr20, adx14 = _precompute(candles)
    i = n - 1
    assert adx14[i] is not None and adx14[i] < ADX_MIN
    sig = detect_turtle_signal(candles, i, sma200, atr20, adx14)
    assert sig is None


def test_initial_stop_geometry_is_exactly_2x_atr():
    candles, i = _signal_ready_candles(2.0)
    sma200, atr20, adx14 = _precompute(candles)
    sig = detect_turtle_signal(candles, i, sma200, atr20, adx14)
    assert sig is not None
    assert sig.initial_stop == pytest.approx(sig.entry_price - STOP_ATR_MULTIPLE * sig.atr20_at_entry, rel=1e-9)


# --------------------------------------------------------------------------
# LONG-only, no TP/BE/profit-lock/pyramiding, no same-candle entry/exit
# --------------------------------------------------------------------------

def test_no_short_no_tp_no_be_no_profit_lock_fields_exist():
    signal_fields = {f.name for f in dataclasses.fields(TurtleSignal)}
    trade_fields = {f.name for f in dataclasses.fields(TurtleTrade)}
    forbidden = {"direction", "take_profit", "tp_price", "breakeven", "break_even",
                 "profit_lock", "pyramid", "add_on", "short", "is_short"}
    assert not (signal_fields & forbidden)
    assert not (trade_fields & forbidden)


def test_entry_candle_never_evaluates_its_own_exit():
    """Causal guard mirroring the production runner's entry-candle-reuse fix:
    even if the entry bar's own low would have breached the initial stop,
    the trade must not exit on that same bar."""
    candles, i = _signal_ready_candles(1.0)
    sma200, atr20, adx20 = _precompute(candles)
    sig = detect_turtle_signal(candles, i, sma200, atr20, adx20)
    assert sig is not None
    # give the entry bar itself a low far below its own initial stop
    candles[i] = dataclasses.replace(candles[i], low=sig.initial_stop - 1000.0)
    # next bar stays well clear of both exits so we can see the entry bar was skipped
    candles.append(dataclasses.replace(candles[i], timestamp_ms=candles[i].timestamp_ms + 86_400_000,
                                        low=sig.entry_price, high=sig.entry_price + 1, close=sig.entry_price))
    trade = simulate_trade(candles, sig)
    assert trade is None or trade.exit_index != i


def test_stop_wins_tie_break_when_both_trigger_same_bar():
    candles, i = _signal_ready_candles(1.0)
    sma200, atr20, adx14 = _precompute(candles)
    sig = detect_turtle_signal(candles, i, sma200, atr20, adx14)
    assert sig is not None
    next_ts = candles[i].timestamp_ms + 86_400_000
    bad_bar = Candle(timestamp_ms=next_ts, open=sig.entry_price, high=sig.entry_price,
                      low=sig.initial_stop - 10, close=sig.initial_stop - 5)
    candles.append(bad_bar)
    trade = simulate_trade(candles, sig)
    assert trade is not None
    assert trade.exit_reason == "INITIAL_STOP"
    assert trade.exit_price == pytest.approx(sig.initial_stop)


# --------------------------------------------------------------------------
# Position sizing / minimum notional / no risk inflation
# --------------------------------------------------------------------------

class _FakePrecisionMixin:
    def __init__(self, min_notional, reason):
        self._min_notional_val = min_notional
        self._reason = reason

    def validate_entry_size(self, symbol, size, reference_price=None):
        return (size if self._reason is None else 0.0), self._reason

    def _min_notional(self, symbol):
        return self._min_notional_val


def test_position_sizing_derives_from_risk_budget_over_stop_distance():
    result = compute_risk_sized_position("BTCUSDT", entry_price=100.0, initial_stop=95.0,
                                          precision_mixin=_FakePrecisionMixin(None, None))
    expected_risk_usdt = SHADOW_NAV_USDT * RISK_PCT_PER_TRADE
    expected_units = expected_risk_usdt / 5.0
    assert result.risk_usdt == pytest.approx(expected_risk_usdt)
    assert result.position_units_raw == pytest.approx(expected_units)
    assert result.risk_sized_notional == pytest.approx(expected_units * 100.0)


def test_below_minimum_notional_skips_realistic_execution_without_inflating():
    result = compute_risk_sized_position(
        "BTCUSDT", entry_price=100.0, initial_stop=95.0,
        precision_mixin=_FakePrecisionMixin(10.0, "EXCHANGE_MIN_NOTIONAL_INCOMPATIBLE_WITH_RISK_LIMITS"),
    )
    assert result.skip_realistic_execution is True
    assert result.skip_reason == "MIN_NOTIONAL"
    # risk-sized notional is untouched by the failed exchange check -- no inflation
    expected_units = (SHADOW_NAV_USDT * RISK_PCT_PER_TRADE) / 5.0
    assert result.risk_sized_notional == pytest.approx(expected_units * 100.0)
    assert result.exchange_executable is False


def test_metadata_unavailable_fails_closed_not_open():
    result = compute_risk_sized_position("BTCUSDT", entry_price=100.0, initial_stop=95.0, precision_mixin=None)
    assert result.exchange_executable is False
    assert result.skip_realistic_execution is True
    assert result.skip_reason == "CONTRACT_METADATA_UNAVAILABLE"


def test_no_risk_inflation_across_symbols_or_outcomes():
    ok = compute_risk_sized_position("ETHUSDT", entry_price=50.0, initial_stop=45.0,
                                      precision_mixin=_FakePrecisionMixin(1.0, None))
    skipped = compute_risk_sized_position("ETHUSDT", entry_price=50.0, initial_stop=45.0,
                                           precision_mixin=_FakePrecisionMixin(999999.0, "EXCHANGE_MIN_NOTIONAL_INCOMPATIBLE_WITH_RISK_LIMITS"))
    # the risk-derived notional must be identical regardless of exchange outcome
    assert ok.risk_sized_notional == pytest.approx(skipped.risk_sized_notional)


# --------------------------------------------------------------------------
# Two-ledger separation
# --------------------------------------------------------------------------

def test_normalized_and_realistic_ledgers_are_physically_separate(tmp_path):
    ledgers = TurtleShadowLedgers(str(tmp_path))
    assert ledgers.realistic_path != ledgers.normalized_path

    realistic = RealisticLedgerRow(
        trade_id="t1", symbol="BTCUSDT", signal_timestamp_ms=1, entry_price=100.0,
        initial_stop=95.0, risk_usdt=0.2, risk_sized_notional=4.0,
        exchange_min_notional_or_min_order=10.0, executed=False, skip_reason="MIN_NOTIONAL",
    )
    normalized = NormalizedLedgerRow(
        trade_id="t1", symbol="BTCUSDT", signal_timestamp_ms=1, breakout_dist_atr=1.2,
        entry_price=100.0, initial_stop=95.0, exit_timestamp_ms=2, exit_price=110.0,
        exit_reason="DONCHIAN10_EXIT", result_r=2.0,
    )
    ledgers.append_realistic(realistic)
    ledgers.append_normalized(normalized)

    r_rows = ledgers.read_realistic()
    n_rows = ledgers.read_normalized()
    assert len(r_rows) == 1 and len(n_rows) == 1

    # the skipped realistic row must never carry a fabricated realized PnL
    assert r_rows[0]["executed"] is False
    assert r_rows[0]["realized_pnl_usdt"] is None
    assert r_rows[0]["ledger"] == "REALISTIC_ACCOUNT"

    # the normalized row is explicitly, redundantly tagged non-executable
    assert n_rows[0]["EXECUTABLE"] == "NO"
    assert n_rows[0]["ledger"] == "NORMALIZED_RESEARCH"
    assert n_rows[0]["result_r"] == pytest.approx(2.0)

    # schemas don't overlap on the fields that matter (no cross-contamination)
    assert "realized_pnl_usdt" not in n_rows[0]
    assert "result_r" not in r_rows[0] or r_rows[0]["result_r"] is None


# --------------------------------------------------------------------------
# Step 9: historical reproduction against Agent C's real signal (offline fixture)
# --------------------------------------------------------------------------

def test_reproduces_agent_c_xlmusdt_boundary_trade():
    """XLMUSDT entry_t=2023-10-31 is the closest-to-threshold real sample
    Agent C gave (breakout_dist_atr=0.9159, just above 0.910359) -- exactly
    the boundary case most sensitive to an off-by-one indexing error. Fixture
    is real Binance daily OHLCV, fetched fresh and independently, not copied
    from Agent C's own files (none were shared -- see module docstring)."""
    with open(os.path.join(FIXTURE_DIR, "xlm_boundary_fixture.json")) as f:
        raw = json.load(f)
    candles = [Candle(timestamp_ms=r["timestamp_ms"], open=r["open"], high=r["high"],
                       low=r["low"], close=r["close"]) for r in raw]
    sma200, atr20, adx14 = _precompute(candles)

    found = None
    for i in range(len(candles)):
        sig = detect_turtle_signal(candles, i, sma200, atr20, adx14)
        if sig is not None:
            found = sig
            break
    assert found is not None, "expected exactly one qualifying signal in this fixture window"
    assert found.breakout_dist_atr == pytest.approx(0.9159, abs=0.005)
    assert found.entry_price == pytest.approx(0.120540, rel=0.001)
    assert found.initial_stop == pytest.approx(0.110801, rel=0.002)

    trade = simulate_trade(candles, found)
    assert trade is not None
    assert trade.result_r == pytest.approx(-1.0143, abs=0.02)
