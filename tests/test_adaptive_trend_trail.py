"""ATR trailing-stop evaluation correctness: ratchet-only, idempotent,
restart-safe (continues from the exchange's own reported stop, never a
stale local value, never reset to initial)."""

from __future__ import annotations

import pytest

from strategies.adaptive_trend_trail import TrailOutcome, evaluate_trail
from strategies.adaptive_trend_tsmom import ATR_PERIOD, Candle6h, Side

SIX_H = 6 * 60 * 60 * 1000


def bars(closes, start_ms=0, pad=1.0):
    out = []
    for i, c in enumerate(closes):
        open_ms = start_ms + i * SIX_H
        out.append(Candle6h(open_ms=open_ms, close_ms=open_ms + SIX_H,
                             open=c, high=c + pad, low=c - pad, close=c))
    return out


WARMUP = ATR_PERIOD + 1


def test_no_new_candle_is_a_pure_noop_race_test_1():
    """Same candle processed twice: idempotent by construction, no stop change."""
    candles = bars([100.0] * WARMUP)
    already_seen = candles[-1].close_ms
    d = evaluate_trail(symbol="BTCUSDT", side=Side.LONG, current_stop=90.0,
                        candles=candles, now_ms=candles[-1].close_ms,
                        last_processed_close_ms=already_seen)
    assert d.outcome == TrailOutcome.NO_NEW_CANDLE
    assert d.new_stop is None  # caller must not touch the exchange stop


def test_long_stop_ratchets_up_on_favorable_move():
    closes = [100.0] * WARMUP + [120.0]
    candles = bars(closes)
    d = evaluate_trail(symbol="BTCUSDT", side=Side.LONG, current_stop=90.0,
                        candles=candles, now_ms=candles[-1].close_ms,
                        last_processed_close_ms=None)
    assert d.outcome == TrailOutcome.UPDATED
    assert d.new_stop > 90.0
    # ATR here is gap-dominated (true range includes |high - prev_close|),
    # not simply 2*pad -- assert the exact ratchet formula against the ATR
    # the decision itself reports, not a hand-guessed constant.
    assert d.new_stop == pytest.approx(120.0 - 2.5 * d.atr)


def test_long_stop_never_loosens_on_adverse_move():
    closes = [100.0] * WARMUP + [80.0]  # price drops -- candidate stop would be far lower
    candles = bars(closes)
    d = evaluate_trail(symbol="BTCUSDT", side=Side.LONG, current_stop=90.0,
                        candles=candles, now_ms=candles[-1].close_ms,
                        last_processed_close_ms=None)
    assert d.outcome == TrailOutcome.UNCHANGED
    assert d.new_stop == 90.0  # unchanged, not loosened


def test_short_stop_ratchets_down_on_favorable_move():
    closes = [100.0] * WARMUP + [80.0]
    candles = bars(closes)
    d = evaluate_trail(symbol="ETHUSDT", side=Side.SHORT, current_stop=110.0,
                        candles=candles, now_ms=candles[-1].close_ms,
                        last_processed_close_ms=None)
    assert d.outcome == TrailOutcome.UPDATED
    assert d.new_stop < 110.0
    assert d.new_stop == pytest.approx(80.0 + 2.5 * d.atr)


def test_short_stop_never_loosens_on_adverse_move():
    closes = [100.0] * WARMUP + [120.0]
    candles = bars(closes)
    d = evaluate_trail(symbol="ETHUSDT", side=Side.SHORT, current_stop=110.0,
                        candles=candles, now_ms=candles[-1].close_ms,
                        last_processed_close_ms=None)
    assert d.outcome == TrailOutcome.UNCHANGED
    assert d.new_stop == 110.0


def test_stale_data_refuses_to_touch_the_stop():
    candles = bars([100.0] * WARMUP)
    far_future = candles[-1].close_ms + 20 * SIX_H
    d = evaluate_trail(symbol="BTCUSDT", side=Side.LONG, current_stop=90.0,
                        candles=candles, now_ms=far_future, last_processed_close_ms=None)
    assert d.outcome == TrailOutcome.DATA_UNHEALTHY
    assert d.new_stop is None


def test_restart_continues_from_exchange_reported_stop_not_initial():
    """Simulates a restart: the position's ORIGINAL initial stop was, say,
    92.0, but price has since moved favorably and the exchange's real stop
    is now 95.0 (set by a prior trailing update before the crash). Passing
    the exchange's current stop -- not the initial one -- must be what the
    ratchet continues from."""
    closes = [100.0] * WARMUP + [96.0]  # modest further move
    candles = bars(closes)
    exchange_reported_stop = 95.0  # NOT the original initial stop (92.0)
    d = evaluate_trail(symbol="BTCUSDT", side=Side.LONG, current_stop=exchange_reported_stop,
                        candles=candles, now_ms=candles[-1].close_ms,
                        last_processed_close_ms=None)
    # candidate = 96.0 - 2.5*1.0 = 93.5, which is BELOW 95.0 -> must not loosen to 93.5
    assert d.outcome == TrailOutcome.UNCHANGED
    assert d.new_stop == 95.0  # held at the exchange-reported stop, never fell back toward "initial"


def test_multiple_candles_closed_while_down_only_latest_drives_the_stop():
    closes = [100.0] * WARMUP + [105.0, 108.0, 130.0]
    candles = bars(closes)
    last_seen = candles[WARMUP - 1].close_ms  # only warmup processed before "crash"
    d = evaluate_trail(symbol="BTCUSDT", side=Side.LONG, current_stop=90.0,
                        candles=candles, now_ms=candles[-1].close_ms,
                        last_processed_close_ms=last_seen)
    assert d.outcome == TrailOutcome.UPDATED
    assert d.last_processed_close_ms == candles[-1].close_ms
    # driven by the LATEST close (130.0), not an intermediate one
    assert d.candle_close == 130.0
    assert d.new_stop == pytest.approx(130.0 - 2.5 * d.atr)
