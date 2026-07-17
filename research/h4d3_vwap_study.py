#!/usr/bin/env python3
"""H-4D-3: VWAP-deviation reversie (pre-geregistreerde event study).

Protocol: docs/RESEARCH_JOURNAL.md H-4D-3 (GEREGISTREERD 2026-07-17, vóór
enige datafetch of test). Kern:

- dev(t) = ln(close_t / VWAP_dag(t)), VWAP = som(tp*v)/som(v), tp=(H+L+C)/3,
  reset 00:00 UTC; geldig vanaf candle-open >= 04:00 UTC en dagvolume > 0.
- Kwintielgrenzen (20%/80%) per symbool op ALLE geldige DEV-signalen
  (ongethind; zelfde grenzen voor beide horizonnen).
- Thinning per symbool per horizon (non-overlappend). Entry = OPEN t+1;
  exit = CLOSE t+N (N=4 @1h, N=16 @4h). Log-returns.
- Spread = mean(Q1) - mean(Q5) per 15m-timestampbucket (beide zijden aanwezig);
  REVERSIE-hypothese: spread > 0. Cluster op UTC-dag. BH over 2 horizonnen.
- DEV = [2024-07-17, 2025-07-17) | REP = [2025-07-17, 2026-07-17).
- Poorten: BH-p<0.05 DEV; zelfde teken + |t|>=2 REP; economische poot
  |Q1| of |Q5| >= 20 bps @1h / 25 bps @4h in DEV en REP; maandconsistentie
  >= 65%; niet gedreven door <=2 symbolen of 1 maand; teken in >=3/4
  subperioden en >=3/4 regimes. Falsificatie: zie journal (verwerpt, redt nooit).
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research.h4d2_session_study import bh_adjust, cstats, month_key, regime_labels

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
           "LINKUSDT", "AVAXUSDT", "ADAUSDT", "SUIUSDT", "LTCUSDT", "DOTUSDT"]
START_MS = int(datetime(2024, 7, 17, tzinfo=timezone.utc).timestamp() * 1000)
SPLIT_MS = int(datetime(2025, 7, 17, tzinfo=timezone.utc).timestamp() * 1000)
END_MS = int(datetime(2026, 7, 17, tzinfo=timezone.utc).timestamp() * 1000)
STEP_MS, HOUR_MS, DAY_MS = 900_000, 3_600_000, 86_400_000
MIN_DAY_MS = 4 * HOUR_MS          # signaal geldig vanaf candle-open 04:00 UTC
HORIZONS = {"1h": 4, "4h": 16}
ECON_LEG_BPS = {"1h": 20.0, "4h": 25.0}
COST_BPS = 14.0

CACHE = ROOT / "data" / "historical"
OUT_DIR = ROOT / "reports" / "analysis" / "h4d3_vwap"


def load_candles() -> dict[str, dict[int, list[float]]]:
    return {sym: {int(c[0]): c for c in json.loads(
        (CACHE / f"h4d3_{sym}_15m.json").read_text())["candles"]}
        for sym in SYMBOLS}


def build_signals(candles: dict[str, dict[int, list[float]]]) -> dict[str, list[tuple[int, float]]]:
    """Per symbool chronologisch (ts, dev); VWAP per UTC-dag cumulatief."""
    out: dict[str, list[tuple[int, float]]] = {}
    for sym, by_ts in candles.items():
        rows = []
        cur_day, cum_pv, cum_v = None, 0.0, 0.0
        for ts in sorted(by_ts):
            c = by_ts[ts]
            day = ts // DAY_MS
            if day != cur_day:
                cur_day, cum_pv, cum_v = day, 0.0, 0.0
            tp = (c[2] + c[3] + c[4]) / 3.0
            cum_pv += tp * c[5]
            cum_v += c[5]
            if ts % DAY_MS >= MIN_DAY_MS and cum_v > 0:
                rows.append((ts, math.log(c[4] / (cum_pv / cum_v))))
        out[sym] = rows
    return out


def forward_return(by_ts: dict[int, list[float]], ts: int, n: int) -> float | None:
    entry_c = by_ts.get(ts + STEP_MS)
    exit_c = by_ts.get(ts + n * STEP_MS)
    if entry_c is None or exit_c is None or entry_c[1] <= 0:
        return None
    return math.log(exit_c[4] / entry_c[1])


def thin(rows: list[tuple[int, float]], window_ms: int) -> list[tuple[int, float]]:
    out, last = [], None
    for ts, dev in rows:
        if last is None or ts - last >= window_ms:
            out.append((ts, dev))
            last = ts
    return out


def build_obs(candles, signals, horizon: str, entry_delay: int = 1,
              exclude: set[str] = frozenset(),
              volume_filter: dict[str, set[int]] | None = None) -> list[dict]:
    """Obs: ts, sym, kwintiel (Q1/Q5/mid), forward return. Grenzen op DEV."""
    n = HORIZONS[horizon]
    obs = []
    for sym in SYMBOLS:
        if sym in exclude:
            continue
        rows = signals[sym]
        devs = sorted(d for ts, d in rows if ts < SPLIT_MS)
        if len(devs) < 1000:
            continue
        lo = devs[int(0.2 * (len(devs) - 1))]
        hi = devs[int(0.8 * (len(devs) - 1))]
        for ts, dev in thin(rows, n * STEP_MS):
            if volume_filter is not None and ts // DAY_MS not in volume_filter[sym]:
                continue
            entry_ts = ts + (entry_delay - 1) * STEP_MS
            r = forward_return(candles[sym], entry_ts, n)
            if r is None:
                continue
            q = "Q1" if dev <= lo else "Q5" if dev >= hi else "mid"
            obs.append({"ts": ts, "sym": sym, "q": q, "ret": r})
    return obs


def spread_series(obs: list[dict], lo=START_MS, hi=END_MS) -> list[tuple[int, float]]:
    buckets: dict[int, dict[str, list[float]]] = defaultdict(lambda: {"Q1": [], "Q5": []})
    for o in obs:
        if lo <= o["ts"] < hi and o["q"] in ("Q1", "Q5"):
            buckets[o["ts"]][o["q"]].append(o["ret"])
    return [(ts, sum(v["Q1"]) / len(v["Q1"]) - sum(v["Q5"]) / len(v["Q5"]))
            for ts, v in sorted(buckets.items()) if v["Q1"] and v["Q5"]]


def leg_series(obs: list[dict], q: str, lo=START_MS, hi=END_MS) -> list[tuple[int, float]]:
    buckets: dict[int, list[float]] = defaultdict(list)
    for o in obs:
        if lo <= o["ts"] < hi and o["q"] == q:
            buckets[o["ts"]].append(o["ret"])
    return [(ts, sum(v) / len(v)) for ts, v in sorted(buckets.items())]


def monthly_share(rows: list[tuple[int, float]], sign: float) -> tuple[float, dict[str, float]]:
    months: dict[str, list[float]] = defaultdict(list)
    for ts, r in rows:
        months[month_key(ts)].append(r)
    means = {k: sum(v) / len(v) for k, v in sorted(months.items())}
    return (sum(1 for v in means.values() if v * sign > 0) / len(means)
            if means else 0.0), means


def volume_top_half(candles) -> dict[str, set[int]]:
    """Per symbool: UTC-dagen in de bovenste helft van dag-quotevolume."""
    out: dict[str, set[int]] = {}
    for sym, by_ts in candles.items():
        day_vol: dict[int, float] = defaultdict(float)
        for ts, c in by_ts.items():
            day_vol[ts // DAY_MS] += c[6]
        med = sorted(day_vol.values())[len(day_vol) // 2]
        out[sym] = {d for d, v in day_vol.items() if v >= med}
    return out


def fmt(s: dict | None) -> str:
    if not s:
        return "n<30"
    return f"{s['bps']:+7.2f} bps t={s['t']:+6.2f} (n={s['n']}, cl={s['clusters']})"


def run() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    candles = load_candles()
    signals = build_signals(candles)
    n_sig = sum(len(v) for v in signals.values())
    print(f"H-4D-3 | geldige signalen: {n_sig} over {len(signals)} symbolen")

    results: dict[str, dict] = {}
    obs_by_h: dict[str, list[dict]] = {}
    for hname in HORIZONS:
        obs = build_obs(candles, signals, hname)
        obs_by_h[hname] = obs
        dev_s = cstats(spread_series(obs, START_MS, SPLIT_MS))
        rep_s = cstats(spread_series(obs, SPLIT_MS, END_MS))
        legs = {}
        for q in ("Q1", "Q5"):
            legs[q] = {"dev": cstats(leg_series(obs, q, START_MS, SPLIT_MS)),
                       "rep": cstats(leg_series(obs, q, SPLIT_MS, END_MS))}
        results[hname] = {"dev": dev_s, "rep": rep_s, "legs": legs,
                          "n_obs": len(obs)}

    bh = bh_adjust({h: results[h]["dev"]["p"] for h in HORIZONS if results[h]["dev"]})
    print("\n=== PRIMAIR: Q1-Q5 spread (reversie => positief; dag-geclusterd) ===")
    for hname, r in results.items():
        r["bh_p"] = bh.get(hname, 1.0)
        print(f"\n{hname}: DEV {fmt(r['dev'])} BH-p={r['bh_p']:.4f}")
        print(f"     REP {fmt(r['rep'])}")
        for q in ("Q1", "Q5"):
            lg = r["legs"][q]
            print(f"     poot {q}: DEV {fmt(lg['dev'])} | REP {fmt(lg['rep'])}")

    candidates = [h for h in HORIZONS
                  if results[h]["dev"] and results[h]["rep"] and results[h]["bh_p"] < 0.05]
    print(f"\n=== KANDIDATEN (BH<0.05 DEV): {candidates or 'GEEN'} ===")

    verdicts = {}
    for hname in candidates:
        r = results[hname]
        d, p = r["dev"], r["rep"]
        obs = obs_by_h[hname]
        sign = 1.0 if d["bps"] > 0 else -1.0
        full = cstats(spread_series(obs))
        cons, months = monthly_share(spread_series(obs), sign)
        leg_ok = {}
        for per in ("dev", "rep"):
            leg_ok[per] = any(
                r["legs"][q][per] and abs(r["legs"][q][per]["bps"]) >= ECON_LEG_BPS[hname]
                and r["legs"][q][per]["bps"] * (1 if q == "Q1" else -1) * sign > 0
                for q in ("Q1", "Q5"))
        gates = {
            "G1_dev_bh": r["bh_p"] < 0.05,
            "G2_rep_teken": d["bps"] * p["bps"] > 0,
            "G3_rep_t2": abs(p["t"]) >= 2.0,
            "G4_registered_richting": d["bps"] > 0,  # reversie vooraf geregistreerd
            "G5_econ_poot_dev_en_rep": leg_ok["dev"] and leg_ok["rep"],
            "G6_maand_65pct": cons >= 0.65,
        }
        verdicts[hname] = {"gates": gates, "monthly": round(cons, 3)}
        print(f"\n-- {hname} | full spread {fmt(full)} | maandconsistentie {cons:.0%}")
        for g, ok in gates.items():
            print(f"   {g:24} {'PASS' if ok else 'FAIL'}")

        if all(gates.values()):
            print(f"\n   FALSIFICATIE {hname}:")
            contrib = {}
            for sym in SYMBOLS:
                sub = [o for o in obs if o["sym"] == sym]
                q1 = [o["ret"] for o in sub if o["q"] == "Q1"]
                q5 = [o["ret"] for o in sub if o["q"] == "Q5"]
                contrib[sym] = ((sum(q1) / len(q1) - sum(q5) / len(q5)) * 1e4
                                if q1 and q5 else 0.0)
            ranked = sorted(contrib, key=lambda s: -contrib[s] * sign)
            for label, excl in (("LOO", ranked[:1]), ("L2O", ranked[:2])):
                o2 = build_obs(candles, signals, hname, exclude=set(excl))
                print(f"   {label} -{excl}: {fmt(cstats(spread_series(o2)))}")
            best_m = max(months, key=lambda k: months[k] * sign)
            no_bm = [(ts, v) for ts, v in spread_series(obs) if month_key(ts) != best_m]
            print(f"   minus beste maand {best_m}: {fmt(cstats(no_bm))}")
            trend, vol = regime_labels(CACHE)
            for rn, lab in (("bull", trend), ("bear", trend),
                            ("high_vol", vol), ("low_vol", vol)):
                sub = [(ts, v) for ts, v in spread_series(obs)
                       if lab.get(ts // DAY_MS) == rn]
                print(f"   regime {rn:9}: {fmt(cstats(sub))}")
            o_delay = build_obs(candles, signals, hname, entry_delay=2)
            print(f"   entry +1 candle (30min): {fmt(cstats(spread_series(o_delay)))}")
            o_vol = build_obs(candles, signals, hname,
                              volume_filter=volume_top_half(candles))
            print(f"   top-helft volume: {fmt(cstats(spread_series(o_vol)))}")
            vals = sorted(v for _, v in spread_series(obs))
            k = int(len(vals) * 0.10)
            tm = sum(vals[k:len(vals) - k]) / max(1, len(vals) - 2 * k) * 1e4
            print(f"   trimmed10%: {tm:+.2f} bps")
            q = (END_MS - START_MS) // 4
            for i in range(4):
                sub = spread_series(obs, START_MS + i * q, START_MS + (i + 1) * q)
                print(f"   subperiode {i + 1}/4: {fmt(cstats(sub))}")
            stress_ok = any(
                r["legs"][q]["rep"] and abs(r["legs"][q]["rep"]["bps"]) >= COST_BPS * 1.5
                for q in ("Q1", "Q5"))
            print(f"   kostenstress x1.5 (21 bps poot, REP): {'PASS' if stress_ok else 'FAIL'}")

    input_hashes = {f"h4d3_{s}_15m.json": hashlib.sha256(
        (CACHE / f"h4d3_{s}_15m.json").read_bytes()).hexdigest() for s in SYMBOLS}
    payload = {"generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "protocol": "docs/RESEARCH_JOURNAL.md H-4D-3 (pre-registratie 2026-07-17)",
               "n_signals": n_sig, "input_sha256": input_hashes,
               "results": {h: {"dev": r["dev"], "rep": r["rep"], "bh_p": r.get("bh_p"),
                               "n_obs": r["n_obs"],
                               "legs": r["legs"]} for h, r in results.items()},
               "candidates": candidates, "verdicts": verdicts}
    out = OUT_DIR / "results.json"
    out.write_text(json.dumps(payload, indent=2))
    print(f"\nresultaten -> {out}")


if __name__ == "__main__":
    run()
