"""Frozen decision model for the controlled same-symbol long grid pilot.

This module is deliberately transport-free. It cannot place, cancel, or close
orders; the runner may therefore use it in SHADOW with a client object that has
no private order methods at all.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from enum import StrEnum
import math
from typing import Any, Iterable


class GridRegime(StrEnum):
    ALLOWED = "GRID_ALLOWED"
    PAUSED_ECONOMICS = "GRID_PAUSED_ECONOMICS"
    PAUSED_SIZE = "GRID_PAUSED_SIZE"
    PAUSED_TREND = "GRID_PAUSED_TREND"
    PAUSED_VOLATILITY = "GRID_PAUSED_VOLATILITY"
    PAUSED_SPREAD = "GRID_PAUSED_SPREAD"


@dataclass(frozen=True, slots=True)
class GridEconomics:
    maker_fee_bps: float
    roundtrip_fee_bps: float
    spread_bps: float
    drag_bps: float
    margin_bps: float
    hurdle_bps: float
    minimum_gross_capture_bps: float
    gross_capture_bps: float
    expected_net_capture_bps: float
    economic_gate_passed: bool


@dataclass(frozen=True, slots=True)
class GridLevel:
    index: int
    entry_price: float
    take_profit_price: float
    notional_usdt: float
    quantity: float
    exchange_min_notional_usdt: float
    strategy_min_notional_usdt: float
    minimum_executable_quantity: float
    client_oid: str


@dataclass(frozen=True, slots=True)
class GridDecision:
    strategy: str
    symbol: str
    candle_timestamp_ms: int
    score: float
    regime: GridRegime
    reason: str
    center: float
    atr: float
    atr_bps: float
    trend_bps: float
    hard_invalidation: float
    sizing_gate_passed: bool
    effective_min_level_notional_usdt: float
    max_grid_loss_usdt: float
    risk_cap_usdt: float
    min_equity_allocation_usdt: float
    min_equity_hard_risk_usdt: float
    levels: tuple[GridLevel, ...]
    economics: GridEconomics

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["regime"] = self.regime.value
        return payload


def _values(candles: Iterable[dict[str, Any]], key: str) -> list[float]:
    result: list[float] = []
    for row in candles:
        try:
            value = float(row[key])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            result.append(value)
    return result


def _ceil_increment(value: float, increment: float) -> float:
    raw = Decimal(str(value))
    step = Decimal(str(increment))
    return float((raw / step).to_integral_value(rounding=ROUND_CEILING) * step)


def _floor_increment(value: float, increment: float) -> float:
    raw = Decimal(str(value))
    step = Decimal(str(increment))
    return float((raw / step).to_integral_value(rounding=ROUND_DOWN) * step)


def ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    result = values[0]
    for value in values[1:]:
        result = (alpha * value) + ((1.0 - alpha) * result)
    return result


def atr(candles: list[dict[str, Any]], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    rows = candles[-(period + 1):]
    ranges: list[float] = []
    for previous, current in zip(rows, rows[1:]):
        try:
            high = float(current["high"])
            low = float(current["low"])
            previous_close = float(previous["close"])
        except (KeyError, TypeError, ValueError):
            continue
        ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return sum(ranges) / len(ranges) if ranges else 0.0


def rolling_vwap(candles: list[dict[str, Any]], period: int = 24) -> float:
    numerator = 0.0
    denominator = 0.0
    for row in candles[-period:]:
        try:
            typical = (float(row["high"]) + float(row["low"]) + float(row["close"])) / 3.0
            volume = float(row["volume"])
        except (KeyError, TypeError, ValueError):
            continue
        if typical > 0 and volume > 0:
            numerator += typical * volume
            denominator += volume
    return numerator / denominator if denominator else 0.0


def deterministic_score(
    *, atr_bps: float, spread_bps: float, depth_usdt: float, trend_bps: float,
    min_atr_bps: float, max_atr_bps: float, max_spread_bps: float,
    min_depth_usdt: float, max_trend_bps: float,
) -> float:
    vol_mid = (min_atr_bps + max_atr_bps) / 2.0
    vol_half = max((max_atr_bps - min_atr_bps) / 2.0, 1.0)
    volatility = max(0.0, 30.0 * (1.0 - abs(atr_bps - vol_mid) / vol_half))
    liquidity = min(max(depth_usdt / max(min_depth_usdt, 1.0), 0.0), 1.0) * 25.0
    spread = max(0.0, 20.0 * (1.0 - spread_bps / max(max_spread_bps, 0.01)))
    trend = max(0.0, 15.0 * (1.0 - trend_bps / max(max_trend_bps, 0.01)))
    excursion = min(max(atr_bps / max(min_atr_bps * 2.0, 1.0), 0.0), 1.0) * 10.0
    return round(volatility + liquidity + spread + trend + excursion, 4)


def build_grid_decision(
    *, symbol: str, candles_5m: list[dict[str, Any]], candles_15m: list[dict[str, Any]],
    candles_1h: list[dict[str, Any]], orderbook: dict[str, Any], maker_fee_rate: float,
    equity_usdt: float, settings: Any, exchange_min_trade_quantity: float,
    exchange_size_increment: float, exchange_min_notional_usdt: float,
    stale: bool = False,
) -> GridDecision:
    if len(candles_5m) < 30 or len(candles_15m) < 30 or len(candles_1h) < 30:
        raise ValueError("dynamic_grid_v1 requires at least 30 candles on 5m, 15m, and 1h")
    closes_5m = _values(candles_5m, "close")
    closes_15m = _values(candles_15m, "close")
    closes_1h = _values(candles_1h, "close")
    if not closes_5m or not closes_15m or not closes_1h:
        raise ValueError("dynamic_grid_v1 candle closes are invalid")

    last = closes_5m[-1]
    atr_value = atr(candles_5m)
    atr_bps = atr_value / last * 10_000.0
    vwap = rolling_vwap(candles_5m)
    center = (vwap + ema(closes_5m[-50:], 20)) / 2.0
    # Context is deliberately slower than management. The larger of 15m and
    # 1h EMA separation prevents a quiet 5m pullback masking a strong trend.
    trend_15m = abs(ema(closes_15m, 20) - ema(closes_15m, 50)) / last * 10_000.0
    trend_1h = abs(ema(closes_1h, 20) - ema(closes_1h, 50)) / last * 10_000.0
    trend_bps = max(trend_15m, trend_1h)
    spread_bps = float(orderbook.get("spread_bps") or 0.0)
    depth_usdt = float(orderbook.get("total_depth_notional") or 0.0)

    score = deterministic_score(
        atr_bps=atr_bps, spread_bps=spread_bps, depth_usdt=depth_usdt,
        trend_bps=trend_bps, min_atr_bps=settings.dynamic_grid_min_atr_bps,
        max_atr_bps=settings.dynamic_grid_max_atr_bps,
        max_spread_bps=settings.dynamic_grid_max_spread_bps,
        min_depth_usdt=settings.dynamic_grid_min_depth_usdt,
        max_trend_bps=settings.dynamic_grid_max_trend_bps,
    )
    if stale or atr_bps < settings.dynamic_grid_min_atr_bps or atr_bps > settings.dynamic_grid_max_atr_bps:
        regime, reason = GridRegime.PAUSED_VOLATILITY, "stale_or_abnormal_volatility"
    elif spread_bps <= 0 or spread_bps > settings.dynamic_grid_max_spread_bps:
        regime, reason = GridRegime.PAUSED_SPREAD, "spread_outside_limit"
    elif trend_bps > settings.dynamic_grid_max_trend_bps:
        regime, reason = GridRegime.PAUSED_TREND, "context_trend_kill_switch"
    elif depth_usdt < settings.dynamic_grid_min_depth_usdt or score < settings.dynamic_grid_min_score:
        regime, reason = GridRegime.PAUSED_SPREAD, "liquidity_or_score_below_gate"
    else:
        regime, reason = GridRegime.ALLOWED, "all_gates_pass"

    maker_fee_bps = abs(float(maker_fee_rate)) * 10_000.0
    roundtrip_bps = maker_fee_bps * 2.0
    hurdle = roundtrip_bps + float(settings.dynamic_grid_drag_bps) + float(settings.dynamic_grid_edge_margin_bps)
    minimum_gross_capture = hurdle * 2.0
    # ATR is the excursion budget; a target may not manufacture edge by being
    # pushed beyond the excursion the regime model actually expects.
    gross_capture = atr_bps * 0.60
    economics = GridEconomics(
        maker_fee_bps=maker_fee_bps,
        roundtrip_fee_bps=roundtrip_bps,
        spread_bps=spread_bps,
        drag_bps=float(settings.dynamic_grid_drag_bps),
        margin_bps=float(settings.dynamic_grid_edge_margin_bps),
        hurdle_bps=hurdle,
        minimum_gross_capture_bps=minimum_gross_capture,
        gross_capture_bps=gross_capture,
        expected_net_capture_bps=gross_capture - hurdle,
        economic_gate_passed=bool(
            maker_fee_bps > 0 and gross_capture >= minimum_gross_capture
        ),
    )
    if not economics.economic_gate_passed:
        regime, reason = (
            GridRegime.PAUSED_ECONOMICS,
            "gross_capture_below_2x_expected_all_in_cost",
        )

    hard_invalidation = center - (
        atr_value * float(settings.dynamic_grid_hard_invalidation_atr)
    )
    spacing = max(
        atr_value * 0.50,
        center * 0.0005,
        center * minimum_gross_capture / 10_000.0,
    )
    entry_prices = tuple(center - spacing * index for index in range(1, 4))
    if any(price <= 0 for price in entry_prices):
        raise ValueError("dynamic_grid_v1 produced a non-positive entry price")
    minimum_quantity = float(exchange_min_trade_quantity)
    size_increment = float(exchange_size_increment)
    minimum_notional = float(exchange_min_notional_usdt)
    if minimum_quantity <= 0 or size_increment <= 0 or minimum_notional <= 0:
        raise ValueError("dynamic_grid_v1 exchange minimum metadata is invalid")

    exchange_minimums: list[tuple[float, float]] = []
    required_minimums: list[tuple[float, float]] = []
    strategy_minimum = float(settings.dynamic_grid_min_level_notional_usdt)
    for entry_price in entry_prices:
        exchange_quantity = _ceil_increment(
            max(minimum_quantity, minimum_notional / entry_price), size_increment,
        )
        exchange_minimum = exchange_quantity * entry_price
        required_quantity = _ceil_increment(
            max(exchange_quantity, strategy_minimum / entry_price), size_increment,
        )
        exchange_minimums.append((exchange_quantity, exchange_minimum))
        required_minimums.append((required_quantity, required_quantity * entry_price))
    effective_minimum = max(
        strategy_minimum,
        *(exchange_minimum for _, exchange_minimum in exchange_minimums),
    )

    loss_fractions = tuple(
        max((entry_price - hard_invalidation) / entry_price, 0.0)
        for entry_price in entry_prices
    )
    loss_fraction_sum = sum(loss_fractions)
    risk_cap_usdt = float(equity_usdt) * float(settings.dynamic_grid_max_equity_risk_pct) / 100.0
    risk_capped_per_level = (
        risk_cap_usdt / loss_fraction_sum if loss_fraction_sum > 0 else 0.0
    )
    per_level = min(
        float(settings.dynamic_grid_max_notional_usdt) / 3.0,
        float(equity_usdt) * float(settings.dynamic_grid_max_equity_pct) / 100.0 / 3.0,
        float(settings.dynamic_grid_max_level_notional_usdt),
        risk_capped_per_level,
    )
    quantities = tuple(
        _floor_increment(per_level / entry_price, size_increment)
        for entry_price in entry_prices
    )
    notionals = tuple(
        quantity * entry_price
        for quantity, entry_price in zip(quantities, entry_prices)
    )
    max_grid_loss_usdt = sum(
        quantity * max(entry_price - hard_invalidation, 0.0)
        for quantity, entry_price in zip(quantities, entry_prices)
    )
    allocation_cap_usdt = (
        float(equity_usdt) * float(settings.dynamic_grid_max_equity_pct) / 100.0
    )
    sizing_gate_passed = bool(
        hard_invalidation < entry_prices[-1]
        and all(
            quantity >= required_quantity
            for quantity, (required_quantity, _) in zip(quantities, required_minimums)
        )
        and sum(notionals) <= allocation_cap_usdt + 1e-12
        and max_grid_loss_usdt <= risk_cap_usdt + 1e-12
    )
    if (
        not sizing_gate_passed
        and economics.economic_gate_passed
        and regime is GridRegime.ALLOWED
    ):
        regime, reason = GridRegime.PAUSED_SIZE, "three_level_minimum_exceeds_risk_caps"
    minimum_grid_notional = sum(notional for _, notional in required_minimums)
    minimum_grid_loss = sum(
        quantity * max(entry_price - hard_invalidation, 0.0)
        for (quantity, _), entry_price in zip(required_minimums, entry_prices)
    )
    min_equity_allocation = minimum_grid_notional / (
        float(settings.dynamic_grid_max_equity_pct) / 100.0
    )
    min_equity_hard_risk = max(minimum_grid_loss, 0.0) / (
        float(settings.dynamic_grid_max_equity_risk_pct) / 100.0
    )
    candle_timestamp_ms = int(candles_5m[-1].get("timestamp") or 0)
    grid_identity = f"dgv1-{symbol.lower()}-{candle_timestamp_ms}"
    levels = tuple(
        GridLevel(
            index=index,
            entry_price=entry_prices[index - 1],
            take_profit_price=entry_prices[index - 1] * (1.0 + gross_capture / 10_000.0),
            notional_usdt=notionals[index - 1],
            quantity=quantities[index - 1],
            exchange_min_notional_usdt=exchange_minimums[index - 1][1],
            strategy_min_notional_usdt=strategy_minimum,
            minimum_executable_quantity=required_minimums[index - 1][0],
            client_oid=f"{grid_identity}-l{index}-entry",
        )
        for index in range(1, 4)
    )
    return GridDecision(
        strategy="dynamic_grid_v1", symbol=symbol.upper(), candle_timestamp_ms=candle_timestamp_ms,
        score=score, regime=regime, reason=reason, center=center, atr=atr_value,
        atr_bps=atr_bps, trend_bps=trend_bps,
        hard_invalidation=hard_invalidation,
        sizing_gate_passed=sizing_gate_passed,
        effective_min_level_notional_usdt=effective_minimum,
        max_grid_loss_usdt=max_grid_loss_usdt,
        risk_cap_usdt=risk_cap_usdt,
        min_equity_allocation_usdt=min_equity_allocation,
        min_equity_hard_risk_usdt=min_equity_hard_risk,
        levels=levels, economics=economics,
    )


def reset_allowed(
    *, old_center: float, new_center: float, atr_value: float, flat: bool,
    working_orders: int, minutes_since_reset: float, settings: Any,
) -> bool:
    return bool(
        flat
        and working_orders == 0
        and atr_value > 0
        and abs(new_center - old_center) >= atr_value * float(settings.dynamic_grid_reset_atr)
        and minutes_since_reset >= float(settings.dynamic_grid_reset_cooldown_minutes)
    )


__all__ = [
    "GridDecision", "GridEconomics", "GridLevel", "GridRegime",
    "atr", "build_grid_decision", "deterministic_score", "ema", "reset_allowed", "rolling_vwap",
]
