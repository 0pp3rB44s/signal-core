"""TradePlan construction correctness for adaptive_trend_tsmom_v1.

Covers: valid plan construction round-trips through TradePlan's own
__post_init__ validation, LONG/SHORT stop direction, no-TP configuration,
rejected-sizing refusal, and Race Test #1 (the same 6H candle evaluated
twice must produce byte-identical candidate_id/plan_id, since that identity
is what the existing intent/lifecycle machinery relies on to dedupe)."""

from __future__ import annotations

import pytest

from strategies.adaptive_trend_plan import STRATEGY_NAME, build_trade_plan
from strategies.adaptive_trend_tsmom import Side, SignalCandidate, size_position


def _candidate(symbol="BTCUSDT", side=Side.LONG, close=100.0, mom=0.05, atr=2.0,
                signal_candle_close_ms=1_800_000_000_000):
    return SignalCandidate(symbol=symbol, side=side, signal_candle_close_ms=signal_candle_close_ms,
                            close=close, mom=mom, atr=atr)


def _sizing(candidate, equity=1000.0, exchange_min_notional=5.0):
    stop = (candidate.close - 2.5 * candidate.atr) if candidate.side is Side.LONG \
        else (candidate.close + 2.5 * candidate.atr)
    return size_position(equity=equity, entry_price=candidate.close, stop_price=stop,
                          exchange_min_notional=exchange_min_notional)


def test_build_trade_plan_round_trips_through_post_init_validation():
    c = _candidate()
    s = _sizing(c)
    plan = build_trade_plan(c, s)
    assert plan.strategy == STRATEGY_NAME
    assert plan.symbol == "BTCUSDT"
    assert plan.direction == "LONG"
    assert plan.take_profits == []
    assert plan.entry_prices == [100.0]


def test_build_trade_plan_long_stop_is_below_entry():
    c = _candidate(side=Side.LONG, close=100.0, atr=2.0)
    s = _sizing(c)
    plan = build_trade_plan(c, s)
    assert plan.stop_loss == pytest.approx(100.0 - 2.5 * 2.0)
    assert plan.stop_loss < plan.entry_prices[0]


def test_build_trade_plan_short_stop_is_above_entry():
    c = _candidate(side=Side.SHORT, close=100.0, mom=-0.05, atr=2.0)
    s = _sizing(c)
    plan = build_trade_plan(c, s)
    assert plan.stop_loss == pytest.approx(100.0 + 2.5 * 2.0)
    assert plan.stop_loss > plan.entry_prices[0]


def test_build_trade_plan_refuses_rejected_sizing():
    c = _candidate(close=100.0, atr=2.0)
    s = size_position(equity=1.0, entry_price=100.0, stop_price=95.0, exchange_min_notional=1000.0)
    assert not s.accepted
    with pytest.raises(ValueError, match="rejected sizing"):
        build_trade_plan(c, s)


def test_build_trade_plan_notional_and_leverage_carry_through():
    c = _candidate()
    s = _sizing(c)
    plan = build_trade_plan(c, s)
    assert plan.position_notional_usdt == pytest.approx(s.rounded_notional)
    assert plan.leverage == pytest.approx(s.leverage)


# --- Race Test #1: same closed candle evaluated twice -> identical identity

def test_same_candle_evaluated_twice_produces_identical_plan_identity():
    """This is the structural guarantee the existing intent/lifecycle
    machinery relies on to dedupe a duplicate scan of the same closed
    candle: candidate_id/plan_id must be pure functions of
    (strategy, symbol, direction, candle_open_ms), not of wall-clock time or
    call order."""
    c1 = _candidate(signal_candle_close_ms=1_800_000_000_000)
    c2 = _candidate(signal_candle_close_ms=1_800_000_000_000)  # re-evaluation of the same candle
    plan1 = build_trade_plan(c1, _sizing(c1))
    plan2 = build_trade_plan(c2, _sizing(c2))
    assert plan1.candidate_id == plan2.candidate_id
    assert plan1.plan_id == plan2.plan_id


def test_different_candle_produces_different_plan_identity():
    c1 = _candidate(signal_candle_close_ms=1_800_000_000_000)
    c2 = _candidate(signal_candle_close_ms=1_800_021_600_000)  # next 6H candle
    plan1 = build_trade_plan(c1, _sizing(c1))
    plan2 = build_trade_plan(c2, _sizing(c2))
    assert plan1.candidate_id != plan2.candidate_id


def test_different_symbol_same_candle_produces_different_identity():
    c1 = _candidate(symbol="BTCUSDT")
    c2 = _candidate(symbol="ETHUSDT")
    plan1 = build_trade_plan(c1, _sizing(c1))
    plan2 = build_trade_plan(c2, _sizing(c2))
    assert plan1.candidate_id != plan2.candidate_id


def test_opposite_side_same_candle_produces_different_identity():
    """A LONG and a SHORT signal on the same candle must never collide --
    this is what lets 'opposite signal is logged only, no hedge' be provable
    at the identity layer, not just by executor-side logic."""
    long_c = _candidate(side=Side.LONG, mom=0.05)
    short_c = _candidate(side=Side.SHORT, mom=-0.05)
    plan_long = build_trade_plan(long_c, _sizing(long_c))
    plan_short = build_trade_plan(short_c, _sizing(short_c))
    assert plan_long.candidate_id != plan_short.candidate_id
