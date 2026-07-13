"""Validatie-motor v1: entry-model × HTF-regime expectancy op het candle-archief.

Gebruik:  .venv/bin/python scripts/validation_engine.py

Replayt het archief (data/history/*_15m.json) bar-voor-bar met de ECHTE
BreakoutEngine en HTF-regime-classifier, simuleert per gedetecteerde setup
de trade-uitkomst met de live geometrie-regels (marktprijs-anker,
structuur-stop, TP1 op planner-vloer, profit-lock BE op 45% van TP1,
timeout, 12bps roundtrip fees) en aggregeert expectancy per
entry-model × HTF-regime × richting.

Output: reports/backtests/validation_matrix.json + tabel op stdout.

Dit is P4.3 v1 (parity op setup-klasse-niveau, niet volledige
strategie-parity — orderbook/1H-confirmatie zijn historisch niet
beschikbaar). Doel: weten welke setup-klasse edge heeft in welk regime
VOORDAT er live geld op gaat.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from market_data.breakout_engine import BreakoutEngine
from market_data.htf_regime import classify_htf_regime

HISTORY = ROOT / "data" / "history"
REPORTS = ROOT / "reports" / "backtests"

FEE_ROUNDTRIP_PCT = 0.12       # 12 bps
TP_R = {"coil": 1.05, "fresh_breakout": 1.05, "ema_reclaim": 1.30}
PROFIT_LOCK_FRACTION = 0.45    # live PROFIT_LOCK_TP1_FRACTION
HORIZON_BARS = 16              # 4 uur op 15m (dead-trade timeout territorium)
STOP_BAND_PCT = (0.15, 1.2)    # realistische stopafstanden
LOOKBACK = 20


def resample(candles: list[dict], factor: int) -> list[dict]:
    out = []
    for i in range(0, len(candles) - factor + 1, factor):
        chunk = candles[i:i + factor]
        out.append({
            "timestamp": chunk[0]["timestamp"],
            "open": chunk[0]["open"],
            "high": max(c["high"] for c in chunk),
            "low": min(c["low"] for c in chunk),
            "close": chunk[-1]["close"],
        })
    return out


def simulate(candles: list[dict], i: int, direction: str, entry: float, stop: float, tp_r: float) -> tuple[str, float]:
    """Uitkomst + netto R (incl. fees en profit-lock BE benadering)."""
    risk = abs(entry - stop)
    if risk <= 0:
        return "SKIP", 0.0
    stop_pct = risk / entry * 100
    fee_r = FEE_ROUNDTRIP_PCT / stop_pct
    sign = 1 if direction == "LONG" else -1
    tp = entry + sign * tp_r * risk
    lock_level = entry + sign * PROFIT_LOCK_FRACTION * tp_r * risk
    locked = False

    for c in candles[i + 1:i + 1 + HORIZON_BARS]:
        hit_tp = c["high"] >= tp if direction == "LONG" else c["low"] <= tp
        hit_lock = c["high"] >= lock_level if direction == "LONG" else c["low"] <= lock_level
        hit_sl = c["low"] <= stop if direction == "LONG" else c["high"] >= stop

        if hit_tp and hit_sl:
            return "AMBIGUOUS", -(1.0 + fee_r)  # conservatief: telt als stop
        if hit_tp:
            return "TP1", tp_r - fee_r
        if hit_sl:
            if locked:
                return "BE_LOCK", -fee_r * 0.5  # fee-adjusted BE benadering
            return "STOP", -(1.0 + fee_r)
        if hit_lock:
            locked = True
    return "TIMEOUT", (0.0 if not locked else -fee_r * 0.5)


def run() -> dict:
    engine = BreakoutEngine()
    matrix: dict[str, dict] = {}
    total_entries = 0

    for path in sorted(HISTORY.glob("*_15m.json")):
        candles = json.loads(path.read_text())
        if len(candles) < 500:
            continue
        candles_4h = resample(candles, 16)
        candles_1d = resample(candles, 96)

        for i in range(200, len(candles) - HORIZON_BARS - 1):
            window = candles[max(0, i - 39):i + 1]
            ctx = engine.analyze([SimpleNamespace(**c) for c in window])
            pressure = float(ctx.get("pressure_score") or 0)
            ready = bool(ctx.get("breakout_ready"))
            hint = str(ctx.get("direction") or "").lower()

            # HTF-regime op dit historische punt (4H/1D t/m bar i)
            h4 = [c for c in candles_4h if c["timestamp"] <= candles[i]["timestamp"]][-60:]
            d1 = [c for c in candles_1d if c["timestamp"] <= candles[i]["timestamp"]][-40:]
            regime = classify_htf_regime(h4, d1)["htf_regime"]

            close = candles[i]["close"]
            prev = candles[i - LOOKBACK:i]
            prev_high = max(c["high"] for c in prev)
            prev_low = min(c["low"] for c in prev)
            ema20 = _ema([c["close"] for c in candles[max(0, i - 60):i + 1]], 20)

            fired: list[tuple[str, str, float]] = []  # (model, direction, stop)

            # COIL: opgerold vlak onder/boven trigger met druk
            bo_pct = (close - prev_high) / prev_high * 100
            bd_pct = (prev_low - close) / prev_low * 100
            if ready and pressure >= 55 and hint == "bullish" and -0.20 <= bo_pct <= 0.0:
                fired.append(("coil", "LONG", min(c["low"] for c in candles[i - 6:i + 1])))
            if ready and pressure >= 55 and hint == "bearish" and -0.20 <= bd_pct <= 0.0:
                fired.append(("coil", "SHORT", max(c["high"] for c in candles[i - 6:i + 1])))

            # FRESH BREAKOUT/BREAKDOWN: eerste candle voorbij het niveau
            prev_high_before = max(c["high"] for c in candles[i - LOOKBACK - 1:i - 1])
            prev_low_before = min(c["low"] for c in candles[i - LOOKBACK - 1:i - 1])
            if bo_pct >= 0.12 and candles[i - 1]["close"] <= prev_high_before:
                fired.append(("fresh_breakout", "LONG", min(c["low"] for c in candles[i - 6:i + 1])))
            if bd_pct >= 0.12 and candles[i - 1]["close"] >= prev_low_before:
                fired.append(("fresh_breakout", "SHORT", max(c["high"] for c in candles[i - 6:i + 1])))

            # EMA-RECLAIM (reclaim-proxy): close herovert EMA20 na verblijf eronder/erboven
            prev_close = candles[i - 1]["close"]
            if prev_close < ema20 <= close and abs(close - ema20) / close * 100 <= 0.25:
                fired.append(("ema_reclaim", "LONG", min(c["low"] for c in candles[i - 6:i + 1])))
            if prev_close > ema20 >= close and abs(close - ema20) / close * 100 <= 0.25:
                fired.append(("ema_reclaim", "SHORT", max(c["high"] for c in candles[i - 6:i + 1])))

            for model, direction, stop in fired:
                stop_pct = abs(close - stop) / close * 100
                if not (STOP_BAND_PCT[0] <= stop_pct <= STOP_BAND_PCT[1]):
                    continue
                outcome, r_net = simulate(candles, i, direction, close, stop, TP_R[model])
                if outcome == "SKIP":
                    continue
                key = f"{model}|{regime}|{direction}"
                bucket = matrix.setdefault(key, {
                    "model": model, "htf_regime": regime, "direction": direction,
                    "n": 0, "tp1": 0, "stop": 0, "be_lock": 0, "timeout": 0, "r_sum": 0.0,
                })
                bucket["n"] += 1
                bucket["r_sum"] += r_net
                bucket[{"TP1": "tp1", "STOP": "stop", "AMBIGUOUS": "stop", "BE_LOCK": "be_lock", "TIMEOUT": "timeout"}[outcome]] += 1
                total_entries += 1

    for bucket in matrix.values():
        n = bucket["n"]
        bucket["tp1_rate"] = round(bucket["tp1"] / n, 4) if n else 0.0
        bucket["expectancy_r"] = round(bucket["r_sum"] / n, 4) if n else 0.0
        bucket["r_sum"] = round(bucket["r_sum"], 4)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "symbols": [p.stem.replace("_15m", "") for p in sorted(HISTORY.glob("*_15m.json"))],
        "total_entries": total_entries,
        "params": {
            "tp_r": TP_R, "profit_lock_fraction": PROFIT_LOCK_FRACTION,
            "horizon_bars": HORIZON_BARS, "fee_roundtrip_pct": FEE_ROUNDTRIP_PCT,
        },
        "matrix": sorted(matrix.values(), key=lambda b: -b["expectancy_r"]),
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "validation_matrix.json").write_text(json.dumps(payload, indent=1))
    return payload


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    ema = values[0]
    for v in values[1:]:
        ema = v * alpha + ema * (1.0 - alpha)
    return ema


def main() -> int:
    payload = run()
    print(f"validation_matrix.json geschreven | entries: {payload['total_entries']}")
    print(f"\n{'model':16} {'regime':14} {'dir':6} {'n':>5} {'TP1%':>6} {'BE%':>5} {'exp(R)':>8}")
    for b in payload["matrix"]:
        if b["n"] < 20:
            continue
        print(f"{b['model']:16} {b['htf_regime']:14} {b['direction']:6} {b['n']:>5} "
              f"{100*b['tp1_rate']:>5.1f}% {100*b['be_lock']/b['n']:>4.1f}% {b['expectancy_r']:>+8.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
