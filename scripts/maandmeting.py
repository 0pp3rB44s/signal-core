#!/usr/bin/env python3
"""Maandmeting (fase 1/2) — PLAN_VOORUIT.md. Eén commando, drie metingen:

A. Hand-expectancy uit het handmatige journal (spoor A).
B. Forward paper-expectancy van de bot: EXECUTABLE plannen sinds observe-mode
   (2026-07-13) gesimuleerd tegen de ECHTE candles erna (SL vs TP, first touch).
   Dit is out-of-sample per definitie — de toekomst bestond nog niet toen het
   plan gelogd werd.
C. Regime-check: BTC 4H trend-sterkte (|EMA20-EMA50|/ATR14).

    python3 scripts/maandmeting.py
"""
from __future__ import annotations

import ast
import csv
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings
from clients.bitget_rest import BitgetRestClient

OBSERVE_START = "2026-07-13T20:00"  # observe-mode actief (PATCH-073)
FEE_PCT = 0.12


def _num_list(raw: str) -> list[float]:
    try:
        v = ast.literal_eval(raw)
        return [float(x) for x in (v if isinstance(v, (list, tuple)) else [v])]
    except Exception:
        return []


def deel_a() -> None:
    print("\n=== A. HAND-EXPECTANCY (spoor A) ===")
    path = ROOT / "data_store" / "manual_journal.csv"
    if not path.exists():
        print("  journal leeg — noteer trades met scripts/journal.py add")
        return
    rows = list(csv.DictReader(path.open()))
    rs = [float(r["r_multiple"]) for r in rows]
    if not rs:
        print("  journal leeg")
        return
    w = [r for r in rs if r > 0]
    print(f"  n={len(rs)} WR={100*len(w)/len(rs):.0f}% expectancy={sum(rs)/len(rs):+.3f}R totaal={sum(rs):+.1f}R")
    if len(rs) >= 20:
        print("  fase-2: " + ("POSITIEF -> bot wordt copiloot" if sum(rs) / len(rs) > 0 else "negatief -> niet opschalen"))
    else:
        print(f"  fase-2: nog {20-len(rs)} trades nodig voor oordeel")


def _candles(client, product, symbol: str, granularity: str = "1H", limit: int = 1000) -> list[dict]:
    raw = client.get_candles(symbol=symbol, product_type=product, granularity=granularity, limit=limit).get("data") or []
    rows = [{"t": int(r[0]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4])} for r in raw if len(r) >= 5]
    rows.sort(key=lambda x: x["t"])
    return rows


def deel_b(client, product) -> None:
    print("\n=== B. FORWARD PAPER (bot, sinds observe-mode) ===")
    path = ROOT / "logs" / "trade_plans.csv"
    plans = [
        r
        for r in csv.DictReader(path.open())
        if r.get("verdict") == "EXECUTABLE" and (r.get("timestamp") or "") >= OBSERVE_START
    ]
    if not plans:
        print(f"  nog geen EXECUTABLE plannen sinds {OBSERVE_START} — de funnel logt door; geduld")
        return
    cache: dict[str, list[dict]] = {}
    per: dict[str, list[float]] = {}
    open_count = 0
    for p in plans:
        sym = p["symbol"]
        entries = _num_list(p.get("entries") or "")
        tps = _num_list(p.get("take_profits") or "")
        try:
            stop = float(p.get("stop_loss") or 0)
        except ValueError:
            continue
        if not entries or not tps or stop <= 0:
            continue
        entry, tp = entries[0], tps[0]
        direction = (p.get("direction") or "").upper()
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        ts = datetime.fromisoformat(p["timestamp"].replace("Z", "+00:00"))
        t_ms = int(ts.timestamp() * 1000)
        if sym not in cache:
            try:
                cache[sym] = _candles(client, product, sym)
            except Exception:
                cache[sym] = []
        after = [c for c in cache[sym] if c["t"] > t_ms]
        outcome = None
        for c in after:
            if direction == "LONG":
                if c["l"] <= stop:
                    outcome = -risk
                    break
                if c["h"] >= tp:
                    outcome = tp - entry
                    break
            else:
                if c["h"] >= stop:
                    outcome = -risk
                    break
                if c["l"] <= tp:
                    outcome = entry - tp
                    break
        if outcome is None:
            open_count += 1
            continue
        fee_r = FEE_PCT / (risk / entry * 100)
        per.setdefault(p.get("strategy") or "?", []).append(outcome / risk - fee_r)
    total: list[float] = []
    for strat, rs in sorted(per.items(), key=lambda kv: -len(kv[1])):
        w = [r for r in rs if r > 0]
        total += rs
        print(f"  {strat[:24]:24} n={len(rs):3d} WR={100*len(w)/len(rs):3.0f}% exp={statistics.mean(rs):+.3f}R")
    if total:
        w = [r for r in total if r > 0]
        exp = statistics.mean(total)
        print(f"  {'TOTAAL':24} n={len(total):3d} WR={100*len(w)/len(total):3.0f}% exp={exp:+.3f}R  ({open_count} nog open)")
        if len(total) >= 30:
            print("  fase-2: " + ("POSITIEF -> kleine live-probe bespreken" if exp > 0 else "negatief -> observe blijft"))
        else:
            print(f"  fase-2: nog {30-len(total)} afgeronde paper-trades nodig voor oordeel")


def deel_c(client, product) -> None:
    print("\n=== C. REGIME (BTC 4H) ===")
    rows = _candles(client, product, "BTCUSDT", "4H", 400)
    c = [r["c"] for r in rows]
    h = [r["h"] for r in rows]
    l = [r["l"] for r in rows]

    def ema(vals, p):
        k = 2 / (p + 1)
        out = [vals[0]]
        for v in vals[1:]:
            out.append(v * k + out[-1] * (1 - k))
        return out

    trs = [h[0] - l[0]]
    for i in range(1, len(c)):
        trs.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    atr = sum(trs[-14:]) / 14
    e20, e50 = ema(c, 20)[-1], ema(c, 50)[-1]
    strength = abs(e20 - e50) / atr if atr > 0 else 0
    label = "TRENDING" if strength >= 1.0 else "CHOP/GRIND"
    print(f"  trend-sterkte: {strength:.2f} -> {label} (drempel 1.0; markt is historisch ~38% v/d tijd trending)")
    if strength >= 1.0:
        print("  nb: trending regime — moment om de trend-following her-test (paper) te overwegen")


def main() -> None:
    settings = Settings()
    client = BitgetRestClient(settings=settings)
    product = settings.bitget_product_type
    print(f"MAANDMETING — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC (regels: docs/PLAN_VOORUIT.md fase 2)")
    deel_a()
    try:
        deel_b(client, product)
    except Exception as exc:
        print(f"  forward-meting fout: {exc}")
    try:
        deel_c(client, product)
    except Exception as exc:
        print(f"  regime-check fout: {exc}")
    print()


if __name__ == "__main__":
    main()
