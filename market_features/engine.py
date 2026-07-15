from __future__ import annotations

from dataclasses import dataclass, field
import math
from statistics import mean
from typing import Any

from clients.schemas import Candle, ContractSpec, MarketSnapshot, TimeframeSnapshot
from market_data.breakout_engine import BreakoutEngine
from market_data.entry_quality import EntryQualityAnalyzer
from market_data.liquidity_heatmap import build_liquidity_heatmap
from market_data.orderbook_analyzer import OrderbookAnalyzer
from market_data.volatility_engine import VolatilityEngine


INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}


class CandleContractError(ValueError):
    pass


@dataclass(frozen=True)
class LiveMarketContext:
    """Typed raw/live inputs; all derived fields are produced by this module."""

    orderbook: dict[str, Any] | None = None
    orderbook_context: dict[str, Any] | None = None
    liquidity: dict[str, Any] | None = None
    spread_bps: float | None = None  # historical trusted spread without an orderbook
    htf_context: dict[str, str] | None = None
    contract: ContractSpec | None = None
    risk_off_flags: dict[str, bool] | None = None
    entry_quality: dict[str, Any] | None = None
    pressure: dict[str, Any] | None = None
    structure: dict[str, Any] | None = None
    extra_notes: tuple[str, ...] = field(default_factory=tuple)


FeatureInputs = LiveMarketContext


def interval_ms(granularity: str) -> int:
    value = INTERVAL_MS.get(str(granularity or "").lower())
    if value is None:
        raise CandleContractError(f"unknown timeframe: {granularity}")
    return value


def select_closed_candles(candles: list[Candle], granularity: str, as_of_timestamp_ms: int) -> list[Candle]:
    step = interval_ms(granularity)
    if not isinstance(as_of_timestamp_ms, int) or as_of_timestamp_ms <= 0:
        raise CandleContractError("invalid as-of timestamp")
    values = list(candles or [])
    if not values:
        raise CandleContractError("empty candle series")
    seen: set[int] = set()
    previous: int | None = None
    for candle in values:
        numbers = (candle.timestamp_ms, candle.open, candle.high, candle.low, candle.close, candle.volume_base)
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in numbers):
            raise CandleContractError("non-finite candle data")
        if candle.timestamp_ms in seen:
            raise CandleContractError("duplicate candle timestamp")
        seen.add(candle.timestamp_ms)
        if previous is not None and candle.timestamp_ms - previous != step:
            raise CandleContractError("out-of-order candle or gap")
        previous = candle.timestamp_ms
        if min(candle.open, candle.high, candle.low, candle.close) <= 0 or candle.volume_base < 0:
            raise CandleContractError("invalid OHLCV")
        if candle.high < max(candle.open, candle.close) or candle.low > min(candle.open, candle.close):
            raise CandleContractError("invalid OHLC")
    closed = [c for c in values if c.timestamp_ms + step <= as_of_timestamp_ms]
    if not closed:
        raise CandleContractError("no demonstrably closed candle")
    if closed != values[:len(closed)]:
        raise CandleContractError("ambiguous candle closure order")
    return closed


def ema(values: list[float], period: int) -> float:
    if not values:
        raise CandleContractError("empty EMA input")
    if period <= 0:
        raise CandleContractError("EMA period must be positive")
    alpha = 2.0 / (period + 1.0)
    result = float(values[0])
    for value in values[1:]:
        result = float(value) * alpha + result * (1.0 - alpha)
    return result


def build_timeframe_snapshot(symbol: str, granularity: str, candles: list[Candle], as_of_timestamp_ms: int) -> TimeframeSnapshot:
    closed = select_closed_candles(candles, granularity, as_of_timestamp_ms)
    if len(closed) < 20:
        raise CandleContractError("fewer than 20 closed candles")
    closes = [c.close for c in closed]
    latest = closed[-1]
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50) if len(closes) >= 50 else ema20
    history = closed[-21:-1]
    average_volume = mean(c.volume_base for c in history) if history else 0.0
    trend = "bullish" if latest.close > ema20 > ema50 else "bearish" if latest.close < ema20 < ema50 else "mixed"
    recent = closed[-20:]
    return TimeframeSnapshot(
        symbol=symbol.upper(), granularity=granularity, latest_close=latest.close,
        change_pct=(latest.close - closed[-2].close) / closed[-2].close * 100 if len(closed) > 1 else 0.0,
        range_pct=(max(c.high for c in recent) - min(c.low for c in recent)) / latest.close * 100,
        volume_ratio_20=latest.volume_base / average_volume if average_volume else 0.0,
        ema20=ema20, ema50=ema50, trend=trend, candles=closed,
        closed_candle_timestamp_ms=latest.timestamp_ms, as_of_timestamp_ms=as_of_timestamp_ms,
    )


def closed_window(timeframe: TimeframeSnapshot, size: int | None = None) -> list[Candle]:
    """Engine-certified candles; the newest closed candle is always index -1."""
    candles = list(timeframe.candles or [])
    if not candles or timeframe.closed_candle_timestamp_ms != candles[-1].timestamp_ms:
        raise CandleContractError("timeframe lacks a valid closed-candle marker")
    if timeframe.as_of_timestamp_ms < candles[-1].timestamp_ms + interval_ms(timeframe.granularity):
        raise CandleContractError("last candle is not closed at timeframe as-of")
    return candles[-size:] if size is not None else candles


def latest_closed_candle(timeframe: TimeframeSnapshot) -> Candle:
    return closed_candle_at_offset(timeframe, 0)


def closed_candle_at_offset(timeframe: TimeframeSnapshot, offset: int) -> Candle:
    """Zero is latest closed; one/two are the preceding closed candles."""
    if offset < 0:
        raise CandleContractError("closed-candle offset must be non-negative")
    candles = closed_window(timeframe)
    if len(candles) <= offset:
        raise CandleContractError("insufficient closed-candle history")
    return candles[len(candles) - 1 - offset]


def previous_closed_candle(timeframe: TimeframeSnapshot, offset: int = 1) -> Candle:
    if offset < 1:
        raise CandleContractError("previous offset must be positive")
    return closed_candle_at_offset(timeframe, offset)


def aggregate_candles(candles: list[Candle], source: str, target: str, as_of_timestamp_ms: int) -> list[Candle]:
    source_ms, target_ms = interval_ms(source), interval_ms(target)
    if target_ms % source_ms:
        raise CandleContractError("non-integral aggregation")
    groups: dict[int, list[Candle]] = {}
    for candle in select_closed_candles(candles, source, as_of_timestamp_ms):
        bucket = candle.timestamp_ms - candle.timestamp_ms % target_ms
        groups.setdefault(bucket, []).append(candle)
    expected = target_ms // source_ms
    result: list[Candle] = []
    for timestamp, group in sorted(groups.items()):
        if len(group) != expected or timestamp + target_ms > as_of_timestamp_ms:
            continue
        result.append(Candle(timestamp, group[0].open, max(c.high for c in group), min(c.low for c in group), group[-1].close, sum(c.volume_base for c in group), sum(c.volume_quote or 0.0 for c in group)))
    return result


def alignment(primary: TimeframeSnapshot, confirmation: TimeframeSnapshot) -> str:
    if primary.trend == confirmation.trend == "bullish": return "aligned_bullish"
    if primary.trend == confirmation.trend == "bearish": return "aligned_bearish"
    if {primary.trend, confirmation.trend} <= {"bullish", "bearish"}: return "conflicted"
    return "mixed"


def volatility_rank(atr_percent: float | int | None) -> float:
    atr = float(atr_percent or 0.0)
    return max(0.0, min(100.0, atr * 100.0 if atr <= 5.0 else atr)) if atr > 0 else 0.0


def score_hint(primary: TimeframeSnapshot, confirmation: TimeframeSnapshot, alignment: str, volatility: float, contract: ContractSpec | None = None, spread_bps: float | None = None) -> float:
    score = 50.0 + (18.0 if alignment.startswith("aligned") else -12.0 if alignment == "conflicted" else 0.0)
    score += 6.0 if primary.trend in {"bullish", "bearish"} else 0.0
    score += 4.0 if confirmation.trend in {"bullish", "bearish"} else 0.0
    score += 8.0 if 15 <= volatility <= 80 else -6.0 if volatility > 90 else 0.0
    score += 6.0 if primary.volume_ratio_20 >= 1.2 else -6.0 if 0 < primary.volume_ratio_20 < .7 else 0.0
    if spread_bps is not None:
        score += -10.0 if spread_bps > 18 else 4.0 if 0 < spread_bps <= 8 else 0.0
    volume_24h = float(getattr(contract, "volume_24h_usdt", 0.0) or 0.0)
    score += 6.0 if volume_24h >= 100_000_000 else -8.0 if 0 < volume_24h < 10_000_000 else 0.0
    return round(max(0.0, min(100.0, score)), 1)


def build_market_snapshot(symbol: str, primary_candles: list[Candle], confirmation_candles: list[Candle], *, as_of_timestamp_ms: int, primary_granularity: str = "15m", confirmation_granularity: str = "1h", inputs: LiveMarketContext | None = None) -> MarketSnapshot:
    inputs = inputs or LiveMarketContext()
    primary = build_timeframe_snapshot(symbol, primary_granularity, primary_candles, as_of_timestamp_ms)
    confirmation = build_timeframe_snapshot(symbol, confirmation_granularity, confirmation_candles, as_of_timestamp_ms)
    alignment_value = alignment(primary, confirmation)
    volatility = VolatilityEngine().analyze(primary.candles)
    breakout = BreakoutEngine().analyze(primary.candles)
    orderbook = inputs.orderbook
    orderbook_context = OrderbookAnalyzer().analyze(orderbook) if orderbook is not None else None
    spread_bps = float(orderbook_context.get("spread_bps", 0.0)) if orderbook_context is not None else inputs.spread_bps
    liquidity = inputs.liquidity if inputs.liquidity is not None else build_liquidity_heatmap(orderbook) if orderbook is not None else None
    latest = primary.candles[-1]
    latest_candle = {"open": latest.open, "high": latest.high, "low": latest.low, "close": latest.close}
    entry_quality = {
        direction: EntryQualityAnalyzer().analyze(direction=direction, latest_candle=latest_candle, orderbook_context=orderbook_context)
        for direction in ("LONG", "SHORT")
    }
    volatility_rank_value = volatility_rank(volatility.get("atr_percent"))
    score_hint_value = score_hint(primary, confirmation, alignment_value, volatility_rank_value, inputs.contract, spread_bps)
    score_hint_value = max(0.0, min(100.0, score_hint_value + volatility_rank_value * .15))
    notes = [
        f"primary_trend={primary.trend}", f"confirmation_trend={confirmation.trend}", f"alignment={alignment_value}",
        f"volatility_rank={volatility_rank_value:.2f}", f"primary_tf={primary.granularity}", f"confirmation_tf={confirmation.granularity}",
        f"latest_close={primary.latest_close:.8f}", f"primary_ema20={primary.ema20:.8f}", f"primary_ema50={primary.ema50:.8f}",
        f"volume_ratio_20={primary.volume_ratio_20:.4f}", f"range_pct={primary.range_pct:.4f}",
        f"volatility_context compression={volatility.get('compression')} expansion_prob={volatility.get('expansion_probability')} pressure={volatility.get('breakout_pressure')}",
        f"origin_distance_score={float(breakout.get('origin_distance_score', 0) or 0):.2f}",
        f"impulse_freshness_score={float(breakout.get('impulse_freshness_score', 100) or 100):.2f}",
        f"expansion_exhaustion_score={float(breakout.get('expansion_exhaustion_score', 0) or 0):.2f}",
        f"breakout_context ready={breakout.get('breakout_ready')} pressure_score={breakout.get('pressure_score')} direction={breakout.get('direction')}",
        f"breakout_ready={str(bool(breakout.get('breakout_ready'))).lower()}", f"breakout_direction={str(breakout.get('direction') or 'unknown').lower()}",
    ]
    notes.extend(f"volatility_note={note}" for note in volatility.get("notes", []))
    for note in breakout.get("notes", []): notes.extend((str(note), f"breakout_note={note}"))
    notes.extend((
        f"range_tightening={str(bool(breakout.get('tightening'))).lower()}",
        f"higher_lows_building={str(bool(breakout.get('higher_lows'))).lower()}",
        f"lower_highs_building={str(bool(breakout.get('lower_highs'))).lower()}",
        f"closes_pressing_highs={str(bool(breakout.get('close_near_high'))).lower()}",
        f"closes_pressing_lows={str(bool(breakout.get('close_near_low'))).lower()}",
        f"breakout_structure_detected={str(bool(breakout.get('diag_structure_detected'))).lower()}",
        f"spread_bps={spread_bps:.3f}" if spread_bps is not None else "spread_available=false",
    ))
    if orderbook_context is not None:
        total_depth = float(orderbook_context.get("total_depth_notional", 0.0) or 0.0)
        liquidity_ok = bool(float(spread_bps or 0.0) <= 8.0 and total_depth >= 25_000.0)
        notes.extend(("orderbook_available=true", f"orderbook_risk_off={str(not liquidity_ok).lower()}", f"orderbook_liquidity_ok={str(liquidity_ok).lower()}", f"orderbook_total_depth_usdt={total_depth:.2f}", f"orderbook_imbalance={float(orderbook_context.get('imbalance', 0.0)):+.3f}", f"orderbook_bias={orderbook_context.get('continuation_bias', 'neutral')}"))
        if not liquidity_ok: notes.append("risk_off_reason=orderbook_spread_or_depth")
    else:
        notes.extend(("orderbook_available=false", "orderbook_risk_off=true", "risk_off_reason=orderbook_context_unavailable"))
    if liquidity and liquidity.get("data_ok"):
        notes.extend((f"liq_above_score={float(liquidity.get('liquidity_above_score', 0)):.1f}", f"liq_below_score={float(liquidity.get('liquidity_below_score', 0)):.1f}", f"liq_magnet={liquidity.get('liquidity_magnet_direction', 'NEUTRAL')}", f"liq_risk_zone={str(bool(liquidity.get('liquidity_risk_zone'))).lower()}"))
    long_quality, short_quality = entry_quality["LONG"], entry_quality["SHORT"]
    notes.extend((f"entry_quality long={long_quality.get('entry_quality_score')} short={short_quality.get('entry_quality_score')} close_pos={long_quality.get('close_position')}", f"entry_quality_long={long_quality.get('entry_quality_score')}", f"entry_quality_short={short_quality.get('entry_quality_score')}", f"close_position={long_quality.get('close_position')}"))
    if inputs.htf_context:
        notes.extend(f"{key}={inputs.htf_context[key]}" for key in ("htf_regime_1d", "htf_regime_4h", "htf_regime") if key in inputs.htf_context)
    else: notes.append("htf_context_available=false")
    if inputs.contract is not None:
        notes.append(f"contract_volume_24h_usdt={float(inputs.contract.volume_24h_usdt or 0.0):.2f}")
        if inputs.contract.change_pct_24h is not None:
            notes.append(f"contract_change_pct_24h={float(inputs.contract.change_pct_24h):.4f}")
    notes.extend(inputs.extra_notes)
    risk_off = bool(orderbook_context is None or float(spread_bps or 0.0) > 8.0 or float((orderbook_context or {}).get("total_depth_notional", 0.0) or 0.0) < 25_000.0)
    live_context = LiveMarketContext(
        orderbook=orderbook, orderbook_context=orderbook_context, liquidity=liquidity,
        spread_bps=spread_bps, htf_context=inputs.htf_context, contract=inputs.contract,
        risk_off_flags={"market_risk_off": risk_off, "orderbook_unavailable": orderbook_context is None},
        entry_quality=entry_quality, pressure=breakout, structure=breakout,
        extra_notes=tuple(inputs.extra_notes),
    )
    return MarketSnapshot(
        symbol=symbol.upper(), contract=inputs.contract, primary=primary, confirmation=confirmation,
        alignment=alignment_value, score_hint=round(score_hint_value, 2), notes=notes,
        volatility_rank=round(volatility_rank_value, 2), context={"live": live_context, "volatility": volatility, "breakout": breakout, "pressure": breakout, "structure": breakout, "spread_bps": spread_bps, "spread_available": spread_bps is not None, "htf": inputs.htf_context, "orderbook": orderbook_context, "liquidity": liquidity, "entry_quality": entry_quality, "risk_off": risk_off, "instrument": inputs.contract},
        origin_distance_score=round(float(breakout.get("origin_distance_score", 0) or 0), 2),
        impulse_freshness_score=round(float(breakout.get("impulse_freshness_score", 100) or 100), 2),
        expansion_exhaustion_score=round(float(breakout.get("expansion_exhaustion_score", 0) or 0), 2),
    )
