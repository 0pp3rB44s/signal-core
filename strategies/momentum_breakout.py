import logging
from dataclasses import dataclass
from typing import Optional

from clients.schemas import Candle, MarketSnapshot, StrategyCandidate
from market_features.engine import closed_candle_at_offset, closed_window, latest_closed_candle


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
        self.min_volume_ratio = 0.90
        self.min_participation_score = 0.75
        self.min_followthrough_volume_ratio = 0.25
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
        self.strict_breakout_min_volume_ratio = 0.70
        self.context_min_expansion_prob = settings.breakout_context_min_expansion_prob
        self.context_min_pressure_score = settings.breakout_context_min_pressure_score
        self.context_min_structure_score = settings.breakout_context_min_structure_score
        self.context_high_prob_pressure_floor = settings.breakout_context_high_prob_pressure_floor

    def detect(self, market: MarketSnapshot) -> Optional[StrategyCandidate]:
        candles = closed_window(market.primary)

        if len(candles) < self.lookback + 5:
            return None

        # Reset per detect-call: coil-status mag nooit van een vorig symbool
        # blijven hangen (instance wordt hergebruikt over de hele watchlist).
        self._coil_candidate = False
        self._coil_distance_pct = 0.0

        entry_candle = latest_closed_candle(market.primary)

        primary_trend = (market.primary.trend or "").lower()
        confirmation_trend = (market.confirmation.trend or "").lower()
        pre_alignment_prearmed_context = self._prearmed_context(market, direction="LONG")
        strict_trend_alignment = (
            market.alignment == "aligned_bullish"
            and primary_trend == "bullish"
            and confirmation_trend == "bullish"
        )
        prearmed_context_override = bool(
            pre_alignment_prearmed_context.get("allowed")
            and pre_alignment_prearmed_context.get("breakout_context_ready")
            and pre_alignment_prearmed_context.get("has_structure")
            and pre_alignment_prearmed_context.get("breakout_direction_pressure")
            and float(pre_alignment_prearmed_context.get("pressure_score", 0.0)) >= self.context_min_pressure_score
            and float(pre_alignment_prearmed_context.get("expansion_prob", 0.0)) >= self.context_min_expansion_prob
        )
        mtf_prearmed_override = (
            pre_alignment_prearmed_context.get("allowed")
            and (
                (
                    market.alignment in {"aligned_bullish", "mixed"}
                    and confirmation_trend in {"bullish", "mixed", "neutral"}
                    and primary_trend in {"bullish", "mixed", "neutral"}
                )
                or prearmed_context_override
            )
        )
        strong_reversal_breakout_override = (
            bool(pre_alignment_prearmed_context.get("breakout_context_ready"))
            and float(pre_alignment_prearmed_context.get("pressure_score", 0.0)) >= 65.0
            and float(pre_alignment_prearmed_context.get("expansion_prob", 0.0)) >= 75.0
            and str(pre_alignment_prearmed_context.get("direction", "")).lower() == "bullish"
            and market.alignment in {"aligned_bearish", "mixed"}
            and primary_trend in {"bearish", "mixed", "neutral"}
        )

        if not strict_trend_alignment and not mtf_prearmed_override and not strong_reversal_breakout_override:
            self._log_rejection(
                market,
                "trend_not_aligned_for_breakout",
                [
                    f"alignment={market.alignment}",
                    f"primary_trend={primary_trend}",
                    f"confirmation_trend={confirmation_trend}",
                    f"mtf_prearmed_override={mtf_prearmed_override}",
                    f"strong_reversal_breakout_override={strong_reversal_breakout_override}",
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

        if strong_reversal_breakout_override and not strict_trend_alignment:
            logger.info(
                "STRONG_REVERSAL_BREAKOUT_OVERRIDE | %s | direction=LONG | alignment=%s | primary=%s | confirmation=%s | pressure_score=%.2f | expansion_prob=%.1f",
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
        ) or bool(prearmed_context.get("breakout_context_ready")) or bool(prearmed_context.get("high_prob_context"))

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
        for offset in range(1, 4):
            candidate_index = len(candles) - offset
            candidate_candle = closed_candle_at_offset(market.primary, offset - 1)
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
                candidate_window = closed_window(market.primary, self.lookback + 1)[:-1]

                if len(candidate_window) >= self.lookback:
                    candidate_prev_high = max(c.high for c in candidate_window)
                    candidate_prev_low = min(c.low for c in candidate_window)
                    candidate_vol_ratio = self._volume_ratio_at(candles, len(candles) - 1)
                    participation_score = self._participation_score(
                        candles,
                        len(candles) - 1,
                        direction="LONG",
                    )

                    high_quality_prearmed = bool(
                        prearmed_context.get("allowed")
                        and prearmed_context.get("breakout_context_ready")
                        and prearmed_context.get("has_structure")
                        and prearmed_context.get("breakout_direction_pressure")
                        and float(prearmed_context.get("pressure_score", 0.0)) >= self.context_min_pressure_score
                        and float(prearmed_context.get("expansion_prob", 0.0)) >= self.context_min_expansion_prob
                    )
                    required_prearmed_volume = 0.20 if high_quality_prearmed else (0.25 if prearmed_context.get("breakout_context_ready") else self.prearmed_min_volume_ratio)
                    choppy_window = self._is_choppy(candidate_window)

                    required_prearmed_participation = 0.55 if high_quality_prearmed else 0.75
                    if (
                        candidate_vol_ratio >= required_prearmed_volume
                        and participation_score >= required_prearmed_participation
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

                        # Pre-breakout coil: prijs zit opgerold vlak ONDER het
                        # triggerniveau (nog geen uitbraak) met hoge druk.
                        # Forward-return studie 2026-07-07 (12 symbolen, 331
                        # entries): coils halen TP1 vaker dan post-breakout
                        # chases; de coil-na-expansie bucket was zelfs de enige
                        # netto-positieve (+0.198R, 61.5% TP1, n=26). De
                        # exhaustion-gate behandelt coil-kandidaten daarom als
                        # probe i.p.v. hard block.
                        coil_distance_pct = (
                            (candidate_prev_high - entry_candle.close) / candidate_prev_high * 100
                            if candidate_prev_high
                            else 999.0
                        )
                        if 0.0 <= coil_distance_pct <= 0.20 and float(prearmed_context.get("pressure_score", 0.0)) >= 55.0:
                            self._coil_candidate = True
                            self._coil_distance_pct = round(coil_distance_pct, 4)
                        else:
                            self._coil_candidate = False

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

        logger.info(
            "MOMENTUM_FUNNEL | %s | strategy=%s | direction=LONG | stage=CANDIDATE_CREATED | result=PASS | pressure_score=%.2f | expansion_prob=%.1f | volume_ratio=%.2f | participation_score=%.2f | followthrough_volume_ratio=%.2f | prearmed=%s",
            market.symbol,
            self.name,
            float(prearmed_context_data.get("pressure_score", 0.0)),
            float(prearmed_context_data.get("expansion_prob", 0.0)),
            float(vol_ratio or 0.0),
            self._participation_score(candles, breakout_index, direction="LONG"),
            self._volume_ratio_at(candles, len(candles) - 1),
            prearmed_breakout,
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
                f"breakout_context_ready={bool(prearmed_context_data.get('breakout_context_ready', False))}",
                f"prearmed_context_allowed={bool(prearmed_context_data.get('allowed', False))}",
                f"prearmed_context_override={bool(locals().get('prearmed_context_override', False))}",
                f"structure_score={prearmed_context_data.get('structure_score', 0)}",
                f"high_quality_breakout_context={bool(prearmed_context_data.get('high_quality_breakout_context', False))}",
            ]
            + (
                [
                    "entry_model=pre_breakout_coil",
                    f"coil_distance_pct={getattr(self, '_coil_distance_pct', 0.0):.4f}",
                ]
                if getattr(self, "_coil_candidate", False)
                else []
            ),
        )

    def _prearmed_context(self, market: "MarketSnapshot", direction: str) -> dict[str, float | bool | str]:
        notes_text = " | ".join(str(note).lower() for note in (market.notes or []))
        spread_bps = self._extract_spread_bps(market.notes or [], 99.0)
        expansion_prob = self._extract_note_float(market.notes or [], "expansion_prob=", 0.0)
        pressure_score = self._extract_note_float(market.notes or [], "pressure_score=", 0.0)

        wanted_pressure = "bullish" if direction == "LONG" else "bearish"
        breakout_context_ready = "breakout_context ready=true" in notes_text
        breakout_context_present = "breakout_context" in notes_text
        breakout_context_direction = f"direction={wanted_pressure}" in notes_text
        volatility_pressure_direction = f"pressure={wanted_pressure}" in notes_text
        named_breakout_pressure = f"{wanted_pressure} breakout pressure" in notes_text

        has_range_tightening = "range tightening" in notes_text or "range_tightening=true" in notes_text
        has_higher_lows = "higher lows building" in notes_text or "higher_lows_building=true" in notes_text
        has_lower_highs = "lower highs building" in notes_text or "lower_highs_building=true" in notes_text
        has_closes_pressing_highs = "closes pressing highs" in notes_text or "closes_pressing_highs=true" in notes_text
        has_closes_pressing_lows = "closes pressing lows" in notes_text or "closes_pressing_lows=true" in notes_text

        structure_score = 0
        if has_range_tightening:
            structure_score += 1
        if (has_higher_lows if direction == "LONG" else has_lower_highs):
            structure_score += 1
        if (has_closes_pressing_highs if direction == "LONG" else has_closes_pressing_lows):
            structure_score += 1

        has_structure = structure_score >= self.context_min_structure_score

        high_prob_context = (
            expansion_prob >= self.context_min_expansion_prob
            and pressure_score >= self.context_high_prob_pressure_floor
            and has_structure
        )

        breakout_direction_pressure = (
            (breakout_context_direction or volatility_pressure_direction or named_breakout_pressure or high_prob_context)
            and has_structure
            and expansion_prob >= self.context_min_expansion_prob
            and pressure_score >= self.context_high_prob_pressure_floor
        )

        inferred_breakout_context_ready = bool(
            breakout_context_ready
            or (
                breakout_context_present
                and breakout_context_direction
                and has_structure
                and expansion_prob >= self.context_min_expansion_prob
                and pressure_score >= self.context_high_prob_pressure_floor
            )
            or high_prob_context
        )

        has_directional_pressure = (
            volatility_pressure_direction
            or named_breakout_pressure
            or (inferred_breakout_context_ready and (breakout_context_direction or high_prob_context))
            or breakout_direction_pressure
        )

        compression_or_high_prob = (
            "compression=true" in notes_text
            or inferred_breakout_context_ready
            or expansion_prob >= self.prearmed_min_expansion_prob
        )

        required_pressure_score = self.context_min_pressure_score if inferred_breakout_context_ready else 50.0

        high_quality_breakout_context = (
            inferred_breakout_context_ready
            and has_directional_pressure
            and has_structure
            and pressure_score >= max(65.0, self.context_min_pressure_score)
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

        context_reasons = []
        if not compression_or_high_prob:
            context_reasons.append("no_compression_or_high_probability")
        if not has_directional_pressure:
            context_reasons.append("no_directional_pressure")
        if not has_structure:
            context_reasons.append(f"structure_score={structure_score}<min")
        if spread_bps > effective_max_spread_bps:
            context_reasons.append(f"spread_bps={spread_bps:.2f}>max_{effective_max_spread_bps:.2f}")
        if pressure_score < required_pressure_score:
            context_reasons.append(f"pressure_score={pressure_score:.2f}<required_{required_pressure_score:.2f}")

        # Observability enhancements
        spread_found = spread_bps != 99.0
        spread_source = "parsed" if spread_found else "missing_fallback_99"
        structure_source_parts = []
        if has_range_tightening:
            structure_source_parts.append("range_tightening")
        if has_higher_lows:
            structure_source_parts.append("higher_lows")
        if has_lower_highs:
            structure_source_parts.append("lower_highs")
        if has_closes_pressing_highs:
            structure_source_parts.append("closes_pressing_highs")
        if has_closes_pressing_lows:
            structure_source_parts.append("closes_pressing_lows")
        structure_source = ",".join(structure_source_parts) if structure_source_parts else "missing"
        notes_sample = " || ".join(str(note) for note in (market.notes or [])[-10:])

        debug_symbols = self.settings.strategy_debug_symbol_set

        if market.symbol.upper() in debug_symbols:
            logger.info(
                "PREARMED_CONTEXT | %s | direction=%s | allowed=%s | spread_bps=%.3f | max_spread_bps=%.3f | pressure_score=%.2f | required_pressure=%.2f | expansion_prob=%.1f | ready=%s | directional=%s | structure=%s | high_quality=%s | compression_or_high_prob=%s | breakout_context_present=%s | breakout_context_direction=%s | volatility_pressure_direction=%s | named_breakout_pressure=%s | has_structure=%s | breakout_direction_pressure=%s | spread_found=%s | spread_source=%s | structure_source=%s | notes_count=%s | notes_sample=%s",
                market.symbol,
                direction,
                allowed,
                spread_bps,
                effective_max_spread_bps,
                pressure_score,
                required_pressure_score,
                expansion_prob,
                inferred_breakout_context_ready,
                has_directional_pressure,
                has_structure,
                high_quality_breakout_context,
                compression_or_high_prob,
                breakout_context_present,
                breakout_context_direction,
                volatility_pressure_direction,
                named_breakout_pressure,
                has_structure,
                breakout_direction_pressure,
                spread_found,
                spread_source,
                structure_source + f"|score={structure_score}",
                len(market.notes or []),
                notes_sample,
            )

        return {
            "allowed": allowed,
            "spread_bps": spread_bps,
            "expansion_prob": expansion_prob,
            "pressure_score": pressure_score,
            "required_pressure_score": required_pressure_score,
            "effective_max_spread_bps": effective_max_spread_bps,
            "high_quality_breakout_context": high_quality_breakout_context,
            "breakout_context_ready": inferred_breakout_context_ready,
            "raw_breakout_context_ready": breakout_context_ready,
            "breakout_direction_pressure": breakout_direction_pressure,
            "high_prob_context": high_prob_context,
            "structure_score": structure_score,
            "context_reasons": context_reasons,
            "direction": wanted_pressure,
            "breakout_context_present": breakout_context_present,
            "breakout_context_direction": breakout_context_direction,
            "volatility_pressure_direction": volatility_pressure_direction,
            "named_breakout_pressure": named_breakout_pressure,
            "has_structure": has_structure,
            "spread_found": spread_found,
            "spread_source": spread_source,
            "structure_source": structure_source,
            "notes_count": len(market.notes or []),
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
        markers = ["spread_bps=", "spread_bps:", "spread=", "spread "]
        for marker in markers:
            if marker not in note_text:
                continue
            try:
                raw = note_text.split(marker, 1)[1].split()[0].strip("|,;")
                raw = raw.replace("bps", "")
                return float(raw)
            except Exception:
                continue
        return default

    def _log_rejection(self, market: MarketSnapshot, reason: str, details: list[str]) -> None:
        debug_symbols = self.settings.strategy_debug_symbol_set
        audit_mode = self.settings.momentum_funnel_audit

        if not audit_mode and market.symbol.upper() not in debug_symbols:
            return

        joined_details = " | ".join(details[-6:]) if details else "no_details"
        stage = str(reason or "unknown")
        logger.info(
            "MOMENTUM_FUNNEL | %s | strategy=%s | direction=LONG | stage=%s | result=FAIL | %s",
            market.symbol,
            self.name,
            stage,
            joined_details,
        )
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

    def _participation_score(self, candles: list[Candle], index: int, direction: str) -> float:
        """Score sustained participation around breakout/breakdown, not just one candle."""
        if index < 3:
            return 0.0

        recent_indexes = [index - 2, index - 1, index]
        ratios = [self._volume_ratio_at(candles, i) for i in recent_indexes]
        candles_slice = [candles[i] for i in recent_indexes]

        score = 0.0

        if ratios[2] >= self.min_volume_ratio:
            score += 1.0

        if ratios[2] >= ratios[1] >= ratios[0] and ratios[2] >= 1.0:
            score += 0.75

        if sum(1 for ratio in ratios if ratio >= 0.80) >= 2:
            score += 0.50

        if direction == "LONG":
            directional_closes = sum(1 for candle in candles_slice if candle.close > candle.open)
            closes_progress = candles_slice[2].close > candles_slice[1].close >= candles_slice[0].close
        else:
            directional_closes = sum(1 for candle in candles_slice if candle.close < candle.open)
            closes_progress = candles_slice[2].close < candles_slice[1].close <= candles_slice[0].close

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
        self.min_volume_ratio = 0.90
        self.min_participation_score = 0.75
        self.min_followthrough_volume_ratio = 0.25
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
        self.strict_breakdown_min_volume_ratio = 0.70
        self.context_min_expansion_prob = settings.breakout_context_min_expansion_prob
        self.context_min_pressure_score = settings.breakout_context_min_pressure_score
        self.context_min_structure_score = settings.breakout_context_min_structure_score
        self.context_high_prob_pressure_floor = settings.breakout_context_high_prob_pressure_floor

    def detect(self, market: MarketSnapshot) -> Optional[StrategyCandidate]:
        candles = closed_window(market.primary)

        if len(candles) < self.lookback + 5:
            return None

        # Zelfde reset als de LONG-kant: geen coil-status van vorig symbool.
        self._coil_candidate = False
        self._coil_distance_pct = 0.0

        entry_candle = latest_closed_candle(market.primary)

        primary_trend = (market.primary.trend or "").lower()
        confirmation_trend = (market.confirmation.trend or "").lower()
        pre_alignment_prearmed_context = self._prearmed_context(market, direction="SHORT")
        strict_trend_alignment = (
            market.alignment == "aligned_bearish"
            and primary_trend == "bearish"
            and confirmation_trend == "bearish"
        )
        prearmed_context_override = bool(
            pre_alignment_prearmed_context.get("allowed")
            and pre_alignment_prearmed_context.get("breakout_context_ready")
            and pre_alignment_prearmed_context.get("has_structure")
            and pre_alignment_prearmed_context.get("breakout_direction_pressure")
            and float(pre_alignment_prearmed_context.get("pressure_score", 0.0)) >= self.context_min_pressure_score
            and float(pre_alignment_prearmed_context.get("expansion_prob", 0.0)) >= self.context_min_expansion_prob
        )
        mtf_prearmed_override = (
            pre_alignment_prearmed_context.get("allowed")
            and (
                (
                    market.alignment in {"aligned_bearish", "mixed"}
                    and confirmation_trend in {"bearish", "mixed", "neutral"}
                    and primary_trend in {"bearish", "mixed", "neutral"}
                )
                or prearmed_context_override
            )
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
        ) or bool(prearmed_context.get("breakout_context_ready")) or bool(prearmed_context.get("high_prob_context"))

        if not breakdown_pressure_ok:
            self._log_breakdown_rejection(
                market,
                "breakdown_lacks_directional_pressure",
                [
                    f"pressure_score={float(prearmed_context.get('pressure_score', 0.0)):.2f}",
                    f"expansion_prob={float(prearmed_context.get('expansion_prob', 0.0)):.1f}",
                    f"breakout_context_ready={prearmed_context.get('breakout_context_ready')}",
                    f"required_score={self.weak_pressure_min_score:.2f}",
                    f"required_expansion={self.weak_pressure_min_expansion_prob:.1f}",
                    f"prearmed_allowed={prearmed_context.get('allowed')}",
                    f"breakout_direction_pressure={prearmed_context.get('breakout_direction_pressure')}",
                ],
            )
            return None

        for offset in range(1, 4):
            candidate_index = len(candles) - offset
            candidate_candle = closed_candle_at_offset(market.primary, offset - 1)
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
                candidate_window = closed_window(market.primary, self.lookback + 1)[:-1]

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

                    required_prearmed_participation = 0.55 if high_quality_prearmed else 0.75
                    if (
                        candidate_vol_ratio >= required_prearmed_volume
                        and participation_score >= required_prearmed_participation
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

                        # Spiegel van de LONG-coil: opgerold vlak BOVEN het
                        # trigger-level (prev_low), nog geen uitbraak omlaag.
                        coil_distance_pct = (
                            (entry_candle.close - candidate_prev_low) / candidate_prev_low * 100
                            if candidate_prev_low
                            else 999.0
                        )
                        if 0.0 <= coil_distance_pct <= 0.20 and float(prearmed_context.get("pressure_score", 0.0)) >= 55.0:
                            self._coil_candidate = True
                            self._coil_distance_pct = round(coil_distance_pct, 4)
                        else:
                            self._coil_candidate = False

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
            self._log_breakdown_rejection(market, "no_confirmation_candles_after_breakdown", [])
            return None

        bars_since_breakdown = len(candles) - 1 - breakdown_index
        if bars_since_breakdown > self.max_bars_since_breakdown and not prearmed_breakdown:
            self._log_breakdown_rejection(
                market,
                "late_breakdown_entry",
                [
                    f"bars_since_breakdown={bars_since_breakdown}",
                    f"max_bars_since_breakdown={self.max_bars_since_breakdown}",
                ],
            )
            return None

        max_reclaim = prev_low * (self.max_reclaim_bps / 10_000)

        highest_after_breakdown = max(c.high for c in candles_after_breakdown) if candles_after_breakdown else entry_candle.high
        reclaim_ceiling = prev_low + max_reclaim
        if highest_after_breakdown > reclaim_ceiling and not prearmed_breakdown:
            self._log_breakdown_rejection(
                market,
                "reclaim_broke_breakdown_level",
                [
                    f"highest_after_breakdown={highest_after_breakdown:.6f}",
                    f"reclaim_ceiling={reclaim_ceiling:.6f}",
                    f"prev_low={prev_low:.6f}",
                    f"max_reclaim_bps={self.max_reclaim_bps}",
                ],
            )
            return None

        if entry_candle.close >= prev_low and not prearmed_breakdown:
            self._log_breakdown_rejection(
                market,
                "entry_close_above_breakdown_level",
                [f"entry_close={entry_candle.close:.6f}", f"prev_low={prev_low:.6f}"],
            )
            return None

        entry_extension_pct = ((prev_low - entry_candle.close) / prev_low) * 100 if prev_low else 0.0
        if entry_extension_pct > self.max_entry_extension_pct:
            self._log_breakdown_rejection(
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
            self._log_breakdown_rejection(
                market,
                "entry_not_bearish_continuation",
                [
                    f"entry_close={entry_candle.close:.6f}",
                    f"entry_open={entry_candle.open:.6f}",
                    f"breakdown_close={breakdown_candle.close:.6f}",
                ],
            )
            return None

        followthrough_volume_ratio = self._volume_ratio_at(candles, len(candles) - 1)
        required_followthrough = 0.45 if prearmed_breakdown else self.min_followthrough_volume_ratio
        if followthrough_volume_ratio < required_followthrough:
            self._log_breakdown_rejection(
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
            self._log_breakdown_rejection(market, "invalid_entry_range", [])
            return None

        close_position_pct = (entry_candle.close - entry_candle.low) / entry_range

        allowed_close_position_pct = 0.65 if prearmed_breakdown else self.max_close_position_pct

        if close_position_pct > allowed_close_position_pct:
            self._log_breakdown_rejection(
                market,
                "weak_entry_close_for_breakdown",
                [
                    f"close_position_pct={close_position_pct:.2f}",
                    f"allowed={allowed_close_position_pct:.2f}",
                ],
            )
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

        logger.info(
            "MOMENTUM_FUNNEL | %s | strategy=%s | direction=SHORT | stage=CANDIDATE_CREATED | result=PASS | pressure_score=%.2f | expansion_prob=%.1f | volume_ratio=%.2f | participation_score=%.2f | followthrough_volume_ratio=%.2f | prearmed=%s",
            market.symbol,
            self.name,
            float(prearmed_context_data.get("pressure_score", 0.0)),
            float(prearmed_context_data.get("expansion_prob", 0.0)),
            float(vol_ratio or 0.0),
            self._participation_score(candles, breakdown_index, direction="SHORT"),
            self._volume_ratio_at(candles, len(candles) - 1),
            prearmed_breakdown,
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
                f"breakout_context_ready={bool(prearmed_context_data.get('breakout_context_ready', False))}",
                f"prearmed_context_allowed={bool(prearmed_context_data.get('allowed', False))}",
                f"prearmed_context_override={bool(locals().get('prearmed_context_override', False))}",
                f"structure_score={prearmed_context_data.get('structure_score', 0)}",
                f"high_quality_breakout_context={bool(prearmed_context_data.get('high_quality_breakout_context', False))}",
            ]
            + (
                [
                    "entry_model=pre_breakout_coil",
                    f"coil_distance_pct={getattr(self, '_coil_distance_pct', 0.0):.4f}",
                ]
                if getattr(self, "_coil_candidate", False)
                else []
            ),
        )
    def _log_breakdown_rejection(self, market: MarketSnapshot, reason: str, details: list[str]) -> None:
        debug_symbols = self.settings.strategy_debug_symbol_set
        audit_mode = self.settings.momentum_funnel_audit

        if not audit_mode and market.symbol.upper() not in debug_symbols:
            return

        joined_details = " | ".join(details[-6:]) if details else "no_details"
        stage = str(reason or "unknown")
        logger.info(
            "MOMENTUM_FUNNEL | %s | strategy=%s | direction=SHORT | stage=%s | result=FAIL | %s",
            market.symbol,
            self.name,
            stage,
            joined_details,
        )
        logger.info(
            "MOMENTUM_BREAKDOWN_REJECT | %s | reason=%s | %s",
            market.symbol,
            reason,
            joined_details,
        )
