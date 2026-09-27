import logging
from app.config import Settings
from clients.schemas import Candle, MarketSnapshot, StrategyCandidate, SweepDetection
from candidate_lifecycle import deterministic_candidate_id
from market_features.engine import closed_candle_at_offset
from market_features.engine import closed_window

logger = logging.getLogger("StartupRunner")


class LiquiditySweepStrategy:
    def _extract_note_text_value(self, market: MarketSnapshot, marker: str, default: str = "") -> str:
        note_text = self._note_text(market)
        marker = marker.lower()
        if marker not in note_text:
            return default
        try:
            return note_text.split(marker, 1)[1].split()[0].strip("|,;").lower()
        except Exception:
            return default
    name = "liquidity_sweep_reversal"
    _reject_counts: dict[str, int] = {}

    def _reject(self, symbol: str, side: str, reason: str, **context: object) -> None:
        reject_signature = (
            str(symbol).upper(),
            side,
            reason,
        )

        signature_key = (str(symbol).upper(), side)

        if getattr(self, "_last_reject_signature", {}).get(signature_key) == reject_signature:
            return

        self._last_reject_signature[signature_key] = reject_signature

        self._reject_counts[reason] = self._reject_counts.get(reason, 0) + 1
        details = " | ".join(f"{key}={value}" for key, value in context.items())
        if details:
            logger.info(
                "LIQUIDITY_SWEEP_REJECT | %s | side=%s | reason=%s | count=%s | %s",
                symbol,
                side,
                reason,
                self._reject_counts.get(reason, 0),
                details,
            )
        else:
            logger.info(
                "LIQUIDITY_SWEEP_REJECT | %s | side=%s | reason=%s | count=%s",
                symbol,
                side,
                reason,
                self._reject_counts.get(reason, 0),
            )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._last_reject_signature: dict[tuple[str, str], tuple[str, str, str]] = {}
        # Slightly relaxed for isolation mode: still requires sweep + reclaim + wick + failed breakout/breakdown.
        self.min_participation_score = 0.70
        self.min_followthrough_volume_ratio = 0.25
        self.mtf_override_min_pressure_score = 40.0
        self.mtf_override_min_expansion_prob = 60.0

    def _trend_allows_direction(self, market: MarketSnapshot, direction: str) -> bool:
        primary_trend = (market.primary.trend or "").lower()
        confirmation_trend = (market.confirmation.trend or "").lower()

        alignment = (market.alignment or "").lower()

        if direction == "LONG":
            if alignment == "conflicted" or primary_trend == "bearish":
                return False
            return confirmation_trend in {"bullish", "neutral"} and primary_trend in {"bullish", "mixed", "neutral"}
        if direction == "SHORT":
            if alignment == "conflicted" or primary_trend == "bullish":
                return False
            return confirmation_trend in {"bearish", "neutral"} and primary_trend in {"bearish", "mixed", "neutral"}
        return False

    def _note_text(self, market: MarketSnapshot) -> str:
        return " | ".join(str(note).lower() for note in (market.notes or []))

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

    def _mtf_sweep_override_context(self, market: MarketSnapshot, direction: str) -> dict[str, object]:
        note_text = self._note_text(market)
        primary_trend = (market.primary.trend or "").lower()
        confirmation_trend = (market.confirmation.trend or "").lower()
        alignment = (market.alignment or "").lower()

        wanted_pressure = "bullish" if direction == "LONG" else "bearish"
        pressure_score = self._extract_note_float(market, "pressure_score=", 0.0)
        expansion_prob = self._extract_note_float(market, "expansion_prob=", 0.0)
        breakout_context_ready = (
            "breakout_context ready=true" in note_text
            or "breakout_ready=true" in note_text
            or "breakout_ready true" in note_text
            or "breakdown_ready=true" in note_text
            or "breakdown_ready true" in note_text
            or "breakout_ready_directional=true" in note_text
        )
        breakout_direction = self._extract_note_text_value(market, "breakout_direction=", "unknown")
        breakout_context_direction = (
            f"direction={wanted_pressure}" in note_text
            or breakout_direction == wanted_pressure
            or (wanted_pressure == "bearish" and breakout_direction in {"short", "down", "breakdown"})
            or (wanted_pressure == "bullish" and breakout_direction in {"long", "up", "breakout"})
        )
        volatility_pressure_direction = f"pressure={wanted_pressure}" in note_text

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
                and pressure_score >= self.mtf_override_min_pressure_score
                and expansion_prob >= self.mtf_override_min_expansion_prob
            )
        )

        allowed = bool(regime_ok and structure_ok and pressure_ok)

        return {
            "allowed": allowed,
            "direction": direction,
            "pressure_score": pressure_score,
            "expansion_prob": expansion_prob,
            "breakout_context_ready": breakout_context_ready,
            "structure_ok": structure_ok,
            "regime_ok": regime_ok,
            "pressure_ok": pressure_ok,
            "alignment": alignment,
            "primary_trend": primary_trend,
            "confirmation_trend": confirmation_trend,
        }

    def _trend_allows_direction_mtf(self, market: MarketSnapshot, direction: str) -> tuple[bool, dict[str, object]]:
        strict_allowed = self._trend_allows_direction(market, direction)
        mtf_context = self._mtf_sweep_override_context(market, direction)

        if strict_allowed:
            return True, {**mtf_context, "mode": "strict"}

        if mtf_context.get("allowed"):
            logger.info(
                "LIQUIDITY_SWEEP_MTF_OVERRIDE | %s | direction=%s | alignment=%s | primary=%s | confirmation=%s | pressure_score=%.2f | expansion_prob=%.1f",
                market.symbol,
                direction,
                mtf_context.get("alignment"),
                mtf_context.get("primary_trend"),
                mtf_context.get("confirmation_trend"),
                float(mtf_context.get("pressure_score", 0.0)),
                float(mtf_context.get("expansion_prob", 0.0)),
            )
            return True, {**mtf_context, "mode": "mtf_override"}

        return False, {**mtf_context, "mode": "blocked"}

    def detect(self, market: MarketSnapshot) -> StrategyCandidate | None:
        candles = closed_window(market.primary)
        if len(candles) < max(40, self.settings.sweep_pivot_lookback + self.settings.sweep_recent_bars + 5):
            return None

        absolute_idx = len(candles) - 1

        bull = self._detect_bullish_sweep(market, candles, absolute_idx)
        bull_allowed, bull_mtf_context = self._trend_allows_direction_mtf(market, "LONG")
        if bull is not None and bull_allowed:
            notes = self._candidate_notes(market, bull)
            notes.append(f"mtf_sweep_mode={bull_mtf_context.get('mode')}")
            notes.append(f"mtf_pressure_score={float(bull_mtf_context.get('pressure_score', 0.0)):.2f}")
            notes.append(f"mtf_expansion_prob={float(bull_mtf_context.get('expansion_prob', 0.0)):.1f}")
            return StrategyCandidate(
                candidate_id=deterministic_candidate_id(self.name, market.symbol, "LONG", closed_candle_at_offset(market.primary, bull.bars_since_sweep).timestamp_ms),
                candidate_candle_open_timestamp_ms=closed_candle_at_offset(market.primary, bull.bars_since_sweep).timestamp_ms,
                symbol=market.symbol,
                strategy=self.name,
                direction="LONG",
                primary_granularity=market.primary.granularity,
                confirmation_granularity=market.confirmation.granularity,
                market=market,
                detection=bull,
                notes=notes,
            )
        if bull is not None:
            self._reject(
                market.symbol,
                "bullish_sweep",
                "trend_filter_blocked_long",
                alignment=market.alignment,
                primary_trend=market.primary.trend,
                confirmation_trend=market.confirmation.trend,
            )

        bear = self._detect_bearish_sweep(market, candles, absolute_idx)
        bear_allowed, bear_mtf_context = self._trend_allows_direction_mtf(market, "SHORT")
        if bear is not None and bear_allowed:
            notes = self._candidate_notes(market, bear)
            notes.append(f"mtf_sweep_mode={bear_mtf_context.get('mode')}")
            notes.append(f"mtf_pressure_score={float(bear_mtf_context.get('pressure_score', 0.0)):.2f}")
            notes.append(f"mtf_expansion_prob={float(bear_mtf_context.get('expansion_prob', 0.0)):.1f}")
            return StrategyCandidate(
                candidate_id=deterministic_candidate_id(self.name, market.symbol, "SHORT", closed_candle_at_offset(market.primary, bear.bars_since_sweep).timestamp_ms),
                candidate_candle_open_timestamp_ms=closed_candle_at_offset(market.primary, bear.bars_since_sweep).timestamp_ms,
                symbol=market.symbol,
                strategy=self.name,
                direction="SHORT",
                primary_granularity=market.primary.granularity,
                confirmation_granularity=market.confirmation.granularity,
                market=market,
                detection=bear,
                notes=notes,
            )
        if bear is not None:
            self._reject(
                market.symbol,
                "bearish_sweep",
                "trend_filter_blocked_short",
                alignment=market.alignment,
                primary_trend=market.primary.trend,
                confirmation_trend=market.confirmation.trend,
            )

        return None

    def _detect_bullish_sweep(self, market: MarketSnapshot, candles: list[Candle], i: int) -> SweepDetection | None:
        if i < self.settings.sweep_pivot_lookback or i >= len(candles):
            self._reject(market.symbol, "bullish_sweep", "not_enough_pivot_history", index=i)
            return None
        candle = candles[i]
        lookback = candles[i - self.settings.sweep_pivot_lookback : i]
        choppy_lookback = self._is_choppy(lookback)

        swept_level = min(c.low for c in lookback)
        reclaim_tolerance = swept_level * (self.settings.sweep_reclaim_tolerance_bps / 10_000)
        reclaimed = candle.close >= swept_level - reclaim_tolerance
        wicked_through = candle.low < swept_level
        displacement_pct = ((candle.close - candle.low) / candle.close) * 100 if candle.close else 0.0
        local_high = max(c.high for c in lookback)
        local_range_pct = ((local_high - swept_level) / candle.close) * 100 if candle.close else 0.0
        sweep_bar_ratio = self._volume_ratio(candles, i)
        candle_range = candle.high - candle.low
        lower_wick = min(candle.open, candle.close) - candle.low
        body = abs(candle.close - candle.open)
        wick_ratio = lower_wick / candle_range if candle_range else 0.0
        close_position_pct = ((candle.close - candle.low) / candle_range) if candle_range else 0.0

        reclaim_strength_pct = ((candle.close - swept_level) / candle.close) * 100 if candle.close else 0.0
        failed_breakdown = candle.close > candle.open and candle.close > swept_level
        participation_score = self._participation_score(candles, i, direction="LONG")
        followthrough_volume_ratio = self._volume_ratio(candles, len(candles) - 1)

        strong_reclaim_exception = (
            choppy_lookback
            and wicked_through
            and reclaimed
            and wick_ratio >= 0.35
            and close_position_pct >= 0.55
            and sweep_bar_ratio >= self.settings.min_sweep_volume_ratio
        )

        if choppy_lookback and not strong_reclaim_exception:
            self._reject(
                market.symbol,
                "bullish_sweep",
                "choppy_lookback",
                wick_ratio=round(wick_ratio, 3),
                close_position_pct=round(close_position_pct, 3),
                volume_ratio=round(sweep_bar_ratio, 3),
            )
            return None

        if not (wicked_through and reclaimed):
            self._reject(
                market.symbol,
                "bullish_sweep",
                "no_sweep_reclaim",
                swept_level=round(swept_level, 8),
                low=round(candle.low, 8),
                close=round(candle.close, 8),
                reclaimed=reclaimed,
                wicked_through=wicked_through,
            )
            return None
        if displacement_pct < self.settings.min_sweep_displacement_pct:
            self._reject(
                market.symbol,
                "bullish_sweep",
                "weak_displacement",
                displacement_pct=round(displacement_pct, 3),
                min_displacement=self.settings.min_sweep_displacement_pct,
            )
            return None
        if sweep_bar_ratio < self.settings.min_sweep_volume_ratio:
            self._reject(
                market.symbol,
                "bullish_sweep",
                "weak_sweep_volume",
                volume_ratio=round(sweep_bar_ratio, 3),
                min_volume=self.settings.min_sweep_volume_ratio,
            )
            return None
        if participation_score < self.min_participation_score:
            self._reject(
                market.symbol,
                "bullish_sweep",
                "weak_participation_score",
                participation_score=round(participation_score, 3),
                minimum=self.min_participation_score,
            )
            return None
        if wick_ratio < 0.25:
            self._reject(market.symbol, "bullish_sweep", "weak_lower_wick", wick_ratio=round(wick_ratio, 3))
            return None
        if body <= 0:
            self._reject(market.symbol, "bullish_sweep", "zero_body")
            return None
        if candle.close <= candle.open:
            self._reject(
                market.symbol,
                "bullish_sweep",
                "no_bullish_close",
                open=round(candle.open, 8),
                close=round(candle.close, 8),
            )
            return None
        if close_position_pct < 0.48:
            self._reject(
                market.symbol,
                "bullish_sweep",
                "weak_close_position",
                close_position_pct=round(close_position_pct, 3),
            )
            return None

        if reclaim_strength_pct < 0.04:
            self._reject(
                market.symbol,
                "bullish_sweep",
                "weak_reclaim_strength",
                reclaim_strength_pct=round(reclaim_strength_pct, 3),
            )
            return None

        if not failed_breakdown:
            self._reject(
                market.symbol,
                "bullish_sweep",
                "no_failed_breakdown_confirmation",
                close=round(candle.close, 8),
                swept_level=round(swept_level, 8),
            )
            return None

        if followthrough_volume_ratio < self.min_followthrough_volume_ratio:
            self._reject(
                market.symbol,
                "bullish_sweep",
                "weak_followthrough_participation",
                followthrough_volume_ratio=round(followthrough_volume_ratio, 3),
                minimum=self.min_followthrough_volume_ratio,
            )
            return None

        return SweepDetection(
            side="bullish_sweep",
            swept_level=swept_level,
            sweep_extreme=candle.low,
            reclaim_level=swept_level,
            entry_hint=candle.close,
            invalidation=candle.low,
            displacement_pct=displacement_pct,
            bars_since_sweep=(len(candles) - 1) - i,
            volume_ratio_on_sweep=sweep_bar_ratio,
            local_range_size_pct=local_range_pct,
            reason_flags=[
                "sell-side liquidity taken",
                "failed breakdown confirmed",
                "close reclaimed prior low",
                "reaction volume present",
                f"participation_score={participation_score:.2f}",
                f"followthrough_volume_ratio={followthrough_volume_ratio:.2f}",
                f"choppy_exception={strong_reclaim_exception}",
                f"tp_origin_hint={local_high:.8f}",
            ],
        )

    def _detect_bearish_sweep(self, market: MarketSnapshot, candles: list[Candle], i: int) -> SweepDetection | None:
        if i < self.settings.sweep_pivot_lookback or i >= len(candles):
            self._reject(market.symbol, "bearish_sweep", "not_enough_pivot_history", index=i)
            return None
        candle = candles[i]
        lookback = candles[i - self.settings.sweep_pivot_lookback : i]
        choppy_lookback = self._is_choppy(lookback)

        swept_level = max(c.high for c in lookback)
        reclaim_tolerance = swept_level * (self.settings.sweep_reclaim_tolerance_bps / 10_000)
        reclaimed = candle.close <= swept_level + reclaim_tolerance
        wicked_through = candle.high > swept_level
        displacement_pct = ((candle.high - candle.close) / candle.close) * 100 if candle.close else 0.0
        local_low = min(c.low for c in lookback)
        local_range_pct = ((swept_level - local_low) / candle.close) * 100 if candle.close else 0.0
        sweep_bar_ratio = self._volume_ratio(candles, i)
        candle_range = candle.high - candle.low
        upper_wick = candle.high - max(candle.open, candle.close)
        body = abs(candle.close - candle.open)
        wick_ratio = upper_wick / candle_range if candle_range else 0.0
        close_position_pct = ((candle.close - candle.low) / candle_range) if candle_range else 0.0

        reclaim_strength_pct = ((swept_level - candle.close) / candle.close) * 100 if candle.close else 0.0
        failed_breakout = candle.close < candle.open and candle.close < swept_level
        participation_score = self._participation_score(candles, i, direction="SHORT")
        followthrough_volume_ratio = self._volume_ratio(candles, len(candles) - 1)

        strong_reclaim_exception = (
            choppy_lookback
            and wicked_through
            and reclaimed
            and wick_ratio >= 0.35
            and close_position_pct <= 0.45
            and sweep_bar_ratio >= self.settings.min_sweep_volume_ratio
        )

        if choppy_lookback and not strong_reclaim_exception:
            self._reject(
                market.symbol,
                "bearish_sweep",
                "choppy_lookback",
                wick_ratio=round(wick_ratio, 3),
                close_position_pct=round(close_position_pct, 3),
                volume_ratio=round(sweep_bar_ratio, 3),
            )
            return None

        if not (wicked_through and reclaimed):
            self._reject(
                market.symbol,
                "bearish_sweep",
                "no_sweep_reclaim",
                swept_level=round(swept_level, 8),
                high=round(candle.high, 8),
                close=round(candle.close, 8),
                reclaimed=reclaimed,
                wicked_through=wicked_through,
            )
            return None
        if displacement_pct < self.settings.min_sweep_displacement_pct:
            self._reject(
                market.symbol,
                "bearish_sweep",
                "weak_displacement",
                displacement_pct=round(displacement_pct, 3),
                min_displacement=self.settings.min_sweep_displacement_pct,
            )
            return None
        if sweep_bar_ratio < self.settings.min_sweep_volume_ratio:
            self._reject(
                market.symbol,
                "bearish_sweep",
                "weak_sweep_volume",
                volume_ratio=round(sweep_bar_ratio, 3),
                min_volume=self.settings.min_sweep_volume_ratio,
            )
            return None
        if participation_score < self.min_participation_score:
            self._reject(
                market.symbol,
                "bearish_sweep",
                "weak_participation_score",
                participation_score=round(participation_score, 3),
                minimum=self.min_participation_score,
            )
            return None
        if wick_ratio < 0.25:
            self._reject(market.symbol, "bearish_sweep", "weak_upper_wick", wick_ratio=round(wick_ratio, 3))
            return None
        if body <= 0:
            self._reject(market.symbol, "bearish_sweep", "zero_body")
            return None
        if candle.close >= candle.open:
            self._reject(
                market.symbol,
                "bearish_sweep",
                "no_bearish_close",
                open=round(candle.open, 8),
                close=round(candle.close, 8),
            )
            return None
        if close_position_pct > 0.48:
            self._reject(
                market.symbol,
                "bearish_sweep",
                "weak_close_position",
                close_position_pct=round(close_position_pct, 3),
            )
            return None

        if reclaim_strength_pct < 0.04:
            self._reject(
                market.symbol,
                "bearish_sweep",
                "weak_reclaim_strength",
                reclaim_strength_pct=round(reclaim_strength_pct, 3),
            )
            return None

        if not failed_breakout:
            self._reject(
                market.symbol,
                "bearish_sweep",
                "no_failed_breakout_confirmation",
                close=round(candle.close, 8),
                swept_level=round(swept_level, 8),
            )
            return None

        if followthrough_volume_ratio < self.min_followthrough_volume_ratio:
            self._reject(
                market.symbol,
                "bearish_sweep",
                "weak_followthrough_participation",
                followthrough_volume_ratio=round(followthrough_volume_ratio, 3),
                minimum=self.min_followthrough_volume_ratio,
            )
            return None

        return SweepDetection(
            side="bearish_sweep",
            swept_level=swept_level,
            sweep_extreme=candle.high,
            reclaim_level=swept_level,
            entry_hint=candle.close,
            invalidation=candle.high,
            displacement_pct=displacement_pct,
            bars_since_sweep=(len(candles) - 1) - i,
            volume_ratio_on_sweep=sweep_bar_ratio,
            local_range_size_pct=local_range_pct,
            reason_flags=[
                "buy-side liquidity taken",
                "failed breakout confirmed",
                "close reclaimed prior high",
                "reaction volume present",
                f"participation_score={participation_score:.2f}",
                f"followthrough_volume_ratio={followthrough_volume_ratio:.2f}",
                f"choppy_exception={strong_reclaim_exception}",
                f"tp_origin_hint={local_low:.8f}",
            ],
        )
    def _participation_score(self, candles: list[Candle], index: int, direction: str) -> float:
        if index < 3:
            return 0.0

        recent_indexes = [index - 2, index - 1, index]
        ratios = [self._volume_ratio(candles, i) for i in recent_indexes]
        candles_slice = [candles[i] for i in recent_indexes]

        score = 0.0

        if ratios[2] >= self.settings.min_sweep_volume_ratio:
            score += 1.0

        if ratios[2] >= ratios[1] >= ratios[0]:
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

    @staticmethod
    def _is_choppy(candles: list[Candle]) -> bool:
        if len(candles) < 8:
            return True

        ranges = [max(c.high - c.low, 0.0) for c in candles]
        avg_range = sum(ranges) / len(ranges) if ranges else 0.0
        if avg_range <= 0:
            return True

        total_high = max(c.high for c in candles)
        total_low = min(c.low for c in candles)
        total_range = total_high - total_low
        if total_range <= 0:
            return True

        overlap_count = 0
        for previous, current in zip(candles, candles[1:]):
            overlap_high = min(previous.high, current.high)
            overlap_low = max(previous.low, current.low)
            if overlap_high > overlap_low:
                overlap_count += 1

        overlap_ratio = overlap_count / max(len(candles) - 1, 1)
        compression_ratio = total_range / avg_range if avg_range else 0.0
        # Only block extreme chop. Normal range liquidity sweeps often happen inside overlap.
        return overlap_ratio > 0.85 and compression_ratio < 2.2

    @staticmethod
    def _volume_ratio(candles: list[Candle], index: int, period: int = 20) -> float:
        start = max(0, index - period)
        sample = candles[start:index]
        if not sample:
            return 1.0
        avg = sum(c.volume_base for c in sample) / len(sample)
        return candles[index].volume_base / avg if avg else 1.0

    @staticmethod
    def _candidate_notes(market: MarketSnapshot, detection: SweepDetection) -> list[str]:
        notes = list(detection.reason_flags)
        notes.append(f"sweep side {detection.side}")
        notes.append(f"{market.primary.granularity} trend {market.primary.trend}")
        notes.append(f"{market.confirmation.granularity} trend {market.confirmation.trend}")
        notes.append(f"bars since sweep {detection.bars_since_sweep}")
        notes.append(f"sweep volume ratio {detection.volume_ratio_on_sweep:.2f}")
        notes.append("entry_model=sweep_reclaim_first")
        notes.append("score_profile=liquidity_sweep_reversal")
        for flag in detection.reason_flags:
            if "participation_score=" in flag or "followthrough_volume_ratio=" in flag:
                notes.append(flag)
        return notes
