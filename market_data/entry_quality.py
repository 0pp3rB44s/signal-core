from __future__ import annotations

from typing import Any


class EntryQualityAnalyzer:
    """Scores whether an entry is efficient or too late in the candle."""

    def analyze(
        self,
        *,
        direction: str,
        latest_candle: dict[str, Any],
        orderbook_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        open_price = float(latest_candle.get("open") or 0.0)
        high = float(latest_candle.get("high") or 0.0)
        low = float(latest_candle.get("low") or 0.0)
        close = float(latest_candle.get("close") or 0.0)

        candle_range = max(high - low, 1e-9)
        close_position = (close - low) / candle_range

        score = 100
        notes: list[str] = []

        exhaustion_risk = False
        confidence_score = 100.0

        if direction.upper() == "LONG":
            if close_position >= 0.80:
                score -= 30
                notes.append("late long entry near candle high")
                exhaustion_risk = True
                confidence_score -= 25
            elif close_position >= 0.65:
                score -= 15
                notes.append("long entry elevated in candle")

        if direction.upper() == "SHORT":
            if close_position <= 0.20:
                score -= 30
                notes.append("late short entry near candle low")
                exhaustion_risk = True
                confidence_score -= 25
            elif close_position <= 0.35:
                score -= 15
                notes.append("short entry extended in candle")

        upper_wick = high - max(open_price, close)
        lower_wick = min(open_price, close) - low
        upper_wick_ratio = upper_wick / candle_range
        lower_wick_ratio = lower_wick / candle_range

        if direction.upper() == "LONG" and upper_wick_ratio >= 0.35:
            score -= 15
            notes.append("upper wick rejection risk")

        if direction.upper() == "SHORT" and lower_wick_ratio >= 0.35:
            score -= 15
            notes.append("lower wick rejection risk")

        if orderbook_context:
            bias = str(orderbook_context.get("continuation_bias") or "neutral")
            spread_bps = float(orderbook_context.get("spread_bps") or 0.0)
            spread_regime = str(orderbook_context.get("spread_regime") or "normal")
            spoofing_risk = bool(orderbook_context.get("spoofing_risk") or False)
            orderbook_confidence = float(orderbook_context.get("confidence_score") or 50.0)
            empty_orderbook_risk_off = bool(orderbook_context.get("empty_orderbook_risk_off") or False)

            if spread_regime == "wide" or spread_bps >= 5:
                score -= 10
                notes.append(f"wide_spread_bps={spread_bps:.2f}")
                confidence_score -= 10

            if empty_orderbook_risk_off:
                score -= 35
                confidence_score -= 35
                exhaustion_risk = True
                notes.append("empty_orderbook_risk_off=true")

            if spread_regime == "extreme":
                score -= 20
                confidence_score -= 20
                notes.append("extreme spread regime")

            if spoofing_risk:
                score -= 25
                confidence_score -= 25
                notes.append("possible spoofing/liquidity trap")

            if direction.upper() == "LONG" and bias == "bearish":
                score -= 15
                notes.append("orderbook bias against long")

            if direction.upper() == "SHORT" and bias == "bullish":
                score -= 15
                notes.append("orderbook bias against short")

            confidence_score = (confidence_score + orderbook_confidence) / 2

        score = max(0, min(100, score))

        return {
            "entry_quality_score": score,
            "confidence_score": round(max(0.0, min(confidence_score, 100.0)), 2),
            "exhaustion_risk": exhaustion_risk,
            "entry_regime": self._entry_regime(score),
            "close_position": round(close_position, 4),
            "upper_wick_ratio": round(upper_wick_ratio, 4),
            "lower_wick_ratio": round(lower_wick_ratio, 4),
            "notes": notes,
        }

    @staticmethod
    def _entry_regime(score: float) -> str:
        if score >= 85:
            return "clean"
        if score >= 70:
            return "acceptable"
        if score >= 50:
            return "late_or_risky"
        return "avoid"
