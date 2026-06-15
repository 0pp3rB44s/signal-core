import logging

from clients.schemas import MarketSnapshot, StrategyCandidate, SweepDetection

logger = logging.getLogger("StartupRunner")


class LowVolReclaimStrategy:
    name = "low_vol_reclaim"
    _last_reject_signature: dict[tuple[str, str], tuple[str, str, str]] = {}
    _last_watch_signature: dict[tuple[str, str], tuple[str, str, str]] = {}
    _reject_counts: dict[str, int] = {}

    # Reclaim Unlock v4: allow normal-vol reclaim probes while continuation stays hard-blocked.
    LOW_VOL_MAX_RANK = 65.0
    MIN_VOLUME_RATIO = 0.12
    MAX_SPREAD_BPS = 6.5
    MIN_BODY_PCT = 0.03
    # Controlled unlock: allow more reclaim data while continuation remains hard-blocked.
    MIN_PARTICIPATION_SCORE = 0.62
    MIN_FOLLOWTHROUGH_VOLUME_RATIO = 0.08
    MAX_EMA_RECLAIM_DISTANCE_PCT = 2.50
    MTF_OVERRIDE_MIN_PRESSURE_SCORE = 38.0
    MTF_OVERRIDE_MIN_EXPANSION_PROB = 58.0
    MTF_OVERRIDE_MIN_VOLUME_RATIO = 0.10
    MTF_OVERRIDE_MIN_PARTICIPATION_SCORE = 0.58
    MTF_OVERRIDE_MAX_VOL_RANK = 75.0

    def _reject(self, market: MarketSnapshot, reason: str, **context: object) -> None:
        symbol = str(market.symbol).upper()
        direction = str(context.get("direction") or "NA").upper()
        signature_key = (symbol, reason)
        reject_signature = (
            symbol,
            reason,
            direction,
        )

        if self._last_reject_signature.get(signature_key) == reject_signature:
            return

        self._last_reject_signature[signature_key] = reject_signature

        self._reject_counts[reason] = self._reject_counts.get(reason, 0) + 1
        details = " | ".join(f"{key}={value}" for key, value in context.items())
        if details:
            logger.info(
                "LOW_VOL_RECLAIM_REJECT | %s | reason=%s | count=%s | %s",
                market.symbol,
                reason,
                self._reject_counts.get(reason, 0),
                details,
            )
        else:
            logger.info(
                "LOW_VOL_RECLAIM_REJECT | %s | reason=%s | count=%s",
                market.symbol,
                reason,
                self._reject_counts.get(reason, 0),
            )

    def _watch(self, market: MarketSnapshot, watch_type: str, **context: object) -> None:
        symbol = str(market.symbol).upper()
        direction = str(context.get("direction") or "NA").upper()
        signature_key = (symbol, watch_type)
        watch_signature = (
            symbol,
            watch_type,
            direction,
        )

        if self._last_watch_signature.get(signature_key) == watch_signature:
            return

        self._last_watch_signature[signature_key] = watch_signature

        details = " | ".join(f"{key}={value}" for key, value in context.items())
        if details:
            logger.info("LOW_VOL_RECLAIM_WATCH | %s | watch=%s | %s", market.symbol, watch_type, details)
        else:
            logger.info("LOW_VOL_RECLAIM_WATCH | %s | watch=%s", market.symbol, watch_type)

    def _note_text(self, market: MarketSnapshot) -> str:
        return " | ".join(str(note).lower() for note in (getattr(market, "notes", []) or []))

    def _extract_market_note_float(self, market: MarketSnapshot, marker: str, default: float = 0.0) -> float:
        note_text = self._note_text(market)
        marker = marker.lower()
        if marker not in note_text:
            return default
        try:
            raw = note_text.split(marker, 1)[1].split()[0].strip("|,;")
            return float(raw)
        except Exception:
            return default

    def _extract_market_note_text(self, market: MarketSnapshot, marker: str, default: str = "") -> str:
        note_text = self._note_text(market)
        marker = marker.lower()
        if marker not in note_text:
            return default
        try:
            return note_text.split(marker, 1)[1].split()[0].strip("|,;").lower()
        except Exception:
            return default

    def _mtf_reclaim_context(
        self,
        market: MarketSnapshot,
        direction: str,
        volume_ratio: float,
        volatility_rank: float,
        spread_bps: float,
    ) -> dict[str, object]:
        note_text = self._note_text(market)
        primary_trend = (market.primary.trend or "").lower()
        confirmation_trend = (market.confirmation.trend or "").lower()
        alignment = (market.alignment or "").lower()
        wanted_pressure = "bullish" if direction == "LONG" else "bearish"

        pressure_score = self._extract_market_note_float(market, "pressure_score=", 0.0)
        expansion_prob = self._extract_market_note_float(market, "expansion_prob=", 0.0)
        breakout_context_ready = (
            "breakout_context ready=true" in note_text
            or "breakout_ready=true" in note_text
            or "breakout_ready true" in note_text
            or "breakdown_ready=true" in note_text
            or "breakdown_ready true" in note_text
            or "breakout_ready_directional=true" in note_text
        )
        breakout_direction = self._extract_market_note_text(market, "breakout_direction=", "unknown")
        breakout_context_direction = (
            f"direction={wanted_pressure}" in note_text
            or breakout_direction == wanted_pressure
            or (wanted_pressure == "bearish" and breakout_direction in {"short", "down", "breakdown"})
            or (wanted_pressure == "bullish" and breakout_direction in {"long", "up", "breakout"})
        )
        volatility_pressure_direction = f"pressure={wanted_pressure}" in note_text
        compression = "compression=true" in note_text or "range tightening" in note_text
        if direction == "LONG":
            structure_ok = (
                "higher lows building" in note_text
                or "closes pressing highs" in note_text
                or compression
            )
            regime_ok = (
                alignment in {"aligned_bullish", "mixed"}
                and confirmation_trend in {"bullish", "neutral", "mixed"}
                and primary_trend in {"bullish", "mixed", "neutral"}
            )
        else:
            structure_ok = (
                "lower highs building" in note_text
                or "closes pressing lows" in note_text
                or compression
            )
            regime_ok = (
                alignment in {"aligned_bearish", "mixed"}
                and confirmation_trend in {"bearish", "neutral", "mixed"}
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
        participation_ok = volume_ratio >= self.MTF_OVERRIDE_MIN_VOLUME_RATIO
        volatility_ok = volatility_rank <= self.MTF_OVERRIDE_MAX_VOL_RANK
        spread_ok = spread_bps <= self.MAX_SPREAD_BPS

        allowed = bool(regime_ok and structure_ok and pressure_ok and participation_ok and volatility_ok and spread_ok)

        return {
            "allowed": allowed,
            "mode": "mtf_override" if allowed else "strict",
            "direction": direction,
            "pressure_score": pressure_score,
            "expansion_prob": expansion_prob,
            "breakout_context_ready": breakout_context_ready,
            "compression": compression,
            "structure_ok": structure_ok,
            "regime_ok": regime_ok,
            "pressure_ok": pressure_ok,
            "participation_ok": participation_ok,
            "volatility_ok": volatility_ok,
            "spread_ok": spread_ok,
            "alignment": alignment,
            "primary_trend": primary_trend,
            "confirmation_trend": confirmation_trend,
        }

    def detect(self, market: MarketSnapshot) -> StrategyCandidate | None:
        primary_candles = market.primary.candles
        confirmation_candles = market.confirmation.candles

        if len(primary_candles) < 8 or len(confirmation_candles) < 3:
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

        notes: list[str] = ["low_vol_reclaim_mode"]
        notes.append("reclaim_unlock_v4=true")
        market_context_notes = [str(note) for note in (getattr(market, "notes", []) or [])]

        primary_trend = (market.primary.trend or "").lower()
        confirmation_trend = (market.confirmation.trend or "").lower()
        alignment = (market.alignment or "").lower()
        volatility_rank = float(getattr(market, "volatility_rank", 0.0) or 0.0)
        volume_ratio = float(market.primary.volume_ratio_20 or 0.0)

        full_range = max(last.high - last.low, 1e-9)
        body = abs(last.close - last.open)
        body_pct = body / full_range
        close_position = (last.close - last.low) / full_range
        anti_late_long = close_position >= 0.72 and last.close > market.primary.ema20
        anti_late_short = close_position <= 0.28 and last.close < market.primary.ema20
        ema_retest_zone_pct = abs(last.close - market.primary.ema20) / max(last.close, 1e-9) * 100
        long_retest_zone = (
            last.close > market.primary.ema20
            and ema_retest_zone_pct <= 0.85
            and 0.32 <= close_position <= 0.68
        )
        short_retest_zone = (
            last.close < market.primary.ema20
            and ema_retest_zone_pct <= 0.85
            and 0.32 <= close_position <= 0.68
        )

        spread_bps = self._extract_spread_bps(market_context_notes, 99.0)
        long_participation_score = self._participation_score(primary_candles, len(primary_candles) - 1, direction="LONG")
        short_participation_score = self._participation_score(primary_candles, len(primary_candles) - 1, direction="SHORT")
        followthrough_volume_ratio = self._volume_ratio_at(primary_candles, len(primary_candles) - 1)
        ema_reclaim_distance_pct = abs(last.close - market.primary.ema20) / max(last.close, 1e-9) * 100
        long_mtf_context = self._mtf_reclaim_context(
            market,
            direction="LONG",
            volume_ratio=volume_ratio,
            volatility_rank=volatility_rank,
            spread_bps=spread_bps,
        )
        short_mtf_context = self._mtf_reclaim_context(
            market,
            direction="SHORT",
            volume_ratio=volume_ratio,
            volatility_rank=volatility_rank,
            spread_bps=spread_bps,
        )
        long_mtf_override = bool(long_mtf_context.get("allowed"))
        short_mtf_override = bool(short_mtf_context.get("allowed"))

        if long_mtf_override:
            logger.info(
                "LOW_VOL_RECLAIM_MTF_OVERRIDE | %s | direction=LONG | alignment=%s | primary=%s | confirmation=%s | pressure_score=%.2f | expansion_prob=%.1f | volume_ratio=%.2f | volatility_rank=%.2f",
                market.symbol,
                long_mtf_context.get("alignment"),
                long_mtf_context.get("primary_trend"),
                long_mtf_context.get("confirmation_trend"),
                float(long_mtf_context.get("pressure_score", 0.0)),
                float(long_mtf_context.get("expansion_prob", 0.0)),
                volume_ratio,
                volatility_rank,
            )

        if short_mtf_override:
            logger.info(
                "LOW_VOL_RECLAIM_MTF_OVERRIDE | %s | direction=SHORT | alignment=%s | primary=%s | confirmation=%s | pressure_score=%.2f | expansion_prob=%.1f | volume_ratio=%.2f | volatility_rank=%.2f",
                market.symbol,
                short_mtf_context.get("alignment"),
                short_mtf_context.get("primary_trend"),
                short_mtf_context.get("confirmation_trend"),
                float(short_mtf_context.get("pressure_score", 0.0)),
                float(short_mtf_context.get("expansion_prob", 0.0)),
                volume_ratio,
                volatility_rank,
            )

        if volatility_rank > self.LOW_VOL_MAX_RANK and not (long_mtf_override or short_mtf_override):
            self._reject(
                market,
                "volatility_too_high_for_low_vol_reclaim",
                volatility_rank=round(volatility_rank, 2),
            )
            return None

        required_volume_ratio = self.MTF_OVERRIDE_MIN_VOLUME_RATIO if (long_mtf_override or short_mtf_override) else self.MIN_VOLUME_RATIO

        if volume_ratio < required_volume_ratio:
            self._reject(
                market,
                "volume_too_weak_for_reclaim",
                volume_ratio=round(volume_ratio, 2),
                minimum=required_volume_ratio,
                mtf_override=long_mtf_override or short_mtf_override,
            )
            return None

        orderbook_liquidity_ok = "orderbook_liquidity_ok=true" in self._note_text(market)

        if spread_bps > self.MAX_SPREAD_BPS:
            if spread_bps >= 99.0 and orderbook_liquidity_ok:
                logger.info(
                    "RECLAIM_PROBE_ALLOWED | %s | reason=spread_fallback_99_ignored | spread_bps=%.1f",
                    market.symbol,
                    spread_bps,
                )
            else:
                self._reject(
                    market,
                    "spread_too_wide_for_scalp",
                    spread_bps=round(spread_bps, 3),
                    orderbook_liquidity_ok=orderbook_liquidity_ok,
                )
                return None

        if body_pct < self.MIN_BODY_PCT:
            self._reject(
                market,
                "candle_body_too_small",
                body_pct=round(body_pct, 3),
            )
            return None

        if followthrough_volume_ratio < self.MIN_FOLLOWTHROUGH_VOLUME_RATIO:
            self._reject(
                market,
                "weak_followthrough_participation",
                followthrough_volume_ratio=round(followthrough_volume_ratio, 3),
                minimum=self.MIN_FOLLOWTHROUGH_VOLUME_RATIO,
            )
            return None

        if ema_reclaim_distance_pct > self.MAX_EMA_RECLAIM_DISTANCE_PCT:
            self._reject(
                market,
                "ema_reclaim_too_extended",
                ema_reclaim_distance_pct=round(ema_reclaim_distance_pct, 3),
                max_allowed=self.MAX_EMA_RECLAIM_DISTANCE_PCT,
            )
            return None

        direction: str | None = None

        bullish_reclaim = (
            (alignment in {"aligned_bullish", "mixed"} or long_mtf_override)
            and (primary_trend in {"bullish", "mixed", "neutral"} or long_mtf_override)
            and (confirmation_trend in {"bullish", "neutral", "mixed"} or long_mtf_override)
            and (
                (
                    prev.low < market.primary.ema20
                    and last.close > market.primary.ema20
                    and last.close > last.open
                    and close_position >= 0.35
                    and close_position <= 0.68
                    and long_retest_zone
                    and not anti_late_long
                )
                or (
                    close_position >= 0.25
                    and close_position <= 0.62
                    and long_participation_score >= 0.75
                    and long_retest_zone
                    and not anti_late_long
                )
            )
        )

        bearish_reclaim = (
            (alignment in {"aligned_bearish", "mixed"} or short_mtf_override)
            and (primary_trend in {"bearish", "mixed", "neutral"} or short_mtf_override)
            and (confirmation_trend in {"bearish", "neutral", "mixed"} or short_mtf_override)
            and (
                (
                    prev.high > market.primary.ema20
                    and last.close < market.primary.ema20
                    and last.close < last.open
                    and close_position >= 0.32
                    and close_position <= 0.65
                    and short_retest_zone
                    and not anti_late_short
                )
                or (
                    close_position >= 0.38
                    and close_position <= 0.75
                    and short_participation_score >= 0.75
                    and short_retest_zone
                    and not anti_late_short
                )
            )
        )

        if bullish_reclaim:
            direction = "LONG"
            required_long_participation = self.MTF_OVERRIDE_MIN_PARTICIPATION_SCORE if long_mtf_override else self.MIN_PARTICIPATION_SCORE

            if long_participation_score < required_long_participation:
                self._reject(
                    market,
                    "weak_long_participation_score",
                    direction="LONG",
                    participation_score=round(long_participation_score, 3),
                    minimum=required_long_participation,
                    mtf_override=long_mtf_override,
                )
                return None
            invalidation = min(prev.low, prev2.low, last.low)
            swept_level = min(prev.low, prev2.low)
            sweep_extreme = min(prev.low, prev2.low, last.low)
            reclaim_level = market.primary.ema20
            logger.info(
                "RECLAIM_PROBE_ALLOWED | %s | direction=LONG | participation=%.2f | volume_ratio=%.2f | volatility_rank=%.2f",
                market.symbol,
                long_participation_score,
                volume_ratio,
                volatility_rank,
            )
            notes.append("bullish low-vol reclaim")
            notes.append(f"mtf_reclaim_mode {long_mtf_context.get('mode')}")
            notes.append(f"mtf_pressure_score {float(long_mtf_context.get('pressure_score', 0.0)):.2f}")
            notes.append(f"mtf_expansion_prob {float(long_mtf_context.get('expansion_prob', 0.0)):.1f}")
        elif bearish_reclaim:
            direction = "SHORT"
            required_short_participation = self.MTF_OVERRIDE_MIN_PARTICIPATION_SCORE if short_mtf_override else self.MIN_PARTICIPATION_SCORE

            if short_participation_score < required_short_participation:
                self._reject(
                    market,
                    "weak_short_participation_score",
                    direction="SHORT",
                    participation_score=round(short_participation_score, 3),
                    minimum=required_short_participation,
                    mtf_override=short_mtf_override,
                )
                return None
            invalidation = max(prev.high, prev2.high, last.high)
            swept_level = max(prev.high, prev2.high)
            sweep_extreme = max(prev.high, prev2.high, last.high)
            reclaim_level = market.primary.ema20
            logger.info(
                "RECLAIM_PROBE_ALLOWED | %s | direction=SHORT | participation=%.2f | volume_ratio=%.2f | volatility_rank=%.2f",
                market.symbol,
                short_participation_score,
                volume_ratio,
                volatility_rank,
            )
            notes.append("bearish low-vol reclaim")
            notes.append(f"mtf_reclaim_mode {short_mtf_context.get('mode')}")
            notes.append(f"mtf_pressure_score {float(short_mtf_context.get('pressure_score', 0.0)):.2f}")
            notes.append(f"mtf_expansion_prob {float(short_mtf_context.get('expansion_prob', 0.0)):.1f}")
        else:
            if alignment in {"aligned_bullish", "mixed"} and last.close > market.primary.ema20 and not long_retest_zone:
                self._watch(
                    market,
                    "WAIT_LONG_RETEST_ZONE",
                    direction="LONG",
                    close_position=round(close_position, 3),
                    ema_retest_zone_pct=round(ema_retest_zone_pct, 3),
                    ema20=round(market.primary.ema20, 8),
                    last_close=round(last.close, 8),
                    note="bullish context detected, but wait for controlled EMA retest zone before long",
                )
                return None

            if alignment in {"aligned_bearish", "mixed"} and last.close < market.primary.ema20 and not short_retest_zone:
                self._watch(
                    market,
                    "WAIT_SHORT_RETEST_ZONE",
                    direction="SHORT",
                    close_position=round(close_position, 3),
                    ema_retest_zone_pct=round(ema_retest_zone_pct, 3),
                    ema20=round(market.primary.ema20, 8),
                    last_close=round(last.close, 8),
                    note="bearish context detected, but wait for controlled EMA retest zone before short",
                )
                return None

            if anti_late_long and alignment in {"aligned_bullish", "mixed"}:
                self._reject(
                    market,
                    "late_long_near_candle_high",
                    direction="LONG",
                    close_position=round(close_position, 3),
                    ema20=round(market.primary.ema20, 8),
                    last_close=round(last.close, 8),
                    note="do not buy extension near candle high; wait for pullback/retest",
                )
                return None

            if anti_late_short and alignment in {"aligned_bearish", "mixed"}:
                self._reject(
                    market,
                    "late_short_near_candle_low",
                    direction="SHORT",
                    close_position=round(close_position, 3),
                    ema20=round(market.primary.ema20, 8),
                    last_close=round(last.close, 8),
                    note="do not short extension near candle low; wait for pullback/retest",
                )
                return None

            if (
                alignment == "aligned_bearish"
                and primary_trend == "bearish"
                and confirmation_trend == "bearish"
                and close_position > 0.55
            ):
                self._watch(
                    market,
                    "WATCH_SHORT_RETEST",
                    direction="SHORT",
                    close_position=round(close_position, 3),
                    ema20=round(market.primary.ema20, 8),
                    last_close=round(last.close, 8),
                    volume_ratio=round(volume_ratio, 2),
                    volatility_rank=round(volatility_rank, 2),
                    note="bearish context but candle still too high for short; wait for reclaim/rejection",
                )

            if (
                alignment == "aligned_bullish"
                and primary_trend == "bullish"
                and confirmation_trend == "bullish"
                and close_position < 0.45
            ):
                self._watch(
                    market,
                    "WATCH_LONG_RETEST",
                    direction="LONG",
                    close_position=round(close_position, 3),
                    ema20=round(market.primary.ema20, 8),
                    last_close=round(last.close, 8),
                    volume_ratio=round(volume_ratio, 2),
                    volatility_rank=round(volatility_rank, 2),
                    note="bullish context but candle still too low for long; wait for reclaim/rejection",
                )

            # Watch for possible bullish reversal context
            if (
                alignment in {"aligned_bearish", "mixed"}
                and primary_trend in {"bearish", "mixed", "neutral"}
                and confirmation_trend in {"bearish", "mixed", "neutral"}
                and "breakout_direction=long" in self._note_text(market)
                and close_position >= 0.45
            ):
                self._watch(
                    market,
                    "WATCH_POSSIBLE_BULLISH_REVERSAL",
                    direction="LONG",
                    close_position=round(close_position, 3),
                    ema_retest_zone_pct=round(ema_retest_zone_pct, 3),
                    ema20=round(market.primary.ema20, 8),
                    last_close=round(last.close, 8),
                    note="bearish regime weakening; bullish breakout context detected, wait for clean reclaim/retest confirmation",
                )
                return None

            # Watch for possible bearish reversal context
            if (
                alignment in {"aligned_bullish", "mixed"}
                and primary_trend in {"bullish", "mixed", "neutral"}
                and confirmation_trend in {"bullish", "mixed", "neutral"}
                and "breakout_direction=short" in self._note_text(market)
                and close_position <= 0.55
            ):
                self._watch(
                    market,
                    "WATCH_POSSIBLE_BEARISH_REVERSAL",
                    direction="SHORT",
                    close_position=round(close_position, 3),
                    ema_retest_zone_pct=round(ema_retest_zone_pct, 3),
                    ema20=round(market.primary.ema20, 8),
                    last_close=round(last.close, 8),
                    note="bullish regime weakening; bearish breakout context detected, wait for clean reclaim/retest confirmation",
                )
                return None

            self._reject(
                market,
                "no_clean_low_vol_reclaim",
                alignment=alignment,
                primary_trend=primary_trend,
                confirmation_trend=confirmation_trend,
                close_position=round(close_position, 3),
                ema20=round(market.primary.ema20, 8),
                last_close=round(last.close, 8),
            )
            return None

        local_range = max(
            max(c.high for c in primary_candles[-8:]) - min(c.low for c in primary_candles[-8:]),
            1e-9,
        )
        local_range_pct = (local_range / max(last.close, 1e-9)) * 100
        displacement_pct = (abs(last.close - prev.close) / max(prev.close, 1e-9)) * 100

        notes.append(f"volatility_rank {volatility_rank:.2f}")
        notes.append(f"volume_ratio {volume_ratio:.2f}")
        notes.append(f"spread_bps {spread_bps:.3f}")
        notes.append(f"body_pct_of_range {body_pct:.2f}")
        notes.append(f"close_position {close_position:.3f}")
        notes.append(f"participation_score {long_participation_score if direction == 'LONG' else short_participation_score:.2f}")
        notes.append(f"followthrough_volume_ratio {followthrough_volume_ratio:.2f}")
        notes.append(f"ema_reclaim_distance_pct {ema_reclaim_distance_pct:.3f}")
        notes.append(f"ema_retest_zone_pct {ema_retest_zone_pct:.3f}")
        notes.append(f"long_retest_zone {long_retest_zone}")
        notes.append(f"short_retest_zone {short_retest_zone}")
        notes.append("entry_model=retest_zone_first")
        notes.append("low_vol_scalp_expectation")
        notes.append("fast_tp_required")
        notes.append("reclaim_unlock_v5=true")
        notes.append("selector_exhaustion_soft_override=true")

        for context_note in market_context_notes:
            if context_note not in notes:
                notes.append(context_note)

        detection = SweepDetection(
            side="low_vol_reclaim",
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

        if ratios[-1] >= self.MIN_VOLUME_RATIO:
            score += 0.75

        if ratios[-1] >= ratios[-2] >= ratios[-3]:
            score += 0.50

        if sum(1 for ratio in ratios if ratio >= self.MIN_VOLUME_RATIO) >= 2:
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

    @staticmethod
    def _extract_note_float(notes: list[str], marker: str, default: float = 0.0) -> float:
        note_text = " ".join(str(note).lower() for note in (notes or []))
        marker = marker.lower()

        if marker not in note_text:
            return default

        try:
            section = note_text.split(marker, 1)[1]
            raw = section.split()[0].strip(";|,")
            return float(raw)
        except Exception:
            return default

    @staticmethod
    def _extract_spread_bps(notes: list[str], default: float = 99.0) -> float:
        note_text = " ".join(str(note).lower() for note in (notes or []))
        marker = "spread "

        if marker not in note_text:
            return default

        try:
            section = note_text.split(marker, 1)[1]
            raw = section.split()[0].strip(";|,")
            raw = raw.replace("bps", "")
            return float(raw)
        except Exception:
            return default


def detect_low_vol_reclaim(market: MarketSnapshot) -> StrategyCandidate | None:
    return LowVolReclaimStrategy().detect(market)