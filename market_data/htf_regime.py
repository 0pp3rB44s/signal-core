"""Higher-timeframe regime-classificatie (1D + 4H).

Doel (BLUEPRINT §4 / roadmap timeframe-uitbreiding): setups op 15m/1H
mogen niet tegen de hogere-timeframe trend in worden geopend. De 1D en
4H trends worden geclassificeerd via EMA20-positie + recente structuur;
de risk gate blokkeert hard wanneer BEIDE HTF's de trade-richting
tegenspreken en degradeert naar probe-size wanneer één van beide dat doet.

Fail-open bewust: geen HTF-data → "neutral" → geen block. De HTF-laag is
een extra filter bovenop bestaande alignment-checks, geen vervanging.
"""

from __future__ import annotations

from typing import Any
from market_features.engine import ema


def _closes(candles: list[Any]) -> list[float]:
    out: list[float] = []
    for c in candles or []:
        try:
            if isinstance(c, dict):
                out.append(float(c.get("close")))
            elif isinstance(c, (list, tuple)) and len(c) >= 5:
                out.append(float(c[4]))
            else:
                out.append(float(getattr(c, "close")))
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def classify_trend(candles: list[Any], ema_period: int = 20) -> str:
    """bullish / bearish / neutral op basis van EMA-positie + structuur."""
    closes = _closes(candles)
    if len(closes) < ema_period + 5:
        return "neutral"

    ema_value = ema(closes, ema_period)
    last = closes[-1]
    # afstand tot EMA als percentage — vlak bij de EMA is geen trend
    distance_pct = (last - ema_value) / ema_value * 100 if ema_value else 0.0

    # structuur: vergelijk recente helft met de helft ervoor
    recent = closes[-5:]
    prior = closes[-10:-5]
    structure_up = min(recent) > min(prior) and max(recent) >= max(prior)
    structure_down = max(recent) < max(prior) and min(recent) <= min(prior)

    if distance_pct > 0.15 and structure_up:
        return "bullish"
    if distance_pct < -0.15 and structure_down:
        return "bearish"
    if distance_pct > 0.50:
        return "bullish"
    if distance_pct < -0.50:
        return "bearish"
    return "neutral"


def classify_htf_regime(candles_4h: list[Any], candles_1d: list[Any]) -> dict[str, str]:
    regime_4h = classify_trend(candles_4h)
    regime_1d = classify_trend(candles_1d)

    if regime_1d == regime_4h and regime_1d != "neutral":
        combined = regime_1d  # volledige HTF-consensus
    elif "neutral" in (regime_1d, regime_4h):
        combined = regime_4h if regime_1d == "neutral" else regime_1d
        combined = f"lean_{combined}" if combined != "neutral" else "neutral"
    else:
        combined = "conflicted"

    return {
        "regime_1d": regime_1d,
        "regime_4h": regime_4h,
        "htf_regime": combined,
    }


def htf_opposition(direction: str, regime: dict[str, str]) -> tuple[int, list[str]]:
    """Hoeveel HTF's spreken deze richting tegen? (0, 1 of 2) + details."""
    direction = str(direction or "").upper()
    opposing = "bearish" if direction == "LONG" else ("bullish" if direction == "SHORT" else "")
    if not opposing:
        return 0, []

    hits: list[str] = []
    if regime.get("regime_1d") == opposing:
        hits.append(f"1D={opposing}")
    if regime.get("regime_4h") == opposing:
        hits.append(f"4H={opposing}")
    return len(hits), hits
