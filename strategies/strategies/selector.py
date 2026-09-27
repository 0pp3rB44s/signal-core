import logging

from app.config import get_settings
from clients.schemas import StrategyCandidate

logger = logging.getLogger("strategy_selector")

def _safe_gate_result(result: object, candidate: StrategyCandidate, gate_name: str) -> tuple[bool, str]:
    if result is None:
        logger.error(
            "GATE_NONE_RETURN | %s | strategy=%s | gate=%s | treating_as_passed",
            candidate.symbol,
            candidate.strategy,
            gate_name,
        )
        return True, f"{gate_name}_none_return_treated_as_passed"

    try:
        ok, reason = result  # type: ignore[misc]
    except Exception as exc:
        logger.error(
            "GATE_BAD_RETURN | %s | strategy=%s | gate=%s | result=%r | error=%s | treating_as_passed",
            candidate.symbol,
            candidate.strategy,
            gate_name,
            result,
            exc,
        )
        return True, f"{gate_name}_bad_return_treated_as_passed"

    return bool(ok), str(reason)



MIN_SCORE = 72  # A+ gate aligned with planner Safe Mode
MIN_MOMENTUM_SCORE = 72
MIN_MOMENTUM_BREAKDOWN_SCORE = 72


MIN_CONTINUATION_SCORE = 72



def _strategy_allowed(candidate: StrategyCandidate) -> bool:
    """Runtime strategy isolation gate for live validation."""
    settings = get_settings()

    if not bool(getattr(settings, "strategy_isolation_enabled", False)):
        return True

    strategy_name = str(candidate.strategy or "").strip().lower()
    enabled = getattr(settings, "enabled_strategy_set", set())
    disabled = getattr(settings, "disabled_strategy_set", set())

    if disabled and any(token in strategy_name for token in disabled):
        logger.warning(
            "STRATEGY_ON_HOLD | %s | strategy=%s | disabled=%s",
            candidate.symbol,
            candidate.strategy,
            sorted(disabled),
        )
        return False

    if enabled and not any(token in strategy_name for token in enabled):
        logger.warning(
            "STRATEGY_ISOLATION_BLOCKED | %s | strategy=%s | enabled=%s",
            candidate.symbol,
            candidate.strategy,
            sorted(enabled),
        )
        return False

    logger.info(
        "STRATEGY_ALLOWED | %s | strategy=%s",
        candidate.symbol,
        candidate.strategy,
    )
    return True



# Helper to extract entry quality score from candidate notes.
def _entry_quality_score(candidate: StrategyCandidate) -> float:
    direction = candidate.direction.upper()
    note_text = " ".join(str(n).lower() for n in (candidate.notes or []))

    marker = "entry_quality "
    if marker not in note_text:
        return 100.0

    try:
        section = note_text.split(marker, 1)[1]
        parts = section.split()
        values: dict[str, float] = {}

        for part in parts:
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            value = value.strip(";|,")
            try:
                values[key] = float(value)
            except ValueError:
                continue

        if direction == "LONG":
            return values.get("long", 100.0)
        if direction == "SHORT":
            return values.get("short", 100.0)

    except Exception:
        return 100.0

    return 100.0


# --- PREARMED breakout helpers
def _note_text(candidate: StrategyCandidate) -> str:
    return " ".join(str(n).lower() for n in (candidate.notes or []))


def _extract_note_float(candidate: StrategyCandidate, marker: str, default: float = 0.0) -> float:
    note_text = _note_text(candidate)
    marker = marker.lower()
    if marker not in note_text:
        return default
    try:
        raw = note_text.split(marker, 1)[1].split()[0].strip(";|,")
        return float(raw)
    except Exception:
        return default


def _is_prearmed(candidate: StrategyCandidate) -> bool:
    note_text = _note_text(candidate)
    return "prearmed_breakout" in note_text or "prearmed_breakdown" in note_text



def _prearmed_quality(candidate: StrategyCandidate) -> bool:
    if not _is_prearmed(candidate):
        return False
    expansion_prob = _extract_note_float(candidate, "prearmed_expansion_prob=", 0.0)
    pressure_score = _extract_note_float(candidate, "prearmed_pressure_score=", 0.0)
    participation_score = _extract_note_float(candidate, "participation_score=", 0.0)
    followthrough_volume_ratio = _extract_note_float(candidate, "followthrough_volume_ratio=", 0.0)
    return (
        expansion_prob >= 80.0
        and pressure_score >= 30.0
        and participation_score >= 1.0
        and followthrough_volume_ratio >= 0.45
    )


# --- MTF override helpers

def _prearmed_context_override(candidate: StrategyCandidate) -> bool:
    note_text = _note_text(candidate)
    pressure_score = _pressure_score(candidate)
    expansion_prob = _expansion_prob(candidate)
    participation_score = _extract_note_float(candidate, "participation_score=", 0.0)
    followthrough_volume_ratio = _extract_note_float(candidate, "followthrough_volume_ratio=", 0.0)
    structure_score = _extract_note_float(candidate, "structure_score=", 0.0)
    return (
        ("prearmed_context_override=true" in note_text or "high_quality_breakout_context=true" in note_text)
        and pressure_score >= 45.0
        and expansion_prob >= 70.0
        and structure_score >= 1.0
        and participation_score >= 0.55
        and followthrough_volume_ratio >= 0.25
    )


def _has_mtf_override(candidate: StrategyCandidate) -> bool:
    note_text = _note_text(candidate)
    return (
        "mtf_prearmed_override" in note_text
        or "mtf_sweep_mode=mtf_override" in note_text
        or "mtf_sweep_mode mtf_override" in note_text
        or "mtf_continuation_mode mtf_override" in note_text
        or "mtf_reclaim_mode mtf_override" in note_text
    )


def _mtf_pressure_score(candidate: StrategyCandidate) -> float:
    for marker in (
        "mtf_pressure_score=",
        "mtf_pressure_score ",
        "prearmed_pressure_score=",
        "prearmed_pressure_score ",
    ):
        value = _extract_note_float(candidate, marker, 0.0)
        if value:
            return value
    return 0.0


def _mtf_expansion_prob(candidate: StrategyCandidate) -> float:
    for marker in (
        "mtf_expansion_prob=",
        "mtf_expansion_prob ",
        "prearmed_expansion_prob=",
        "prearmed_expansion_prob ",
    ):
        value = _extract_note_float(candidate, marker, 0.0)
        if value:
            return value
    return 0.0


# --- Directional pressure and compression helpers
def _pressure_score(candidate: StrategyCandidate) -> float:
    for marker in (
        "pressure_score=",
        "pressure_score ",
        "mtf_pressure_score=",
        "mtf_pressure_score ",
        "prearmed_pressure_score=",
        "prearmed_pressure_score ",
    ):
        value = _extract_note_float(candidate, marker, 0.0)
        if value:
            return value
    return 0.0


def _expansion_prob(candidate: StrategyCandidate) -> float:
    for marker in (
        "expansion_prob=",
        "expansion_prob ",
        "mtf_expansion_prob=",
        "mtf_expansion_prob ",
        "prearmed_expansion_prob=",
        "prearmed_expansion_prob ",
    ):
        value = _extract_note_float(candidate, marker, 0.0)
        if value:
            return value
    return 0.0


def _origin_distance_score(candidate: StrategyCandidate) -> float:
    return _extract_note_float(candidate, "origin_distance_score=", 0.0)


def _impulse_freshness_score(candidate: StrategyCandidate) -> float:
    return _extract_note_float(candidate, "impulse_freshness_score=", 100.0)


def _expansion_exhaustion_score(candidate: StrategyCandidate) -> float:
    return _extract_note_float(candidate, "expansion_exhaustion_score=", 0.0)


# --- Candle structure helpers
def _candle_body_pct(candidate: StrategyCandidate) -> float:
    return _extract_note_float(candidate, "candle_body_pct=", 0.0)


def _upper_wick_pct(candidate: StrategyCandidate) -> float:
    return _extract_note_float(candidate, "upper_wick_pct=", 0.0)


def _lower_wick_pct(candidate: StrategyCandidate) -> float:
    return _extract_note_float(candidate, "lower_wick_pct=", 0.0)


def _close_strength(candidate: StrategyCandidate) -> float:
    return _extract_note_float(candidate, "close_strength=", 0.5)


def _breakout_context_ready(candidate: StrategyCandidate) -> bool:
    note_text = _note_text(candidate)
    return (
        "breakout_context ready=true" in note_text
        or "breakout_context_ready=true" in note_text
        or "breakout setup ready" in note_text
        or "breakout_ready=true" in note_text
        or "breakout_ready true" in note_text
        or "breakdown_ready=true" in note_text
        or "breakdown_ready true" in note_text
        or "entry_model=retest_zone_first" in note_text
    )


def _directional_pressure_ok(candidate: StrategyCandidate) -> bool:
    note_text = _note_text(candidate)
    direction = (candidate.direction or "").upper()
    wanted = "bullish" if direction == "LONG" else "bearish"
    pressure_score = _pressure_score(candidate)
    expansion_prob = _expansion_prob(candidate)

    return (
        f"pressure={wanted}" in note_text
        or f"direction={wanted}" in note_text
        or _breakout_context_ready(candidate)
        or (pressure_score >= 30.0 and expansion_prob >= 55.0)
    )


def _compression_quality(candidate: StrategyCandidate) -> bool:
    note_text = _note_text(candidate)
    pressure_score = _pressure_score(candidate)
    expansion_prob = _expansion_prob(candidate)
    return (
        ("compression_quality true" in note_text or "compression_quality=true" in note_text or "range tightening" in note_text)
        and pressure_score >= 45.0
        and expansion_prob >= 65.0
    )


def _mtf_quality(candidate: StrategyCandidate) -> bool:
    if not _has_mtf_override(candidate):
        return False

    pressure_score = _mtf_pressure_score(candidate)
    expansion_prob = _mtf_expansion_prob(candidate)
    participation_score = _extract_note_float(candidate, "participation_score=", 0.0)
    if participation_score == 0.0:
        participation_score = _extract_note_float(candidate, "participation_score ", 0.0)

    return (
        pressure_score >= 30.0
        and expansion_prob >= 55.0
        and participation_score >= 0.75
    )


def _execution_penalty(candidate: StrategyCandidate) -> float:
    notes = candidate.notes or []
    note_text = _note_text(candidate)
    prearmed_quality = _prearmed_quality(candidate)

    penalty = 0.0

    close_pos = None

    if "close_pos=" in note_text:
        try:
            section = note_text.split("close_pos=", 1)[1]
            raw = section.split()[0].strip(";|,")
            close_pos = float(raw)
        except Exception:
            close_pos = None

    breakout_ready = _breakout_context_ready(candidate)

    if "late long entry near candle high" in note_text:
        penalty += 10 if prearmed_quality else 18

        if close_pos is not None and close_pos >= 0.90:
            penalty += 6 if prearmed_quality else 12

        elif close_pos is not None and close_pos >= 0.80:
            penalty += 3 if prearmed_quality else 6

    if "late short entry near candle low" in note_text:
        penalty += 10 if prearmed_quality else 18

        if close_pos is not None and close_pos <= 0.10:
            penalty += 6 if prearmed_quality else 12

        elif close_pos is not None and close_pos <= 0.20:
            penalty += 3 if prearmed_quality else 6

    if "upper wick rejection risk" in note_text:
        penalty += 5

    if "lower wick rejection risk" in note_text:
        penalty += 5

    if "wide spread" in note_text:
        penalty += 6

    if breakout_ready and "entry_quality long=60" in note_text:
        penalty += 10

    if breakout_ready and "entry_quality short=60" in note_text:
        penalty += 10

    return penalty


# -- Retest-required helper
def _retest_required_reason(candidate: StrategyCandidate, execution_penalty: float) -> str | None:
    note_text = " ".join(str(n).lower() for n in (candidate.notes or []))
    direction = candidate.direction.upper()
    alignment = (candidate.market.alignment or "").lower()
    primary_trend = (candidate.market.primary.trend or "").lower()
    confirmation_trend = (candidate.market.confirmation.trend or "").lower()
    strategy = (candidate.strategy or "").lower()

    strong_trend = (
        (direction == "LONG" and alignment == "aligned_bullish" and primary_trend == "bullish" and confirmation_trend == "bullish")
        or (direction == "SHORT" and alignment == "aligned_bearish" and primary_trend == "bearish" and confirmation_trend == "bearish")
    )

    has_breakout_pressure = (
        "breakout setup ready" in note_text
        or "volatility_note=bearish breakout pressure" in note_text
        or "volatility_note=bullish breakout pressure" in note_text
        or "high expansion probability" in note_text
        or "breakout_ready=true" in note_text
        or "breakdown_ready=true" in note_text
        or "entry_model=retest_zone_first" in note_text
    )

    if not strong_trend or not has_breakout_pressure:
        return None

    if direction == "SHORT" and "late short entry near candle low" in note_text:
        return "RETEST_REQUIRED: bearish pressure strong but short entry is too low in candle; wait for pullback/retest"

    if direction == "LONG" and "late long entry near candle high" in note_text:
        return "RETEST_REQUIRED: bullish pressure strong but long entry is too high in candle; wait for pullback/retest"

    if "reclaim timing extended" in note_text and execution_penalty >= 18:
        return "RETEST_REQUIRED: reclaim timing extended; wait for cleaner continuation retest"

    if direction == "LONG" and execution_penalty >= 12:
        return "RETEST_REQUIRED: long entry quality degraded; wait for pullback/retest"

    if direction == "SHORT" and execution_penalty >= 12:
        return "RETEST_REQUIRED: short entry quality degraded; wait for pullback/retest"

    return None



def _continuation_volume_threshold(candidate: StrategyCandidate) -> float:
    volatility_rank = float(
        getattr(candidate.market, "volatility_rank", 0.0)
        or getattr(candidate.market.primary, "atr_percent", 0.0)
        or 0.0
    )
    alignment = (candidate.market.alignment or "").lower()
    primary_trend = (candidate.market.primary.trend or "").lower()
    confirmation_trend = (candidate.market.confirmation.trend or "").lower()

    threshold = 1.0

    # Strong aligned trend + elevated volatility can tolerate slightly lower raw volume.
    # This helps capture grind continuations without opening the floodgates in chop.
    if (
        alignment in {"aligned_bullish", "aligned_bearish"}
        and primary_trend in {"bullish", "bearish"}
        and confirmation_trend in {"bullish", "bearish"}
    ):
        if volatility_rank >= 15:
            threshold = 0.75
        elif volatility_rank >= 10:
            threshold = 0.85

    return threshold


# --- Adaptive required score helper
def _adaptive_required_score(candidate: StrategyCandidate, base_required_score: float) -> float:
    """Lower/raise selector gate only when market context justifies it."""
    strategy = (candidate.strategy or "").lower()
    note_text = _note_text(candidate)
    pressure_score = _pressure_score(candidate)
    expansion_prob = _expansion_prob(candidate)
    participation_score = _extract_note_float(candidate, "participation_score=", 0.0)
    if participation_score == 0.0:
        participation_score = _extract_note_float(candidate, "participation_score ", 0.0)
    followthrough_volume_ratio = _extract_note_float(candidate, "followthrough_volume_ratio=", 0.0)
    if followthrough_volume_ratio == 0.0:
        followthrough_volume_ratio = _extract_note_float(candidate, "followthrough_volume_ratio ", 0.0)
    volume_ratio = float(candidate.market.primary.volume_ratio_20 or 0.0)
    entry_quality = _entry_quality_score(candidate)
    execution_penalty = _execution_penalty(candidate)
    mtf_quality = _mtf_quality(candidate)
    compression_quality = _compression_quality(candidate)

    required_score = float(base_required_score)

    strong_execution = entry_quality >= 72.0 and execution_penalty <= 14.0
    strong_participation = participation_score >= 1.35 and followthrough_volume_ratio >= 0.55
    strong_pressure = pressure_score >= 55.0 and expansion_prob >= 70.0
    volume_ok = volume_ratio >= 0.90

    if "low_vol_reclaim" in strategy:
        if "reclaim_unlock_v5=true" in note_text:
            required_score = min(required_score, 72.0)
        elif mtf_quality:
            required_score = min(required_score, 72.0)

    if "continuation" in strategy:
        if "continuation_regime trend_participation" in note_text and strong_execution and strong_participation and volume_ok:
            required_score -= 5.0
        elif "continuation_regime compression_pre_expansion" in note_text and strong_execution and strong_pressure and volume_ok:
            required_score -= 4.0
        elif mtf_quality and strong_execution and (strong_participation or strong_pressure):
            required_score -= 3.0

        if "continuation_regime post_expansion_exhaustion" in note_text:
            required_score += 6.0
        elif "continuation_regime chop_or_conflict" in note_text:
            required_score += 8.0
        elif "continuation_regime low_energy" in note_text and not compression_quality:
            required_score += 4.0

    if "momentum" in strategy or "breakout" in strategy:
        if _prearmed_quality(candidate) and strong_execution and strong_pressure:
            required_score -= 3.0

    return max(62.0, required_score)


def _selector_score(candidate: StrategyCandidate) -> float:
    score = 0.0

    alignment = (candidate.market.alignment or "").lower()
    direction = candidate.direction.upper()
    strategy = (candidate.strategy or "").lower()
    primary_trend = (candidate.market.primary.trend or "").lower()
    confirmation_trend = (candidate.market.confirmation.trend or "").lower()
    notes = candidate.notes or []

    # Alignment is the core filter.
    if direction == "LONG" and alignment == "aligned_bullish":
        score += 40
    elif direction == "SHORT" and alignment == "aligned_bearish":
        score += 40
    elif alignment == "conflicted":
        score -= 18
    elif alignment == "mixed":
        score -= 4 if _mtf_quality(candidate) else 10

    # Strategy edge weighting.
    if "sweep" in strategy:
        score += 25
    elif "momentum_breakdown" in strategy:
        score += 22
    elif "momentum" in strategy or "breakout" in strategy:
        score += 22
    elif "low_vol_reclaim" in strategy:
        score += 14
    elif "continuation" in strategy:
        score += 6

    # Trend agreement.
    if direction == "LONG":
        if primary_trend == "bullish":
            score += 12
        if confirmation_trend == "bullish":
            score += 12
        elif confirmation_trend == "neutral":
            score += 5
    elif direction == "SHORT":
        if primary_trend == "bearish":
            score += 12
        if confirmation_trend == "bearish":
            score += 12
        elif confirmation_trend == "neutral":
            score += 5

    # Notes are weak confirmation, capped.
    score += min(len(notes), 8)

    # Strategy-specific confirmations from notes.
    note_text = _note_text(candidate)
    prearmed_quality = _prearmed_quality(candidate)
    prearmed_context_override = _prearmed_context_override(candidate)
    mtf_override = _has_mtf_override(candidate)
    mtf_quality = _mtf_quality(candidate)
    pressure_ok = _directional_pressure_ok(candidate)
    compression_quality = _compression_quality(candidate)
    if "volume expansion" in note_text:
        score += 8
    if "breakout above range" in note_text:
        score += 8
    if "breakdown below range" in note_text:
        score += 8
    if "reclaim failed" in note_text:
        score += 8
    if "sweep" in note_text or "liquidity" in note_text:
        score += 6

    if prearmed_quality:
        score += 14
    if _prearmed_context_override(candidate):
        score += 10
    if mtf_quality:
        score += 10
    if "prearmed_breakout" in note_text or "prearmed_breakdown" in note_text:
        score += 6
    if "range tightening" in note_text:
        score += 4
    if _breakout_context_ready(candidate):
        score += 6
    if mtf_override:
        score += 4

    volume_ratio = float(candidate.market.primary.volume_ratio_20 or 0.0)

    if volume_ratio >= 2.0:
        score += 10
    elif volume_ratio >= 1.5:
        score += 7
    elif volume_ratio >= 1.0:
        score += 4
    elif volume_ratio < 0.8:
        if mtf_quality:
            score -= 2
        else:
            score -= 4 if prearmed_quality else 12

    if "continuation" in strategy and volume_ratio < 1.0:
        score -= 3 if mtf_quality else 8

    entry_quality = _entry_quality_score(candidate)
    if entry_quality < 50:
        score -= 18
    elif entry_quality < 65:
        score -= 12
    elif entry_quality < 80:
        score -= 6

    if "continuation" in strategy and not pressure_ok:
        if compression_quality:
            score -= 4
        else:
            score -= 12 if _pressure_score(candidate) < 25.0 else 6

    execution_penalty = _execution_penalty(candidate)
    retest_reason = _retest_required_reason(candidate, execution_penalty)
    if retest_reason:
        score -= 20
    score -= execution_penalty

    return score


# Emits info about candidates that are near the selection threshold or have strong/override qualities.
def _emit_near_selector_candidate(
    candidate: StrategyCandidate,
    score: float,
    required_score: float,
    entry_quality: float,
    execution_penalty: float,
    reason: str,
) -> None:
    score_gap = required_score - score
    near_score = score_gap <= 8.0
    strong_score = score >= 66.0
    mtf_quality = _mtf_quality(candidate)
    prearmed_quality = _prearmed_quality(candidate)

    if not (near_score or strong_score or mtf_quality or prearmed_quality):
        return

    note_text = _note_text(candidate)
    volume_ratio = float(candidate.market.primary.volume_ratio_20 or 0.0)

    logger.info(
        "NEAR_SELECTOR_CANDIDATE | %s | strategy=%s | direction=%s | score=%.1f | required=%.1f | gap=%.1f | entry_quality=%.1f | execution_penalty=%.1f | mtf_quality=%s | prearmed_quality=%s | volume_ratio=%.2f | alignment=%s | primary=%s | confirmation=%s | reason=%s | notes=%s",
        candidate.symbol,
        candidate.strategy,
        candidate.direction,
        score,
        required_score,
        score_gap,
        entry_quality,
        execution_penalty,
        mtf_quality,
        prearmed_quality,
        volume_ratio,
        candidate.market.alignment,
        candidate.market.primary.trend,
        candidate.market.confirmation.trend,
        reason,
        note_text[:240],
    )



def _emit_selector_reject_intelligence(
    candidate: StrategyCandidate,
    score: float,
    required_score: float,
    entry_quality: float,
    execution_penalty: float,
    reason: str,
    stage: str,
) -> None:
    """Structured reject intelligence logging without changing selection behavior."""
    strategy = (candidate.strategy or "").lower()
    note_text = _note_text(candidate)
    score_gap = required_score - score

    pressure_score = _pressure_score(candidate)
    expansion_prob = _expansion_prob(candidate)

    participation_score = _extract_note_float(candidate, "participation_score=", 0.0)
    if participation_score == 0.0:
        participation_score = _extract_note_float(candidate, "participation_score ", 0.0)

    followthrough_volume_ratio = _extract_note_float(candidate, "followthrough_volume_ratio=", 0.0)
    if followthrough_volume_ratio == 0.0:
        followthrough_volume_ratio = _extract_note_float(candidate, "followthrough_volume_ratio ", 0.0)

    volume_ratio = float(candidate.market.primary.volume_ratio_20 or 0.0)
    required_volume = _continuation_volume_threshold(candidate) if "continuation" in strategy else 0.0

    logger.info(
        "SELECTOR_REJECT_INTELLIGENCE | %s | stage=%s | strategy=%s | direction=%s | reason=%s | "
        "score=%.1f | required=%.1f | gap=%.1f | entry_quality=%.1f | execution_penalty=%.1f | "
        "pressure_ok=%s | pressure_score=%.1f | expansion_prob=%.1f | compression_quality=%s | "
        "participation_score=%.2f | followthrough_volume_ratio=%.2f | volume_ratio=%.2f | required_volume=%.2f | "
        "mtf_quality=%s | prearmed_quality=%s | alignment=%s | primary=%s | confirmation=%s | notes=%s",
        candidate.symbol,
        stage,
        candidate.strategy,
        candidate.direction,
        reason,
        score,
        required_score,
        score_gap,
        entry_quality,
        execution_penalty,
        _directional_pressure_ok(candidate),
        pressure_score,
        expansion_prob,
        _compression_quality(candidate),
        participation_score,
        followthrough_volume_ratio,
        volume_ratio,
        required_volume,
        _mtf_quality(candidate),
        _prearmed_quality(candidate),
        candidate.market.alignment,
        candidate.market.primary.trend,
        candidate.market.confirmation.trend,
        note_text[:260],
    )


def _hard_filters(candidate: StrategyCandidate) -> tuple[bool, str]:
    alignment = (candidate.market.alignment or "").lower()
    direction = candidate.direction.upper()
    strategy = (candidate.strategy or "").lower()
    primary_trend = (candidate.market.primary.trend or "").lower()
    confirmation_trend = (candidate.market.confirmation.trend or "").lower()
    notes = candidate.notes or []
    note_text = _note_text(candidate)
    prearmed_quality = _prearmed_quality(candidate)
    prearmed_context_override = _prearmed_context_override(candidate)
    mtf_override = _has_mtf_override(candidate)
    mtf_quality = _mtf_quality(candidate)
    pressure_ok = _directional_pressure_ok(candidate)
    compression_quality = _compression_quality(candidate)

    origin_distance_score = _origin_distance_score(candidate)
    impulse_freshness_score = _impulse_freshness_score(candidate)
    expansion_exhaustion_score = _expansion_exhaustion_score(candidate)
    logger.info(
        "TIMING_DEBUG | %s | origin=%.2f | freshness=%.2f | exhaustion=%.2f",
        candidate.symbol,
        origin_distance_score,
        impulse_freshness_score,
        expansion_exhaustion_score,
    )

    entry_quality = _entry_quality_score(candidate)
    close_pos = _extract_note_float(candidate, "close_pos=", 0.5)

    candle_body_pct = _candle_body_pct(candidate)
    upper_wick_pct = _upper_wick_pct(candidate)
    lower_wick_pct = _lower_wick_pct(candidate)
    close_strength = _close_strength(candidate)

    if (
        direction == "LONG"
        and upper_wick_pct >= 45.0
        and close_strength <= 0.55
    ):
        logger.info(
            "LONG_BLOCKED_UPPER_WICK_REJECTION | %s | upper_wick_pct=%.2f | close_strength=%.2f",
            candidate.symbol,
            upper_wick_pct,
            close_strength,
        )
        return False, "blocked: upper_wick_rejection"

    if (
        direction == "SHORT"
        and lower_wick_pct >= 45.0
        and close_strength >= 0.45
    ):
        logger.info(
            "SHORT_BLOCKED_LOWER_WICK_REJECTION | %s | lower_wick_pct=%.2f | close_strength=%.2f",
            candidate.symbol,
            lower_wick_pct,
            close_strength,
        )
        return False, "blocked: lower_wick_rejection"

    if candle_body_pct <= 20.0 and entry_quality < 70.0:
        logger.info(
            "WEAK_CANDLE_BODY_BLOCKED | %s | body_pct=%.2f | entry_quality=%.2f",
            candidate.symbol,
            candle_body_pct,
            entry_quality,
        )
        return False, "blocked: weak_candle_body"

    if (
        direction == "LONG"
        and close_pos > 0.85
        and entry_quality < 60.0
        and expansion_exhaustion_score > 70.0
    ):
        logger.info(
            "EXTENSION_EXHAUSTION_BLOCKED | %s | direction=LONG | close_pos=%.2f | entry_quality=%.1f | exhaustion=%.1f",
            candidate.symbol,
            close_pos,
            entry_quality,
            expansion_exhaustion_score,
        )
        return False, "blocked: late_long_extension"

    if (
        direction == "SHORT"
        and close_pos < 0.15
        and entry_quality < 60.0
        and expansion_exhaustion_score > 70.0
    ):
        logger.info(
            "EXTENSION_EXHAUSTION_BLOCKED | %s | direction=SHORT | close_pos=%.2f | entry_quality=%.1f | exhaustion=%.1f",
            candidate.symbol,
            close_pos,
            entry_quality,
            expansion_exhaustion_score,
        )
        return False, "blocked: late_short_extension"

    if origin_distance_score > 80.0:
        logger.info(
            "LATE_ENTRY_BLOCKED | %s | reason=far_from_origin | origin_distance_score=%.2f",
            candidate.symbol,
            origin_distance_score,
        )
        return False, "blocked: far_from_origin"

    if impulse_freshness_score < 25.0:
        logger.info(
            "LATE_ENTRY_BLOCKED | %s | reason=stale_impulse | impulse_freshness_score=%.2f",
            candidate.symbol,
            impulse_freshness_score,
        )
        return False, "blocked: stale_impulse"

    if expansion_exhaustion_score > 80.0:
        if "low_vol_reclaim" in strategy and "selector_exhaustion_soft_override=true" in note_text:
            logger.info(
                "LATE_ENTRY_WATCH | %s | reason=high_exhaustion_allowed_for_low_vol_reclaim | expansion_exhaustion_score=%.2f",
                candidate.symbol,
                expansion_exhaustion_score,
            )
            return True, "watch: high_exhaustion_allowed_for_low_vol_reclaim"
        else:
            logger.info(
                "LATE_ENTRY_BLOCKED | %s | reason=high_exhaustion | expansion_exhaustion_score=%.2f",
                candidate.symbol,
                expansion_exhaustion_score,
            )
            return False, "blocked: high_exhaustion"

    if alignment == "conflicted":
        return False, "blocked: bad alignment"

    if alignment == "mixed" and not mtf_quality:
        return False, "blocked: mixed alignment without MTF confirmation"

    if len(notes) < 2:
        return False, "blocked: low context"

    # Sweep edge: reversal after liquidity grab. This already supports LONG and SHORT.
    if "sweep" in strategy:
        return True, "ok"

    # Momentum breakout edge: clean bullish breakout + pullback/reclaim.
    if "momentum_breakout" in strategy:
        if direction != "LONG":
            return False, "blocked: momentum breakout must be LONG"
        if alignment != "aligned_bullish" and not mtf_quality and not prearmed_context_override:
            return False, "blocked: momentum breakout requires bullish alignment or MTF/prearmed context confirmation"
        if (primary_trend != "bullish" or confirmation_trend != "bullish") and not mtf_quality and not prearmed_context_override:
            return False, "blocked: momentum breakout requires bullish confirmation or MTF/prearmed context override"
        fallback_momentum_context = (
            alignment == "aligned_bullish"
            and primary_trend == "bullish"
            and confirmation_trend == "bullish"
            and _entry_quality_score(candidate) >= 85.0
            and (
                "range expansion" in note_text
                or "range_tightening=true" in note_text
                or "higher_lows_building=true" in note_text
                or "breakout_pressure=bullish" in note_text
            )
        )

        if "volume expansion" not in note_text and not prearmed_quality and not prearmed_context_override and not fallback_momentum_context:
            return False, "blocked: momentum breakout needs volume expansion"
        if (
            "breakout above range" not in note_text
            and "range expansion" not in note_text
            and "higher_lows_building=true" not in note_text
            and "range_tightening=true" not in note_text
            and not prearmed_context_override
        ):
            return False, "blocked: momentum breakout needs range breakout"
        if (
            "pullback held" not in note_text
            and "higher_lows_building=true" not in note_text
            and "close_pos=" not in note_text
            and not prearmed_context_override
        ):
            return False, "blocked: momentum breakout needs pullback hold"
        return True, "ok"

    # Momentum breakdown edge: clean bearish breakdown + failed reclaim.
    if "momentum_breakdown" in strategy:
        if direction != "SHORT":
            return False, "blocked: momentum breakdown must be SHORT"
        if alignment != "aligned_bearish" and not mtf_quality and not prearmed_context_override:
            return False, "blocked: momentum breakdown requires bearish alignment or MTF/prearmed context confirmation"
        if (primary_trend != "bearish" or confirmation_trend != "bearish") and not mtf_quality and not prearmed_context_override:
            return False, "blocked: momentum breakdown requires bearish confirmation or MTF/prearmed context override"
        if "volume expansion" not in note_text and not prearmed_quality and not prearmed_context_override:
            return False, "blocked: momentum breakdown needs volume expansion"
        if "breakdown below range" not in note_text and not prearmed_context_override:
            return False, "blocked: momentum breakdown needs range breakdown"
        if "reclaim failed" not in note_text and not prearmed_context_override:
            return False, "blocked: momentum breakdown needs failed reclaim"
        return True, "ok"

    # Continuation edge: only allow when trend alignment is fully clean.
    # This keeps Safe Mode strict, but prevents strong aligned continuation setups
    # from being rejected as "unsupported" before scoring.
    if "continuation" in strategy:
        if direction == "LONG":
            if alignment != "aligned_bullish" and not mtf_quality:
                return False, "blocked: long continuation requires bullish alignment or MTF override"
            if (primary_trend != "bullish" or confirmation_trend != "bullish") and not mtf_quality:
                return False, "blocked: long continuation requires bullish confirmation or MTF override"
        elif direction == "SHORT":
            if alignment != "aligned_bearish" and not mtf_quality:
                return False, "blocked: short continuation requires bearish alignment or MTF override"
            if (primary_trend != "bearish" or confirmation_trend != "bearish") and not mtf_quality:
                return False, "blocked: short continuation requires bearish confirmation or MTF override"
        else:
            return False, "blocked: continuation direction must be LONG or SHORT"
        fallback_continuation_context = (
            alignment in {"aligned_bullish", "aligned_bearish"}
            and _entry_quality_score(candidate) >= 80.0
            and (
                "range expansion" in note_text
                or "higher_lows_building=true" in note_text
                or "higher_highs_building=true" in note_text
                or "breakout_pressure=bullish" in note_text
                or "breakout_pressure=bearish" in note_text
            )
        )
        if not pressure_ok and not compression_quality and not fallback_continuation_context:
            return False, "blocked: continuation lacks directional pressure"

        volume_ratio = float(candidate.market.primary.volume_ratio_20 or 0.0)
        required_volume = _continuation_volume_threshold(candidate)

        if volume_ratio < required_volume and not fallback_continuation_context:
            return False, (
                f"blocked: continuation requires volume confirmation "
                f"({volume_ratio:.2f} < {required_volume:.2f})"
            )

        return True, "ok"

    # Low-vol reclaim edge: only allow clean aligned reclaim scalps.
    if "low_vol_reclaim" in strategy:
        if direction == "LONG":
            if alignment != "aligned_bullish" and not mtf_quality:
                return False, "blocked: low-vol long reclaim requires bullish alignment or MTF override"
            if (primary_trend != "bullish" or confirmation_trend != "bullish") and not mtf_quality:
                return False, "blocked: low-vol long reclaim requires bullish confirmation or MTF override"
        elif direction == "SHORT":
            if alignment != "aligned_bearish" and not mtf_quality:
                return False, "blocked: low-vol short reclaim requires bearish alignment or MTF override"
            if (primary_trend != "bearish" or confirmation_trend != "bearish") and not mtf_quality:
                return False, "blocked: low-vol short reclaim requires bearish confirmation or MTF override"
        else:
            return False, "blocked: low-vol reclaim direction must be LONG or SHORT"

        if "low_vol_reclaim_mode" not in note_text:
            return False, "blocked: low-vol reclaim missing mode confirmation"
        if "weak_followthrough_participation" in note_text:
            return False, "blocked: low-vol reclaim weak followthrough"
        if "ema_reclaim_too_extended" in note_text:
            return False, "blocked: low-vol reclaim too extended"

        return True, "ok"

    return False, f"blocked: unsupported strategy for Safe Mode: {candidate.strategy}"

    return True, "passed"

def select_best_candidate(
    sweep_candidate: StrategyCandidate | None = None,
    continuation_candidate: StrategyCandidate | None = None,
    low_vol_reclaim_candidate: StrategyCandidate | None = None,
    momentum_candidate: StrategyCandidate | None = None,
    momentum_breakdown_candidate: StrategyCandidate | None = None,
) -> tuple[StrategyCandidate | None, str]:
    if (
        sweep_candidate is None
        and continuation_candidate is None
        and low_vol_reclaim_candidate is None
        and momentum_candidate is None
        and momentum_breakdown_candidate is None
    ):
        return None, "no candidates"

    # Preference order: sweep first, then momentum candidates by score, continuation last, then low_vol_reclaim.
    ordered = [
        c
        for c in [
            sweep_candidate,
            momentum_candidate,
            momentum_breakdown_candidate,
            continuation_candidate,
            low_vol_reclaim_candidate,
        ]
        if c is not None
    ]

    best = None
    best_score = -1.0
    best_reason = ""
    reject_reasons: list[str] = []

    for candidate in ordered:
        hard_filter_result = _hard_filters(candidate)
        if hard_filter_result is None:
            logger.error(
                "HARD_FILTER_NONE_RETURN | %s | strategy=%s | treating_as_passed",
                candidate.symbol,
                candidate.strategy,
            )
            ok, reason = True, "hard_filter_none_return_treated_as_passed"
        else:
            ok, reason = hard_filter_result
        if not ok:
            rough_score = _selector_score(candidate)
            rough_entry_quality = _entry_quality_score(candidate)
            rough_execution_penalty = _execution_penalty(candidate)

            _emit_near_selector_candidate(
                candidate=candidate,
                score=rough_score,
                required_score=MIN_SCORE,
                entry_quality=rough_entry_quality,
                execution_penalty=rough_execution_penalty,
                reason=reason,
            )

            _emit_selector_reject_intelligence(
                candidate=candidate,
                score=rough_score,
                required_score=MIN_SCORE,
                entry_quality=rough_entry_quality,
                execution_penalty=rough_execution_penalty,
                reason=reason,
                stage="hard_filter",
            )

            reject_reasons.append(f"{candidate.strategy} {candidate.direction}: {reason}")
            continue

        score = _selector_score(candidate)
        entry_quality = _entry_quality_score(candidate)
        execution_penalty = _execution_penalty(candidate)
        strategy = (candidate.strategy or "").lower()
        prearmed_quality = _prearmed_quality(candidate)
        mtf_quality = _mtf_quality(candidate)
        pressure_ok = _directional_pressure_ok(candidate)
        compression_quality = _compression_quality(candidate)

        retest_reason = _retest_required_reason(candidate, execution_penalty)

        prearmed_context_override = _prearmed_context_override(candidate)
        if "momentum_breakdown" in strategy:
            required_score = 70 if (prearmed_quality or prearmed_context_override) else MIN_MOMENTUM_BREAKDOWN_SCORE
        elif "momentum" in strategy or "breakout" in strategy:
            required_score = 70 if (prearmed_quality or prearmed_context_override) else MIN_MOMENTUM_SCORE
        elif "continuation" in strategy:
            required_score = 74 if mtf_quality else MIN_CONTINUATION_SCORE
        elif "low_vol_reclaim" in strategy:
            required_score = 72.0
        else:
            required_score = MIN_SCORE
        if "continuation" in strategy and not pressure_ok:
            if compression_quality:
                required_score += 4
            else:
                required_score += 18

        base_required_score = required_score
        required_score = _adaptive_required_score(candidate, required_score)
        if required_score != base_required_score:
            logger.info(
                "ADAPTIVE_SELECTOR_GATE | %s | strategy=%s | direction=%s | base_required=%.1f | adaptive_required=%.1f | pressure_score=%.1f | expansion_prob=%.1f | entry_quality=%.1f | execution_penalty=%.1f | mtf_quality=%s | compression_quality=%s | notes=%s",
                candidate.symbol,
                candidate.strategy,
                candidate.direction,
                base_required_score,
                required_score,
                _pressure_score(candidate),
                _expansion_prob(candidate),
                entry_quality,
                execution_penalty,
                mtf_quality,
                compression_quality,
                _note_text(candidate)[:220],
            )

        if score >= required_score - 8 or mtf_quality or prearmed_quality:
            _emit_near_selector_candidate(
                candidate=candidate,
                score=score,
                required_score=required_score,
                entry_quality=entry_quality,
                execution_penalty=execution_penalty,
                reason="selector_pre_gate_check",
            )

        if entry_quality < 45:
            reason = f"entry quality too poor ({entry_quality:.1f})"
            _emit_selector_reject_intelligence(
                candidate=candidate,
                score=score,
                required_score=required_score,
                entry_quality=entry_quality,
                execution_penalty=execution_penalty,
                reason=reason,
                stage="entry_quality_gate",
            )
            reject_reasons.append(
                f"{candidate.strategy} {candidate.direction}: {reason}"
            )
            continue

        execution_penalty_limit = 36 if mtf_quality else (32 if prearmed_quality else 25)
        if "low_vol_reclaim" in strategy and "selector_exhaustion_soft_override=true" in _note_text(candidate):
            execution_penalty_limit = max(execution_penalty_limit, 35)
        if execution_penalty >= execution_penalty_limit:
            if retest_reason:
                reason = f"{retest_reason} | execution_penalty={execution_penalty:.1f}"
            else:
                reason = f"execution quality penalty too high ({execution_penalty:.1f} >= {execution_penalty_limit:.1f})"
            _emit_selector_reject_intelligence(
                candidate=candidate,
                score=score,
                required_score=required_score,
                entry_quality=entry_quality,
                execution_penalty=execution_penalty,
                reason=reason,
                stage="execution_quality_gate",
            )
            reject_reasons.append(
                f"{candidate.strategy} {candidate.direction}: {reason}"
            )
            continue

        if retest_reason and score < required_score + 8:
            reason = f"{retest_reason} | score={score:.1f} required={required_score:.1f}"
            _emit_selector_reject_intelligence(
                candidate=candidate,
                score=score,
                required_score=required_score,
                entry_quality=entry_quality,
                execution_penalty=execution_penalty,
                reason=reason,
                stage="retest_gate",
            )
            reject_reasons.append(
                f"{candidate.strategy} {candidate.direction}: {reason}"
            )
            continue

        if score < required_score:
            reason = f"score {score:.1f} < required {required_score:.1f} pressure_ok={pressure_ok} compression_quality={compression_quality}"
            _emit_selector_reject_intelligence(
                candidate=candidate,
                score=score,
                required_score=required_score,
                entry_quality=entry_quality,
                execution_penalty=execution_penalty,
                reason=reason,
                stage="score_gate",
            )
            reject_reasons.append(
                f"{candidate.strategy} {candidate.direction}: {reason}"
            )
            continue

        volume_ratio = float(candidate.market.primary.volume_ratio_20 or 0.0)
        if score > best_score:
            best = candidate
            best_score = score
            best_reason = (
                f"selected: score={score:.1f} required={required_score:.1f} "
                f"entry_quality={entry_quality:.1f} execution_penalty={execution_penalty:.1f} "
                f"volume_ratio={volume_ratio:.2f}"
            )

    if best is not None:
        return best, best_reason or "selected best candidate"

    if reject_reasons:
        return None, "; ".join(reject_reasons[-5:])

    return None, "no eligible candidate"
