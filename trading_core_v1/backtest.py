"""Deterministic, event-driven backtester.

Execution convention (documented explicitly, per the mission spec):
  - A strategy signal is generated using bar i's CLOSE (Strategy.evaluate is
    given history=candles[:i] and current_bar=candles[i]).
  - The EARLIEST possible fill is bar i+1's OPEN. A signal can never fill on
    information ("this bar's close") that would not yet have been tradeable at
    that instant in a real market -- there is no same-bar, free execution.
  - Once a position is open, its stop-loss and take-profit are checked against
    each subsequent bar's high/low (starting with the bar it was filled on).
  - Same-bar stop/TP ambiguity (one candle's range touches both levels): we
    cannot know which happened first from OHLC alone, so we ALWAYS assume the
    stop was hit first. This is the conservative choice -- it never picks
    whichever outcome is more profitable.
  - Entries and stop-loss exits are modeled as taker (market) fills; take-profit
    exits are modeled as maker (limit) fills. This is a modeling assumption,
    stated here explicitly, not a guarantee of real fill behavior.

Fees and slippage are always applied and always reported separately
(GROSS_PNL vs NET_PNL). Position sizing is delegated to risk.py -- this module
never invents its own sizing rule.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from trading_core_v1.data import Candle
from trading_core_v1.journal import TradeRecord
from trading_core_v1.models import Side, Signal
from trading_core_v1.risk import compute_position_size
from trading_core_v1.strategy import Strategy


@dataclass(frozen=True)
class CostModel:
    maker_fee_bps: float
    taker_fee_bps: float
    slippage_bps: float


@dataclass(frozen=True)
class BacktestConfig:
    symbol: str
    risk_pct: float
    initial_equity: float
    min_notional: float
    take_profit_r_multiple: float
    compounding: bool
    costs: CostModel


@dataclass
class _OpenPosition:
    signal: Signal
    side: Side
    entry_time: int
    entry_price: float
    stop_price: float
    take_profit_price: float
    position_size: float
    risk_amount: float
    entry_fee: float
    entry_slippage_cost: float


@dataclass
class BacktestResult:
    trades: list[TradeRecord] = field(default_factory=list)
    equity_curve: list[tuple[int, float]] = field(default_factory=list)  # (timestamp, equity)
    rejections: list[dict] = field(default_factory=list)


def _bps(value: float, bps: float) -> float:
    return value * (bps / 10_000.0)


def run_backtest(
    candles: Sequence[Candle], strategy: Strategy, config: BacktestConfig
) -> BacktestResult:
    if len(candles) < 2:
        raise ValueError("need at least 2 candles to run a backtest (signal bar + fill bar)")

    result = BacktestResult()
    equity = config.initial_equity
    result.equity_curve.append((candles[0].timestamp, equity))

    open_position: _OpenPosition | None = None
    pending_signal: Signal | None = None
    trade_id = 0

    for i in range(len(candles)):
        history = candles[:i]
        current_bar = candles[i]

        # --- (a) process a pending fill from the previous bar's signal ---
        if pending_signal is not None and open_position is None:
            sig = pending_signal
            pending_signal = None
            if sig.side in (Side.LONG, Side.SHORT) and sig.stop_reference is not None:
                raw_entry = current_bar.open
                if sig.side is Side.LONG:
                    entry_price = raw_entry * (1 + config.costs.slippage_bps / 10_000.0)
                else:
                    entry_price = raw_entry * (1 - config.costs.slippage_bps / 10_000.0)

                stop_price = sig.stop_reference
                stop_distance = abs(entry_price - stop_price)
                if sig.side is Side.LONG:
                    take_profit_price = entry_price + config.take_profit_r_multiple * stop_distance
                else:
                    take_profit_price = entry_price - config.take_profit_r_multiple * stop_distance

                risk = compute_position_size(
                    equity=equity,
                    risk_pct=config.risk_pct,
                    entry_price=entry_price,
                    stop_price=stop_price,
                    min_notional=config.min_notional,
                )
                if not risk.approved:
                    result.rejections.append(
                        {
                            "timestamp": current_bar.timestamp,
                            "reason": risk.reason,
                            "symbol": config.symbol,
                        }
                    )
                else:
                    notional = risk.position_size * entry_price
                    entry_fee = _bps(notional, config.costs.taker_fee_bps)
                    entry_slippage_cost = risk.position_size * abs(entry_price - raw_entry)
                    open_position = _OpenPosition(
                        signal=sig,
                        side=sig.side,
                        entry_time=current_bar.timestamp,
                        entry_price=entry_price,
                        stop_price=stop_price,
                        take_profit_price=take_profit_price,
                        position_size=risk.position_size,
                        risk_amount=risk.risk_amount,
                        entry_fee=entry_fee,
                        entry_slippage_cost=entry_slippage_cost,
                    )

        # --- (b) check exit conditions for an open position against this bar ---
        if open_position is not None:
            pos = open_position
            stop_hit = (
                current_bar.low <= pos.stop_price
                if pos.side is Side.LONG
                else current_bar.high >= pos.stop_price
            )
            tp_hit = (
                current_bar.high >= pos.take_profit_price
                if pos.side is Side.LONG
                else current_bar.low <= pos.take_profit_price
            )

            exit_now = False
            exit_reason = ""
            exit_price = 0.0
            fee_bps_for_exit = config.costs.taker_fee_bps

            if stop_hit:
                # Conservative same-bar ambiguity rule: stop always wins.
                exit_now = True
                exit_reason = "STOP"
                exit_price = pos.stop_price
                fee_bps_for_exit = config.costs.taker_fee_bps
            elif tp_hit:
                exit_now = True
                exit_reason = "TAKE_PROFIT"
                exit_price = pos.take_profit_price
                fee_bps_for_exit = config.costs.maker_fee_bps
            elif i == len(candles) - 1:
                exit_now = True
                exit_reason = "END_OF_DATA"
                exit_price = current_bar.close
                fee_bps_for_exit = config.costs.taker_fee_bps

            if exit_now:
                if pos.side is Side.LONG:
                    raw_exit_slip_price = exit_price * (1 - config.costs.slippage_bps / 10_000.0)
                else:
                    raw_exit_slip_price = exit_price * (1 + config.costs.slippage_bps / 10_000.0)

                if pos.side is Side.LONG:
                    gross_pnl = (raw_exit_slip_price - pos.entry_price) * pos.position_size
                else:
                    gross_pnl = (pos.entry_price - raw_exit_slip_price) * pos.position_size

                exit_notional = pos.position_size * raw_exit_slip_price
                exit_fee = _bps(exit_notional, fee_bps_for_exit)
                exit_slippage_cost = pos.position_size * abs(exit_price - raw_exit_slip_price)

                fees_total = pos.entry_fee + exit_fee
                slippage_total = pos.entry_slippage_cost + exit_slippage_cost
                net_pnl = gross_pnl - fees_total

                # `equity` (the sizing base passed to compute_position_size) only
                # advances when compounding=True. The realized equity ledger
                # reported in the journal/curve always accumulates net_pnl
                # regardless of compounding -- "fixed equity" describes the
                # SIZING assumption, not a refusal to report realized results.
                equity_before = equity
                realized_equity_after = (
                    result.equity_curve[-1][1] if result.equity_curve else config.initial_equity
                ) + net_pnl
                trade_id += 1
                r_multiple = net_pnl / pos.risk_amount if pos.risk_amount else 0.0
                result.trades.append(
                    TradeRecord(
                        trade_id=trade_id,
                        strategy=pos.signal.strategy_name,
                        symbol=config.symbol,
                        side=pos.side.value,
                        signal_time=pos.signal.timestamp,
                        entry_time=pos.entry_time,
                        entry_price=pos.entry_price,
                        stop_price=pos.stop_price,
                        exit_time=current_bar.timestamp,
                        exit_price=raw_exit_slip_price,
                        exit_reason=exit_reason,
                        position_size=pos.position_size,
                        gross_pnl=gross_pnl,
                        fees=fees_total,
                        slippage=slippage_total,
                        net_pnl=net_pnl,
                        equity_before=equity_before,
                        equity_after=realized_equity_after,
                        risk_amount=pos.risk_amount,
                        r_multiple=r_multiple,
                    )
                )
                if config.compounding:
                    equity = realized_equity_after
                result.equity_curve.append((current_bar.timestamp, realized_equity_after))
                open_position = None

        # --- (c) evaluate the strategy for a fresh signal when flat ---
        if open_position is None and pending_signal is None:
            sig = strategy.evaluate(history, current_bar)
            if sig.side in (Side.LONG, Side.SHORT):
                pending_signal = sig

    return result


def compute_metrics(
    result: BacktestResult, *, initial_equity: float, start_ts: int, end_ts: int
) -> dict:
    """Compute the required metric set from a BacktestResult.

    Never calls something profitable from win rate alone: PROFIT_FACTOR,
    EXPECTANCY, and NET_PNL are all reported alongside WIN_RATE, and
    basic_edge_gate() requires all three of PROFIT_FACTOR/EXPECTANCY/NET_PNL,
    not win rate, to PASS.
    """
    trades = result.trades
    total_trades = len(trades)

    if total_trades == 0:
        return {
            "TOTAL_TRADES": 0,
            "WIN_RATE": 0.0,
            "GROSS_PROFIT": 0.0,
            "GROSS_LOSS": 0.0,
            "PROFIT_FACTOR": 0.0,
            "EXPECTANCY_PER_TRADE": 0.0,
            "EXPECTANCY_R": 0.0,
            "NET_PNL": 0.0,
            "RETURN_PCT": 0.0,
            "MAX_DRAWDOWN": 0.0,
            "MAX_DRAWDOWN_PCT": 0.0,
            "AVG_WIN": 0.0,
            "AVG_LOSS": 0.0,
            "AVG_R_MULTIPLE": 0.0,
            "TRADES_PER_DAY": 0.0,
            "TRADES_PER_MONTH": 0.0,
            "FEES_TOTAL": 0.0,
            "SLIPPAGE_TOTAL": 0.0,
        }

    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]

    gross_profit = sum(t.gross_pnl for t in trades if t.gross_pnl > 0)
    gross_loss = sum(t.gross_pnl for t in trades if t.gross_pnl < 0)  # negative
    net_pnl = sum(t.net_pnl for t in trades)
    fees_total = sum(t.fees for t in trades)
    slippage_total = sum(t.slippage for t in trades)

    profit_factor = (gross_profit / abs(gross_loss)) if gross_loss != 0 else (
        float("inf") if gross_profit > 0 else 0.0
    )

    equity_curve = result.equity_curve
    peak = equity_curve[0][1] if equity_curve else initial_equity
    max_dd = 0.0
    max_dd_pct = 0.0
    for _, eq in equity_curve:
        peak = max(peak, eq)
        dd = peak - eq
        dd_pct = (dd / peak * 100.0) if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
        max_dd_pct = max(max_dd_pct, dd_pct)

    span_days = max((end_ts - start_ts) / 86_400_000.0, 1e-9)
    span_months = span_days / 30.4375

    metrics = {
        "TOTAL_TRADES": total_trades,
        "WIN_RATE": len(wins) / total_trades,
        "GROSS_PROFIT": gross_profit,
        "GROSS_LOSS": gross_loss,
        "PROFIT_FACTOR": profit_factor,
        "EXPECTANCY_PER_TRADE": net_pnl / total_trades,
        "EXPECTANCY_R": sum(t.r_multiple for t in trades) / total_trades,
        "NET_PNL": net_pnl,
        "RETURN_PCT": (net_pnl / initial_equity) * 100.0,
        "MAX_DRAWDOWN": max_dd,
        "MAX_DRAWDOWN_PCT": max_dd_pct,
        "AVG_WIN": (sum(t.net_pnl for t in wins) / len(wins)) if wins else 0.0,
        "AVG_LOSS": (sum(t.net_pnl for t in losses) / len(losses)) if losses else 0.0,
        "AVG_R_MULTIPLE": sum(t.r_multiple for t in trades) / total_trades,
        "TRADES_PER_DAY": total_trades / span_days,
        "TRADES_PER_MONTH": total_trades / span_months,
        "FEES_TOTAL": fees_total,
        "SLIPPAGE_TOTAL": slippage_total,
    }
    return metrics


def basic_edge_gate(metrics: dict) -> str:
    """PASS only if, after realistic costs: PROFIT_FACTOR > 1.0 AND
    EXPECTANCY_PER_TRADE > 0 AND NET_PNL > 0. This is a MINIMUM bar, not proof
    of robustness -- it says nothing about overfitting, regime dependence, or
    sample size."""
    if metrics.get("TOTAL_TRADES", 0) == 0:
        return "FAIL"
    if (
        metrics["PROFIT_FACTOR"] > 1.0
        and metrics["EXPECTANCY_PER_TRADE"] > 0
        and metrics["NET_PNL"] > 0
    ):
        return "PASS"
    return "FAIL"
