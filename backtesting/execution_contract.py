from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Sequence

from clients.schemas import Candle


@dataclass(frozen=True)
class BacktestExecutionConfig:
    """Minimal deterministic execution assumptions used by historical tests."""

    entry_type: str = "MARKET"
    limit_expiration_candles: int = 3
    maker_fee_bps: float = 2.0
    taker_fee_bps: float = 6.0
    spread_bps: float = 4.0
    entry_slippage_bps: float = 2.0
    exit_slippage_bps: float = 2.0
    same_candle_policy: str = "CONSERVATIVE"
    starting_equity: float = 1_000.0
    risk_per_trade_pct: float = 0.75
    leverage_cap: float = 5.0
    maximum_notional: float = 35.0
    available_equity_notional_pct: float = 100.0
    minimum_quantity: float = 0.001
    minimum_notional: float = 10.0
    quantity_step: float = 0.001
    price_tick: float = 0.0001
    tp1_partial_pct: float = 40.0
    break_even_policy: str = "FEE_ADJUSTED"
    break_even_fee_buffer_bps: float = 12.0
    max_hold_candles: int = 6

    @classmethod
    def from_settings(cls, settings) -> "BacktestExecutionConfig":
        return cls(**{
            field: getattr(settings, f"backtest_{field}", default)
            for field, default in asdict(cls()).items()
        })

    def validate(self) -> None:
        if self.entry_type.upper() not in {"MARKET", "LIMIT"}:
            raise ValueError("backtest entry_type must be MARKET or LIMIT")
        if self.same_candle_policy.upper() not in {"STOP_FIRST", "TARGET_FIRST", "CONSERVATIVE"}:
            raise ValueError("invalid same-candle policy")
        if self.break_even_policy.upper() not in {"ENTRY", "FEE_ADJUSTED", "NONE"}:
            raise ValueError("invalid break-even policy")
        for name in ("starting_equity", "risk_per_trade_pct", "leverage_cap", "quantity_step", "price_tick"):
            if float(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass
class ExecutionRecord:
    strategy: str
    symbol: str
    timeframe: str
    direction: str
    signal_timestamp: int
    requested_entry: float
    executed_entry: float = 0.0
    entry_type: str = "MARKET"
    fill_timestamp: int | None = None
    fill_status: str = "PENDING"
    spread_cost: float = 0.0
    entry_slippage: float = 0.0
    entry_fee: float = 0.0
    initial_quantity: float = 0.0
    raw_quantity: float = 0.0
    initial_stop: float = 0.0
    tp1_price: float = 0.0
    tp1_quantity: float = 0.0
    tp1_executed_price: float = 0.0
    tp1_fee: float = 0.0
    stop_after_tp1: float = 0.0
    final_exit_price: float = 0.0
    final_exit_reason: str = ""
    exit_slippage: float = 0.0
    final_exit_fee: float = 0.0
    gross_pnl: float = 0.0
    total_fees: float = 0.0
    net_pnl: float = 0.0
    r_multiple: float = 0.0
    equity_before: float = 0.0
    equity_after: float = 0.0
    risk_budget: float = 0.0
    notional: float = 0.0
    intrabar_ambiguous: bool = False
    intrabar_policy_used: str = "CONSERVATIVE"
    rejection_reason: str = ""
    candles_held: int = 0
    timed_exit: bool = False


def _step(value: float, step: float, rounding: str) -> float:
    units = Decimal(str(value)) / Decimal(str(step))
    mode = ROUND_CEILING if rounding == "UP" else ROUND_FLOOR
    return float(units.to_integral_value(rounding=mode) * Decimal(str(step)))


def _adverse_price(reference: float, direction: str, bps: float, tick: float, *, entry: bool) -> float:
    buy = (direction == "LONG") == entry
    raw = reference * (1.0 + bps / 10_000.0) if buy else reference * (1.0 - bps / 10_000.0)
    return _step(raw, tick, "UP" if buy else "DOWN")


def _gross(direction: str, entry: float, exit_price: float, quantity: float) -> float:
    return (exit_price - entry) * quantity if direction == "LONG" else (entry - exit_price) * quantity


class BacktestExecutionContract:
    def __init__(self, config: BacktestExecutionConfig) -> None:
        config.validate()
        self.config = config

    def execute(
        self, *, strategy: str, symbol: str, timeframe: str, direction: str,
        signal_timestamp: int, requested_entry: float, stop: float, targets: Sequence[float],
        candles: Sequence[Candle], equity: float,
    ) -> ExecutionRecord:
        cfg = self.config
        direction = direction.upper()
        record = ExecutionRecord(
            strategy=strategy, symbol=symbol, timeframe=timeframe, direction=direction,
            signal_timestamp=signal_timestamp, requested_entry=requested_entry,
            entry_type=cfg.entry_type.upper(), initial_stop=stop,
            tp1_price=float(targets[0]) if targets else 0.0, equity_before=equity,
            equity_after=equity, intrabar_policy_used=cfg.same_candle_policy.upper(),
        )
        if direction not in {"LONG", "SHORT"} or requested_entry <= 0 or stop <= 0 or not targets:
            record.fill_status = "REJECTED"
            record.rejection_reason = "INVALID_ORDER_GEOMETRY"
            return record

        fill_index: int | None = None
        spread_per_unit = 0.0
        entry_slippage_per_unit = 0.0
        if cfg.entry_type.upper() == "MARKET":
            if not candles:
                record.fill_status = "UNFILLED"
                record.rejection_reason = "NO_ENTRY_CANDLE"
                return record
            reference = float(candles[0].open)
            record.executed_entry = _adverse_price(
                reference, direction, cfg.spread_bps + cfg.entry_slippage_bps,
                cfg.price_tick, entry=True,
            )
            spread_only = _adverse_price(reference, direction, cfg.spread_bps, cfg.price_tick, entry=True)
            spread_per_unit = abs(spread_only - reference)
            entry_slippage_per_unit = abs(record.executed_entry - spread_only)
            fill_index = 0
        else:
            requested = _step(requested_entry, cfg.price_tick, "DOWN" if direction == "LONG" else "UP")
            if abs(requested - requested_entry) > 1e-12:
                record.fill_status = "REJECTED"
                record.rejection_reason = "INVALID_PRICE_STEP"
                return record
            for index, candle in enumerate(candles[: max(0, cfg.limit_expiration_candles)]):
                touched = float(candle.low) <= requested if direction == "LONG" else float(candle.high) >= requested
                if touched:
                    record.executed_entry = requested
                    fill_index = index
                    break
            if fill_index is None:
                record.fill_status = "UNFILLED"
                record.rejection_reason = "LIMIT_EXPIRED"
                return record

        record.fill_status = "FILLED"
        record.fill_timestamp = int(candles[fill_index].timestamp_ms)
        risk_per_unit = abs(record.executed_entry - stop)
        if risk_per_unit <= 0:
            record.fill_status = "REJECTED"
            record.rejection_reason = "INVALID_STOP_DISTANCE"
            return record
        record.risk_budget = equity * cfg.risk_per_trade_pct / 100.0
        record.raw_quantity = record.risk_budget / risk_per_unit
        cap_notional = min(
            equity * cfg.leverage_cap,
            equity * cfg.available_equity_notional_pct / 100.0,
            cfg.maximum_notional if cfg.maximum_notional > 0 else float("inf"),
        )
        capped_quantity = min(record.raw_quantity, cap_notional / record.executed_entry)
        record.initial_quantity = _step(capped_quantity, cfg.quantity_step, "DOWN")
        record.notional = record.initial_quantity * record.executed_entry
        if record.initial_quantity <= 0:
            record.fill_status = "REJECTED"
            record.rejection_reason = "ZERO_EXECUTABLE_QTY"
            return record
        if record.initial_quantity < cfg.minimum_quantity:
            record.fill_status = "REJECTED"
            record.rejection_reason = "BELOW_MIN_QTY"
            return record
        if record.notional < cfg.minimum_notional:
            record.fill_status = "REJECTED"
            record.rejection_reason = "BELOW_MIN_NOTIONAL"
            return record

        record.spread_cost = spread_per_unit * record.initial_quantity
        record.entry_slippage = entry_slippage_per_unit * record.initial_quantity

        entry_fee_bps = cfg.maker_fee_bps if cfg.entry_type.upper() == "LIMIT" else cfg.taker_fee_bps
        record.entry_fee = record.notional * entry_fee_bps / 10_000.0
        remaining = record.initial_quantity
        stop_price = stop
        exit_candles = candles[fill_index + 1 : fill_index + 1 + cfg.max_hold_candles]
        tp1_done = False
        gross = 0.0
        exit_fees = 0.0

        for held, candle in enumerate(exit_candles, start=1):
            record.candles_held = held
            target_index = 1 if tp1_done and len(targets) > 1 else 0
            target = float(targets[target_index])
            stop_hit = float(candle.low) <= stop_price if direction == "LONG" else float(candle.high) >= stop_price
            target_hit = float(candle.high) >= target if direction == "LONG" else float(candle.low) <= target
            if stop_hit and target_hit:
                record.intrabar_ambiguous = True
                choose_target = cfg.same_candle_policy.upper() == "TARGET_FIRST"
            else:
                choose_target = target_hit

            if stop_hit and not choose_target:
                executed = _adverse_price(stop_price, direction, cfg.exit_slippage_bps + cfg.spread_bps, cfg.price_tick, entry=False)
                fee = executed * remaining * cfg.taker_fee_bps / 10_000.0
                gross += _gross(direction, record.executed_entry, executed, remaining)
                exit_fees += fee
                record.final_exit_price = executed
                record.final_exit_fee = fee
                record.exit_slippage += abs(executed - stop_price) * remaining
                record.final_exit_reason = "BREAK_EVEN_STOP" if tp1_done else "STOP_LOSS"
                remaining = 0.0
                break

            if choose_target:
                executed = _adverse_price(target, direction, cfg.exit_slippage_bps + cfg.spread_bps, cfg.price_tick, entry=False)
                if not tp1_done and len(targets) > 1:
                    quantity = _step(record.initial_quantity * cfg.tp1_partial_pct / 100.0, cfg.quantity_step, "DOWN")
                    quantity = min(quantity, remaining)
                    fee = executed * quantity * cfg.taker_fee_bps / 10_000.0
                    gross += _gross(direction, record.executed_entry, executed, quantity)
                    exit_fees += fee
                    remaining -= quantity
                    record.tp1_quantity = quantity
                    record.tp1_executed_price = executed
                    record.tp1_fee = fee
                    record.exit_slippage += abs(executed - target) * quantity
                    tp1_done = True
                    if cfg.break_even_policy.upper() == "FEE_ADJUSTED":
                        shift = cfg.break_even_fee_buffer_bps / 10_000.0
                        stop_price = record.executed_entry * (1 + shift if direction == "LONG" else 1 - shift)
                    elif cfg.break_even_policy.upper() == "ENTRY":
                        stop_price = record.executed_entry
                    record.stop_after_tp1 = stop_price
                    continue
                fee = executed * remaining * cfg.taker_fee_bps / 10_000.0
                gross += _gross(direction, record.executed_entry, executed, remaining)
                exit_fees += fee
                record.final_exit_price = executed
                record.final_exit_fee = fee
                record.exit_slippage += abs(executed - target) * remaining
                record.final_exit_reason = "FINAL_TARGET" if tp1_done else "TAKE_PROFIT"
                remaining = 0.0
                break

            if held == len(exit_candles):
                reference = float(candle.close)
                executed = _adverse_price(reference, direction, cfg.exit_slippage_bps + cfg.spread_bps, cfg.price_tick, entry=False)
                fee = executed * remaining * cfg.taker_fee_bps / 10_000.0
                gross += _gross(direction, record.executed_entry, executed, remaining)
                exit_fees += fee
                record.final_exit_price = executed
                record.final_exit_fee = fee
                record.exit_slippage += abs(executed - reference) * remaining
                record.final_exit_reason = "TIME_EXIT"
                record.timed_exit = True
                remaining = 0.0

        if remaining > 1e-12:
            record.final_exit_reason = "OPEN_AT_DATA_END"
            return record
        record.gross_pnl = gross
        record.total_fees = record.entry_fee + exit_fees
        record.net_pnl = gross - record.total_fees
        record.equity_after = equity + record.net_pnl
        record.r_multiple = record.net_pnl / record.risk_budget if record.risk_budget else 0.0
        return record
