from dataclasses import dataclass

@dataclass
class ContinuationSetup:
    symbol: str
    direction: str
    entry: float
    invalidation: float
    score: float
    notes: str


def detect_continuation(symbol, data_15m, data_1h):
    # simpele inputs
    last_15m = data_15m[-1]
    prev_15m = data_15m[-2]

    last_1h = data_1h[-1]

    notes = []
    score = 0

    # ---- HTF TREND ----
    if last_1h["trend"] == "bullish":
        score += 20
        direction = "LONG"
        notes.append("HTF bullish")
    elif last_1h["trend"] == "bearish":
        score += 20
        direction = "SHORT"
        notes.append("HTF bearish")
    else:
        return None

    # ---- PULLBACK ----
    if last_15m["pullback"]:
        score += 15
        notes.append("pullback detected")

    # ---- RECLAIM ----
    if last_15m["reclaim"]:
        score += 20
        notes.append("reclaim confirmed")

    # ---- MOMENTUM ----
    if last_15m["momentum"]:
        score += 15
        notes.append("momentum present")

    # ---- VOLUME ----
    if last_15m["volume_ratio"] > 1.2:
        score += 10
        notes.append("volume expansion")

    # ---- STRUCTURE ----
    if last_15m["clean_structure"]:
        score += 10
        notes.append("clean structure")

    # ---- ENTRY & INVALIDATION ----
    entry = last_15m["close"]

    if direction == "LONG":
        invalidation = last_15m["swing_low"]
    else:
        invalidation = last_15m["swing_high"]

    return ContinuationSetup(
        symbol=symbol,
        direction=direction,
        entry=entry,
        invalidation=invalidation,
        score=score,
        notes=", ".join(notes)
    )