"""adaptive_trend_tsmom_v1 -- signal, ATR, sizing, ranking correctness.

Covers the implementation-correctness proof points from spec section 13 that
belong to the pure-logic layer: 6H boundaries, no signal before close, MOM
calculation, LONG/SHORT/no-signal thresholds, ATR calculation, stop-ratchet
directionality, risk-to-stop sizing, exposure cap, exchange-minimum
rejection, and deterministic multi-signal ranking.

Execution-integration proof points (protection-lifecycle integration,
restart/reconciliation, duplicate-entry protection, the one-position rule as
enforced by the executor) are NOT covered here -- they require the execution
wiring this pass does not yet include, and are explicitly called out as
outstanding in the implementation report.
"""

from __future__ import annotations

import pytest

from strategies.adaptive_trend_tsmom import (
    ATR_MULT,
    ATR_PERIOD,
    Candle6h,
    MOM_LOOKBACK,
    MOM_THRESHOLD,
    Side,
    SignalCandidate,
    candle_6h_boundary,
    classify_signal,
    closed_candles_as_of,
    compute_atr,
    compute_momentum,
    fee_observability,
    initial_stop,
    rank_candidates,
    size_position,
    update_trailing_stop,
)

SIX_H_MS = 6 * 60 * 60 * 1000


def make_candles(closes: list[float], start_ms: int = 0, high_pad=0.0, low_pad=0.0) -> list[Candle6h]:
    out = []
    for i, c in enumerate(closes):
        open_ms = start_ms + i * SIX_H_MS
        close_ms = open_ms + SIX_H_MS
        o = closes[i - 1] if i > 0 else c
        out.append(Candle6h(open_ms=open_ms, close_ms=close_ms, open=o,
                             high=max(o, c) + high_pad, low=min(o, c) - low_pad, close=c))
    return out


# --- 6H boundaries and no-look-ahead --------------------------------------

def test_candle_6h_boundary_is_utc_aligned():
    # 2026-01-01T00:00:00Z is exactly epoch-aligned to a 6h boundary.
    epoch_midnight = 1767225600000
    assert candle_6h_boundary(epoch_midnight) == epoch_midnight
    assert candle_6h_boundary(epoch_midnight + 3600_000) == epoch_midnight
    assert candle_6h_boundary(epoch_midnight + SIX_H_MS) == epoch_midnight + SIX_H_MS


def test_closed_candles_as_of_excludes_the_forming_candle():
    candles = make_candles([100, 101, 102])
    # now_ms sits mid-way through the third candle: only the first two are closed.
    now_ms = candles[2].open_ms + 1000
    closed = closed_candles_as_of(candles, now_ms)
    assert len(closed) == 2
    assert closed[-1].close == 101


def test_signal_cannot_be_generated_before_its_candle_closes():
    """The structural guarantee: compute_momentum only ever sees closed_candles_as_of's output."""
    candles = make_candles([100.0] * (MOM_LOOKBACK + 1) + [200.0])  # huge move on the LAST candle
    now_before_close = candles[-1].open_ms + 1  # candle still forming
    closed = closed_candles_as_of(candles, now_before_close)
    mom = compute_momentum(closed, lookback=MOM_LOOKBACK)
    # the 200.0 close is not yet visible -- momentum must reflect only closed data
    assert mom == 0.0 or mom is None


# --- momentum --------------------------------------------------------------

def test_momentum_calculation_is_exact():
    closes = [100.0] * MOM_LOOKBACK + [103.0]
    candles = make_candles(closes)
    mom = compute_momentum(candles, lookback=MOM_LOOKBACK)
    assert mom == pytest.approx(0.03)


def test_momentum_none_with_insufficient_history():
    candles = make_candles([100.0] * 5)
    assert compute_momentum(candles, lookback=MOM_LOOKBACK) is None


def test_long_threshold_exact_boundary_included():
    closes = [100.0] * MOM_LOOKBACK + [103.0]  # exactly +3.0%
    mom = compute_momentum(make_candles(closes), lookback=MOM_LOOKBACK)
    assert classify_signal(mom, MOM_THRESHOLD) is Side.LONG


def test_short_threshold_exact_boundary_included():
    closes = [100.0] * MOM_LOOKBACK + [97.0]  # exactly -3.0%
    mom = compute_momentum(make_candles(closes), lookback=MOM_LOOKBACK)
    assert classify_signal(mom, MOM_THRESHOLD) is Side.SHORT


def test_no_signal_zone_is_strictly_between_thresholds():
    closes = [100.0] * MOM_LOOKBACK + [101.5]  # +1.5%, inside the dead zone
    mom = compute_momentum(make_candles(closes), lookback=MOM_LOOKBACK)
    assert classify_signal(mom, MOM_THRESHOLD) is None


def test_no_signal_when_momentum_unavailable():
    assert classify_signal(None) is None


# --- ATR ---------------------------------------------------------------

def test_atr_calculation_matches_hand_computation():
    # Flat series with a fixed high/low pad -> every true range equals 2*pad.
    candles = make_candles([100.0] * (ATR_PERIOD + 1), high_pad=1.0, low_pad=1.0)
    atr = compute_atr(candles, period=ATR_PERIOD)
    assert atr == pytest.approx(2.0)


def test_atr_none_with_insufficient_history():
    candles = make_candles([100.0] * 5)
    assert compute_atr(candles, period=ATR_PERIOD) is None


# --- trailing stop ratchet ------------------------------------------------

def test_long_stop_never_moves_downward():
    stop = 95.0
    # price falls -- candidate stop would be lower, but ratchet must hold.
    new_stop = update_trailing_stop(stop, latest_close=90.0, atr=1.0, side=Side.LONG, mult=2.5)
    assert new_stop == stop


def test_long_stop_rises_when_price_advances():
    stop = 95.0
    new_stop = update_trailing_stop(stop, latest_close=110.0, atr=1.0, side=Side.LONG, mult=2.5)
    assert new_stop == pytest.approx(110.0 - 2.5 * 1.0)
    assert new_stop > stop


def test_short_stop_never_moves_upward():
    stop = 105.0
    new_stop = update_trailing_stop(stop, latest_close=110.0, atr=1.0, side=Side.SHORT, mult=2.5)
    assert new_stop == stop


def test_short_stop_falls_when_price_declines():
    stop = 105.0
    new_stop = update_trailing_stop(stop, latest_close=90.0, atr=1.0, side=Side.SHORT, mult=2.5)
    assert new_stop == pytest.approx(90.0 + 2.5 * 1.0)
    assert new_stop < stop


def test_initial_stop_long_and_short():
    assert initial_stop(100.0, atr=2.0, side=Side.LONG, mult=2.5) == pytest.approx(95.0)
    assert initial_stop(100.0, atr=2.0, side=Side.SHORT, mult=2.5) == pytest.approx(105.0)


# --- sizing ------------------------------------------------------------

def test_risk_to_stop_sizing_matches_hand_computation():
    r = size_position(equity=1000.0, entry_price=100.0, stop_price=98.0,
                       exchange_min_notional=5.0, risk_pct=0.005)
    # risk_usdt = 5.0, stop_distance_pct = 0.02 -> raw_notional = 250.0
    assert r.risk_usdt == pytest.approx(5.0)
    assert r.stop_distance_pct == pytest.approx(0.02)
    assert r.raw_notional == pytest.approx(250.0)
    assert r.accepted is True
    assert r.effective_risk_pct == pytest.approx(0.005, abs=1e-6)


def test_exchange_minimum_forces_rounding_up_and_may_exceed_risk_cap():
    # Wide (10%) stop on ample equity -> raw_notional under the exchange
    # minimum, but still comfortably inside the exposure cap, so rounding up
    # is valid: risk_usdt=5, stop_pct=0.10 -> raw_notional=50... use a
    # slightly wider stop so raw_notional is unambiguously below the floor.
    r = size_position(equity=1000.0, entry_price=100.0, stop_price=88.0,
                       exchange_min_notional=50.0, risk_pct=0.005)
    assert r.raw_notional < r.exchange_min_notional
    assert r.rounded_notional == pytest.approx(50.0)
    # effective risk is now higher than the intended 0.5% because of min-notional rounding
    assert r.effective_risk_pct > 0.005


def test_exchange_minimum_above_exposure_cap_rejects_cleanly():
    # Small equity: the exchange's own floor sits above what 100% exposure
    # allows. Rounding down to the cap would silently submit a sub-minimum
    # order the exchange would reject -- must refuse instead.
    r = size_position(equity=27.44, entry_price=100.0, stop_price=99.0,
                       exchange_min_notional=50.0, risk_pct=0.005,
                       max_total_exposure_pct=1.0)
    assert r.accepted is False
    assert r.rejection_reason == "ACCOUNT_TOO_SMALL_FOR_SAFE_ORDER"
    assert r.rounded_notional == 0.0


def test_account_too_small_rejects_rather_than_silently_raising_risk():
    r = size_position(equity=10.0, entry_price=100.0, stop_price=98.0,
                       exchange_min_notional=50.0, risk_pct=0.005,
                       absolute_max_risk_pct=0.01)
    assert r.accepted is False
    assert r.rejection_reason == "ACCOUNT_TOO_SMALL_FOR_SAFE_ORDER"


def test_exposure_cap_bounds_notional_at_100_pct_equity():
    r = size_position(equity=1000.0, entry_price=100.0, stop_price=99.99,
                       exchange_min_notional=5.0, risk_pct=0.005,
                       max_total_exposure_pct=1.0)
    assert r.rounded_notional <= 1000.0 + 1e-6
    assert r.projected_total_exposure_pct <= 1.0 + 1e-9


def test_leverage_ceiling_rejects_when_exceeded():
    # notional forced above 2x equity by a very tight stop combined with min-notional floor
    r = size_position(equity=27.44, entry_price=100.0, stop_price=99.9,
                       exchange_min_notional=100.0, risk_pct=0.005,
                       max_leverage=2.0, absolute_max_risk_pct=1.0,
                       max_total_exposure_pct=10.0)
    assert r.leverage > 2.0
    assert r.accepted is False
    assert r.rejection_reason == "leverage_exceeds_ceiling"


def test_invalid_equity_rejected_cleanly():
    r = size_position(equity=0.0, entry_price=100.0, stop_price=98.0, exchange_min_notional=5.0)
    assert r.accepted is False
    assert r.rejection_reason == "invalid_equity_or_price"


def test_zero_stop_distance_rejected_cleanly():
    r = size_position(equity=1000.0, entry_price=100.0, stop_price=100.0, exchange_min_notional=5.0)
    assert r.accepted is False
    assert r.rejection_reason == "zero_stop_distance"


# --- deterministic multi-signal ranking ------------------------------------

def test_ranking_prefers_higher_mom_strength():
    weak = SignalCandidate("ETHUSDT", Side.LONG, 0, close=100.0, mom=0.03, atr=2.0)
    strong = SignalCandidate("SOLUSDT", Side.SHORT, 0, close=50.0, mom=-0.08, atr=1.0)
    winner = rank_candidates([weak, strong])
    assert winner.symbol == "SOLUSDT"


def test_ranking_tie_break_is_btc_then_eth_then_sol():
    # Identical mom_strength for all three -> BTC must win.
    btc = SignalCandidate("BTCUSDT", Side.LONG, 0, close=100.0, mom=0.03, atr=2.0)
    eth = SignalCandidate("ETHUSDT", Side.LONG, 0, close=100.0, mom=0.03, atr=2.0)
    sol = SignalCandidate("SOLUSDT", Side.LONG, 0, close=100.0, mom=0.03, atr=2.0)
    winner = rank_candidates([sol, eth, btc])
    assert winner.symbol == "BTCUSDT"

    winner2 = rank_candidates([eth, sol])
    assert winner2.symbol == "ETHUSDT"


def test_ranking_empty_list_returns_none():
    assert rank_candidates([]) is None


def test_mom_strength_handles_zero_atr_safely():
    c = SignalCandidate("BTCUSDT", Side.LONG, 0, close=100.0, mom=0.03, atr=0.0)
    assert c.mom_strength == 0.0


# --- fee observability (log-only, never blocks) -----------------------------

def test_fee_observability_never_returns_a_blocking_flag():
    result = fee_observability(notional=100.0, taker_fee_rate=0.0006, atr_pct=0.01,
                                stop_distance_pct=0.001)
    assert "block" not in result
    assert "reject" not in result
    assert result["estimated_round_trip_cost"] == pytest.approx(0.12)
    assert result["cost_as_pct_of_stop_distance"] is not None
