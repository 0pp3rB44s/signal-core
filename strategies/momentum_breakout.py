import logging
import os
from dataclasses import dataclass
from typing import Optional

from clients.schemas import Candle, MarketSnapshot, StrategyCandidate


logger = logging.getLogger("StartupRunner")


@dataclass
class BreakoutDetection:
    breakout_level: float
    close: float
    entry_hint: float
    reclaim_level: float
    invalidation: float
    sweep_extreme: float
    bars_since_sweep: int
    volume_ratio: float
    volume_ratio_on_sweep: float
    displacement_pct: float
    range_pct: float
    local_range_size_pct: float
    bars_lookback: int
    reason_flags: list[str]


class MomentumBreakoutStrategy:
    name = "momentum_breakout"

    def __init__(self, settings) -> None:
        self.settings = settings

        self.lookback = 20
        self.min_breakout_pct = 0.12
        self.min_volume_ratio = 1.15
        self.min_participation_score = 0.85
        self.min_followthrough_volume_ratio = 0.35
        self.max_pullback_bps = 45
        self.min_close_position_pct = 0.55
        self.max_bars_since_breakout = 3
        self.max_entry_extension_pct = 0.60
        self.prearmed_min_expansion_prob = 70.0
        self.prearmed_min_volume_ratio = 0.15
        self.prearmed_max_spread_bps = 5.0
        self.prearmed_min_pressure_score = 25.0
        self.prearmed_min_close_position_pct = 0.50
        self.weak_pressure_min_score = 30.0
        self.weak_pressure_min_expansion_prob = 55.0
        self.strict_breakout_min_volume_ratio = 0.85

    def detect(self, market: MarketSnapshot) -> Optional[StrategyCandidate]:
        candles = market.primary.candles

        if len(candles) < self.lookback + 5:
            return None

        entry_candle = candles[-1]

        primary_trend = (market.primary.trend or "").lower()
        confirmation_trend = (market.confirmation.trend or "").lower()
        pre_alignment_prearmed_context = self._prearmed_context(market, direction="LONG")
        strict_trend_alignment = (
            market.alignment == "aligned_bullish"
            and primary_trend == "bullish"
            and confirmation_trend == "bullish"
        )
        mtf_prearmed_override = (
            pre_alignment_prearmed_context.get("allowed")
            and market.alignment in {"aligned_bullish", "mixed"}
            and confirmation_trend == "bullish"
            and primary_trend in {"bullish", "mixed", "neutral"}
        )

        if not strict_trend_alignment and not mtf_prearmed_override:
            self._log_rejection(
                market,
                "trend_not_aligned_for_breakout",
                [
                    f"alignment={market.alignment}",
                    f"primary_trend={primary_trend}",
                    f"confirmation_trend={confirmation_trend}",
                    f"mtf_prearmed_override={mtf_prearmed_override}",
                ],
            )
            return None

        if mtf_prearmed_override and not strict_trend_alignment:
            logger.info(
                "MTF_PREARMED_OVERRIDE | %s | direction=LONG | alignment=%s | primary=%s | confirmation=%s | pressure_score=%.2f | expansion_prob=%.1f",
                market.symbol,
                market.alignment,
                primary_trend,
                confirmation_trend,
                float(pre_alignment_prearmed_context.get("pressure_score", 0.0)),
                float(pre_alignment_prearmed_context.get("expansion_prob", 0.0)),
            )

        breakout_candle = None
        breakout_index = None
        breakout_pct = 0.0
        vol_ratio = 0.0
        prev_window = None
        prev_high = 0.0
        prev_low = 0.0
        rejection_reasons: list[str] = []

        prearmed_context = pre_alignment_prearmed_context

        breakout_pressure_ok = (
            float(prearmed_context.get("pressure_score", 0.0)) >= self.weak_pressure_min_score
            and float(prearmed_context.get("expansion_prob", 0.0)) >= self.weak_pressure_min_expansion_prob
        ) or bool(prearmed_context.get("breakout_context_ready"))

        if not breakout_pressure_ok:
            self._log_rejection(
                market,
                "breakout_lacks_directional_pressure",
                [
                    f"pressure_score={float(prearmed_context.get('pressure_score', 0.0)):.2f}",
                    f"expansion_prob={float(prearmed_context.get('expansion_prob', 0.0)):.1f}",
                    f"breakout_context_ready={prearmed_context.get('breakout_context_ready')}",
                ],
            )
            return None

        # Accept a fresh breakout from the last 3 closed candles instead of only candles[-2].
        # This prevents fast impulse moves from being missed when the confirmation candle arrives 1-2 scans later.
        for offset in range(2, 5):
            candidate_index = len(candles) - offset
            candidate_candle = candles[candidate_index]
            candidate_window = candles[candidate_index - self.lookback : candidate_index]

            if len(candidate_window) < self.lookback:
                rejection_reasons.append(f"offset={offset}: insufficient_window len={len(candidate_window)}")
                continue

            candidate_prev_high = max(c.high for c in candidate_window)
            candidate_prev_low = min(c.low for c in candidate_window)
            candidate_breakout_pct = (
                ((candidate_candle.close - candidate_prev_high) / candidate_prev_high) * 100
                if candidate_prev_high
                else 0.0
            )

            if candidate_breakout_pct < self.min_breakout_pct:
                rejection_reasons.append(
                    f"offset={offset}: breakout_pct {candidate_breakout_pct:.3f} < min {self.min_breakout_pct:.3f}"
                )
                continue

            if candidate_candle.close <= candidate_prev_high:
                rejection_reasons.append(
                    f"offset={offset}: close {candidate_candle.close:.6f} <= prev_high {candidate_prev_high:.6f}"
                )
                continue

            if candidate_candle.close <= candidate_candle.open:
                rejection_reasons.append(
                    f"offset={offset}: candle not bullish close={candidate_candle.close:.6f} open={candidate_candle.open:.6f}"
                )
                continue

            candidate_vol_ratio = self._volume_ratio_at(candles, candidate_index)
            required_breakout_volume = self.strict_breakout_min_volume_ratio if prearmed_context.get("breakout_context_ready") else self.min_volume_ratio
            if candidate_vol_ratio < required_breakout_volume:
                rejection_reasons.append(
                    f"offset={offset}: volume_ratio {candidate_vol_ratio:.2f} < min {required_breakout_volume:.2f}"
                )
                continue

            # prearmed_context is now initialized before loop; do not re-initialize here.
            prearmed_allowed = (
                prearmed_context["allowed"]
                and candidate_vol_ratio >= self.prearmed_min_volume_ratio
                and candidate_breakout_pct <= self.max_entry_extension_pct
            )

            participation_score = self._participation_score(candles, candidate_index, direction="LONG")

            aggressive_prearmed_allowed = (
                prearmed_allowed
                and float(prearmed_context.get("pressure_score", 0.0)) >= 35.0
                and float(prearmed_context.get("expansion_prob", 0.0)) >= 60.0
            )

            required_participation = 0.75 if aggressive_prearmed_allowed else (1.0 if prearmed_allowed else self.min_participation_score)

            logger.info(
                "PREARMED_ALLOWED | %s | direction=LONG | prearmed=%s | aggressive=%s | participation_score=%.2f | required=%.2f | pressure_score=%.2f | expansion_prob=%.1f",
                market.symbol,
                prearmed_allowed,
                aggressive_prearmed_allowed,
                participation_score,
                required_participation,
                float(prearmed_context.get("pressure_score", 0.0)),
                float(prearmed_context.get("expansion_prob", 0.0)),
            )

            if participation_score < required_participation:
                rejection_reasons.append(
                    f"offset={offset}: participation_score {participation_score:.2f} < min {required_participation:.2f} prearmed={prearmed_allowed}"
                )

                if aggressive_prearmed_allowed:
                    logger.info(
                        "PREARMED_BLOCKED | %s | direction=LONG | reason=participation_too_low | participation_score=%.2f | required=%.2f",
                        market.symbol,
                        participation_score,
                        required_participation,
                    )

                continue

            if self._is_choppy(candidate_window):
                rejection_reasons.append(f"offset={offset}: choppy_window")
                continue

            breakout_candle = candidate_candle
            breakout_index = candidate_index
            breakout_pct = candidate_breakout_pct
            vol_ratio = candidate_vol_ratio
            prev_window = candidate_window
            prev_high = candidate_prev_high
            prev_low = candidate_prev_low
            prearmed_breakout = prearmed_allowed
            prearmed_context_data = prearmed_context
            break

        prearmed_breakout = locals().get("prearmed_breakout", False)
        prearmed_context_data = locals().get("prearmed_context_data", {"allowed": False})

        if breakout_candle is None or breakout_index is None or prev_window is None:
            if prearmed_context.get("allowed"):
                candidate_window = candles[-self.lookback - 1 : -1]

                if len(candidate_window) >= self.lookback:
                    candidate_prev_high = max(c.high for c in candidate_window)
                    candidate_prev_low = min(c.low for c in candidate_window)
                    candidate_vol_ratio = self._volume_ratio_at(candles, len(candles) - 1)
                    participation_score = self._participation_score(
                        candles,
                        len(candles) - 1,
                        direction="LONG",
                    )

                    required_prearmed_volume = 0.25 if prearmed_context.get("breakout_context_ready") else self.prearmed_min_volume_ratio
                    high_quality_prearmed = (
                        (
                            bool(prearmed_context.get("breakout_context_ready"))
                            or float(prearmed_context.get("pressure_score", 0.0)) >= 30.0
                        )
                        and float(prearmed_context.get("expansion_prob", 0.0)) >= 55.0
                        and float(candidate_vol_ratio or 0.0) >= 0.45
                        and float(participation_score or 0.0) >= 0.55
                    )
                    choppy_window = self._is_choppy(candidate_window)

                    if (
                        candidate_vol_ratio >= required_prearmed_volume
                        and participation_score >= 0.75
                        and (not choppy_window or high_quality_prearmed)
                    ):
                        breakout_candle = entry_candle
                        breakout_index = len(candles) - 1
                        breakout_pct = 0.0
                        vol_ratio = candidate_vol_ratio
                        prev_window = candidate_window
                        prev_high = candidate_prev_high
                        prev_low = candidate_prev_low
                        prearmed_breakout = True
                        prearmed_context_data = prearmed_context

                        logger.info(
                            "PREARMED_BREAKOUT_CANDIDATE | %s | pressure_score=%.2f | expansion_prob=%.1f | volume_ratio=%.2f | participation_score=%.2f",
                            market.symbol,
                            float(prearmed_context.get("pressure_score", 0.0)),
                            float(prearmed_context.get("expansion_prob", 0.0)),
                            candidate_vol_ratio,
                            participation_score,
                        )

                        if choppy_window and high_quality_prearmed:
                            logger.info(
                                "PREARMED_BREAKOUT_CHOP_OVERRIDE | %s | pressure_score=%.2f | expansion_prob=%.1f | volume_ratio=%.2f | participation_score=%.2f",
                                market.symbol,
                                float(prearmed_context.get("pressure_score", 0.0)),
                                float(prearmed_context.get("expansion_prob", 0.0)),
                                candidate_vol_ratio,
                                participation_score,
                            )

            if breakout_candle is None or breakout_index is None or prev_window is None:
                self._log_rejection(market, "no_fresh_breakout", rejection_reasons)
                return None

        max_pullback = prev_high * (self.max_pullback_bps / 10_000)

        candles_after_breakout = candles[breakout_index + 1 :]
        if not candles_after_breakout and not prearmed_breakout:
            self._log_rejection(market, "no_confirmation_candles_after_breakout", [])
            return None
        if not candles_after_breakout and prearmed_breakout:
            logger.info(
                "PREARMED_BREAKOUT_CONFIRMATION_BYPASS | %s | reason=no_confirmation_candles_expected_for_prearmed | pressure_score=%.2f | expansion_prob=%.1f | volume_ratio=%.2f",
                market.symbol,
                float(prearmed_context_data.get("pressure_score", 0.0)),
                float(prearmed_context_data.get("expansion_prob", 0.0)),
                float(vol_ratio or 0.0),
            )

        bars_since_breakout = len(candles) - 1 - breakout_index
        if bars_since_breakout > self.max_bars_since_breakout and not prearmed_breakout:
            self._log_rejection(
                market,
                "late_breakout_entry",
                [
                    f"bars_since_breakout={bars_since_breakout}",
                    f"max_bars_since_breakout={self.max_bars_since_breakout}",
                ],
            )
            return None

        lowest_after_breakout = min(c.low for c in candles_after_breakout) if candles_after_breakout else entry_candle.low
        pullback_floor = prev_high - max_pullback
        if lowest_after_breakout < pullback_floor and not prearmed_breakout:
            self._log_rejection(
                market,
                "pullback_broke_breakout_level",
                [
                    f"lowest_after_breakout={lowest_after_breakout:.6f}",
                    f"pullback_floor={pullback_floor:.6f}",
                    f"prev_high={prev_high:.6f}",
                    f"max_pullback_bps={self.max_pullback_bps}",
                ],
            )
            return None

        if entry_candle.close <= prev_high and not prearmed_breakout:
            self._log_rejection(
                market,
                "entry_close_below_breakout_level",
                [f"entry_close={entry_candle.close:.6f}", f"prev_high={prev_high:.6f}"],
            )
            return None

        entry_extension_pct = ((entry_candle.close - prev_high) / prev_high) * 100 if prev_high else 0.0
        if entry_extension_pct > self.max_entry_extension_pct and not prearmed_breakout:
            self._log_rejection(
                market,
                "entry_too_extended_after_breakout",
                [
                    f"entry_extension_pct={entry_extension_pct:.3f}",
                    f"max_entry_extension_pct={self.max_entry_extension_pct:.3f}",
                    f"entry_close={entry_candle.close:.6f}",
                    f"prev_high={prev_high:.6f}",
                ],
            )
            return None

        if entry_candle.close <= entry_candle.open and not prearmed_breakout:
            self._log_rejection(
                market,
                "entry_not_bullish_continuation",
                [
                    f"entry_close={entry_candle.close:.6f}",
                    f"entry_open={entry_candle.open:.6f}",
                    f"breakout_close={breakout_candle.close:.6f}",
                ],
            )
            return None

        followthrough_volume_ratio = self._volume_ratio_at(candles, len(candles) - 1)
        required_followthrough = 0.45 if prearmed_breakout else self.min_followthrough_volume_ratio
        if followthrough_volume_ratio < required_followthrough:
            self._log_rejection(
                market,
                "weak_followthrough_participation",
                [
                    f"followthrough_volume_ratio={followthrough_volume_ratio:.2f}",
                    f"required={required_followthrough:.2f}",
                    f"prearmed={prearmed_breakout}",
                ],
            )
            return None

        entry_range = entry_candle.high - entry_candle.low
        if entry_range <= 0:
            self._log_rejection(market, "invalid_entry_range", [])
            return None

        close_position_pct = (entry_candle.close - entry_candle.low) / entry_range

        required_close_position_pct = self.prearmed_min_close_position_pct if prearmed_breakout else self.min_close_position_pct

        if close_position_pct < required_close_position_pct:
            self._log_rejection(
                market,
                "weak_entry_close",
                [
                    f"close_position_pct={close_position_pct:.2f}",
                    f"required={required_close_position_pct:.2f}",
                ],
            )
            return None

        invalidation = min(entry_candle.low, breakout_candle.low)

        detection = BreakoutDetection(
            breakout_level=prev_high,
            close=entry_candle.close,
            entry_hint=entry_candle.close,
            reclaim_level=prev_high,
            invalidation=invalidation,
            sweep_extreme=invalidation,
            bars_since_sweep=bars_since_breakout,
            volume_ratio=vol_ratio,
            volume_ratio_on_sweep=vol_ratio,
            displacement_pct=breakout_pct,
            range_pct=((prev_high - prev_low) / entry_candle.close) * 100 if entry_candle.close else 0,
            local_range_size_pct=((prev_high - prev_low) / entry_candle.close) * 100 if entry_candle.close else 0,
            bars_lookback=self.lookback,
            reason_flags=[
                "breakout above range",
                "volume expansion",
                "pullback held breakout level",
                "strong continuation close",
                "prearmed_breakout" if prearmed_breakout else "standard_breakout",
                "mtf_prearmed_override" if mtf_prearmed_override else "strict_trend_alignment",
                f"prearmed_expansion_prob={prearmed_context_data.get('expansion_prob', 0.0):.1f}",
                f"prearmed_pressure_score={prearmed_context_data.get('pressure_score', 0.0):.2f}",
                f"participation_score={self._participation_score(candles, breakout_index, direction='LONG'):.2f}",
                f"followthrough_volume_ratio={self._volume_ratio_at(candles, len(candles) - 1):.2f}",
            ],
        )

        return StrategyCandidate(
            symbol=market.symbol,
            strategy=self.name,
            direction="LONG",
            primary_granularity=market.primary.granularity,
            confirmation_granularity=market.confirmation.granularity,
            market=market,
            detection=detection,
            notes=[
                "breakout above range",
                "volume expansion",
                "pullback held breakout level",
                "strong continuation close",
                "prearmed_breakout" if prearmed_breakout else "standard_breakout",
                "mtf_prearmed_override" if mtf_prearmed_override else "strict_trend_alignment",
                f"prearmed_expansion_prob={prearmed_context_data.get('expansion_prob', 0.0):.1f}",
                f"prearmed_pressure_score={prearmed_context_data.get('pressure_score', 0.0):.2f}",
                f"breakout_pct={breakout_pct:.2f}",
                f"volume_ratio={vol_ratio:.2f}",
                f"bars_since_breakout={bars_since_breakout}",
                f"participation_score={self._participation_score(candles, breakout_index, direction='LONG'):.2f}",
                f"followthrough_volume_ratio={self._volume_ratio_at(candles, len(candles) - 1):.2f}",
                f"directional_pressure_ok={breakout_pressure_ok}",
            ],
        )

    def _prearmed_context(self, market: "MarketSnapshot", direction: str) -> dict[str, float | bool | str]:
        notes_text = " | ".join(str(note).lower() for note in (market.notes or []))
        spread_bps = self._extract_spread_bps(market.notes or [], 99.0)
        expansion_prob = self._extract_note_float(market.notes or [], "expansion_prob=", 0.0)
        pressure_score = self._extract_note_float(market.notes or [], "pressure_score=", 0.0)

        wanted_pressure = "bullish" if direction == "LONG" else "bearish"
        breakout_context_ready = "breakout_context ready=true" in notes_text
        breakout_context_direction = f"direction={wanted_pressure}" in notes_text
        volatility_pressure_direction = f"pressure={wanted_pressure}" in notes_text
        named_breakout_pressure = f"{wanted_pressure} breakout pressure" in notes_text

        has_structure = (
            "range tightening" in notes_text
            or ("higher lows building" in notes_text if direction == "LONG" else "lower highs building" in notes_text)
            or ("closes pressing highs" in notes_text if direction == "LONG" else "closes pressing lows" in notes_text)
        )
        breakout_direction_pressure = (
            breakout_context_direction
            and has_structure
            and expansion_prob >= 75.0
            and pressure_score >= 50.0
        )

        has_directional_pressure = (
            volatility_pressure_direction
            or named_breakout_pressure
            or (breakout_context_ready and breakout_context_direction)
            or breakout_direction_pressure
        )
        compression_or_high_prob = (
            "compression=true" in notes_text
            or breakout_context_ready
            or expansion_prob >= self.prearmed_min_expansion_prob
        )

        required_pressure_score = 60.0 if breakout_context_ready else 50.0
        high_quality_breakout_context = (
            breakout_context_ready
            and breakout_context_direction
            and has_structure
            and pressure_score >= 65.0
            and expansion_prob >= 75.0
        )
        effective_max_spread_bps = 5.5 if high_quality_breakout_context else self.prearmed_max_spread_bps

        allowed = (
            compression_or_high_prob
            and has_directional_pressure
            and has_structure
            and spread_bps <= effective_max_spread_bps
            and pressure_score >= required_pressure_score
        )

        raw_symbols = os.getenv(
            "STRATEGY_DEBUG_SYMBOLS",
            "NEARUSDT,FETUSDT,FILUSDT,OPUSDT,ADAUSDT,LINKUSDT,WIFUSDT,AAVEUSDT",
        )
        debug_symbols = {symbol.strip().upper() for symbol in raw_symbols.split(",") if symbol.strip()}

        if market.symbol.upper() in debug_symbols:
            logger.info(
                "PREARMED_CONTEXT | %s | direction=%s | allowed=%s | spread_bps=%.3f | max_spread_bps=%.3f | pressure_score=%.2f | required_pressure=%.2f | expansion_prob=%.1f | ready=%s | directional=%s | structure=%s | high_quality=%s | compression_or_high_prob=%s",
                market.symbol,
                direction,
                allowed,
                spread_bps,
                effective_max_spread_bps,
                pressure_score,
                required_pressure_score,
                expansion_prob,
                breakout_context_ready,
                has_directional_pressure,
                has_structure,
                high_quality_breakout_context,
                compression_or_high_prob,
            )

        return {
            "allowed": allowed,
            "spread_bps": spread_bps,
            "expansion_prob": expansion_prob,
            "pressure_score": pressure_score,
            "required_pressure_score": required_pressure_score,
            "effective_max_spread_bps": effective_max_spread_bps,
            "high_quality_breakout_context": high_quality_breakout_context,
            "breakout_context_ready": breakout_context_ready,
            "breakout_direction_pressure": breakout_direction_pressure,
            "direction": wanted_pressure,
        }

    @staticmethod
    def _extract_note_float(notes: list[str], marker: str, default: float = 0.0) -> float:
        note_text = " | ".join(str(note).lower() for note in (notes or []))
        marker = marker.lower()
        if marker not in note_text:
            return default
        try:
            raw = note_text.split(marker, 1)[1].split()[0].strip("|,;")
            return float(raw)
        except Exception:
            return default

    @staticmethod
    def _extract_spread_bps(notes: list[str], default: float = 99.0) -> float:
        note_text = " | ".join(str(note).lower() for note in (notes or []))
        marker = "spread "
        if marker not in note_text:
            return default
        try:
            raw = note_text.split(marker, 1)[1].split()[0].strip("|,;")
            raw = raw.replace("bps", "")
            return float(raw)
        except Exception:
            return default

    def _log_rejection(self, market: MarketSnapshot, reason: str, details: list[str]) -> None:
        raw_symbols = os.getenv("STRATEGY_DEBUG_SYMBOLS", "NEARUSDT,FETUSDT,FILUSDT,OPUSDT,ADAUSDT,LINKUSDT,WIFUSDT,AAVEUSDT")
        debug_symbols = {symbol.strip().upper() for symbol in raw_symbols.split(",") if symbol.strip()}

        if market.symbol.upper() not in debug_symbols:
            return

        joined_details = " | ".join(details[-6:]) if details else "no_details"
        logger.info(
            "MOMENTUM_BREAKOUT_REJECT | %s | reason=%s | %s",
            market.symbol,
            reason,
            joined_details,
        )

    def _volume_ratio_at(self, candles: list[Candle], index: int, period: int = 20) -> float:
        if index < period:
            return 0.0

        last = candles[index].volume_base
        sample = candles[index - period : index]
        avg = sum(c.volume_base for c in sample) / period

        return last / avg if avg else 0.0

    def _volume_ratio(self, candles: list[Candle], period: int = 20) -> float:
        if len(candles) < period + 1:
            return 0.0

        last = candles[-1].volume_base
        avg = sum(c.volume_base for c in candles[-period - 1 : -1]) / period

        return last / avg if avg else 0.0

    def _participation_score(self, candles: list[Candle], index: int, direction: str) -> float:
        """Score sustained participation around breakout/breakdown, not just one candle."""
        if index < 3:
            return 0.0

        recent_indexes = [index - 2, index - 1, index]
        ratios = [self._volume_ratio_at(candles, i) for i in recent_indexes]
        candles_slice = [candles[i] for i in recent_indexes]

        score = 0.0

        if ratios[-1] >= self.min_volume_ratio:
            score += 1.0

        if ratios[-1] >= ratios[-2] >= ratios[-3] and ratios[-1] >= 1.0:
            score += 0.75

        if sum(1 for ratio in ratios if ratio >= 0.80) >= 2:
            score += 0.50

        if direction == "LONG":
            directional_closes = sum(1 for candle in candles_slice if candle.close > candle.open)
            closes_progress = candles_slice[-1].close > candles_slice[-2].close >= candles_slice[-3].close
        else:
            directional_closes = sum(1 for candle in candles_slice if candle.close < candle.open)
            closes_progress = candles_slice[-1].close < candles_slice[-2].close <= candles_slice[-3].close

        if directional_closes >= 2:
            score += 0.50

        if closes_progress:
            score += 0.50

        return score

    def _is_choppy(self, candles: list[Candle]) -> bool:
        if len(candles) < 8:
            return True

        ranges = [c.high - c.low for c in candles]
        avg_range = sum(ranges) / len(ranges) if ranges else 0

        highs = max(c.high for c in candles)
        lows = min(c.low for c in candles)
        total_range = highs - lows

        if avg_range == 0 or total_range == 0:
            return True

        compression = total_range / avg_range
        return compression < 2.5


class MomentumBreakdownStrategy(MomentumBreakoutStrategy):
    name = "momentum_breakdown"

    def __init__(self, settings) -> None:
        self.settings = settings

        self.lookback = 20
        self.min_breakdown_pct = 0.12
        self.min_volume_ratio = 1.15
        self.min_participation_score = 1.0
        self.min_followthrough_volume_ratio = 0.45
        self.max_reclaim_bps = 35
        self.max_close_position_pct = 0.38
        self.max_bars_since_breakdown = 2
        self.max_entry_extension_pct = 0.60
        self.prearmed_min_expansion_prob = 70.0
        self.prearmed_min_volume_ratio = 0.20
        self.prearmed_max_spread_bps = 5.0
        self.prearmed_min_pressure_score = 25.0
        self.prearmed_max_close_position_pct = 0.45
        self.weak_pressure_min_score = 45.0
        self.weak_pressure_min_expansion_prob = 65.0
        self.strict_breakdown_min_volume_ratio = 1.05

    def detect(self, market: MarketSnapshot) -> Optional[StrategyCandidate]:
        candles = market.primary.candles

        if len(candles) < self.lookback + 5:
            return None

        entry_candle = candles[-1]

        primary_trend = (market.primary.trend or "").lower()
        confirmation_trend = (market.confirmation.trend or "").lower()
        pre_alignment_prearmed_context = self._prearmed_context(market, direction="SHORT")
        strict_trend_alignment = (
            market.alignment == "aligned_bearish"
            and primary_trend == "bearish"
            and confirmation_trend == "bearish"
        )
        mtf_prearmed_override = (
            pre_alignment_prearmed_context.get("allowed")
            and market.alignment in {"aligned_bearish", "mixed"}
            and confirmation_trend == "bearish"
            and primary_trend in {"bearish", "mixed", "neutral"}
        )

        if not strict_trend_alignment and not mtf_prearmed_override:
            self._log_rejection(
                market,
                "trend_not_aligned_for_breakdown",
                [
                    f"alignment={market.alignment}",
                    f"primary_trend={primary_trend}",
                    f"confirmation_trend={confirmation_trend}",
                    f"mtf_prearmed_override={mtf_prearmed_override}",
                ],
            )
            return None

        if mtf_prearmed_override and not strict_trend_alignment:
            logger.info(
                "MTF_PREARMED_OVERRIDE | %s | direction=SHORT | alignment=%s | primary=%s | confirmation=%s | pressure_score=%.2f | expansion_prob=%.1f",
                market.symbol,
                market.alignment,
                primary_trend,
                confirmation_trend,
                float(pre_alignment_prearmed_context.get("pressure_score", 0.0)),
                float(pre_alignment_prearmed_context.get("expansion_prob", 0.0)),
            )

        breakdown_candle = None
        breakdown_index = None
        breakdown_pct = 0.0
        vol_ratio = 0.0
        prev_window = None
        prev_high = 0.0
        prev_low = 0.0
        rejection_reasons: list[str] = []

        prearmed_context = pre_alignment_prearmed_context

        breakdown_pressure_ok = (
            float(prearmed_context.get("pressure_score", 0.0)) >= self.weak_pressure_min_score
            and float(prearmed_context.get("expansion_prob", 0.0)) >= self.weak_pressure_min_expansion_prob
        ) or bool(prearmed_context.get("breakout_context_ready"))

        if not breakdown_pressure_ok:
            self._log_breakdown_rejection(
                market,
                "breakdown_lacks_directional_pressure",
                [
                    f"pressure_score={float(prearmed_context.get('pressure_score', 0.0)):.2f}",
                    f"expansion_prob={float(prearmed_context.get('expansion_prob', 0.0)):.1f}",
                    f"breakout_context_ready={prearmed_context.get('breakout_context_ready')}",
                ],
            )
            return None

        for offset in range(2, 5):
            candidate_index = len(candles) - offset
            candidate_candle = candles[candidate_index]
            candidate_window = candles[candidate_index - self.lookback : candidate_index]

            if len(candidate_window) < self.lookback:
                rejection_reasons.append(f"offset={offset}: insufficient_window len={len(candidate_window)}")
                continue

            candidate_prev_high = max(c.high for c in candidate_window)
            candidate_prev_low = min(c.low for c in candidate_window)
            candidate_breakdown_pct = (
                ((candidate_prev_low - candidate_candle.close) / candidate_prev_low) * 100
                if candidate_prev_low
                else 0.0
            )

            if candidate_breakdown_pct < self.min_breakdown_pct:
                rejection_reasons.append(
                    f"offset={offset}: breakdown_pct {candidate_breakdown_pct:.3f} < min {self.min_breakdown_pct:.3f}"
                )
                continue

            if candidate_candle.close >= candidate_prev_low:
                rejection_reasons.append(
                    f"offset={offset}: close {candidate_candle.close:.6f} >= prev_low {candidate_prev_low:.6f}"
                )
                continue

            if candidate_candle.close >= candidate_candle.open:
                rejection_reasons.append(
                    f"offset={offset}: candle not bearish close={candidate_candle.close:.6f} open={candidate_candle.open:.6f}"
                )
                continue

            candidate_vol_ratio = self._volume_ratio_at(candles, candidate_index)
            required_breakdown_volume = self.strict_breakdown_min_volume_ratio if prearmed_context.get("breakout_context_ready") else self.min_volume_ratio
            if candidate_vol_ratio < required_breakdown_volume:
                rejection_reasons.append(
                    f"offset={offset}: volume_ratio {candidate_vol_ratio:.2f} < min {required_breakdown_volume:.2f}"
                )
                continue

            prearmed_allowed = (
                prearmed_context["allowed"]
                and candidate_vol_ratio >= self.prearmed_min_volume_ratio
                and candidate_breakdown_pct <= self.max_entry_extension_pct
            )
            participation_score = self._participation_score(candles, candidate_index, direction="SHORT")

            aggressive_prearmed_allowed = (
                prearmed_allowed
                and float(prearmed_context.get("pressure_score", 0.0)) >= 50.0
                and float(prearmed_context.get("expansion_prob", 0.0)) >= 75.0
            )

            required_participation = 0.75 if aggressive_prearmed_allowed else (1.0 if prearmed_allowed else self.min_participation_score)

            logger.info(
                "PREARMED_ALLOWED | %s | direction=SHORT | prearmed=%s | aggressive=%s | participation_score=%.2f | required=%.2f | pressure_score=%.2f | expansion_prob=%.1f",
                market.symbol,
                prearmed_allowed,
                aggressive_prearmed_allowed,
                participation_score,
                required_participation,
                float(prearmed_context.get("pressure_score", 0.0)),
                float(prearmed_context.get("expansion_prob", 0.0)),
            )

            if participation_score < required_participation:
                rejection_reasons.append(
                    f"offset={offset}: participation_score {participation_score:.2f} < min {required_participation:.2f} prearmed={prearmed_allowed}"
                )

                if aggressive_prearmed_allowed:
                    logger.info(
                        "PREARMED_BLOCKED | %s | direction=SHORT | reason=participation_too_low | participation_score=%.2f | required=%.2f",
                        market.symbol,
                        participation_score,
                        required_participation,
                    )

                continue

            if self._is_choppy(candidate_window):
                rejection_reasons.append(f"offset={offset}: choppy_window")
                continue

            breakdown_candle = candidate_candle
            breakdown_index = candidate_index
            breakdown_pct = candidate_breakdown_pct
            vol_ratio = candidate_vol_ratio
            prev_window = candidate_window
            prev_high = candidate_prev_high
            prev_low = candidate_prev_low
            prearmed_breakdown = prearmed_allowed
            prearmed_context_data = prearmed_context
            break

        prearmed_breakdown = locals().get("prearmed_breakdown", False)
        prearmed_context_data = locals().get("prearmed_context_data", {"allowed": False})

        if breakdown_candle is None or breakdown_index is None or prev_window is None:
            if prearmed_context.get("allowed"):
                candidate_window = candles[-self.lookback - 1 : -1]

                if len(candidate_window) >= self.lookback:
                    candidate_prev_high = max(c.high for c in candidate_window)
                    candidate_prev_low = min(c.low for c in candidate_window)
                    candidate_vol_ratio = self._volume_ratio_at(candles, len(candles) - 1)
                    participation_score = self._participation_score(
                        candles,
                        len(candles) - 1,
                        direction="SHORT",
                    )

                    required_prearmed_volume = 0.25 if prearmed_context.get("breakout_context_ready") else self.prearmed_min_volume_ratio
                    high_quality_prearmed = (
                        bool(prearmed_context.get("breakout_context_ready"))
                        and float(prearmed_context.get("pressure_score", 0.0)) >= 60.0
                        and float(prearmed_context.get("expansion_prob", 0.0)) >= 70.0
                        and float(candidate_vol_ratio or 0.0) >= 0.75
                        and float(participation_score or 0.0) >= 0.75
                    )
                    choppy_window = self._is_choppy(candidate_window)

                    if (
                        candidate_vol_ratio >= required_prearmed_volume
                        and participation_score >= 0.75
                        and (not choppy_window or high_quality_prearmed)
                    ):
                        breakdown_candle = entry_candle
                        breakdown_index = len(candles) - 1
                        breakdown_pct = 0.0
                        vol_ratio = candidate_vol_ratio
                        prev_window = candidate_window
                        prev_high = candidate_prev_high
                        prev_low = candidate_prev_low
                        prearmed_breakdown = True
                        prearmed_context_data = prearmed_context

                        logger.info(
                            "PREARMED_BREAKDOWN_CANDIDATE | %s | pressure_score=%.2f | expansion_prob=%.1f | volume_ratio=%.2f | participation_score=%.2f",
                            market.symbol,
                            float(prearmed_context.get("pressure_score", 0.0)),
                            float(prearmed_context.get("expansion_prob", 0.0)),
                            candidate_vol_ratio,
                            participation_score,
                        )

                        if choppy_window and high_quality_prearmed:
                            logger.info(
                                "PREARMED_BREAKDOWN_CHOP_OVERRIDE | %s | pressure_score=%.2f | expansion_prob=%.1f | volume_ratio=%.2f | participation_score=%.2f",
                                market.symbol,
                                float(prearmed_context.get("pressure_score", 0.0)),
                                float(prearmed_context.get("expansion_prob", 0.0)),
                                candidate_vol_ratio,
                                participation_score,
                            )

            if breakdown_candle is None or breakdown_index is None or prev_window is None:
                self._log_breakdown_rejection(market, "no_fresh_breakdown", rejection_reasons)
                return None

        candles_after_breakdown = candles[breakdown_index + 1 :]
        if not candles_after_breakdown:
            return None

        bars_since_breakdown = len(candles) - 1 - breakdown_index
        if bars_since_breakdown > self.max_bars_since_breakdown and not prearmed_breakdown:
            return None

        max_reclaim = prev_low * (self.max_reclaim_bps / 10_000)

        highest_after_breakdown = max(c.high for c in candles_after_breakdown) if candles_after_breakdown else entry_candle.high
        reclaim_ceiling = prev_low + max_reclaim
        if highest_after_breakdown > reclaim_ceiling and not prearmed_breakdown:
            return None

        if entry_candle.close >= prev_low and not prearmed_breakdown:
            return None

        entry_extension_pct = ((prev_low - entry_candle.close) / prev_low) * 100 if prev_low else 0.0
        if entry_extension_pct > self.max_entry_extension_pct:
            self._log_rejection(
                market,
                "entry_too_extended_after_breakdown",
                [
                    f"entry_extension_pct={entry_extension_pct:.3f}",
                    f"max_entry_extension_pct={self.max_entry_extension_pct:.3f}",
                    f"entry_close={entry_candle.close:.6f}",
                    f"prev_low={prev_low:.6f}",
                ],
            )
            return None

        if entry_candle.close >= entry_candle.open and not prearmed_breakdown:
            return None

        followthrough_volume_ratio = self._volume_ratio_at(candles, len(candles) - 1)
        required_followthrough = 0.45 if prearmed_breakdown else self.min_followthrough_volume_ratio
        if followthrough_volume_ratio < required_followthrough:
            self._log_rejection(
                market,
                "weak_followthrough_participation",
                [
                    f"followthrough_volume_ratio={followthrough_volume_ratio:.2f}",
                    f"required={required_followthrough:.2f}",
                    f"prearmed={prearmed_breakdown}",
                ],
            )
            return None

        entry_range = entry_candle.high - entry_candle.low
        if entry_range <= 0:
            return None

        close_position_pct = (entry_candle.close - entry_candle.low) / entry_range

        allowed_close_position_pct = 0.65 if prearmed_breakdown else self.max_close_position_pct

        if close_position_pct > allowed_close_position_pct:
            return None

        invalidation = max(entry_candle.high, breakdown_candle.high)

        detection = BreakoutDetection(
            breakout_level=prev_low,
            close=entry_candle.close,
            entry_hint=entry_candle.close,
            reclaim_level=prev_low,
            invalidation=invalidation,
            sweep_extreme=invalidation,
            bars_since_sweep=bars_since_breakdown,
            volume_ratio=vol_ratio,
            volume_ratio_on_sweep=vol_ratio,
            displacement_pct=breakdown_pct,
            range_pct=((prev_high - prev_low) / entry_candle.close) * 100 if entry_candle.close else 0,
            local_range_size_pct=((prev_high - prev_low) / entry_candle.close) * 100 if entry_candle.close else 0,
            bars_lookback=self.lookback,
            reason_flags=[
                "breakdown below range",
                "volume expansion",
                "reclaim failed below breakdown level",
                "strong continuation close",
                "prearmed_breakdown" if prearmed_breakdown else "standard_breakdown",
                "mtf_prearmed_override" if mtf_prearmed_override else "strict_trend_alignment",
                f"prearmed_expansion_prob={prearmed_context_data.get('expansion_prob', 0.0):.1f}",
                f"prearmed_pressure_score={prearmed_context_data.get('pressure_score', 0.0):.2f}",
                f"participation_score={self._participation_score(candles, breakdown_index, direction='SHORT'):.2f}",
                f"followthrough_volume_ratio={self._volume_ratio_at(candles, len(candles) - 1):.2f}",
            ],
        )

        return StrategyCandidate(
            symbol=market.symbol,
            strategy=self.name,
            direction="SHORT",
            primary_granularity=market.primary.granularity,
            confirmation_granularity=market.confirmation.granularity,
            market=market,
            detection=detection,
            notes=[
                "breakdown below range",
                "volume expansion",
                "reclaim failed below breakdown level",
                "strong continuation close",
                "prearmed_breakdown" if prearmed_breakdown else "standard_breakdown",
                "mtf_prearmed_override" if mtf_prearmed_override else "strict_trend_alignment",
                f"prearmed_expansion_prob={prearmed_context_data.get('expansion_prob', 0.0):.1f}",
                f"prearmed_pressure_score={prearmed_context_data.get('pressure_score', 0.0):.2f}",
                f"breakdown_pct={breakdown_pct:.2f}",
                f"volume_ratio={vol_ratio:.2f}",
                f"bars_since_breakdown={bars_since_breakdown}",
                f"participation_score={self._participation_score(candles, breakdown_index, direction='SHORT'):.2f}",
                f"followthrough_volume_ratio={self._volume_ratio_at(candles, len(candles) - 1):.2f}",
                f"directional_pressure_ok={breakdown_pressure_ok}",
            ],
        )
    def _log_breakdown_rejection(self, market: MarketSnapshot, reason: str, details: list[str]) -> None:
        raw_symbols = os.getenv("STRATEGY_DEBUG_SYMBOLS", "NEARUSDT,FETUSDT,FILUSDT,OPUSDT,ADAUSDT,LINKUSDT,WIFUSDT,AAVEUSDT")
        debug_symbols = {symbol.strip().upper() for symbol in raw_symbols.split(",") if symbol.strip()}

        if market.symbol.upper() not in debug_symbols:
            return

        joined_details = " | ".join(details[-6:]) if details else "no_details"

        logger.info(
            "MOMENTUM_BREAKDOWN_REJECT | %s | reason=%s | %s",
            market.symbol,
            reason,
            joined_details,
        )