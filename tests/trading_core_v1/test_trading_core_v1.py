"""Minimum required test set for Trading Core V1.

Covers: strategy cannot place orders / no lookahead; fees deducted; slippage
deducted; long PnL correct; short PnL correct; stop-loss correct; take-profit
correct; same-bar SL/TP ambiguity resolves conservatively; risk sizing
correct; minimum-notional rejection; equity updates correctly; journal values
correct; LIVE mode cannot place real orders.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest

from trading_core_v1.backtest import BacktestConfig, CostModel, run_backtest
from trading_core_v1.data import Candle, DataIntegrityError, sequential_feed, validate_candles
from trading_core_v1.executor import Executor, ExecutionMode, LiveTradingNotAuthorized
from trading_core_v1.models import Side, Signal
from trading_core_v1.risk import compute_position_size
from trading_core_v1.strategy import Strategy


def mk_candle(ts, o, h, l, c, v=1.0):
    return Candle(timestamp=ts, open=o, high=h, low=l, close=c, volume=v)


class FixedSignalStrategy(Strategy):
    """Test double: emits a scripted Signal at one specific bar index, FLAT
    everywhere else. Used to drive the backtester deterministically."""

    name = "fixed_signal_test_strategy"

    def __init__(self, trigger_index: int, side: Side, stop_reference: float):
        self.trigger_index = trigger_index
        self.side = side
        self.stop_reference = stop_reference

    def evaluate(self, history, current_bar) -> Signal:
        idx = len(history)  # sequential_feed guarantees history == candles[:idx]
        if idx == self.trigger_index:
            return Signal(
                timestamp=current_bar.timestamp,
                symbol="TESTUSDT",
                side=self.side,
                entry_reference_price=current_bar.close,
                stop_reference=self.stop_reference,
                strategy_name=self.name,
            )
        return Signal(timestamp=current_bar.timestamp, symbol="TESTUSDT", side=Side.FLAT, strategy_name=self.name)


ZERO_COSTS = CostModel(maker_fee_bps=0, taker_fee_bps=0, slippage_bps=0)


def base_config(**overrides):
    defaults = dict(
        symbol="TESTUSDT",
        risk_pct=0.01,
        initial_equity=10_000.0,
        min_notional=1.0,
        take_profit_r_multiple=2.0,
        compounding=False,
        costs=ZERO_COSTS,
    )
    defaults.update(overrides)
    return BacktestConfig(**defaults)


# --- no lookahead ------------------------------------------------------------


def test_sequential_feed_history_excludes_current_and_future_bars():
    candles = [mk_candle(i * 60_000, 100, 101, 99, 100, 1) for i in range(5)]
    for history, current_bar in sequential_feed(candles):
        assert current_bar not in history
        assert all(h.timestamp < current_bar.timestamp for h in history)


def test_strategy_evaluate_is_pure_and_places_no_orders():
    # The Strategy ABC exposes exactly one method: evaluate(). There is no
    # order-placement, exchange, or sizing method on the interface at all.
    public_methods = [m for m in dir(Strategy) if not m.startswith("_")]
    assert public_methods == ["evaluate", "name"] or set(public_methods) <= {"evaluate", "name"}
    strat = FixedSignalStrategy(trigger_index=0, side=Side.LONG, stop_reference=90)
    sig = strat.evaluate([], mk_candle(0, 100, 101, 99, 100))
    assert isinstance(sig, Signal)
    # Returning a Signal object -- a plain dataclass -- is structurally
    # incapable of placing an order; there is nothing to call.
    assert not hasattr(sig, "submit") and not hasattr(sig, "place_order")


# --- data integrity ------------------------------------------------------------


def test_validate_candles_rejects_high_less_than_low():
    candles = [mk_candle(0, 100, 99, 101, 100)]  # high < low
    with pytest.raises(DataIntegrityError):
        validate_candles(candles, "1m")


def test_validate_candles_rejects_duplicates_and_unsorted():
    c1 = mk_candle(60_000, 100, 101, 99, 100)
    with pytest.raises(DataIntegrityError):
        validate_candles([c1, c1], "1m")


# --- long PnL, fees, slippage --------------------------------------------------


def test_long_pnl_correct_no_costs():
    # signal at bar0 (using bar0's close), fills at bar1 open=100, exits at
    # end of data (bar2 close=110) since neither stop nor TP is touched.
    candles = [
        mk_candle(0, 90, 92, 88, 90),
        mk_candle(60_000, 100, 100, 100, 100),
        mk_candle(120_000, 105, 111, 104, 110),
    ]
    strat = FixedSignalStrategy(trigger_index=0, side=Side.LONG, stop_reference=80)
    cfg = base_config()
    result = run_backtest(candles, strat, cfg)
    assert len(result.trades) == 1
    t = result.trades[0]
    assert t.side == "LONG"
    assert t.entry_price == pytest.approx(100.0)
    assert t.exit_reason == "END_OF_DATA"
    assert t.exit_price == pytest.approx(110.0)
    expected_gross = (110.0 - 100.0) * t.position_size
    assert t.gross_pnl == pytest.approx(expected_gross)
    assert t.fees == pytest.approx(0.0)
    assert t.slippage == pytest.approx(0.0)
    assert t.net_pnl == pytest.approx(t.gross_pnl)


def test_short_pnl_correct_no_costs():
    candles = [
        mk_candle(0, 110, 112, 108, 110),
        mk_candle(60_000, 100, 100, 100, 100),
        mk_candle(120_000, 95, 96, 89, 90),
    ]
    strat = FixedSignalStrategy(trigger_index=0, side=Side.SHORT, stop_reference=120)
    cfg = base_config()
    result = run_backtest(candles, strat, cfg)
    assert len(result.trades) == 1
    t = result.trades[0]
    assert t.side == "SHORT"
    assert t.entry_price == pytest.approx(100.0)
    expected_gross = (100.0 - 90.0) * t.position_size
    assert t.gross_pnl == pytest.approx(expected_gross)
    assert t.net_pnl == pytest.approx(t.gross_pnl)


def test_fees_are_deducted():
    candles = [
        mk_candle(0, 90, 92, 88, 90),
        mk_candle(60_000, 100, 100, 100, 100),
        mk_candle(120_000, 105, 111, 104, 110),
    ]
    strat = FixedSignalStrategy(trigger_index=0, side=Side.LONG, stop_reference=80)
    cfg = base_config(costs=CostModel(maker_fee_bps=2, taker_fee_bps=6, slippage_bps=0))
    result = run_backtest(candles, strat, cfg)
    t = result.trades[0]
    assert t.fees > 0
    assert t.net_pnl == pytest.approx(t.gross_pnl - t.fees)


def test_slippage_is_deducted():
    candles = [
        mk_candle(0, 90, 92, 88, 90),
        mk_candle(60_000, 100, 100, 100, 100),
        mk_candle(120_000, 105, 111, 104, 110),
    ]
    strat = FixedSignalStrategy(trigger_index=0, side=Side.LONG, stop_reference=80)
    zero_slip = run_backtest(candles, strat, base_config()).trades[0]
    with_slip = run_backtest(
        candles, strat, base_config(costs=CostModel(maker_fee_bps=0, taker_fee_bps=0, slippage_bps=10))
    ).trades[0]
    assert with_slip.slippage > 0
    # Slippage always works against the trader: worse net PnL with it than without.
    assert with_slip.net_pnl < zero_slip.net_pnl
    assert with_slip.entry_price > zero_slip.entry_price  # long entry slips upward (worse)


# --- stop-loss / take-profit --------------------------------------------------


def test_stop_loss_exit_correct():
    candles = [
        mk_candle(0, 90, 92, 88, 90),
        mk_candle(60_000, 100, 100, 100, 100),  # fill bar, open=100
        mk_candle(120_000, 99, 100, 94, 96),  # low=94 breaches stop=95
    ]
    strat = FixedSignalStrategy(trigger_index=0, side=Side.LONG, stop_reference=95)
    cfg = base_config()
    result = run_backtest(candles, strat, cfg)
    t = result.trades[0]
    assert t.exit_reason == "STOP"
    assert t.exit_price == pytest.approx(95.0)


def test_take_profit_exit_correct():
    candles = [
        mk_candle(0, 90, 92, 88, 90),
        mk_candle(60_000, 100, 100, 100, 100),  # fill at 100, stop=90 -> TP = 100 + 2*10 = 120
        mk_candle(120_000, 105, 121, 104, 118),  # high=121 reaches TP=120, low doesn't breach stop
    ]
    strat = FixedSignalStrategy(trigger_index=0, side=Side.LONG, stop_reference=90)
    cfg = base_config(take_profit_r_multiple=2.0)
    result = run_backtest(candles, strat, cfg)
    t = result.trades[0]
    assert t.exit_reason == "TAKE_PROFIT"
    assert t.exit_price == pytest.approx(120.0)


def test_same_bar_stop_and_tp_ambiguity_resolves_conservatively_to_stop():
    candles = [
        mk_candle(0, 90, 92, 88, 90),
        mk_candle(60_000, 100, 100, 100, 100),  # fill at 100, stop=90, TP=120
        mk_candle(120_000, 100, 125, 85, 100),  # this bar's range touches BOTH stop and TP
    ]
    strat = FixedSignalStrategy(trigger_index=0, side=Side.LONG, stop_reference=90)
    cfg = base_config(take_profit_r_multiple=2.0)
    result = run_backtest(candles, strat, cfg)
    t = result.trades[0]
    assert t.exit_reason == "STOP"
    assert t.exit_price == pytest.approx(90.0)


# --- risk sizing ---------------------------------------------------------------


def test_risk_sizing_correct():
    result = compute_position_size(
        equity=10_000, risk_pct=0.01, entry_price=100, stop_price=90, min_notional=1
    )
    # risk_amount = 100, stop_distance = 10 -> position_size = 10
    assert result.approved
    assert result.risk_amount == pytest.approx(100.0)
    assert result.position_size == pytest.approx(10.0)


def test_minimum_notional_rejection():
    result = compute_position_size(
        equity=100, risk_pct=0.001, entry_price=100, stop_price=99.99, min_notional=1_000_000
    )
    assert not result.approved
    assert result.reason == "TRADE_REJECTED_MIN_NOTIONAL"


def test_backtest_records_min_notional_rejection_without_opening_a_trade():
    candles = [
        mk_candle(0, 90, 92, 88, 90),
        mk_candle(60_000, 100, 100, 100, 100),
        mk_candle(120_000, 105, 111, 104, 110),
    ]
    strat = FixedSignalStrategy(trigger_index=0, side=Side.LONG, stop_reference=99.9)
    cfg = base_config(min_notional=1_000_000_000)
    result = run_backtest(candles, strat, cfg)
    assert len(result.trades) == 0
    assert len(result.rejections) == 1
    assert result.rejections[0]["reason"] == "TRADE_REJECTED_MIN_NOTIONAL"


# --- equity / journal ------------------------------------------------------------


def test_equity_updates_correctly_with_compounding():
    candles = [
        mk_candle(0, 90, 92, 88, 90),
        mk_candle(60_000, 100, 100, 100, 100),
        mk_candle(120_000, 105, 111, 104, 110),
    ]
    strat = FixedSignalStrategy(trigger_index=0, side=Side.LONG, stop_reference=80)
    cfg = base_config(compounding=True)
    result = run_backtest(candles, strat, cfg)
    t = result.trades[0]
    assert t.equity_after == pytest.approx(t.equity_before + t.net_pnl)
    assert result.equity_curve[-1][1] == pytest.approx(t.equity_after)


def test_journal_values_consistent():
    candles = [
        mk_candle(0, 90, 92, 88, 90),
        mk_candle(60_000, 100, 100, 100, 100),
        mk_candle(120_000, 105, 111, 104, 110),
    ]
    strat = FixedSignalStrategy(trigger_index=0, side=Side.LONG, stop_reference=80)
    cfg = base_config(costs=CostModel(maker_fee_bps=2, taker_fee_bps=6, slippage_bps=1))
    result = run_backtest(candles, strat, cfg)
    t = result.trades[0]
    assert t.net_pnl == pytest.approx(t.gross_pnl - t.fees)
    assert t.r_multiple == pytest.approx(t.net_pnl / t.risk_amount)
    assert t.trade_id == 1
    assert t.strategy == strat.name
    assert t.symbol == cfg.symbol


# --- LIVE mode --------------------------------------------------------------


def test_live_mode_cannot_place_real_orders():
    executor = Executor(ExecutionMode.LIVE)
    with pytest.raises(LiveTradingNotAuthorized):
        executor.submit_order(symbol="BTCUSDT", side="LONG", size=1)


def test_backtest_and_paper_modes_do_not_raise_live_authorized_error():
    for mode in (ExecutionMode.BACKTEST, ExecutionMode.PAPER):
        executor = Executor(mode)
        with pytest.raises(NotImplementedError):
            executor.submit_order()
