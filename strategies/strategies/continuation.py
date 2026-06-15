import logging
import os
from clients.schemas import MarketSnapshot, StrategyCandidate, SweepDetection

logger = logging.getLogger("StartupRunner")


class ContinuationStrategy:
    name = "trend_continuation"
    ISOLATION_DISABLED = True
    _last_reject_signature: dict[tuple[str, str], tuple[str, str, str, str]] = {}
    MIN_CONTINUATION_VOLUME_RATIO = 0.65
    MIN_CONTINUATION_VOLATILITY_RANK = 6.0
    MAX_RECLAIM_PROXIMITY_PCT = 1.00
    LONG_MAX_CLOSE_POSITION = 0.80
    SHORT_MIN_CLOSE_POSITION = 0.20
    MIN_PARTICIPATION_SCORE = 0.75
    MIN_FOLLOWTHROUGH_VOLUME_RATIO = 0.35
    # SHORT-specific stricter requirements
    SHORT_MIN_PARTICIPATION_SCORE = 1.10
    SHORT_MIN_FOLLOWTHROUGH_VOLUME_RATIO = 0.45
    SHORT_MIN_PRESSURE_SCORE = 48.0
    SHORT_MIN_EXPANSION_PROB = 65.0
    SHORT_MIN_VOLUME_RATIO = 0.80
    SHORT_MIN_VOLATILITY_RANK = 8.0
    MTF_OVERRIDE_MIN_PRESSURE_SCORE = 38.0
    MTF_OVERRIDE_MIN_EXPANSION_PROB = 62.0
    MTF_OVERRIDE_MIN_VOLUME_RATIO = 0.35
    MTF_OVERRIDE_MIN_VOLATILITY_RANK = 4.0
    COMPRESSION_MIN_PRESSURE_SCORE = 42.0
    COMPRESSION_MIN_EXPANSION_PROB = 60.0
    COMPRESSION_MIN_PARTICIPATION_SCORE = 0.55
    COMPRESSION_MIN_VOLUME_RATIO = 0.25
    COMPRESSION_MIN_VOLATILITY_RANK = 3.0

    def _reject(self, market: MarketSnapshot, reason: str, **context: object) -> None:
        symbol = str(market.symbol).upper()
        direction = str(context.get("direction") or "NA").upper()
        signature_key = (symbol, reason)
        reject_signature = (
            symbol,
            reason,
            direction,
            str(context.get("primary_trend", "")),
            str(context.get("confirmation_trend", "")),
        )

        if self._last_reject_signature.get(signature_key) == reject_signature:
            return

        self._last_reject_signature[signature_key] = reject_signature

        details = " | ".join(f"{key}={value}" for key, value in context.items())
        if details:
            logger.info("CONTINUATION_REJECT | %s | reason=%s | %s", market.symbol, reason, details)
        else:
            logger.info("CONTINUATION_REJECT | %s | reason=%s", market.symbol, reason)

    def _note_text(self, market: MarketSnapshot) -> str:
        return " | ".join(str(note).lower() for note in (getattr(market, "notes", []) or []))

    def _extract_note_float(self, market: MarketSnapshot, marker: str, default: float = 0.0) -> float:
        note_text = self._note_text(market)
        marker = marker.lower()
        if marker not in note_text:
            return default
        try:
            raw = note_text.split(marker, 1)[1].split()[0].strip("|,;")
            return float(raw)
        except Exception:
            return default

    def _gate_snapshot(
        self,
        market: MarketSnapshot,
        direction: str,
        stage: str,
        volume_ratio: float,
        volatility_rank: float,
        participation_score: float,
        followthrough_volume_ratio: float,
        mtf_context: dict[str, object],
        regime_hint: str,
        close_position: float,
        body_pct_of_range: float,
        extra: dict | None = None,
    ) -> None:
        context = extra or {}
        logger.info(
            "CONTINUATION_GATE_SNAPSHOT | %s | stage=%s | direction=%s | regime=%s | align=%s | primary=%s | confirmation=%s | pressure_ok=%s | structure_ok=%s | participation_ok=%s | compression_quality=%s | pressure_score=%.2f | expansion_prob=%.1f | volume_ratio=%.2f | volatility_rank=%.2f | participation_score=%.2f | followthrough_volume_ratio=%.2f | close_position=%.3f | body_pct=%.3f | extra=%s",
            market.symbol,
            stage,
            direction,
            regime_hint,
            market.alignment,
            market.primary.trend,
            market.confirmation.trend,
            mtf_context.get("pressure_ok"),
            mtf_context.get("structure_ok"),
            mtf_context.get("participation_ok"),
            mtf_context.get("compression_quality"),
            float(mtf_context.get("pressure_score", 0.0)),
            float(mtf_context.get("expansion_prob", 0.0)),
            float(volume_ratio or 0.0),
            float(volatility_rank or 0.0),
            float(participation_score or 0.0),
            float(followthrough_volume_ratio or 0.0),
            float(close_position),
            float(body_pct_of_range),
            context,
        )

    def _continuation_regime_hint(
        self,
        market: MarketSnapshot,
        direction: str,
        volume_ratio: float,
        volatility_rank: float,
        pressure_score: float,
        expansion_prob: float,
        compression: bool,
        close_position: float | None = None,
        body_pct_of_range: float | None = None,
    ) -> str:
        alignment = (market.alignment or "").lower()
        primary_trend = (market.primary.trend or "").lower()
        confirmation_trend = (market.confirmation.trend or "").lower()

        aligned_long = (
            direction == "LONG"
            and alignment == "aligned_bullish"
            and primary_trend == "bullish"
            and confirmation_trend == "bullish"
        )
        aligned_short = (
            direction == "SHORT"
            and alignment == "aligned_bearish"
            and primary_trend == "bearish"
            and confirmation_trend == "bearish"
        )
        aligned_trend = aligned_long or aligned_short

        late_long = direction == "LONG" and close_position is not None and close_position >= 0.88
        late_short = direction == "SHORT" and close_position is not None and close_position <= 0.12
        strong_body = body_pct_of_range is not None and body_pct_of_range >= 0.60

        if aligned_trend and volume_ratio >= 1.10 and volatility_rank >= 18 and pressure_score >= 65:
            return "trend_participation"

        if compression and pressure_score >= 55 and expansion_prob >= 70:
            return "compression_pre_expansion"

        if (late_long or late_short) and strong_body and expansion_prob >= 80:
            return "post_expansion_exhaustion"

        if alignment in {"conflicted", "mixed"} and pressure_score < 50 and volume_ratio < 0.90:
            return "chop_or_conflict"

        if volatility_rank < 8 and volume_ratio < 0.75:
            return "low_energy"

        return "neutral_continuation"

    def _mtf_continuation_context(
        self,
        market: MarketSnapshot,
        direction: str,
        volume_ratio: float,
        volatility_rank: float,
    ) -> dict[str, object]:
        note_text = self._note_text(market)
        primary_trend = (market.primary.trend or "").lower()
        confirmation_trend = (market.confirmation.trend or "").lower()
        alignment = (market.alignment or "").lower()
        wanted_pressure = "bullish" if direction == "LONG" else "bearish"

        pressure_score = self._extract_note_float(market, "pressure_score=", 0.0)
        expansion_prob = self._extract_note_float(market, "expansion_prob=", 0.0)
        breakout_context_ready = "breakout_context ready=true" in note_text
        breakout_context_direction = f"direction={wanted_pressure}" in note_text
        volatility_pressure_direction = f"pressure={wanted_pressure}" in note_text
        compression = "compression=true" in note_text or "range tightening" in note_text
        regime_hint = self._continuation_regime_hint(
            market=market,
            direction=direction,
            volume_ratio=float(volume_ratio or 0.0),
            volatility_rank=float(volatility_rank or 0.0),
            pressure_score=float(pressure_score or 0.0),
            expansion_prob=float(expansion_prob or 0.0),
            compression=bool(compression),
        )

        if direction == "LONG":
            structure_ok = (
                "higher lows building" in note_text
                or "closes pressing highs" in note_text
                or "range tightening" in note_text
            )
            regime_ok = (
                alignment in {"aligned_bullish", "mixed"}
                and confirmation_trend in {"bullish", "neutral"}
                and primary_trend in {"bullish", "mixed", "neutral"}
            )
        else:
            structure_ok = (
                "lower highs building" in note_text
                or "closes pressing lows" in note_text
                or "range tightening" in note_text
            )
            regime_ok = (
                alignment in {"aligned_bearish", "mixed"}
                and confirmation_trend in {"bearish", "neutral"}
                and primary_trend in {"bearish", "mixed", "neutral"}
            )

        pressure_ok = (
            volatility_pressure_direction
            or (breakout_context_ready and breakout_context_direction)
            or (
                breakout_context_direction
                and pressure_score >= self.MTF_OVERRIDE_MIN_PRESSURE_SCORE
                and expansion_prob >= self.MTF_OVERRIDE_MIN_EXPANSION_PROB
            )
        )
        compression_quality = (
            compression
            and pressure_score >= self.COMPRESSION_MIN_PRESSURE_SCORE
            and expansion_prob >= self.COMPRESSION_MIN_EXPANSION_PROB
            and volume_ratio >= self.COMPRESSION_MIN_VOLUME_RATIO
            and volatility_rank >= self.COMPRESSION_MIN_VOLATILITY_RANK
        )
        participation_ok = (
            volume_ratio >= self.MTF_OVERRIDE_MIN_VOLUME_RATIO
            and volatility_rank >= self.MTF_OVERRIDE_MIN_VOLATILITY_RANK
        ) or compression_quality

        allowed = bool(regime_ok and structure_ok and pressure_ok and participation_ok)

        return {
            "allowed": allowed,
            "direction": direction,
            "mode": "mtf_override" if allowed else "strict",
            "pressure_score": pressure_score,
            "expansion_prob": expansion_prob,
            "breakout_context_ready": breakout_context_ready,
            "compression": compression,
            "compression_quality": compression_quality,
            "structure_ok": structure_ok,
            "regime_ok": regime_ok,
            "pressure_ok": pressure_ok,
            "participation_ok": participation_ok,
            "alignment": alignment,
            "primary_trend": primary_trend,
            "confirmation_trend": confirmation_trend,
            "regime_hint": regime_hint,
        }

    def detect(self, market: MarketSnapshot) -> StrategyCandidate | None:
        logger.warning(
            "CONTINUATION_DISABLED | %s | reason=strategy_isolation_liquidity_sweep_only",
            market.symbol,
        )
        return None
        primary_candles = market.primary.candles
        confirmation_candles = market.confirmation.candles

        if len(primary_candles) < 6 or len(confirmation_candles) < 3:
            self._reject(
                market,
                "not_enough_candles",
                primary=len(primary_candles),
                confirmation=len(confirmation_candles),
            )
            return None

        last = primary_candles[-1]
        prev = primary_candles[-2]
        prev2 = primary_candles[-3]

        notes: list[str] = []
        market_context_notes = [str(note) for note in (getattr(market, "notes", []) or [])]

        primary_trend = (market.primary.trend or "").lower()
        confirmation_trend = (market.confirmation.trend or "").lower()
        note_text = self._note_text(market)
        bullish_pressure = "direction=bullish" in note_text or "pressure=bullish" in note_text
        bearish_pressure = "direction=bearish" in note_text or "pressure=bearish" in note_text

        if primary_trend == "bullish" and confirmation_trend in {"bullish", "neutral"}:
            direction = "LONG"
            notes.append("trend bullish")
        elif primary_trend == "bearish" and confirmation_trend in {"bearish", "neutral"}:
            direction = "SHORT"
            notes.append("trend bearish")
        elif confirmation_trend == "bullish" and primary_trend in {"mixed", "neutral"} and bullish_pressure:
            direction = "LONG"
            notes.append("trend bullish mtf-prearmed")
        elif confirmation_trend == "bearish" and primary_trend in {"mixed", "neutral"} and bearish_pressure:
            direction = "SHORT"
            notes.append("trend bearish mtf-prearmed")
        else:
            self._reject(
                market,
                "trend_not_aligned",
                primary_trend=primary_trend,
                confirmation_trend=confirmation_trend,
                alignment=market.alignment,
            )
            return None

        body = abs(last.close - last.open)
        full_range = max(last.high - last.low, 1e-9)
        body_pct_of_range = body / full_range
        volume_ratio = market.primary.volume_ratio_20
        volatility_rank = float(getattr(market, "volatility_rank", 0.0) or 0.0)
        close_position = (last.close - last.low) / full_range
        ema_distance_pct = (abs(last.close - market.primary.ema20) / max(last.close, 1e-9)) * 100
        raw_debug_symbols = os.getenv("STRATEGY_DEBUG_SYMBOLS", "")
        debug_symbols = {item.strip().upper() for item in raw_debug_symbols.split(",") if item.strip()}
        debug_enabled = market.symbol.upper() in debug_symbols

        participation_score = self._participation_score(primary_candles, len(primary_candles) - 1, direction=direction)
        followthrough_volume_ratio = self._volume_ratio_at(primary_candles, len(primary_candles) - 1)
        mtf_context = self._mtf_continuation_context(
            market,
            direction=direction,
            volume_ratio=float(volume_ratio or 0.0),
            volatility_rank=float(volatility_rank or 0.0),
        )
        mtf_override = bool(mtf_context.get("allowed"))

        compression_quality = bool(mtf_context.get("compression_quality"))
        regime_hint = self._continuation_regime_hint(
            market=market,
            direction=direction,
            volume_ratio=float(volume_ratio or 0.0),
            volatility_rank=float(volatility_rank or 0.0),
            pressure_score=float(mtf_context.get("pressure_score", 0.0)),
            expansion_prob=float(mtf_context.get("expansion_prob", 0.0)),
            compression=bool(mtf_context.get("compression")),
            close_position=float(close_position),
            body_pct_of_range=float(body_pct_of_range),
        )
        mtf_context["regime_hint"] = regime_hint

        if mtf_override:
            logger.info(
                "CONTINUATION_MTF_OVERRIDE | %s | direction=%s | alignment=%s | primary=%s | confirmation=%s | pressure_score=%.2f | expansion_prob=%.1f | volume_ratio=%.2f | volatility_rank=%.2f | compression_quality=%s",
                market.symbol,
                direction,
                mtf_context.get("alignment"),
                mtf_context.get("primary_trend"),
                mtf_context.get("confirmation_trend"),
                float(mtf_context.get("pressure_score", 0.0)),
                float(mtf_context.get("expansion_prob", 0.0)),
                float(volume_ratio or 0.0),
                float(volatility_rank or 0.0),
                bool(mtf_context.get("compression_quality")),
            )
        logger.info(
            "CONTINUATION_REGIME | %s | direction=%s | regime=%s | alignment=%s | primary=%s | confirmation=%s | pressure_score=%.2f | expansion_prob=%.1f | volume_ratio=%.2f | volatility_rank=%.2f | close_position=%.3f | body_pct=%.3f | compression_quality=%s",
            market.symbol,
            direction,
            regime_hint,
            market.alignment,
            primary_trend,
            confirmation_trend,
            float(mtf_context.get("pressure_score", 0.0)),
            float(mtf_context.get("expansion_prob", 0.0)),
            float(volume_ratio or 0.0),
            float(volatility_rank or 0.0),
            float(close_position),
            float(body_pct_of_range),
            bool(mtf_context.get("compression_quality")),
        )
        directional_pressure_ok = bool(mtf_context.get("pressure_ok"))

        # --- Structure quality hard gate ---
        structure_ok = bool(mtf_context.get("structure_ok"))

        if not structure_ok:
            self._gate_snapshot(
                market=market,
                direction=direction,
                stage="structure_quality_gate",
                volume_ratio=float(volume_ratio or 0.0),
                volatility_rank=float(volatility_rank or 0.0),
                participation_score=float(participation_score or 0.0),
                followthrough_volume_ratio=float(followthrough_volume_ratio or 0.0),
                mtf_context=mtf_context,
                regime_hint=regime_hint,
                close_position=float(close_position),
                body_pct_of_range=float(body_pct_of_range),
            )
            self._reject(
                market,
                "structure_not_confirmed",
                direction=direction,
                structure_ok=structure_ok,
                pressure_ok=directional_pressure_ok,
                regime_hint=regime_hint,
            )
            return None

        if not directional_pressure_ok and not compression_quality:
            self._gate_snapshot(
                market=market,
                direction=direction,
                stage="directional_pressure_gate",
                volume_ratio=float(volume_ratio or 0.0),
                volatility_rank=float(volatility_rank or 0.0),
                participation_score=float(participation_score or 0.0),
                followthrough_volume_ratio=float(followthrough_volume_ratio or 0.0),
                mtf_context=mtf_context,
                regime_hint=regime_hint,
                close_position=float(close_position),
                body_pct_of_range=float(body_pct_of_range),
            )
            self._reject(
                market,
                "continuation_lacks_directional_pressure",
                direction=direction,
                pressure_score=round(float(mtf_context.get("pressure_score", 0.0)), 2),
                expansion_prob=round(float(mtf_context.get("expansion_prob", 0.0)), 1),
                breakout_context_ready=mtf_context.get("breakout_context_ready"),
                compression_quality=compression_quality,
                regime_hint=regime_hint,
                alignment=market.alignment,
                primary_trend=primary_trend,
                confirmation_trend=confirmation_trend,
            )
            return None

        # SHORT-specific pressure quality gate after directional_pressure check
        if direction == "SHORT" and not compression_quality:
            short_pressure_score = float(mtf_context.get("pressure_score", 0.0))
            short_expansion_prob = float(mtf_context.get("expansion_prob", 0.0))
            if (
                short_pressure_score < self.SHORT_MIN_PRESSURE_SCORE
                or short_expansion_prob < self.SHORT_MIN_EXPANSION_PROB
            ):
                self._gate_snapshot(
                    market=market,
                    direction=direction,
                    stage="short_pressure_quality_gate",
                    volume_ratio=float(volume_ratio or 0.0),
                    volatility_rank=float(volatility_rank or 0.0),
                    participation_score=float(participation_score or 0.0),
                    followthrough_volume_ratio=float(followthrough_volume_ratio or 0.0),
                    mtf_context=mtf_context,
                    regime_hint=regime_hint,
                    close_position=float(close_position),
                    body_pct_of_range=float(body_pct_of_range),
                    extra={
                        "required_pressure_score": self.SHORT_MIN_PRESSURE_SCORE,
                        "required_expansion_prob": self.SHORT_MIN_EXPANSION_PROB,
                    },
                )
                self._reject(
                    market,
                    "short_continuation_pressure_quality_too_weak",
                    direction=direction,
                    pressure_score=round(short_pressure_score, 2),
                    minimum_pressure=self.SHORT_MIN_PRESSURE_SCORE,
                    expansion_prob=round(short_expansion_prob, 1),
                    minimum_expansion=self.SHORT_MIN_EXPANSION_PROB,
                    regime_hint=regime_hint,
                )
                return None

        if compression_quality:
            required_participation_score = self.COMPRESSION_MIN_PARTICIPATION_SCORE
        elif mtf_override:
            required_participation_score = 1.0
        else:
            required_participation_score = self.MIN_PARTICIPATION_SCORE
        if regime_hint == "trend_participation":
            required_participation_score = min(required_participation_score, 1.10)
        elif regime_hint in {"post_expansion_exhaustion", "chop_or_conflict"}:
            required_participation_score = max(required_participation_score, 1.75)
        if direction == "SHORT" and not compression_quality:
            required_participation_score = max(
                required_participation_score,
                self.SHORT_MIN_PARTICIPATION_SCORE,
            )

        if participation_score < required_participation_score:
            self._gate_snapshot(
                market=market,
                direction=direction,
                stage="participation_gate",
                volume_ratio=float(volume_ratio or 0.0),
                volatility_rank=float(volatility_rank or 0.0),
                participation_score=float(participation_score or 0.0),
                followthrough_volume_ratio=float(followthrough_volume_ratio or 0.0),
                mtf_context=mtf_context,
                regime_hint=regime_hint,
                close_position=float(close_position),
                body_pct_of_range=float(body_pct_of_range),
                extra={"required_participation_score": required_participation_score},
            )
            self._reject(
                market,
                "continuation_participation_too_weak",
                direction=direction,
                participation_score=round(float(participation_score), 3),
                minimum=required_participation_score,
                mtf_override=mtf_override,
            )
            return None

        required_followthrough_volume_ratio = 0.45 if compression_quality else self.MIN_FOLLOWTHROUGH_VOLUME_RATIO
        if regime_hint == "trend_participation":
            required_followthrough_volume_ratio = min(required_followthrough_volume_ratio, 0.55)
        elif regime_hint in {"post_expansion_exhaustion", "chop_or_conflict"}:
            required_followthrough_volume_ratio = max(required_followthrough_volume_ratio, 0.75)
        if direction == "SHORT" and not compression_quality:
            required_followthrough_volume_ratio = max(
                required_followthrough_volume_ratio,
                self.SHORT_MIN_FOLLOWTHROUGH_VOLUME_RATIO,
            )
        if followthrough_volume_ratio < required_followthrough_volume_ratio:
            self._gate_snapshot(
                market=market,
                direction=direction,
                stage="followthrough_volume_gate",
                volume_ratio=float(volume_ratio or 0.0),
                volatility_rank=float(volatility_rank or 0.0),
                participation_score=float(participation_score or 0.0),
                followthrough_volume_ratio=float(followthrough_volume_ratio or 0.0),
                mtf_context=mtf_context,
                regime_hint=regime_hint,
                close_position=float(close_position),
                body_pct_of_range=float(body_pct_of_range),
                extra={"required_followthrough_volume_ratio": required_followthrough_volume_ratio},
            )
            self._reject(
                market,
                "continuation_followthrough_volume_too_weak",
                direction=direction,
                followthrough_volume_ratio=round(float(followthrough_volume_ratio), 3),
                minimum=required_followthrough_volume_ratio,
                compression_quality=compression_quality,
            )
            return None

        if compression_quality:
            required_volume_ratio = self.COMPRESSION_MIN_VOLUME_RATIO
        elif mtf_override:
            required_volume_ratio = self.MTF_OVERRIDE_MIN_VOLUME_RATIO
        else:
            required_volume_ratio = self.MIN_CONTINUATION_VOLUME_RATIO
        if direction == "SHORT" and not compression_quality:
            required_volume_ratio = max(
                required_volume_ratio,
                self.SHORT_MIN_VOLUME_RATIO,
            )

        if volume_ratio < required_volume_ratio:
            self._gate_snapshot(
                market=market,
                direction=direction,
                stage="volume_gate",
                volume_ratio=float(volume_ratio or 0.0),
                volatility_rank=float(volatility_rank or 0.0),
                participation_score=float(participation_score or 0.0),
                followthrough_volume_ratio=float(followthrough_volume_ratio or 0.0),
                mtf_context=mtf_context,
                regime_hint=regime_hint,
                close_position=float(close_position),
                body_pct_of_range=float(body_pct_of_range),
                extra={"required_volume_ratio": required_volume_ratio},
            )
            self._reject(
                market,
                "continuation_volume_too_weak",
                direction=direction,
                volume_ratio=round(float(volume_ratio), 3),
                minimum=required_volume_ratio,
                mtf_override=mtf_override,
            )
            return None

        if compression_quality:
            required_volatility_rank = self.COMPRESSION_MIN_VOLATILITY_RANK
        elif mtf_override:
            required_volatility_rank = self.MTF_OVERRIDE_MIN_VOLATILITY_RANK
        else:
            required_volatility_rank = self.MIN_CONTINUATION_VOLATILITY_RANK
        if direction == "SHORT" and not compression_quality:
            required_volatility_rank = max(
                required_volatility_rank,
                self.SHORT_MIN_VOLATILITY_RANK,
            )

        if volatility_rank < required_volatility_rank:
            self._gate_snapshot(
                market=market,
                direction=direction,
                stage="volatility_gate",
                volume_ratio=float(volume_ratio or 0.0),
                volatility_rank=float(volatility_rank or 0.0),
                participation_score=float(participation_score or 0.0),
                followthrough_volume_ratio=float(followthrough_volume_ratio or 0.0),
                mtf_context=mtf_context,
                regime_hint=regime_hint,
                close_position=float(close_position),
                body_pct_of_range=float(body_pct_of_range),
                extra={"required_volatility_rank": required_volatility_rank},
            )
            self._reject(
                market,
                "continuation_volatility_too_weak",
                direction=direction,
                volatility_rank=round(float(volatility_rank), 3),
                minimum=required_volatility_rank,
                mtf_override=mtf_override,
            )
            return None

        if direction == "LONG" and close_position > self.LONG_MAX_CLOSE_POSITION:
            self._reject(
                market,
                "long_continuation_entry_too_high",
                direction=direction,
                close_position=round(float(close_position), 3),
                max_allowed=self.LONG_MAX_CLOSE_POSITION,
            )
            return None

        if direction == "SHORT" and close_position < self.SHORT_MIN_CLOSE_POSITION:
            self._reject(
                market,
                "short_continuation_entry_too_low",
                direction=direction,
                close_position=round(float(close_position), 3),
                min_allowed=self.SHORT_MIN_CLOSE_POSITION,
            )
            return None

        if direction == "LONG":
            pulled_back = prev.low <= market.primary.ema20 or prev2.low <= market.primary.ema20
            pullback_depth_pct = (
                (market.primary.ema20 - min(prev.low, prev2.low, last.low))
                / max(last.close, 1e-9)
            ) * 100
            reclaim_proximity_pct = (
                abs(last.close - market.primary.ema20)
                / max(last.close, 1e-9)
            ) * 100
            vertical_extension = close_position >= 0.88 and body_pct_of_range >= 0.60

            strong_bullish_trend = (
                (
                    market.alignment == "aligned_bullish"
                    and primary_trend == "bullish"
                    and confirmation_trend == "bullish"
                )
                or mtf_override
            ) and volume_ratio >= 0.80 and volatility_rank >= 10

            shallow_trend_continuation = (
                strong_bullish_trend
                and last.close > prev.close
                and last.close > market.primary.ema20
                and body_pct_of_range >= 0.35
            )

            if shallow_trend_continuation:
                pulled_back = True
                notes.append("shallow bullish continuation")
            if debug_enabled:
                logger.info(
                    "CONTINUATION_DEBUG | %s | direction=LONG | align=%s | primary=%s | confirmation=%s | vr=%.2f | vol_rank=%.2f | body=%.3f | pulled_back=%s | shallow=%s | last_close=%.8f | prev_close=%.8f | ema20=%.8f | last_gt_prev=%s | last_gt_ema=%s",
                    market.symbol,
                    market.alignment,
                    primary_trend,
                    confirmation_trend,
                    volume_ratio,
                    volatility_rank,
                    body_pct_of_range,
                    pulled_back,
                    shallow_trend_continuation,
                    last.close,
                    prev.close,
                    market.primary.ema20,
                    last.close > prev.close,
                    last.close > market.primary.ema20,
                )

            ema_reclaimed = last.close > market.primary.ema20
            structure_reclaimed = last.close > prev.high
            strong_reclaim = ema_reclaimed and last.close > prev.close and body_pct_of_range >= 0.55
            reclaimed = ema_reclaimed and (structure_reclaimed or strong_reclaim or shallow_trend_continuation)
            # --- Reclaim quality score (LONG) ---
            reclaim_strength_score = 0.0
            if ema_reclaimed:
                reclaim_strength_score += 30.0
            if structure_reclaimed:
                reclaim_strength_score += 30.0
            if strong_reclaim:
                reclaim_strength_score += 20.0
            reclaim_strength_score += min(body_pct_of_range * 20.0, 20.0)
            momentum_ok = (
                (last.close > last.open and body_pct_of_range >= 0.40)
                or (
                    market.alignment == "aligned_bullish"
                    and volume_ratio >= 0.80
                    and volatility_rank >= 10
                    and body_pct_of_range >= 0.35
                    and last.close > prev.close
                )
            )
            invalidation = min(prev.low, prev2.low, last.low)
            swept_level = min(prev.low, prev2.low)
            sweep_extreme = min(prev.low, prev2.low, last.low)
            reclaim_level = max(market.primary.ema20, prev.high)
        else:
            pulled_back = prev.high >= market.primary.ema20 or prev2.high >= market.primary.ema20
            pullback_depth_pct = (
                (max(prev.high, prev2.high, last.high) - market.primary.ema20)
                / max(last.close, 1e-9)
            ) * 100
            reclaim_proximity_pct = (
                abs(last.close - market.primary.ema20)
                / max(last.close, 1e-9)
            ) * 100
            vertical_extension = close_position <= 0.12 and body_pct_of_range >= 0.60

            strong_bearish_trend = (
                (
                    market.alignment == "aligned_bearish"
                    and primary_trend == "bearish"
                    and confirmation_trend == "bearish"
                )
                or mtf_override
            ) and volume_ratio >= 0.80 and volatility_rank >= 10

            shallow_trend_continuation = (
                strong_bearish_trend
                and last.close < prev.close
                and last.close < market.primary.ema20
                and body_pct_of_range >= 0.35
            )

            if shallow_trend_continuation:
                pulled_back = True
                notes.append("shallow bearish continuation")
            if debug_enabled:
                logger.info(
                    "CONTINUATION_DEBUG | %s | direction=SHORT | align=%s | primary=%s | confirmation=%s | vr=%.2f | vol_rank=%.2f | body=%.3f | pulled_back=%s | shallow=%s | last_close=%.8f | prev_close=%.8f | ema20=%.8f | last_lt_prev=%s | last_lt_ema=%s",
                    market.symbol,
                    market.alignment,
                    primary_trend,
                    confirmation_trend,
                    volume_ratio,
                    volatility_rank,
                    body_pct_of_range,
                    pulled_back,
                    shallow_trend_continuation,
                    last.close,
                    prev.close,
                    market.primary.ema20,
                    last.close < prev.close,
                    last.close < market.primary.ema20,
                )

            ema_reclaimed = last.close < market.primary.ema20
            structure_reclaimed = last.close < prev.low
            strong_reclaim = ema_reclaimed and last.close < prev.close and body_pct_of_range >= 0.55
            reclaimed = ema_reclaimed and (structure_reclaimed or strong_reclaim or shallow_trend_continuation)
            # --- Reclaim quality score (SHORT) ---
            reclaim_strength_score = 0.0
            if ema_reclaimed:
                reclaim_strength_score += 30.0
            if structure_reclaimed:
                reclaim_strength_score += 30.0
            if strong_reclaim:
                reclaim_strength_score += 20.0
            reclaim_strength_score += min(body_pct_of_range * 20.0, 20.0)
            momentum_ok = (
                (last.close < last.open and body_pct_of_range >= 0.40)
                or (
                    market.alignment == "aligned_bearish"
                    and volume_ratio >= 0.80
                    and volatility_rank >= 10
                    and body_pct_of_range >= 0.35
                    and last.close < prev.close
                )
            )
            invalidation = max(prev.high, prev2.high, last.high)
            swept_level = max(prev.high, prev2.high)
            sweep_extreme = max(prev.high, prev2.high, last.high)
            reclaim_level = min(market.primary.ema20, prev.low)

        if not pulled_back:
            self._gate_snapshot(
                market=market,
                direction=direction,
                stage="pullback_gate",
                volume_ratio=float(volume_ratio or 0.0),
                volatility_rank=float(volatility_rank or 0.0),
                participation_score=float(participation_score or 0.0),
                followthrough_volume_ratio=float(followthrough_volume_ratio or 0.0),
                mtf_context=mtf_context,
                regime_hint=regime_hint,
                close_position=float(close_position),
                body_pct_of_range=float(body_pct_of_range),
            )
            self._reject(
                market,
                "no_pullback_to_ema20",
                direction=direction,
                prev_low=round(prev.low, 8),
                prev_high=round(prev.high, 8),
                prev2_low=round(prev2.low, 8),
                prev2_high=round(prev2.high, 8),
                ema20=round(market.primary.ema20, 8),
            )
            return None
        notes.append("pullback detected")

        if not reclaimed:
            self._gate_snapshot(
                market=market,
                direction=direction,
                stage="reclaim_gate",
                volume_ratio=float(volume_ratio or 0.0),
                volatility_rank=float(volatility_rank or 0.0),
                participation_score=float(participation_score or 0.0),
                followthrough_volume_ratio=float(followthrough_volume_ratio or 0.0),
                mtf_context=mtf_context,
                regime_hint=regime_hint,
                close_position=float(close_position),
                body_pct_of_range=float(body_pct_of_range),
            )
            self._reject(
                market,
                "no_reclaim",
                direction=direction,
                last_close=round(last.close, 8),
                prev_close=round(prev.close, 8),
                prev_high=round(prev.high, 8),
                prev_low=round(prev.low, 8),
                ema20=round(market.primary.ema20, 8),
                body_pct=round(body_pct_of_range, 3),
            )
            return None
        notes.append("reclaim confirmed")

        retest_quality_score = max(
            0.0,
            min(
                100.0,
                reclaim_strength_score
                + max(0.0, 25.0 - (reclaim_proximity_pct * 10.0))
                + max(0.0, 20.0 - (pullback_depth_pct * 5.0)),
            ),
        )

        notes.append(f"reclaim_strength_score {reclaim_strength_score:.2f}")
        notes.append(f"retest_quality_score {retest_quality_score:.2f}")
        # Add parser-friendly variants for future scoring/selector components
        notes.append(f"reclaim_strength_score={reclaim_strength_score:.2f}")
        notes.append(f"retest_quality_score={retest_quality_score:.2f}")
        notes.append(f"pullback_depth_pct {pullback_depth_pct:.3f}")
        notes.append(f"reclaim_proximity_pct {reclaim_proximity_pct:.3f}")
        notes.append(f"close_position {close_position:.3f}")

        logger.info(
            "RECLAIM_QUALITY | %s | direction=%s | reclaim_strength_score=%.2f | retest_quality_score=%.2f | pullback_depth_pct=%.3f | reclaim_proximity_pct=%.3f",
            market.symbol,
            direction,
            reclaim_strength_score,
            retest_quality_score,
            pullback_depth_pct,
            reclaim_proximity_pct,
        )

        if reclaim_proximity_pct <= 0.35:
            notes.append("reclaim timing efficient")
        elif reclaim_proximity_pct >= self.MAX_RECLAIM_PROXIMITY_PCT:
            self._reject(
                market,
                "reclaim_timing_too_extended",
                direction=direction,
                reclaim_proximity_pct=round(float(reclaim_proximity_pct), 3),
                max_allowed=self.MAX_RECLAIM_PROXIMITY_PCT,
            )
            return None

        if vertical_extension:
            notes.append("vertical extension risk")
            self._reject(
                market,
                "vertical_extension_blocked",
                direction=direction,
                close_position=round(float(close_position), 3),
                body_pct=round(float(body_pct_of_range), 3),
            )
            return None

        if not momentum_ok:
            self._reject(
                market,
                "weak_momentum_candle",
                direction=direction,
                open=round(last.open, 8),
                close=round(last.close, 8),
                body_pct=round(body_pct_of_range, 3),
            )
            return None
        notes.append("momentum present")

        if volume_ratio >= 1.10:
            notes.append("volume expansion")

        if market.alignment in {"aligned_bullish", "aligned_bearish"}:
            notes.append(f"alignment {market.alignment}")

        local_range = max(
            max(c.high for c in primary_candles[-6:]) - min(c.low for c in primary_candles[-6:]),
            1e-9,
        )
        local_range_pct = (local_range / max(last.close, 1e-9)) * 100
        displacement_pct = (abs(last.close - prev.close) / max(prev.close, 1e-9)) * 100

        detection = SweepDetection(
            side="continuation_pullback",
            swept_level=swept_level,
            sweep_extreme=sweep_extreme,
            reclaim_level=reclaim_level,
            entry_hint=last.close,
            invalidation=invalidation,
            displacement_pct=displacement_pct,
            bars_since_sweep=0,
            volume_ratio_on_sweep=volume_ratio,
            local_range_size_pct=local_range_pct,
            reason_flags=notes.copy(),
        )

        notes.append(f"body_pct_of_range {body_pct_of_range:.2f}")
        notes.append(f"volume_ratio {volume_ratio:.2f}")
        notes.append(f"participation_score {participation_score:.2f}")
        notes.append(f"followthrough_volume_ratio {followthrough_volume_ratio:.2f}")

        notes.append(f"mtf_continuation_mode {mtf_context.get('mode')}")
        notes.append(f"mtf_pressure_score {float(mtf_context.get('pressure_score', 0.0)):.2f}")
        notes.append(f"mtf_expansion_prob {float(mtf_context.get('expansion_prob', 0.0)):.1f}")
        notes.append(f"compression_quality {bool(mtf_context.get('compression_quality'))}")
        notes.append(f"directional_pressure_ok {directional_pressure_ok}")
        notes.append(f"continuation_regime {regime_hint}")
        if direction == "SHORT":
            notes.append("short_continuation_strict_mode true")

        for context_note in market_context_notes:
            if context_note not in notes:
                notes.append(context_note)

        return StrategyCandidate(
            symbol=market.symbol,
            strategy=self.name,
            direction=direction,
            primary_granularity=market.primary.granularity,
            confirmation_granularity=market.confirmation.granularity,
            market=market,
            detection=detection,
            notes=notes,
        )



    def _participation_score(self, candles: list, index: int, direction: str) -> float:
        if index < 3:
            return 0.0

        recent_indexes = [index - 2, index - 1, index]
        ratios = [self._volume_ratio_at(candles, i) for i in recent_indexes]
        candles_slice = [candles[i] for i in recent_indexes]

        score = 0.0

        if ratios[-1] >= self.MIN_CONTINUATION_VOLUME_RATIO:
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

    @staticmethod
    def _volume_ratio_at(candles: list, index: int, period: int = 20) -> float:
        if index <= 0 or len(candles) < period + 1:
            return 0.0

        start = max(0, index - period)
        history = candles[start:index]
        if not history:
            return 0.0

        avg_volume = sum(float(c.volume_base or 0.0) for c in history) / len(history)
        if avg_volume <= 0:
            return 0.0

        return float(candles[index].volume_base or 0.0) / avg_volume


def detect_continuation(market: MarketSnapshot) -> StrategyCandidate | None:
    return ContinuationStrategy().detect(market)