from app.config import Settings
from clients.schemas import StrategyCandidate, StrategyScore


class StrategyScorer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @staticmethod
    def _notes_text(candidate: StrategyCandidate) -> str:
        det = candidate.detection
        market_notes = [str(note) for note in (getattr(candidate.market, "notes", []) or [])]

        return " ".join(
            [str(note) for note in (candidate.notes or [])]
            + market_notes
            + [str(flag) for flag in (getattr(det, "reason_flags", []) or [])]
        ).lower()

    @staticmethod
    def _extract_note_float(candidate: StrategyCandidate, marker: str, default: float = 0.0) -> float:
        notes_text = StrategyScorer._notes_text(candidate)
        marker = marker.lower()
        if marker not in notes_text:
            return default
        try:
            raw = notes_text.split(marker, 1)[1].split()[0].strip("|,;").replace("bps", "")
            return float(raw)
        except Exception:
            return default

    @staticmethod
    def _has_mtf_override(candidate: StrategyCandidate) -> bool:
        notes_text = StrategyScorer._notes_text(candidate)
        return (
            "mtf_prearmed_override" in notes_text
            or "prearmed_breakout" in notes_text
            or "prearmed_breakdown" in notes_text
            or "mtf_sweep_mode=mtf_override" in notes_text
            or "mtf_sweep_mode mtf_override" in notes_text
            or "mtf_continuation_mode mtf_override" in notes_text
            or "mtf_reclaim_mode mtf_override" in notes_text
        )

    @staticmethod
    def _mtf_pressure_score(candidate: StrategyCandidate) -> float:
        for marker in (
            "mtf_pressure_score=",
            "mtf_pressure_score ",
            "prearmed_pressure_score=",
            "prearmed_pressure_score ",
            "pressure_score=",
            "pressure_score ",
        ):
            value = StrategyScorer._extract_note_float(candidate, marker, 0.0)
            if value:
                return value
        return 0.0

    @staticmethod
    def _mtf_expansion_prob(candidate: StrategyCandidate) -> float:
        for marker in (
            "mtf_expansion_prob=",
            "mtf_expansion_prob ",
            "prearmed_expansion_prob=",
            "prearmed_expansion_prob ",
            "expansion_prob=",
            "expansion_prob ",
        ):
            value = StrategyScorer._extract_note_float(candidate, marker, 0.0)
            if value:
                return value
        return 0.0

    @staticmethod
    def _pressure_score(candidate: StrategyCandidate) -> float:
        for marker in (
            "pressure_score=",
            "pressure_score ",
            "mtf_pressure_score=",
            "mtf_pressure_score ",
            "prearmed_pressure_score=",
            "prearmed_pressure_score ",
        ):
            value = StrategyScorer._extract_note_float(candidate, marker, 0.0)
            if value:
                return value
        return 0.0

    @staticmethod
    def _expansion_prob(candidate: StrategyCandidate) -> float:
        for marker in (
            "expansion_prob=",
            "expansion_prob ",
            "mtf_expansion_prob=",
            "mtf_expansion_prob ",
            "prearmed_expansion_prob=",
            "prearmed_expansion_prob ",
        ):
            value = StrategyScorer._extract_note_float(candidate, marker, 0.0)
            if value:
                return value
        return 0.0

    @staticmethod
    def _breakout_context_ready(candidate: StrategyCandidate) -> bool:
        notes_text = StrategyScorer._notes_text(candidate)
        return (
            "breakout_context ready=true" in notes_text
            or "breakout_context_ready=true" in notes_text
            or "breakout setup ready" in notes_text
            or "breakout_ready=true" in notes_text
            or "breakout_ready true" in notes_text
            or "breakdown_ready=true" in notes_text
            or "breakdown_ready true" in notes_text
            or "entry_model=retest_zone_first" in notes_text
        )

    @staticmethod
    def _directional_pressure_ok(candidate: StrategyCandidate) -> bool:
        notes_text = StrategyScorer._notes_text(candidate)
        direction = (candidate.direction or "").upper()
        wanted = "bullish" if direction == "LONG" else "bearish"
        pressure_score = StrategyScorer._pressure_score(candidate)
        expansion_prob = StrategyScorer._expansion_prob(candidate)

        return (
            f"pressure={wanted}" in notes_text
            or f"direction={wanted}" in notes_text
            or StrategyScorer._breakout_context_ready(candidate)
            or (pressure_score >= 50.0 and expansion_prob >= 70.0)
        )

    @staticmethod
    def _continuation_pressure_penalty(candidate: StrategyCandidate) -> tuple[float, str | None]:
        notes_text = StrategyScorer._notes_text(candidate)
        pressure_score = StrategyScorer._pressure_score(candidate)
        expansion_prob = StrategyScorer._expansion_prob(candidate)
        pressure_ok = StrategyScorer._directional_pressure_ok(candidate)
        compression_quality = "compression_quality true" in notes_text or "compression_quality=true" in notes_text

        if pressure_ok:
            return 0.0, None

        if compression_quality and pressure_score >= 45.0 and expansion_prob >= 65.0:
            return -6.0, "soft_pressure_missing_in_compression"

        if pressure_score < 35.0:
            return -24.0, "no_directional_pressure"

        return -16.0, "weak_directional_pressure"

    @staticmethod
    def _score_mtf_confluence(candidate: StrategyCandidate) -> float:
        if not StrategyScorer._has_mtf_override(candidate):
            return 0.0

        pressure_score = StrategyScorer._mtf_pressure_score(candidate)
        expansion_prob = StrategyScorer._mtf_expansion_prob(candidate)

        score = 2.0
        if pressure_score >= 65.0:
            score += 5.0
        elif pressure_score >= 50.0:
            score += 3.0

        if expansion_prob >= 85.0:
            score += 5.0
        elif expansion_prob >= 70.0:
            score += 3.0

        if candidate.market.alignment == "mixed":
            score += 2.0

        return min(12.0, score)

    def score(self, candidate: StrategyCandidate) -> StrategyScore:
        strategy_name = (candidate.strategy or "").lower()
        notes_text = self._notes_text(candidate)

        if (
            "low_vol_reclaim" in strategy_name
            or "low vol reclaim" in strategy_name
            or "entry_model=retest_zone_first" in notes_text
            or "low_vol_reclaim_confirmed" in notes_text
        ):
            return self._score_low_vol_reclaim(candidate)

        if strategy_name == "adaptive_momentum_continuation":
            return self._score_adaptive_momentum_continuation(candidate)

        if "fallback_candidate_bridge=true" in notes_text:
            return self._score_low_vol_reclaim(candidate)

        if "momentum" in strategy_name or "breakout" in strategy_name or "breakdown" in strategy_name:
            return self._score_momentum(candidate)
        if "continuation" in strategy_name:
            return self._score_continuation(candidate)
        return self._score_sweep(candidate)
    def _score_adaptive_momentum_continuation(self, candidate: StrategyCandidate) -> StrategyScore:
        notes_text = self._notes_text(candidate)

        strategy_name = (candidate.strategy or "").lower()

        if (
            "low_vol_reclaim" in strategy_name
            or "entry_model=retest_zone_first" in notes_text
            or "reclaim_unlock_v5=true" in notes_text
            or "mtf_reclaim_mode" in notes_text
        ):
            return self._score_low_vol_reclaim(candidate)
        breakdown: dict[str, float] = {}
        reasons: list[str] = []

        fallback_execution_score = self._extract_note_float(candidate, "fallback_execution_score=", 0.0)
        entry_quality_long = max(
            self._extract_note_float(candidate, "entry_quality_long=", 0.0),
            self._extract_note_float(candidate, "entry_quality long=", 0.0),
        )
        entry_quality_short = max(
            self._extract_note_float(candidate, "entry_quality_short=", 0.0),
            self._extract_note_float(candidate, "entry_quality short=", 0.0),
        )
        pressure_score = self._pressure_score(candidate)
        expansion_prob = self._expansion_prob(candidate)
        close_position = max(
            self._extract_note_float(candidate, "close_position=", 0.0),
            self._extract_note_float(candidate, "close_position ", 0.5),
        )
        direction = (candidate.direction or "").upper()

        reclaim_strength_score = self._extract_note_float(
            candidate,
            "reclaim_strength_score=",
            0.0,
        )

        retest_quality_score = self._extract_note_float(
            candidate,
            "retest_quality_score=",
            0.0,
        )

        direction = (candidate.direction or "").upper()
        wanted_alignment = "aligned_bullish" if direction == "LONG" else "aligned_bearish"
        wanted_trend = "bullish" if direction == "LONG" else "bearish"
        entry_quality = entry_quality_long if direction == "LONG" else entry_quality_short

        base_score = fallback_execution_score or float(candidate.market.score_hint or 0.0)
        base_score = max(base_score, 45.0)

        breakdown["fallback_execution_score"] = min(base_score, 80.0)

        alignment_score = 0.0
        if candidate.market.alignment == wanted_alignment:
            alignment_score += 6.0
        if candidate.market.primary.trend == wanted_trend:
            alignment_score += 4.0
        if candidate.market.confirmation.trend == wanted_trend:
            alignment_score += 4.0
        breakdown["trend_alignment_bonus"] = min(alignment_score, 10.0)

        quality_bonus = 0.0
        if entry_quality >= 85.0:
            quality_bonus += 4.0
        elif entry_quality >= 75.0:
            quality_bonus += 2.0

        if "orderbook_risk_off=false" in notes_text and "orderbook_liquidity_ok=true" in notes_text:
            quality_bonus += 2.0

        if "range expansion" in notes_text or "range_expansion=true" in notes_text:
            quality_bonus += 2.0

        if "breakout_pressure=bullish" in notes_text and direction == "LONG":
            quality_bonus += 2.0
        elif "breakout_pressure=bearish" in notes_text and direction == "SHORT":
            quality_bonus += 2.0

        if expansion_prob >= 80.0:
            quality_bonus += 2.0
        elif expansion_prob >= 65.0:
            quality_bonus += 1.0

        if pressure_score >= 40.0:
            quality_bonus += 1.0

        breakdown["fallback_quality_bonus"] = min(quality_bonus, 10.0)

        penalty = 0.0
        penalty_reasons: list[str] = []

        if "orderbook_risk_off=true" in notes_text or "orderbook_available=false" in notes_text:
            penalty -= 8.0
            penalty_reasons.append("orderbook_risk_off")

        if "wide_spread_bps=" in notes_text:
            penalty -= 3.0
            penalty_reasons.append("wide_spread")

        if "possible spoofing/liquidity trap" in notes_text:
            penalty -= 4.0
            penalty_reasons.append("liquidity_trap_risk")

        if "atr_expansion_risk" in notes_text:
            penalty -= 4.0
            penalty_reasons.append("atr_expansion_risk")

        if entry_quality <= 0.0:
            penalty -= 4.0
            penalty_reasons.append("adaptive_entry_quality_missing")
        elif entry_quality < 75.0:
            penalty -= 28.0
            penalty_reasons.append("adaptive_entry_quality_below_75")
        elif entry_quality < 85.0:
            penalty -= 8.0
            penalty_reasons.append("adaptive_entry_quality_watch")

        if pressure_score < 35.0 and expansion_prob < 65.0:
            penalty -= 18.0
            penalty_reasons.append("adaptive_weak_pressure_and_expansion")
        elif pressure_score < 25.0:
            penalty -= 12.0
            penalty_reasons.append("adaptive_pressure_too_weak")

        if (
            "breakout_context ready=false" in notes_text
            and not self._breakout_context_ready(candidate)
            and pressure_score < 45.0
        ):
            penalty -= 12.0
            penalty_reasons.append("adaptive_no_breakout_context")

        breakdown["fallback_penalty"] = penalty


        if reclaim_strength_score > 0.0 and reclaim_strength_score < 60.0:
            breakdown["reclaim_quality_penalty"] = -8.0

        if retest_quality_score > 0.0 and retest_quality_score < 65.0:
            breakdown["retest_quality_penalty"] = -10.0

        total = round(max(sum(breakdown.values()), 0.0), 1)
        total = min(total, 88.0)
        verdict = self._verdict(total)

        reasons.extend(self._reason_pack(candidate, breakdown, total, verdict))
        reasons.append("score_profile=adaptive_momentum_continuation")
        reasons.append(f"fallback_execution_score={fallback_execution_score:.1f}")
        reasons.append(f"entry_quality={entry_quality:.1f}")
        reasons.append(f"pressure_score={pressure_score:.1f}")
        reasons.append(f"expansion_prob={expansion_prob:.1f}")
        if penalty_reasons:
            reasons.append(f"fallback_penalties={' | '.join(penalty_reasons)}")

        return StrategyScore(total=total, breakdown=breakdown, verdict=verdict, reasons=reasons)

    def _score_sweep(self, candidate: StrategyCandidate) -> StrategyScore:
        breakdown: dict[str, float] = {}
        reasons: list[str] = []

        # --- Context-quality logic for sweep (mirroring low-vol reclaim inputs) ---
        notes_text = self._notes_text(candidate)
        direction = (candidate.direction or "").upper()
        entry_quality_long = max(
            self._extract_note_float(candidate, "entry_quality_long=", 0.0),
            self._extract_note_float(candidate, "entry_quality long=", 0.0),
        )
        entry_quality_short = max(
            self._extract_note_float(candidate, "entry_quality_short=", 0.0),
            self._extract_note_float(candidate, "entry_quality short=", 0.0),
        )
        entry_quality = entry_quality_long if direction == "LONG" else entry_quality_short
        pressure_score = self._pressure_score(candidate)
        expansion_prob = self._expansion_prob(candidate)

        breakdown["sweep_quality"] = self._score_sweep_quality(candidate)
        breakdown["reclaim_quality"] = self._score_reclaim_quality(candidate)
        breakdown["market_structure"] = self._score_market_structure(candidate)
        breakdown["htf_alignment"] = self._score_htf_alignment(candidate)
        breakdown["volume_confirmation"] = self._score_volume_confirmation(candidate)
        breakdown["target_room"] = self._score_target_room(candidate)
        breakdown["cleanliness"] = self._score_cleanliness(candidate)
        breakdown["mtf_confluence"] = self._score_mtf_confluence(candidate)

        # --- Add sweep_context_quality and penalty, using same context-quality as low-vol reclaim ---
        sweep_context_bonus = 0.0
        if entry_quality >= 85.0:
            sweep_context_bonus += 6.0
        elif entry_quality >= 75.0:
            sweep_context_bonus += 4.0
        elif entry_quality >= 65.0:
            sweep_context_bonus += 2.0

        if pressure_score >= 70.0:
            sweep_context_bonus += 4.0
        elif pressure_score >= 55.0:
            sweep_context_bonus += 2.0

        if expansion_prob >= 80.0:
            sweep_context_bonus += 3.0
        elif expansion_prob >= 65.0:
            sweep_context_bonus += 1.0

        if self._breakout_context_ready(candidate):
            sweep_context_bonus += 3.0

        breakdown["sweep_context_quality"] = min(sweep_context_bonus, 14.0)

        sweep_penalty = 0.0
        sweep_penalty_reasons: list[str] = []

        if entry_quality <= 0.0:
            sweep_penalty -= 4.0
            sweep_penalty_reasons.append("sweep_entry_quality_missing")
        elif entry_quality < 65.0:
            sweep_penalty -= 10.0
            sweep_penalty_reasons.append("sweep_entry_quality_weak")

        if "orderbook_risk_off=true" in notes_text or "orderbook_available=false" in notes_text:
            sweep_penalty -= 10.0
            sweep_penalty_reasons.append("orderbook_risk_off")

        if not self._directional_pressure_ok(candidate) and pressure_score < 45.0:
            sweep_penalty -= 8.0
            sweep_penalty_reasons.append("sweep_directional_pressure_weak")

        breakdown["sweep_context_penalty"] = sweep_penalty

        total = round(max(sum(breakdown.values()), 0.0), 1)
        verdict = self._verdict(total)
        reasons.extend(self._reason_pack(candidate, breakdown, total, verdict))
        reasons.append("score_profile=liquidity_sweep_reversal")
        reasons.append(f"entry_quality={entry_quality:.1f}")
        reasons.append(f"pressure_score={pressure_score:.1f}")
        reasons.append(f"expansion_prob={expansion_prob:.1f}")
        if sweep_penalty_reasons:
            reasons.append(f"sweep_penalties={' | '.join(sweep_penalty_reasons)}")
        return StrategyScore(total=total, breakdown=breakdown, verdict=verdict, reasons=reasons)

    def _score_momentum(self, candidate: StrategyCandidate) -> StrategyScore:
        breakdown: dict[str, float] = {}
        reasons: list[str] = []
        det = candidate.detection
        primary = candidate.market.primary
        confirmation = candidate.market.confirmation

        notes_text = self._notes_text(candidate)
        pressure_score = self._pressure_score(candidate)
        expansion_prob = self._expansion_prob(candidate)

        reclaim_strength_score = self._extract_note_float(
            candidate,
            "reclaim_strength_score=",
            0.0,
        )

        retest_quality_score = self._extract_note_float(
            candidate,
            "retest_quality_score=",
            0.0,
        )

        mtf_override = self._has_mtf_override(candidate)
        mtf_score = self._score_mtf_confluence(candidate)
        is_prearmed = "prearmed_breakout" in notes_text or "prearmed_breakdown" in notes_text
        orderbook_risk_off = "orderbook_risk_off=true" in notes_text or "orderbook_available=false" in notes_text

        penalty_reasons: list[str] = []
        penalty_score = 0.0

        volume_ratio = getattr(det, "volume_ratio", 0.0)
        if volume_ratio == 0.0:
            penalty_reasons.append("volume_ratio_zero")
        breakout_level = getattr(det, "breakout_level", 0.0)
        close = getattr(det, "close", 0.0)
        range_pct = getattr(det, "range_pct", 0.0)

        direction = (candidate.direction or "").upper()
        if direction == "SHORT":
            breakout_pct = ((breakout_level - close) / breakout_level * 100) if breakout_level else 0.0
        else:
            breakout_pct = ((close - breakout_level) / breakout_level * 100) if breakout_level else 0.0

        breakout_score = 10.0
        if breakout_pct >= 0.35:
            breakout_score += 8.0
        elif breakout_pct >= 0.20:
            breakout_score += 6.0
        elif breakout_pct >= 0.10:
            breakout_score += 3.0
        breakdown["breakout_quality"] = min(20.0, breakout_score)

        volume_score = 4.0
        if volume_ratio >= 2.0:
            volume_score = 15.0
        elif volume_ratio >= 1.5:
            volume_score = 12.0
        elif volume_ratio >= 1.0:
            volume_score = 9.0
        elif volume_ratio >= 0.7:
            volume_score = 6.0
        breakdown["volume_expansion"] = volume_score

        trend_score = 0.0
        wanted_trend = "bearish" if direction == "SHORT" else "bullish"
        wanted_alignment = "aligned_bearish" if direction == "SHORT" else "aligned_bullish"

        if primary.trend == wanted_trend:
            trend_score += 10.0
        if confirmation.trend == wanted_trend:
            trend_score += 10.0
        if candidate.market.alignment == wanted_alignment:
            trend_score += 8.0
        breakdown["trend_alignment"] = min(25.0, trend_score)

        structure_score = 7.0
        if range_pct >= 0.8:
            structure_score += 5.0
        if primary.range_pct >= 0.7:
            structure_score += 3.0
        breakdown["market_structure"] = min(15.0, structure_score)

        trigger_score = 0.0
        if "breakout above range" in notes_text or "breakdown below range" in notes_text:
            trigger_score += 4.0
        if "pullback held breakout level" in notes_text or "pullback held breakdown level" in notes_text:
            trigger_score += 7.0
        if "strong continuation close" in notes_text:
            trigger_score += 5.0
        if self._breakout_context_ready(candidate):
            trigger_score += 4.0
        if is_prearmed:
            trigger_score += 5.0
            penalty_reasons.append("prearmed_momentum_profile")
        breakdown["trigger_quality"] = min(15.0, trigger_score)
        breakdown["mtf_confluence"] = mtf_score

        if pressure_score >= 60.0:
            breakdown["mtf_confluence"] += 2.0

        if expansion_prob >= 80.0:
            breakdown["mtf_confluence"] += 2.0
        if is_prearmed and pressure_score >= 55.0 and expansion_prob >= 75.0:
            breakdown["mtf_confluence"] += 4.0

        cleanliness_score = 5.0
        if abs(primary.change_pct) <= 4.0:
            cleanliness_score += 3.0
        if primary.volume_ratio_20 >= 0.8:
            cleanliness_score += 2.0
        breakdown["cleanliness"] = min(10.0, cleanliness_score)

        # Failed trade pattern detector
        if breakout_pct >= 1.20:
            penalty_score -= 10.0
            penalty_reasons.append("overextended_move")
        elif breakout_pct >= 0.85 and volume_ratio < 4.5:
            penalty_score -= 6.0
            penalty_reasons.append("extended_without_exceptional_volume")

        if volume_ratio < 0.9:
            if is_prearmed and pressure_score >= 55.0 and expansion_prob >= 75.0:
                penalty_score -= 2.0
                penalty_reasons.append("prearmed_low_followthrough_softened")
            else:
                penalty_score -= 3.0 if mtf_override else 6.0
                penalty_reasons.append("low_follow_through_mtf_softened" if mtf_override else "low_follow_through")

        if range_pct >= 3.5:
            penalty_score -= 5.0
            penalty_reasons.append("weak_continuation_candle")

        if abs(primary.change_pct) >= 6.0:
            if is_prearmed and pressure_score >= 60.0:
                penalty_score -= 2.0
                penalty_reasons.append("prearmed_late_entry_softened")
            else:
                penalty_score -= 5.0
                penalty_reasons.append("late_entry")

        if breakout_pct >= 0.8 and volume_ratio < 1.0:
            penalty_score -= 4.0 if mtf_override else 8.0
            penalty_reasons.append("fake_breakout_risk_mtf_softened" if mtf_override else "fake_breakout_risk")

        if "weak_continuation_candle" in notes_text:
            penalty_score -= 8.0
            penalty_reasons.append("weak_continuation_candle")

        quality_retest_structure = (
            "pullback held breakout level" in notes_text
            or "pullback held breakdown level" in notes_text
        )
        if quality_retest_structure and "strong continuation close" in notes_text and volume_ratio >= 3.0 and breakout_pct <= 0.65:
            penalty_score += 4.0
            penalty_reasons.append("quality_retest_structure_boost")

        if orderbook_risk_off:
            penalty_score -= 12.0
            penalty_reasons.append("orderbook_risk_off")

        breakdown["pattern_penalty"] = penalty_score

        total = round(max(sum(breakdown.values()), 0.0), 1)
        verdict = self._verdict(total)
        reasons.extend(self._reason_pack(candidate, breakdown, total, verdict))
        pct_label = "breakdown_pct" if direction == "SHORT" else "breakout_pct"
        reasons.append(f"{pct_label}={breakout_pct:.2f}")
        reasons.append(f"volume_ratio={volume_ratio:.2f}")
        if is_prearmed:
            reasons.append("score_profile=prearmed_momentum")
        if penalty_reasons:
            reasons.append(f"pattern_flags={' | '.join(penalty_reasons)}")
        return StrategyScore(total=total, breakdown=breakdown, verdict=verdict, reasons=reasons)

    def _score_continuation(self, candidate: StrategyCandidate) -> StrategyScore:
        breakdown: dict[str, float] = {}
        reasons: list[str] = []
        det = candidate.detection
        primary = candidate.market.primary
        confirmation = candidate.market.confirmation

        direction = (candidate.direction or "").upper()
        wanted_trend = "bearish" if direction == "SHORT" else "bullish"
        wanted_alignment = "aligned_bearish" if direction == "SHORT" else "aligned_bullish"
        notes_text = self._notes_text(candidate)
        pressure_score = self._pressure_score(candidate)
        expansion_prob = self._expansion_prob(candidate)
        reclaim_strength_score = self._extract_note_float(
            candidate,
            "reclaim_strength_score=",
            0.0,
        )
        retest_quality_score = self._extract_note_float(
            candidate,
            "retest_quality_score=",
            0.0,
        )
        mtf_override = self._has_mtf_override(candidate)
        mtf_score = self._score_mtf_confluence(candidate)
        if primary.volume_ratio_20 == 0.0:
            reasons.append("volume_ratio_zero")
        pressure_penalty, pressure_penalty_reason = self._continuation_pressure_penalty(candidate)
        orderbook_risk_off = "orderbook_risk_off=true" in notes_text or "orderbook_available=false" in notes_text

        alignment_score = 0.0
        if candidate.market.alignment == wanted_alignment:
            alignment_score += 12.0
        elif candidate.market.alignment == "mixed":
            alignment_score += 3.0
        if mtf_override and candidate.market.alignment == "mixed":
            alignment_score += 5.0
        if primary.trend == wanted_trend:
            alignment_score += 8.0
        if confirmation.trend == wanted_trend:
            alignment_score += 8.0
        breakdown["trend_alignment"] = min(25.0, alignment_score)

        trigger_score = 0.0
        if "pullback detected" in notes_text:
            trigger_score += 8.0
        if "reclaim confirmed" in notes_text:
            trigger_score += 8.0
        if "momentum present" in notes_text:
            trigger_score += 6.0

        if reclaim_strength_score >= 80.0:
            trigger_score += 6.0
        elif reclaim_strength_score >= 60.0:
            trigger_score += 3.0

        if retest_quality_score >= 85.0:
            trigger_score += 6.0
        elif retest_quality_score >= 70.0:
            trigger_score += 3.0
        breakdown["trigger_quality"] = min(20.0, trigger_score)
        breakdown["mtf_confluence"] = mtf_score
        if pressure_score >= 60.0:
            breakdown["mtf_confluence"] += 2.0
        if expansion_prob >= 80.0:
            breakdown["mtf_confluence"] += 2.0

        volume_score = 2.0
        if "volume expansion" in notes_text:
            volume_score = 15.0
        elif primary.volume_ratio_20 >= 1.5:
            volume_score = 12.0
        elif primary.volume_ratio_20 >= 1.1:
            volume_score = 8.0
        elif primary.volume_ratio_20 >= 0.8:
            volume_score = 5.0
        breakdown["volume_confirmation"] = volume_score

        structure_score = 5.0
        if primary.range_pct >= 0.7:
            structure_score += 5.0
        if primary.range_pct <= 2.5:
            structure_score += 3.0
        if abs(primary.change_pct) <= 3.5:
            structure_score += 2.0
        breakdown["market_structure"] = min(15.0, structure_score)

        room_score = 4.0
        local_range = getattr(det, "local_range_size_pct", 0.0)
        if local_range >= 1.5:
            room_score = 10.0
        elif local_range >= 1.0:
            room_score = 8.0
        elif local_range >= 0.6:
            room_score = 6.0
        breakdown["target_room"] = room_score

        cleanliness_score = 5.0
        if abs(primary.change_pct) <= 4.0:
            cleanliness_score += 2.0
        if primary.volume_ratio_20 >= 0.8:
            cleanliness_score += 2.0
        if confirmation.trend == wanted_trend:
            cleanliness_score += 1.0
        breakdown["cleanliness"] = min(10.0, cleanliness_score)

        penalty_score = pressure_penalty
        penalty_reasons: list[str] = []
        if pressure_penalty_reason:
            penalty_reasons.append(pressure_penalty_reason)
        if candidate.market.alignment != wanted_alignment:
            penalty_score -= 6.0 if mtf_override else 15.0
            penalty_reasons.append("alignment_not_clean_mtf_softened" if mtf_override else "alignment_not_clean")
        if primary.trend != wanted_trend or confirmation.trend != wanted_trend:
            penalty_score -= 5.0 if mtf_override else 12.0
            penalty_reasons.append("trend_not_confirmed_mtf_softened" if mtf_override else "trend_not_confirmed")
        if "volume expansion" not in notes_text and primary.volume_ratio_20 < 1.1:
            penalty_score -= 8.0
            penalty_reasons.append("weak_volume_confirmation")
        if not self._directional_pressure_ok(candidate) and primary.volume_ratio_20 < 0.50:
            penalty_score -= 10.0
            penalty_reasons.append("chop_continuation_volume_dead")
        if abs(primary.change_pct) >= 5.0:
            penalty_score -= 6.0
            penalty_reasons.append("late_or_overextended_entry")

        strategy_name = (candidate.strategy or "").lower()
        if direction == "SHORT":
            if pressure_score >= 62.0 and expansion_prob >= 78.0:
                penalty_score += 3.0
                penalty_reasons.append("short_continuation_pressure_confirmed")
            elif pressure_score < 55.0 or expansion_prob < 70.0:
                penalty_score -= 5.0
                penalty_reasons.append("short_continuation_pressure_discount")

        if "low_vol_reclaim" in strategy_name:
            if pressure_score >= 55.0:
                penalty_score += 3.0
                penalty_reasons.append("low_vol_reclaim_pressure_support")
            if expansion_prob >= 75.0:
                penalty_score += 2.0
                penalty_reasons.append("low_vol_reclaim_expansion_support")

        if reclaim_strength_score > 0.0 and reclaim_strength_score < 60.0:
            penalty_score -= 8.0
            penalty_reasons.append("reclaim_quality_weak")

        if retest_quality_score > 0.0 and retest_quality_score < 65.0:
            penalty_score -= 10.0
            penalty_reasons.append("retest_quality_weak")

        breakdown["pattern_penalty"] = penalty_score
        raw_total = sum(breakdown.values())
        total = round(max(raw_total, 0.0), 1)

        if not self._directional_pressure_ok(candidate):
            cap = 62.0 if mtf_override else 58.0
            if total > cap:
                penalty_reasons.append(f"continuation_score_capped_no_pressure_{cap:.0f}")
                total = cap
        verdict = self._verdict(total)
        reasons.extend(self._reason_pack(candidate, breakdown, total, verdict))
        if penalty_reasons:
            reasons.append(f"pattern_flags={' | '.join(penalty_reasons)}")
        if direction == "SHORT":
            reasons.append("score_profile=short_continuation")
        return StrategyScore(total=total, breakdown=breakdown, verdict=verdict, reasons=reasons)

    def _score_sweep_quality(self, candidate: StrategyCandidate) -> float:
        det = candidate.detection
        score = 8.0
        if det.bars_since_sweep <= 1:
            score += 6.0
        elif det.bars_since_sweep <= 3:
            score += 4.0
        if det.displacement_pct >= 0.30:
            score += 4.0
        elif det.displacement_pct >= 0.18:
            score += 2.0
        if det.local_range_size_pct >= 1.0:
            score += 2.0
        return min(20.0, score)

    def _score_reclaim_quality(self, candidate: StrategyCandidate) -> float:
        det = candidate.detection
        distance_bps = abs(det.entry_hint - det.reclaim_level) / det.entry_hint * 10_000 if det.entry_hint else 0.0
        score = 10.0
        if distance_bps <= 12:
            score += 6.0
        elif distance_bps <= 30:
            score += 3.0
        if det.bars_since_sweep == 0:
            score += 4.0
        elif det.bars_since_sweep <= 2:
            score += 2.0
        return min(20.0, score)

    @staticmethod
    def _score_market_structure(candidate: StrategyCandidate) -> float:
        primary = candidate.market.primary
        score = 7.0
        if primary.range_pct >= 0.7:
            score += 4.0
        if primary.trend != "mixed":
            score += 4.0
        return min(15.0, score)

    @staticmethod
    def _score_htf_alignment(candidate: StrategyCandidate) -> float:
        alignment = candidate.market.alignment
        direction = candidate.direction
        mtf_override = StrategyScorer._has_mtf_override(candidate)
        if alignment == "aligned_bullish" and direction == "LONG":
            return 15.0
        if alignment == "aligned_bearish" and direction == "SHORT":
            return 15.0
        if alignment == "mixed" and mtf_override:
            return 11.0
        if alignment == "mixed":
            return 7.0
        return 3.0

    @staticmethod
    def _score_volume_confirmation(candidate: StrategyCandidate) -> float:
        vr = getattr(candidate.detection, "volume_ratio_on_sweep", 0.0)
        if vr >= 1.8:
            return 10.0
        if vr >= 1.4:
            return 8.0
        if vr >= 1.1:
            return 6.0
        return 2.0

    @staticmethod
    def _score_target_room(candidate: StrategyCandidate) -> float:
        room = getattr(candidate.detection, "local_range_size_pct", 0.0)
        if room >= 1.8:
            return 10.0
        if room >= 1.0:
            return 7.0
        if room >= 0.6:
            return 5.0
        return 2.0

    @staticmethod
    def _score_cleanliness(candidate: StrategyCandidate) -> float:
        primary = candidate.market.primary
        score = 5.0
        if primary.volume_ratio_20 >= 1.2:
            score += 2.0
        if primary.range_pct <= 2.5:
            score += 2.0
        if abs(primary.change_pct) <= 3.0:
            score += 1.0
        return min(10.0, score)

    def _verdict(self, total: float) -> str:
        if total >= self.settings.strategy_score_go_threshold:
            return "GO"
        if total >= self.settings.strategy_score_watch_threshold:
            return "WATCH"
        return "NO_GO"

    @staticmethod
    def _reason_pack(candidate: StrategyCandidate, breakdown: dict[str, float], total: float, verdict: str) -> list[str]:
        top_factors = sorted(breakdown.items(), key=lambda kv: kv[1], reverse=True)[:3]
        reasons = [f"{k}={v:.1f}" for k, v in top_factors]
        if StrategyScorer._has_mtf_override(candidate):
            reasons.append("mtf_override=True")
            reasons.append(f"mtf_pressure_score={StrategyScorer._mtf_pressure_score(candidate):.2f}")
            reasons.append(f"mtf_expansion_prob={StrategyScorer._mtf_expansion_prob(candidate):.1f}")
        reasons.append(f"alignment={candidate.market.alignment}")
        reasons.append(f"score={total:.1f}")
        reasons.append(f"verdict={verdict}")
        return reasons

    def _score_low_vol_reclaim(self, candidate: StrategyCandidate) -> StrategyScore:
        notes_text = self._notes_text(candidate)
        primary = candidate.market.primary
        confirmation = candidate.market.confirmation
        direction = (candidate.direction or "").upper()
        wanted_trend = "bearish" if direction == "SHORT" else "bullish"
        wanted_alignment = "aligned_bearish" if direction == "SHORT" else "aligned_bullish"

        pressure_score = self._pressure_score(candidate)
        expansion_prob = self._expansion_prob(candidate)
        close_position = max(
            self._extract_note_float(candidate, "close_position=", 0.0),
            self._extract_note_float(candidate, "close_position ", 0.5),
        )
        volume_ratio = self._extract_note_float(candidate, "volume_ratio ", getattr(primary, "volume_ratio_20", 0.0) or 0.0)
        followthrough_volume_ratio = self._extract_note_float(candidate, "followthrough_volume_ratio ", 0.0)
        ema_retest_zone_pct = self._extract_note_float(candidate, "ema_retest_zone_pct ", 99.0)
        mtf_override = self._has_mtf_override(candidate)

        breakdown: dict[str, float] = {}
        reasons: list[str] = []

        alignment_score = 0.0
        if candidate.market.alignment == wanted_alignment:
            alignment_score += 16.0
        elif candidate.market.alignment == "mixed" and mtf_override:
            alignment_score += 11.0
        elif candidate.market.alignment == "mixed":
            alignment_score += 7.0
        else:
            alignment_score += 2.0

        if primary.trend == wanted_trend:
            alignment_score += 7.0
        if confirmation.trend == wanted_trend:
            alignment_score += 6.0
        breakdown["reclaim_alignment"] = min(alignment_score, 25.0)

        retest_score = 6.0
        if "entry_model=retest_zone_first" in notes_text:
            retest_score += 8.0
        if "long_retest_zone true" in notes_text or "short_retest_zone true" in notes_text:
            retest_score += 7.0
        if ema_retest_zone_pct <= 0.35:
            retest_score += 6.0
        elif ema_retest_zone_pct <= 0.85:
            retest_score += 4.0
        breakdown["retest_zone_quality"] = min(retest_score, 22.0)

        pressure_score_component = 0.0
        if pressure_score >= 70.0:
            pressure_score_component += 12.0
        elif pressure_score >= 55.0:
            pressure_score_component += 9.0
        elif pressure_score >= 40.0:
            pressure_score_component += 5.0
        else:
            pressure_score_component += 2.0

        if expansion_prob >= 85.0:
            pressure_score_component += 8.0
        elif expansion_prob >= 70.0:
            pressure_score_component += 5.0
        elif expansion_prob >= 55.0:
            pressure_score_component += 3.0
        breakdown["pressure_expansion"] = min(pressure_score_component, 20.0)

        volume_score = 3.0
        if volume_ratio >= 1.0:
            volume_score += 8.0
        elif volume_ratio >= 0.45:
            volume_score += 5.0
        elif volume_ratio >= 0.18:
            volume_score += 3.0

        if followthrough_volume_ratio >= 0.35:
            volume_score += 6.0
        elif followthrough_volume_ratio >= 0.18:
            volume_score += 4.0
        elif followthrough_volume_ratio >= 0.08:
            volume_score += 2.0
        breakdown["volume_followthrough"] = min(volume_score, 16.0)

        entry_score = 8.0
        if direction == "LONG":
            if 0.32 <= close_position <= 0.68:
                entry_score += 10.0
            elif close_position >= 0.72:
                entry_score -= 14.0
        elif direction == "SHORT":
            if 0.32 <= close_position <= 0.68:
                entry_score += 10.0
            elif close_position <= 0.28:
                entry_score -= 14.0
        breakdown["entry_position_quality"] = max(min(entry_score, 18.0), -16.0)

        cleanliness_score = 5.0
        if abs(getattr(primary, "change_pct", 0.0) or 0.0) <= 4.0:
            cleanliness_score += 3.0
        if getattr(primary, "range_pct", 0.0) <= 3.5:
            cleanliness_score += 2.0
        if "orderbook_risk_off=true" not in notes_text and "orderbook_available=false" not in notes_text:
            cleanliness_score += 2.0
        breakdown["cleanliness"] = min(cleanliness_score, 12.0)

        mtf_score = self._score_mtf_confluence(candidate)
        if mtf_score:
            breakdown["mtf_confluence"] = mtf_score

        penalty = 0.0
        penalty_reasons: list[str] = []
        if "orderbook_risk_off=true" in notes_text or "orderbook_available=false" in notes_text:
            penalty -= 10.0
            penalty_reasons.append("orderbook_risk_off")
        if direction == "LONG" and close_position >= 0.72:
            penalty -= 18.0
            penalty_reasons.append("late_long_near_high")
        if direction == "SHORT" and close_position <= 0.28:
            penalty -= 18.0
            penalty_reasons.append("late_short_near_low")
        if ema_retest_zone_pct > 1.25:
            penalty -= 8.0
            penalty_reasons.append("far_from_retest_zone")
        breakdown["reclaim_penalty"] = penalty

        total = round(max(sum(breakdown.values()), 0.0), 1)
        total = min(total, 92.0)

        if total >= 64.0:
            verdict = "GO"
        elif total >= 54.0:
            verdict = "WATCH"
        else:
            verdict = "NO_GO"

        reasons.extend(self._reason_pack(candidate, breakdown, total, verdict))
        reasons.append("score_profile=low_vol_reclaim")
        reasons.append("entry_model=retest_zone_first")
        reasons.append(f"close_position={close_position:.3f}")
        reasons.append(f"ema_retest_zone_pct={ema_retest_zone_pct:.3f}")
        reasons.append(f"volume_ratio={volume_ratio:.2f}")
        reasons.append(f"followthrough_volume_ratio={followthrough_volume_ratio:.2f}")
        reasons.append(f"pressure_score={pressure_score:.1f}")
        reasons.append(f"expansion_prob={expansion_prob:.1f}")
        if penalty_reasons:
            reasons.append(f"reclaim_penalties={' | '.join(penalty_reasons)}")

        return StrategyScore(total=total, breakdown=breakdown, verdict=verdict, reasons=reasons)