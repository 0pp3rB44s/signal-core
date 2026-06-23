from __future__ import annotations

from statistics import mean
from typing import Any


class BreakoutEngine:
    """Detect pre-breakout pressure and breakout readiness."""

    def analyze(
        self,
        candles: list[Any],
    ) -> dict[str, Any]:

        if len(candles) < 12:
            return {
                "breakout_ready": False,
                "pressure_score": 0.0,
                "direction": "neutral",
                "notes": ["not enough candles"],
            }

        recent = candles[-6:]
        recent_24 = candles[-24:] if len(candles) >= 24 else candles

        highs = [float(c.high) for c in recent]
        lows = [float(c.low) for c in recent]
        closes = [float(c.close) for c in recent]
        volumes = [float(getattr(c, "volume_base", 0.0) or 0.0) for c in recent]
        ranges = [abs(float(c.high) - float(c.low)) for c in recent]

        tightening = self._is_tightening(ranges)
        higher_lows = self._higher_lows(lows)
        lower_highs = self._lower_highs(highs)
        acceleration = self._acceleration(closes)
        close_near_high = self._close_near_high(recent)
        close_near_low = self._close_near_low(recent)
        range_expansion = self._range_expansion(ranges)
        bullish_wick_rejection = self._bullish_wick_rejection(recent)
        bearish_wick_rejection = self._bearish_wick_rejection(recent)
        participation_score = self._participation_score(volumes)
        fake_breakout_risk = self._fake_breakout_risk(
            candles=recent,
            acceleration=acceleration,
            range_expansion=range_expansion,
        )

        origin_distance_score = self._origin_distance_score(recent_24)
        impulse_freshness_score = self._impulse_freshness_score(recent_24)
        expansion_exhaustion_score = self._expansion_exhaustion_score(recent_24)

        bullish_pressure = 0
        bearish_pressure = 0

        if higher_lows:
            bullish_pressure += 30

        if lower_highs:
            bearish_pressure += 30

        if tightening:
            bullish_pressure += 20
            bearish_pressure += 20

        if close_near_high:
            bullish_pressure += 15

        if close_near_low:
            bearish_pressure += 15

        if range_expansion:
            bullish_pressure += 10
            bearish_pressure += 10

        bullish_pressure += participation_score
        bearish_pressure += participation_score

        if bullish_wick_rejection:
            bullish_pressure += 12

        if bearish_wick_rejection:
            bearish_pressure += 12

        if fake_breakout_risk:
            bullish_pressure -= 15
            bearish_pressure -= 15

        if acceleration > 0:
            bullish_pressure += min(acceleration * 250, 25)

        if acceleration < 0:
            bearish_pressure += min(abs(acceleration) * 250, 25)

        confidence_score = 55.0
        confidence_score += min(participation_score, 15)

        if bullish_wick_rejection or bearish_wick_rejection:
            confidence_score += 10

        if tightening:
            confidence_score += 5

        if fake_breakout_risk:
            confidence_score -= 25

        late_move_penalty = 0.0

        if origin_distance_score >= 70:
            late_move_penalty += 10

        if impulse_freshness_score <= 30:
            late_move_penalty += 12

        if expansion_exhaustion_score >= 70:
            late_move_penalty += 10

        bullish_pressure -= late_move_penalty
        bearish_pressure -= late_move_penalty

        pressure_score = max(bullish_pressure, bearish_pressure)

        pressure_regime = self._pressure_regime(float(pressure_score))
        breakout_ready = pressure_score >= 55

        if bullish_pressure > bearish_pressure + 10:
            direction = "bullish"
        elif bearish_pressure > bullish_pressure + 10:
            direction = "bearish"
        else:
            direction = "neutral"

        notes: list[str] = []

        notes.append(f"pressure_regime={pressure_regime}")
        notes.append(f"pressure_score={round(float(pressure_score), 2)}")
        notes.append(f"confidence_score={round(max(0.0, min(confidence_score, 100.0)), 2)}")

        notes.append(f"origin_distance_score={round(origin_distance_score, 2)}")
        notes.append(f"impulse_freshness_score={round(impulse_freshness_score, 2)}")
        notes.append(f"expansion_exhaustion_score={round(expansion_exhaustion_score, 2)}")

        if tightening:
            notes.append("range_tightening=true")

        if higher_lows:
            notes.append("higher_lows_building=true")

        if lower_highs:
            notes.append("lower_highs_building=true")

        # Diagnostics for structure flags
        notes.append(f"diag_tightening={tightening}")
        notes.append(f"diag_higher_lows={higher_lows}")
        notes.append(f"diag_lower_highs={lower_highs}")

        if acceleration > 0.2:
            notes.append("bullish_acceleration=true")

        if acceleration < -0.2:
            notes.append("bearish_acceleration=true")

        if close_near_high:
            notes.append("closes_pressing_highs=true")

        if close_near_low:
            notes.append("closes_pressing_lows=true")

        # Diagnostics for close position flags
        notes.append(f"diag_close_near_high={close_near_high}")
        notes.append(f"diag_close_near_low={close_near_low}")

        if range_expansion:
            notes.append("range_expansion=true")

        if bullish_wick_rejection:
            notes.append("bullish_wick_rejection=true")

        if bearish_wick_rejection:
            notes.append("bearish_wick_rejection=true")

        if participation_score >= 10:
            notes.append(f"participation_score={round(participation_score, 2)}")

        if fake_breakout_risk:
            notes.append("fake_breakout_risk=true")

        if breakout_ready:
            notes.append("breakout_ready=true")

        # Structure diagnostics before return
        structure_detected = any([
            tightening,
            higher_lows,
            lower_highs,
            close_near_high,
            close_near_low,
        ])
        notes.append(f"diag_structure_detected={structure_detected}")

        return {
            "breakout_ready": breakout_ready,
            "pressure_score": round(float(pressure_score), 2),
            "pressure_regime": pressure_regime,
            "confidence_score": round(max(0.0, min(confidence_score, 100.0)), 2),
            "direction": direction,
            # Diagnostics fields
            "diag_tightening": tightening,
            "diag_higher_lows": higher_lows,
            "diag_lower_highs": lower_highs,
            "diag_close_near_high": close_near_high,
            "diag_close_near_low": close_near_low,
            "diag_structure_detected": structure_detected,
            # Existing structure
            "tightening": tightening,
            "higher_lows": higher_lows,
            "lower_highs": lower_highs,
            "acceleration": round(float(acceleration), 4),
            "close_near_high": close_near_high,
            "close_near_low": close_near_low,
            "range_expansion": range_expansion,
            "bullish_wick_rejection": bullish_wick_rejection,
            "bearish_wick_rejection": bearish_wick_rejection,
            "participation_score": round(float(participation_score), 2),
            "fake_breakout_risk": fake_breakout_risk,
            "origin_distance_score": round(float(origin_distance_score), 2),
            "impulse_freshness_score": round(float(impulse_freshness_score), 2),
            "expansion_exhaustion_score": round(float(expansion_exhaustion_score), 2),
            "notes": notes,
        }

    @staticmethod
    def _pressure_regime(pressure_score: float) -> str:
        if pressure_score >= 80:
            return "explosive"
        if pressure_score >= 65:
            return "strong"
        if pressure_score >= 55:
            return "ready"
        if pressure_score >= 40:
            return "building"
        return "weak"

    @staticmethod
    def _is_tightening(ranges: list[float]) -> bool:
        if len(ranges) < 4:
            return False

        first_half = mean(ranges[:3])
        second_half = mean(ranges[-3:])

        return second_half < first_half * 0.85

    @staticmethod
    def _higher_lows(lows: list[float]) -> bool:
        score = 0

        for idx in range(1, len(lows)):
            if lows[idx] > lows[idx - 1]:
                score += 1

        return score >= len(lows) - 2

    @staticmethod
    def _lower_highs(highs: list[float]) -> bool:
        score = 0

        for idx in range(1, len(highs)):
            if highs[idx] < highs[idx - 1]:
                score += 1

        return score >= len(highs) - 2

    @staticmethod
    def _close_near_high(candles: list[Any]) -> bool:
        if not candles:
            return False

        score = 0
        for candle in candles[-4:]:
            candle_range = max(float(candle.high) - float(candle.low), 1e-9)
            close_position = (float(candle.close) - float(candle.low)) / candle_range
            if close_position >= 0.65:
                score += 1

        return score >= 3

    @staticmethod
    def _close_near_low(candles: list[Any]) -> bool:
        if not candles:
            return False

        score = 0
        for candle in candles[-4:]:
            candle_range = max(float(candle.high) - float(candle.low), 1e-9)
            close_position = (float(candle.close) - float(candle.low)) / candle_range
            if close_position <= 0.35:
                score += 1

        return score >= 3

    @staticmethod
    def _range_expansion(ranges: list[float]) -> bool:
        if len(ranges) < 6:
            return False

        recent_avg = mean(ranges[-2:])
        prior_avg = mean(ranges[-6:-2])

        return recent_avg > prior_avg * 1.15

    @staticmethod
    def _bullish_wick_rejection(candles: list[Any]) -> bool:
        score = 0

        for candle in candles[-4:]:
            candle_range = max(float(candle.high) - float(candle.low), 1e-9)
            lower_wick = min(float(candle.open), float(candle.close)) - float(candle.low)
            wick_ratio = lower_wick / candle_range

            if wick_ratio >= 0.35 and float(candle.close) > float(candle.open):
                score += 1

        return score >= 2

    @staticmethod
    def _bearish_wick_rejection(candles: list[Any]) -> bool:
        score = 0

        for candle in candles[-4:]:
            candle_range = max(float(candle.high) - float(candle.low), 1e-9)
            upper_wick = float(candle.high) - max(float(candle.open), float(candle.close))
            wick_ratio = upper_wick / candle_range

            if wick_ratio >= 0.35 and float(candle.close) < float(candle.open):
                score += 1

        return score >= 2

    @staticmethod
    def _participation_score(volumes: list[float]) -> float:
        if len(volumes) < 4:
            return 0.0

        recent_avg = mean(volumes[-2:])
        baseline_avg = mean(volumes[:-2]) if volumes[:-2] else 0.0

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
    def _fake_breakout_risk(
        candles: list[Any],
        acceleration: float,
        range_expansion: bool,
    ) -> bool:

        if len(candles) < 3:
            return False

        latest = candles[-1]
        candle_range = max(float(latest.high) - float(latest.low), 1e-9)
        body = abs(float(latest.close) - float(latest.open))
        body_ratio = body / candle_range

        return bool(
            range_expansion
            and abs(acceleration) >= 0.002
            and body_ratio < 0.28
        )

    @staticmethod
    def _acceleration(closes: list[float]) -> float:
        if len(closes) < 4:
            return 0.0

        first_move = closes[-3] - closes[-4]
        second_move = closes[-2] - closes[-3]
        third_move = closes[-1] - closes[-2]

        weighted = (first_move * 0.5) + (second_move * 1.0) + (third_move * 1.5)

        baseline = abs(closes[-1]) if closes[-1] != 0 else 1.0

        return weighted / baseline
    @staticmethod
    def _origin_distance_score(candles: list[Any]) -> float:
        if len(candles) < 10:
            return 0.0

        start_price = float(candles[0].close)
        current_price = float(candles[-1].close)

        if start_price <= 0:
            return 0.0

        move_pct = abs((current_price - start_price) / start_price) * 100.0
        return min(100.0, move_pct * 8.0)

    @staticmethod
    def _impulse_freshness_score(candles: list[Any]) -> float:
        if len(candles) < 8:
            return 100.0

        closes = [float(c.close) for c in candles[-8:]]

        directional = 0
        for idx in range(1, len(closes)):
            if closes[idx] > closes[idx - 1]:
                directional += 1

        freshness = 100.0 - (directional * 12.5)
        return max(0.0, min(100.0, freshness))

    @staticmethod
    def _expansion_exhaustion_score(candles: list[Any]) -> float:
        if len(candles) < 10:
            return 0.0

        highs = [float(c.high) for c in candles]
        lows = [float(c.low) for c in candles]
        close = float(candles[-1].close)

        local_high = max(highs)
        local_low = min(lows)
        span = max(local_high - local_low, 1e-9)

        position = (close - local_low) / span
        return max(0.0, min(100.0, position * 100.0))