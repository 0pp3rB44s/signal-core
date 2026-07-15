from dataclasses import asdict, replace

import pytest

from backtesting.execution_contract import BacktestExecutionConfig, BacktestExecutionContract
from clients.schemas import Candle


def candle(ts, open_, high, low, close=None):
    return Candle(ts, open_, high, low, open_ if close is None else close, 100.0)


def config(**changes):
    base = BacktestExecutionConfig(
        spread_bps=0, entry_slippage_bps=0, exit_slippage_bps=0,
        maker_fee_bps=0, taker_fee_bps=0, risk_per_trade_pct=10,
        leverage_cap=10, maximum_notional=10_000,
        available_equity_notional_pct=1_000, minimum_quantity=0.001,
        minimum_notional=0, quantity_step=0.001, price_tick=0.01,
        max_hold_candles=4,
    )
    return replace(base, **changes)


def execute(cfg=None, *, direction="LONG", requested=100, stop=90, targets=(110, 120), candles=None, equity=1000):
    return BacktestExecutionContract(cfg or config()).execute(
        strategy="fixture", symbol="BTCUSDT", timeframe="15m", direction=direction,
        signal_timestamp=1, requested_entry=requested, stop=stop, targets=targets,
        candles=candles or [candle(2, 100, 101, 99), candle(3, 100, 121, 99)], equity=equity,
    )


def test_market_long_entry_applies_adverse_spread_and_slippage():
    row = execute(config(spread_bps=4, entry_slippage_bps=2))
    assert row.executed_entry == pytest.approx(100.06)
    assert row.spread_cost == pytest.approx(0.04 * row.initial_quantity)
    assert row.entry_slippage == pytest.approx(0.02 * row.initial_quantity)


def test_market_short_entry_applies_adverse_spread_and_slippage():
    row = execute(config(spread_bps=4, entry_slippage_bps=2), direction="SHORT", stop=110, targets=(90, 80), candles=[candle(2, 100, 101, 99), candle(3, 100, 101, 79)])
    assert row.executed_entry == pytest.approx(99.94)
    assert row.spread_cost == pytest.approx(0.04 * row.initial_quantity)
    assert row.entry_slippage == pytest.approx(0.02 * row.initial_quantity)


def test_limit_fills_only_when_touched_and_records_fill_candle():
    row = execute(config(entry_type="LIMIT", limit_expiration_candles=3), candles=[candle(2, 102, 103, 101), candle(3, 101, 102, 99), candle(4, 101, 121, 99)])
    assert row.fill_status == "FILLED"
    assert row.executed_entry == 100
    assert row.fill_timestamp == 3


def test_limit_untouched_expires_unfilled():
    row = execute(config(entry_type="LIMIT", limit_expiration_candles=2), candles=[candle(2, 102, 103, 101), candle(3, 102, 104, 100.01), candle(4, 100, 121, 99)])
    assert row.fill_status == "UNFILLED"
    assert row.rejection_reason == "LIMIT_EXPIRED"


def test_limit_touch_after_expiration_does_not_fill():
    row = execute(config(entry_type="LIMIT", limit_expiration_candles=1), candles=[candle(2, 102, 103, 101), candle(3, 100, 101, 99)])
    assert row.fill_status == "UNFILLED"


def test_entry_and_exit_fees_are_each_applied_once():
    row = execute(config(taker_fee_bps=10), targets=(110,), candles=[candle(2, 100, 101, 99), candle(3, 105, 111, 104)])
    assert row.initial_quantity == 10
    assert row.entry_fee == pytest.approx(1.0)
    assert row.final_exit_fee == pytest.approx(1.1)
    assert row.total_fees == pytest.approx(2.1)
    assert row.net_pnl == pytest.approx(97.9)


def test_fee_is_applied_to_partial_and_final_exit():
    row = execute(config(taker_fee_bps=10), candles=[candle(2, 100, 101, 99), candle(3, 105, 111, 104), candle(4, 115, 121, 114)])
    assert row.tp1_quantity == 4
    assert row.tp1_fee == pytest.approx(0.44)
    assert row.final_exit_fee == pytest.approx(0.72)
    assert row.total_fees == pytest.approx(2.16)


def test_same_candle_stop_and_target_is_conservative_and_flagged():
    row = execute(config(same_candle_policy="CONSERVATIVE"), candles=[candle(2, 100, 101, 99), candle(3, 100, 111, 89)])
    assert row.final_exit_reason == "STOP_LOSS"
    assert row.intrabar_ambiguous is True
    assert row.intrabar_policy_used == "CONSERVATIVE"


def test_target_first_policy_is_explicit():
    row = execute(config(same_candle_policy="TARGET_FIRST"), targets=(110,), candles=[candle(2, 100, 101, 99), candle(3, 100, 111, 89)])
    assert row.final_exit_reason == "TAKE_PROFIT"
    assert row.intrabar_ambiguous is True


def test_tp1_partial_then_entry_break_even_stop_uses_weighted_pnl():
    row = execute(config(break_even_policy="ENTRY"), candles=[candle(2, 100, 101, 99), candle(3, 105, 111, 104), candle(4, 101, 102, 99)])
    assert row.tp1_quantity == 4
    assert row.stop_after_tp1 == 100
    assert row.final_exit_reason == "BREAK_EVEN_STOP"
    assert row.gross_pnl == pytest.approx(40)
    assert row.net_pnl == pytest.approx(40)
    assert row.r_multiple == pytest.approx(0.4)


def test_tp1_partial_then_final_target_uses_weighted_pnl():
    row = execute(candles=[candle(2, 100, 101, 99), candle(3, 105, 111, 104), candle(4, 115, 121, 114)])
    assert row.tp1_quantity == 4
    assert row.gross_pnl == pytest.approx(160)
    assert row.net_pnl == pytest.approx(160)
    assert row.equity_after == pytest.approx(1160)


def test_quantity_is_floored_to_step():
    row = execute(config(risk_per_trade_pct=1, quantity_step=0.1), stop=97, targets=(103,), candles=[candle(2, 100, 101, 99), candle(3, 101, 104, 100)])
    assert row.raw_quantity == pytest.approx(10 / 3)
    assert row.initial_quantity == pytest.approx(3.3)


def test_rejects_below_minimum_quantity():
    row = execute(config(risk_per_trade_pct=0.01, minimum_quantity=0.1, quantity_step=0.01), stop=90)
    assert row.fill_status == "REJECTED"
    assert row.rejection_reason == "BELOW_MIN_QTY"


def test_rejects_below_minimum_notional():
    row = execute(config(risk_per_trade_pct=0.1, minimum_notional=20), stop=90)
    assert row.fill_status == "REJECTED"
    assert row.rejection_reason == "BELOW_MIN_NOTIONAL"


def test_equity_changes_by_net_not_gross_pnl():
    row = execute(config(taker_fee_bps=10), targets=(110,), candles=[candle(2, 100, 101, 99), candle(3, 105, 111, 104)])
    assert row.equity_after == pytest.approx(row.equity_before + row.net_pnl)
    assert row.equity_after != pytest.approx(row.equity_before + row.gross_pnl)


def test_repeated_execution_is_byte_equivalent():
    kwargs = dict(cfg=config(taker_fee_bps=10, spread_bps=4, entry_slippage_bps=2, exit_slippage_bps=2))
    assert asdict(execute(**kwargs)) == asdict(execute(**kwargs))
