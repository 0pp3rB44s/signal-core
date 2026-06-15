from __future__ import annotations

from statistics import mean
from typing import Any


class VolatilityEngine:
    """Detect volatility compression and breakout expansion probability."""

    def analyze(
        self,
        candles: list[Any],
    ) -> dict[str, Any]:

        if len(candles) < 20:
            return {
                "compression": False,
                "expansion_probability": 0.0,
                "breakout_pressure": "neutral",
                "notes": ["not enough candles"],
            }

        recent = candles[-10:]
        historical = candles[-20:-10]

        recent_ranges = [self._range(c) for c in recent]
        recent_wick_bias = self._wick_bias(recent)
        recent_volumes = [float(getattr(c, "volume_base", 0.0) or 0.0) for c in recent]
        historical_ranges = [self._range(c) for c in historical]

        recent_avg = mean(recent_ranges)
        historical_avg = mean(historical_ranges)
        latest_close = float(recent[-1].close)
        atr_percent = ((recent_avg / latest_close) * 100.0) if latest_close > 0 else 0.0

        compression_ratio = recent_avg / historical_avg if historical_avg > 0 else 1.0

        compression = compression_ratio < 0.75
        squeeze_quality = self._squeeze_quality(compression_ratio)
        volatility_regime = self._volatility_regime(compression_ratio)
        participation_score = self._participation_score(recent_volumes)

        bullish_closes = 0
        bearish_closes = 0

        for candle in recent:
            if candle.close > candle.open:
                bullish_closes += 1
            elif candle.close < candle.open:
                bearish_closes += 1

        pressure_delta = bullish_closes - bearish_closes

        if pressure_delta >= 4:
            breakout_pressure = "bullish"
        elif pressure_delta <= -4:
            breakout_pressure = "bearish"
        elif compression and abs(pressure_delta) <= 1:
            breakout_pressure = "balanced_compression"
        elif compression_ratio <= 0.55:
            breakout_pressure = "low_energy"
        elif compression_ratio >= 1.25:
            breakout_pressure = "chaotic_expansion"
        else:
            breakout_pressure = "neutral"

        expansion_probability = 50.0

        if compression:
            expansion_probability += 20

        expansion_probability += min(abs(pressure_delta) * 5, 25)

        if recent_avg > historical_avg:
            expansion_probability += 10

        expansion_probability += participation_score

        if recent_wick_bias == "bullish":
            expansion_probability += 8

        if recent_wick_bias == "bearish":
            expansion_probability += 8

        if breakout_pressure == "chaotic_expansion":
            expansion_probability -= 15

        expansion_probability = max(0.0, min(100.0, expansion_probability))

        notes: list[str] = []

        if compression:
            notes.append("volatility compression")
        notes.append(f"volatility_regime={volatility_regime}")
        notes.append(f"squeeze_quality={squeeze_quality}")
        notes.append(f"atr_percent={round(atr_percent, 4)}")
        notes.append(f"wick_bias={recent_wick_bias}")

        if participation_score >= 10:
            notes.append(f"participation_score={round(participation_score, 2)}")

        if breakout_pressure != "neutral":
            notes.append(f"breakout_pressure={breakout_pressure}")

        if expansion_probability >= 75:
            notes.append("high expansion probability")

        return {
            "compression": compression,
            "volatility_regime": volatility_regime,
            "squeeze_quality": squeeze_quality,
            "compression_ratio": round(compression_ratio, 4),
            "atr_percent": round(atr_percent, 4),
            "wick_bias": recent_wick_bias,
            "expansion_probability": round(expansion_probability, 2),
            "breakout_pressure": breakout_pressure,
            "participation_score": round(float(participation_score), 2),
            "confidence_score": round(max(0.0, min(expansion_probability, 100.0)), 2),
            "bullish_closes": bullish_closes,
            "bearish_closes": bearish_closes,
            "notes": notes,
        }

    @staticmethod
    def _wick_bias(candles: list[Any]) -> str:
        bullish_score = 0
        bearish_score = 0

        for candle in candles[-5:]:
            high = float(candle.high)
            low = float(candle.low)
            open_price = float(candle.open)
            close = float(candle.close)

            candle_range = max(high - low, 1e-9)

            upper_wick = high - max(open_price, close)
            lower_wick = min(open_price, close) - low

            upper_ratio = upper_wick / candle_range
            lower_ratio = lower_wick / candle_range

            if lower_ratio >= 0.35 and close > open_price:
                bullish_score += 1

            if upper_ratio >= 0.35 and close < open_price:
                bearish_score += 1

        if bullish_score >= bearish_score + 2:
            return "bullish"

        if bearish_score >= bullish_score + 2:
            return "bearish"

        return "neutral"

    @staticmethod
    def _volatility_regime(compression_ratio: float) -> str:
        if compression_ratio <= 0.45:
            return "extreme_compression"

        if compression_ratio <= 0.70:
            return "compression"

        if compression_ratio <= 1.10:
            return "normal"

        return "expanded"

    @staticmethod
    def _squeeze_quality(compression_ratio: float) -> float:
        quality = (1.0 - compression_ratio) * 100.0
        return round(max(0.0, min(quality, 100.0)), 2)

    @staticmethod
    def _participation_score(volumes: list[float]) -> float:
        if len(volumes) < 4:
            return 0.0

        recent_avg = mean(volumes[-3:])
        baseline_avg = mean(volumes[:-3]) if volumes[:-3] else 0.0

        if baseline_avg <= 0:
            return 0.0

        ratio = recent_avg / baseline_avg

        if ratio >= 2.0:
            return 15.0

        if ratio >= 1.5:
            return 10.0

        if ratio >= 1.2:
            return 5.0

        return 0.0

    @staticmethod
    def _range(candle: Any) -> float:
        return abs(float(candle.high) - float(candle.low))