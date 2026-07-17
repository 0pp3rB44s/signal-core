#!/usr/bin/env python3
"""H-4D-2: time-of-day / sessie-structuur (pre-geregistreerde event study).

Protocol: docs/RESEARCH_JOURNAL.md H-4D-2 (GEREGISTREERD 2026-07-16) + het
interpretatie-amendement 2026-07-17 (vastgelegd en gecommit VOOR enige testrun):

- Return: log(close/open) van de 1H-candle die op uur h opent (entry = open van
  uur h, exit = close van uur h). Cross-sectioneel gelijkgewogen gemiddelde per
  timestamp; timestamp telt alleen mee bij >= 8/12 symbolen; geen forward-fill.
- 30 primaire tests = 24 UTC-uren + 6 vensters (elk 2 candle-open-uren):
    asia_open {0,1} | eu_open_funding08 {7,8} | us_open {13,14}
    us_close {20,21} | funding_00 {23,0} | funding_16 {15,16}
  Pre-registratie telt 24+6=30: EU-open (07-09) en funding-08 (+-1h rond 08)
  vallen samen en tellen als EEN venster.
- DEV = [2024-07-17, 2025-07-17) | REP = [2025-07-17, 2026-07-17) (kalender).
- Cluster = UTC-dag van candle-open; CR-SE; tweezijdige p (normale benadering,
  >=300 clusters); BH step-up (monotoon) over de 30 DEV-tests.
- Poorten (alle verplicht): (1) BH-p<0.05 in DEV; (2) zelfde teken in REP;
  (3) |t|>=2 in REP; (4) min(|DEV|,|REP|) >= 4 bps/uur; (5) verhandelbare
  constructie boven kosten: som uur-effecten per trade > 14 bps roundtrip
  (12 taker + 2 slippage); (6) maand-tekenconsistentie >= 65% (volle periode).
- Falsificatie (alleen verwerpen, nooit redden): LOO/L2O-symbolen, beste maand
  eruit, regimes (bull/bear via BTC-90d, hoog/laag-vol via mediaan-split),
  +-3h placebo, 1h-vertraagde entry, kostenstress (x1.5), 10%-trimmed mean,
  subperiode-verval (4 kwartalen van de sample), DST-split voor US-vensters.
  NB (ex ante): voor klok-effecten is het signaal oneindig ver vooraf bekend;
  de 1h-vertraagde test meet randscherpte/placebo, geen signaallatentie.
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

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
           "LINKUSDT", "AVAXUSDT", "ADAUSDT", "SUIUSDT", "LTCUSDT", "DOTUSDT"]
START_MS = int(datetime(2024, 7, 17, tzinfo=timezone.utc).timestamp() * 1000)
SPLIT_MS = int(datetime(2025, 7, 17, tzinfo=timezone.utc).timestamp() * 1000)
END_MS = int(datetime(2026, 7, 17, tzinfo=timezone.utc).timestamp() * 1000)
HOUR_MS, DAY_MS = 3_600_000, 86_400_000
MIN_SYMBOLS = 8
COST_BPS = 14.0            # 12 taker roundtrip + 2 slippage (pre-registratie)
ECON_BPS_PER_HOUR = 4.0
MONTH_CONSISTENCY = 0.65

CACHE = ROOT / "data" / "historical"
OUT_DIR = ROOT / "reports" / "analysis" / "h4d2_time_of_day"

WINDOWS = {"asia_open": (0, 1), "eu_open_funding08": (7, 8), "us_open": (13, 14),
           "us_close": (20, 21), "funding_00": (23, 0), "funding_16": (15, 16)}
TESTS: dict[str, tuple[int, ...]] = {f"h{h:02d}": (h,) for h in range(24)} | WINDOWS

# US DST (tweede zondag maart -> eerste zondag november), datumgrenzen UTC
DST = [("2024-03-10", "2024-11-03"), ("2025-03-09", "2025-11-02"),
       ("2026-03-08", "2026-11-01")]


def load_returns() -> dict[str, dict[int, float]]:
    data = {}
    for sym in SYMBOLS:
        path = CACHE / f"h4d2_{sym}_1H.json"
        payload = json.loads(path.read_text())
        data[sym] = {int(c[0]): math.log(c[4] / c[1]) for c in payload["candles"]}
    return data


def cross_section(data: dict[str, dict[int, float]],
                  exclude: set[str] = frozenset()) -> list[tuple[int, float]]:
    """Per-timestamp gelijkgewogen gemiddelde; alleen bij >= MIN_SYMBOLS aanwezig."""
    active = [s for s in SYMBOLS if s not in exclude]
    out = []
    for ts in range(START_MS, END_MS, HOUR_MS):
        vals = [data[s][ts] for s in active if ts in data[s]]
        if len(vals) >= min(MIN_SYMBOLS, len(active) - 1):
            out.append((ts, sum(vals) / len(vals)))
    return out


def hour_of(ts: int) -> int:
    return (ts // HOUR_MS) % 24


def in_test(ts: int, hours: tuple[int, ...]) -> bool:
    return hour_of(ts) in hours


def cstats(rows: list[tuple[int, float]]) -> dict | None:
    """Gemiddelde met dag-geclusterde (CR0) SE; tweezijdige normale p."""
    n = len(rows)
    if n < 30:
        return None
    m = sum(r for _, r in rows) / n
    clusters: dict[int, list[float]] = defaultdict(list)
    for ts, r in rows:
        clusters[ts // DAY_MS].append(r)
    var = sum((sum(v) - len(v) * m) ** 2 for v in clusters.values()) / n ** 2
    se = math.sqrt(var) if var > 0 else 1e-12
    t = m / se
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2))))
    return {"n": n, "clusters": len(clusters), "bps": m * 1e4, "t": t, "p": p}


def bh_adjust(pvals: dict[str, float]) -> dict[str, float]:
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m = len(items)
    adj = [p * m / (i + 1) for i, (_, p) in enumerate(items)]
    for i in range(m - 2, -1, -1):
        adj[i] = min(adj[i], adj[i + 1])
    return {k: min(1.0, a) for (k, _), a in zip(items, adj)}


def month_key(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m")


def monthly_consistency(rows: list[tuple[int, float]], sign: float) -> tuple[float, dict]:
    months: dict[str, list[float]] = defaultdict(list)
    for ts, r in rows:
        months[month_key(ts)].append(r)
    means = {k: sum(v) / len(v) for k, v in sorted(months.items())}
    agree = sum(1 for v in means.values() if v * sign > 0)
    return agree / len(means) if means else 0.0, means


def trimmed_mean_bps(rows: list[tuple[int, float]], trim: float = 0.10) -> float:
    vals = sorted(r for _, r in rows)
    k = int(len(vals) * trim)
    core = vals[k:len(vals) - k] if len(vals) > 2 * k else vals
    return (sum(core) / len(core)) * 1e4 if core else 0.0


def select(rows, hours, lo=START_MS, hi=END_MS):
    return [(ts, r) for ts, r in rows if lo <= ts < hi and in_test(ts, hours)]


def per_symbol_contribution(data, hours, lo=START_MS, hi=END_MS) -> dict[str, float]:
    out = {}
    for sym in SYMBOLS:
        vals = [r for ts, r in data[sym].items()
                if lo <= ts < hi and in_test(ts, hours)]
        out[sym] = (sum(vals) / len(vals)) * 1e4 if vals else 0.0
    return out


def regime_labels(cache_dir: Path) -> tuple[dict[int, str], dict[int, str]]:
    """Trend (bull/bear) via BTC 90d; vol (high/low) via mediaan van 30d realized
    vol van BTC-dagreturns. Dagen zonder lookback: geen label."""
    payload = json.loads((cache_dir / "h4d2_BTCUSDT_1H.json").read_text())
    day_close: dict[int, float] = {}
    for c in payload["candles"]:
        day_close[int(c[0]) // DAY_MS] = c[4]
    days = sorted(day_close)
    trend, vol_raw = {}, {}
    for i, d in enumerate(days):
        if i >= 90:
            trend[d] = "bull" if day_close[d] > day_close[days[i - 90]] else "bear"
        if i >= 30:
            rets = [math.log(day_close[days[j]] / day_close[days[j - 1]])
                    for j in range(i - 29, i + 1)]
            mu = sum(rets) / len(rets)
            vol_raw[d] = math.sqrt(sum((x - mu) ** 2 for x in rets) / (len(rets) - 1))
    med = sorted(vol_raw.values())[len(vol_raw) // 2] if vol_raw else 0.0
    vol = {d: ("high_vol" if v >= med else "low_vol") for d, v in vol_raw.items()}
    return trend, vol


def dst_flag(ts: int) -> bool:
    d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    return any(a <= d < b for a, b in DST)


def fmt(s: dict | None) -> str:
    if not s:
        return "n<30"
    return f"{s['bps']:+7.2f} bps t={s['t']:+6.2f} (n={s['n']}, cl={s['clusters']})"


def run() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    data = load_returns()
    rows = cross_section(data)
    input_hashes = {f"h4d2_{s}_1H.json": hashlib.sha256(
        (CACHE / f"h4d2_{s}_1H.json").read_bytes()).hexdigest() for s in SYMBOLS}

    n_dev = sum(1 for ts, _ in rows if ts < SPLIT_MS)
    print(f"H-4D-2 | cross-sectie timestamps: {len(rows)} "
          f"(DEV {n_dev} / REP {len(rows) - n_dev}) | eis >=8/12 symbolen")

    results: dict[str, dict] = {}
    for name, hours in TESTS.items():
        dev = cstats(select(rows, hours, START_MS, SPLIT_MS))
        rep = cstats(select(rows, hours, SPLIT_MS, END_MS))
        results[name] = {"hours": hours, "dev": dev, "rep": rep}

    bh = bh_adjust({k: v["dev"]["p"] for k, v in results.items() if v["dev"]})
    for k in results:
        results[k]["bh_p"] = bh.get(k, 1.0)

    print("\n=== 30 PRIMAIRE TESTS (bps per uur, dag-geclusterd) ===")
    print(f"{'test':18} {'DEV bps':>8} {'t':>6} {'BH-p':>7} | {'REP bps':>8} {'t':>6} | teken")
    for name, r in results.items():
        d, p = r["dev"], r["rep"]
        if not d or not p:
            print(f"{name:18} onvoldoende data")
            continue
        same = "JA" if d["bps"] * p["bps"] > 0 else "nee"
        star = " *" if r["bh_p"] < 0.05 else ""
        print(f"{name:18} {d['bps']:>+8.2f} {d['t']:>+6.2f} {r['bh_p']:>7.4f} | "
              f"{p['bps']:>+8.2f} {p['t']:>+6.2f} | {same}{star}")

    # Poorten per kandidaat (BH<0.05 in DEV)
    candidates = [k for k, r in results.items()
                  if r["dev"] and r["rep"] and r["bh_p"] < 0.05]
    verdicts = {}
    print(f"\n=== KANDIDATEN (BH-p<0.05 in DEV): {candidates or 'GEEN'} ===")
    for name in candidates:
        r = results[name]
        d, p = r["dev"], r["rep"]
        hours = r["hours"]
        full = cstats(select(rows, hours))
        sign = 1.0 if full["bps"] > 0 else -1.0
        cons, months = monthly_consistency(select(rows, hours), sign)
        per_trade = full["bps"] * len(hours)
        gates = {
            "G1_dev_bh": r["bh_p"] < 0.05,
            "G2_rep_sign": d["bps"] * p["bps"] > 0,
            "G3_rep_t": abs(p["t"]) >= 2.0,
            "G4_econ_4bps": min(abs(d["bps"]), abs(p["bps"])) >= ECON_BPS_PER_HOUR,
            "G5_boven_kosten": abs(per_trade) > COST_BPS,
            "G6_maand_65pct": cons >= MONTH_CONSISTENCY,
        }
        verdicts[name] = {"gates": gates, "monthly_consistency": round(cons, 3),
                          "per_trade_bps": round(per_trade, 2)}
        print(f"\n-- {name} {hours} | full {fmt(full)} | per-trade {per_trade:+.1f} bps"
              f" | maandconsistentie {cons:.0%}")
        for g, ok in gates.items():
            print(f"   {g:16} {'PASS' if ok else 'FAIL'}")

        if all(gates.values()):
            print(f"\n   FALSIFICATIE {name}:")
            contrib = per_symbol_contribution(data, hours)
            ranked = sorted(contrib, key=lambda s: -contrib[s] * sign)
            for label, excl in (("LOO", ranked[:1]), ("L2O", ranked[:2])):
                sub = cstats(select(cross_section(data, exclude=set(excl)), hours))
                print(f"   {label} -{excl}: {fmt(sub)}")
            _, mm = monthly_consistency(select(rows, hours), sign)
            best_m = max(mm, key=lambda k: mm[k] * sign)
            no_bm = [(ts, r2) for ts, r2 in select(rows, hours)
                     if month_key(ts) != best_m]
            print(f"   minus beste maand {best_m}: {fmt(cstats(no_bm))}")
            trend, vol = regime_labels(CACHE)
            for rn, lab in (("bull", trend), ("bear", trend),
                            ("high_vol", vol), ("low_vol", vol)):
                sub = [(ts, r2) for ts, r2 in select(rows, hours)
                       if lab.get(ts // DAY_MS) == rn]
                print(f"   regime {rn:9}: {fmt(cstats(sub))}")
            for shift in (-3, 3, 1):
                sh = tuple((h + shift) % 24 for h in hours)
                lbl = "delay+1h" if shift == 1 else f"placebo{shift:+d}h"
                print(f"   {lbl} {sh}: {fmt(cstats(select(rows, sh)))}")
            print(f"   trimmed10% full: {trimmed_mean_bps(select(rows, hours)):+.2f} bps")
            q = (END_MS - START_MS) // 4
            for i in range(4):
                sub = select(rows, hours, START_MS + i * q, START_MS + (i + 1) * q)
                print(f"   subperiode {i + 1}/4: {fmt(cstats(sub))}")
            if name in ("us_open", "us_close") or set(hours) & {13, 14, 20, 21}:
                for flag, lbl in ((True, "DST"), (False, "geen-DST")):
                    sub = [(ts, r2) for ts, r2 in select(rows, hours)
                           if dst_flag(ts) is flag]
                    print(f"   {lbl:9}: {fmt(cstats(sub))}")
            stress = abs(per_trade) > COST_BPS * 1.5
            print(f"   kostenstress x1.5 ({COST_BPS * 1.5:.0f} bps): "
                  f"{'PASS' if stress else 'FAIL'}")

    payload = {"generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "protocol": "docs/RESEARCH_JOURNAL.md H-4D-2 + amendement 2026-07-17",
               "window_utc": [START_MS, SPLIT_MS, END_MS],
               "min_symbols": MIN_SYMBOLS, "n_timestamps": len(rows),
               "input_sha256": input_hashes,
               "results": {k: {"hours": list(v["hours"]),
                               "dev": v["dev"], "rep": v["rep"], "bh_p": v["bh_p"]}
                           for k, v in results.items()},
               "candidates": candidates, "verdicts": verdicts}
    out = OUT_DIR / "results.json"
    out.write_text(json.dumps(payload, indent=2))
    print(f"\nresultaten -> {out}")


if __name__ == "__main__":
    run()
