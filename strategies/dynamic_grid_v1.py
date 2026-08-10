"""Frozen decision model for the controlled same-symbol long grid pilot.

This module is deliberately transport-free. It cannot place, cancel, or close
orders; the runner may therefore use it in SHADOW with a client object that has
no private order methods at all.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
import math
from typing import Any, Iterable


class GridRegime(StrEnum):
    ALLOWED = "GRID_ALLOWED"
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
    gross_capture_bps: float
    expected_net_capture_bps: float


@dataclass(frozen=True, slots=True)
class GridLevel:
    index: int
    entry_price: float
    take_profit_price: float
    notional_usdt: float
    quantity: float
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
    equity_usdt: float, settings: Any, stale: bool = False,
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
        gross_capture_bps=gross_capture,
        expected_net_capture_bps=gross_capture - hurdle,
    )
    if maker_fee_bps <= 0 or economics.expected_net_capture_bps <= 0:
        regime, reason = GridRegime.PAUSED_SPREAD, "authenticated_fee_hurdle_failed"

    total_notional = min(
        float(settings.dynamic_grid_max_notional_usdt),
        float(equity_usdt) * float(settings.dynamic_grid_max_equity_pct) / 100.0,
    )
    per_level = total_notional / 3.0
    if per_level < float(settings.dynamic_grid_min_level_notional_usdt):
        regime, reason = GridRegime.PAUSED_VOLATILITY, "minimum_practical_size_exceeds_cap"
    spacing = max(atr_value * 0.50, center * 0.0005)
    candle_timestamp_ms = int(candles_5m[-1].get("timestamp") or 0)
    grid_identity = f"dgv1-{symbol.lower()}-{candle_timestamp_ms}"
    levels = tuple(
        GridLevel(
            index=index,
            entry_price=center - spacing * index,
            take_profit_price=(center - spacing * index) * (1.0 + gross_capture / 10_000.0),
            notional_usdt=per_level,
            quantity=per_level / (center - spacing * index),
            client_oid=f"{grid_identity}-l{index}-entry",
        )
        for index in range(1, 4)
    )
    return GridDecision(
        strategy="dynamic_grid_v1", symbol=symbol.upper(), candle_timestamp_ms=candle_timestamp_ms,
        score=score, regime=regime, reason=reason, center=center, atr=atr_value,
        atr_bps=atr_bps, trend_bps=trend_bps,
        hard_invalidation=center - (atr_value * float(settings.dynamic_grid_hard_invalidation_atr)),
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
