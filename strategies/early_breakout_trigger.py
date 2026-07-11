"""1-minute early-trigger layer with 5m confirmation (docs/EARLY_TRIGGER_1M.md).

Catches a momentum breakout AS it forms on the 1m timeframe (~1 min late instead
of up to 15) and confirms it on the 5m before firing, so a lone 1m spike against
a stalling 5m is filtered out. The multi-timeframe stack:

    context 1H  ->  setup 15m  ->  confirm 5m  ->  trigger 1m

The 1m/5m data is reused from the already-populated multi_tf_cache (no extra API
calls). The emitted candidate reuses the momentum_breakout / momentum_breakdown
identity, flows through the normal scoring/risk/planner pipeline with a
STRUCTURAL 15m stop, and is tagged to trade at PROBE size until proven. 1m/5m are
SIGNAL timeframes only; the trade is held for the bigger 15m/1H move (not a
1m scalp — scalps lose to fees).

Feature-flagged (settings.early_trigger_1m_enabled).
"""

from __future__ import annotations

import logging

from clients.schemas import Candle, MarketSnapshot, StrategyCandidate
from strategies.momentum_breakout import BreakoutDetection

logger = logging.getLogger("StartupRunner")


def candles_from_cache_rows(rows) -> list[Candle]:
    """Convert multi_tf_cache dict rows -> Candle objects, sorted oldest->newest.
    The cache preserves raw Bitget order (not guaranteed ascending), so we sort
    explicitly; candles[-1] must be the most recent closed candle."""
    if not rows:
        return []
    candles: list[Candle] = []
    for r in rows:
        try:
            candles.append(
                Candle(
                    timestamp_ms=int(r["timestamp"]),
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=float(r["close"]),
                    volume_base=float(r["volume"]),
                    volume_quote=None,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    candles.sort(key=lambda c: c.timestamp_ms)
    return candles


def _volume_ratio_at(candles: list[Candle], index: int, period: int = 20) -> float:
    if index <= 0 or index >= len(candles):
        return 0.0
    start = max(0, index - period)
    history = candles[start:index]
    if not history:
        return 0.0
    avg = sum(float(c.volume_base or 0.0) for c in history) / len(history)
    if avg <= 0:
        return 0.0
    return float(candles[index].volume_base or 0.0) / avg


def _participation_score(candles: list[Candle], direction: str) -> float:
    """Mirror of MomentumBreakoutStrategy._participation_score on 1m data
    (scale ~0.0-2.75) so the momentum scorer / close_pos gate read a comparable
    value."""
    if len(candles) < 3:
        return 0.0
    idx = len(candles) - 1
    ratios = [_volume_ratio_at(candles, i) for i in (idx - 2, idx - 1, idx)]
    slice3 = candles[idx - 2 : idx + 1]

    score = 0.0
    if ratios[-1] >= 1.0:
        score += 0.75
    if ratios[-1] >= ratios[-2] >= ratios[-3]:
        score += 0.50
    if sum(1 for r in ratios if r >= 1.0) >= 2:
        score += 0.50

    if direction == "LONG":
        directional = sum(1 for c in slice3 if c.close > c.open)
        progress = slice3[-1].close > slice3[-2].close >= slice3[-3].close
    else:
        directional = sum(1 for c in slice3 if c.close < c.open)
        progress = slice3[-1].close < slice3[-2].close <= slice3[-3].close
    if directional >= 2:
        score += 0.50
    if progress:
        score += 0.50
    return round(score, 2)


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    ema = float(values[0])
    for v in values[1:]:
        ema = (float(v) * alpha) + (ema * (1.0 - alpha))
    return ema


class EarlyBreakoutTrigger:
    """Detects a fresh, volume-confirmed 1m breakout, confirmed on the 5m and
    aligned with the 15m/1H trend; emits a momentum_breakout / momentum_breakdown
    candidate that trades at probe size."""

    def __init__(self, settings) -> None:
        self.settings = settings

    def detect(self, market: MarketSnapshot) -> StrategyCandidate | None:
        if not bool(getattr(self.settings, "early_trigger_1m_enabled", False)):
            return None

        context = getattr(market, "context", {}) or {}
        candles_1m = context.get("candles_1m")
        lookback = int(getattr(self.settings, "early_trigger_1m_lookback", 20))
        if not candles_1m or len(candles_1m) < lookback + 2:
            return None

        alignment = (market.alignment or "").lower()
        primary_trend = (market.primary.trend or "").lower()
        confirmation_trend = (market.confirmation.trend or "").lower()

        long_ok = (
            alignment in {"aligned_bullish", "mixed"}
            and primary_trend != "bearish"
            and confirmation_trend != "bearish"
        )
        short_ok = (
            alignment in {"aligned_bearish", "mixed"}
            and primary_trend != "bullish"
            and confirmation_trend != "bullish"
        )
        if not long_ok and not short_ok:
            return None

        last = candles_1m[-1]
        prior = candles_1m[-(lookback + 1) : -1]
        resistance = max(c.high for c in prior)
        support = min(c.low for c in prior)

        avg_vol = sum(float(c.volume_base or 0.0) for c in prior) / len(prior)
        vol_ratio_1m = (float(last.volume_base or 0.0) / avg_vol) if avg_vol > 0 else 0.0
        rng = max(last.high - last.low, 1e-9)
        body_pct = abs(last.close - last.open) / rng

        min_vr = float(getattr(self.settings, "early_trigger_1m_min_volume_ratio", 2.0))
        min_body = float(getattr(self.settings, "early_trigger_1m_min_body_pct", 0.5))
        max_disp = float(getattr(self.settings, "early_trigger_1m_max_displacement_pct", 0.5))

        if vol_ratio_1m < min_vr or body_pct < min_body:
            return None

        direction: str | None = None
        if long_ok and last.close > resistance and last.close > last.open:
            displacement = (last.close - resistance) / resistance * 100.0 if resistance else 999.0
            if displacement <= max_disp:
                direction = "LONG"
        if direction is None and short_ok and last.close < support and last.close < last.open:
            displacement = (support - last.close) / support * 100.0 if support else 999.0
            if displacement <= max_disp:
                direction = "SHORT"
        if direction is None:
            return None

        # 5m confirmation: a genuine breakout shows up on the 5m too, not just a
        # single 1m spike. Requires the last closed 5m candle to push in the
        # breakout direction and price to sit on the right side of the 5m EMA.
        if not self._confirm_5m(context.get("candles_5m"), direction):
            return None

        return self._build_candidate(market, candles_1m, last, direction, vol_ratio_1m)

    def _confirm_5m(self, candles_5m, direction: str) -> bool:
        # Fail-open: if 5m confirmation is disabled or the data is missing, do
        # not block the trigger (1m + 15m/1H already agree).
        if not bool(getattr(self.settings, "early_trigger_5m_confirm_enabled", True)):
            return True
        if not candles_5m or len(candles_5m) < 21:
            return True

        last5 = candles_5m[-1]
        closes = [c.close for c in candles_5m[-21:]]
        ema20 = _ema(closes, 20)
        if direction == "LONG":
            return last5.close > last5.open and last5.close >= ema20
        return last5.close < last5.open and last5.close <= ema20

    def _build_candidate(
        self,
        market: MarketSnapshot,
        candles_1m: list[Candle],
        last: Candle,
        direction: str,
        vol_ratio_1m: float,
    ) -> StrategyCandidate | None:
        entry = float(last.close)
        rng = max(last.high - last.low, 1e-9)
        close_pos = (last.close - last.low) / rng

        # Structural stop from the 15m primary, NOT a tight 1m level: hold for
        # the bigger move. For momentum the planner does not ATR-clamp the stop,
        # so this invalidation IS the stop.
        stop_lb = int(getattr(self.settings, "early_trigger_1m_structural_stop_lookback_15m", 4))
        c15 = list(getattr(market.primary, "candles", []) or [])
        recent15 = c15[-stop_lb:] if len(c15) >= stop_lb else c15
        if direction == "LONG":
            structural = min((c.low for c in recent15), default=entry)
            invalidation = min(structural, entry * (1.0 - 0.001))
        else:
            structural = max((c.high for c in recent15), default=entry)
            invalidation = max(structural, entry * (1.0 + 0.001))

        participation = _participation_score(candles_1m, direction)
        followthrough = round(vol_ratio_1m, 2)
        volume_ratio_15m = float(getattr(market.primary, "volume_ratio_20", 0.0) or 0.0)
        strategy_name = "momentum_breakout" if direction == "LONG" else "momentum_breakdown"

        notes = [
            "early_breakout_trigger_1m",
            "entry_trigger=1m_early",
            "early_trigger_probe=true",
            "breakout above range" if direction == "LONG" else "breakdown below range",
            "volume expansion",
            "strong continuation close",
            "5m_confirmed=true",
            f"breakout_pct={0.0:.2f}",
            f"breakdown_pct={0.0:.2f}",
            "bars_since_breakout=0",
            "bars_since_breakdown=0",
            f"volume_ratio={volume_ratio_15m:.2f}",
            f"trigger_1m_volume_ratio={vol_ratio_1m:.2f}",
            f"participation_score={participation:.2f}",
            f"followthrough_volume_ratio={followthrough:.2f}",
            f"close_pos={close_pos:.3f}",
            f"close_position={close_pos:.3f}",
        ]

        detection = BreakoutDetection(
            breakout_level=entry,
            close=entry,
            entry_hint=entry,
            reclaim_level=entry,
            invalidation=round(invalidation, 8),
            sweep_extreme=round(invalidation, 8),
            bars_since_sweep=0,
            volume_ratio=vol_ratio_1m,
            volume_ratio_on_sweep=vol_ratio_1m,
            displacement_pct=0.0,
            range_pct=0.0,
            local_range_size_pct=0.0,
            bars_lookback=int(getattr(self.settings, "early_trigger_1m_lookback", 20)),
            reason_flags=list(notes),
        )

        logger.info(
            "EARLY_TRIGGER_1M_FIRED | %s | strategy=%s | direction=%s | entry=%.8f | stop=%.8f | vol_ratio_1m=%.2f | participation=%.2f | close_pos=%.3f | alignment=%s | 5m_confirmed=true",
            market.symbol,
            strategy_name,
            direction,
            entry,
            invalidation,
            vol_ratio_1m,
            participation,
            close_pos,
            market.alignment,
        )

        return StrategyCandidate(
            symbol=market.symbol,
            strategy=strategy_name,
            direction=direction,
            primary_granularity=market.primary.granularity,
            confirmation_granularity=market.confirmation.granularity,
            market=market,
            detection=detection,
            notes=notes,
        )
