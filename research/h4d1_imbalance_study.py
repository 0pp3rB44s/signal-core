#!/usr/bin/env python3
"""H-4D-1: orderbook-imbalance -> forward return (event study).

Protocol en succescriteria: docs/RESEARCH_JOURNAL.md (pre-registratie
2026-07-16, VOOR enige test). Dit script voert exact dat protocol uit.

- Signaal: orderbook_imbalance uit market_context.csv(.N) snapshots.
- Forward returns: verse 15m-candles; entry = OPEN van de eerste candle die
  volledig NA het snapshot opent (geen look-ahead).
- DEV: 2026-07-08..11 | REP: 2026-07-12..16. Kwintielen op DEV-verdeling.
- Primair: Q5-Q1 spread @15m/1h/4h, cluster-robuust per timestamp-bucket,
  BH-correctie over 3 tests.
"""
from __future__ import annotations

import csv
import glob
import math
import sys
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings
from clients.bitget_rest import BitgetRestClient

DEV_END = datetime(2026, 7, 12, tzinfo=timezone.utc)  # DEV < 07-12, REP >= 07-12
HORIZONS = {"15m": 1, "1h": 4, "4h": 16}  # in 15m-candles
CAND_LIMIT = 1000

IMB_RE = __import__("re").compile(r"orderbook_imbalance=([+-]?\d+(?:\.\d+)?)")
SPR_RE = __import__("re").compile(r"spread_bps=([+-]?\d+(?:\.\d+)?)")

def load_snapshots() -> list[dict]:
    """Amendement 2026-07-16 (voor enige testrun): market_context.csv bleek de
    orderbook-kolommen nooit te vullen (0/79.719). Zelfde metingen staan als
    tekst in de notes van strategy_performance.csv-scanrijen; zelfde bron
    (top-50 merge-depth via bitget_market_client), zelfde cadans. Dedupe op
    (symbool, seconde)."""
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    files = sorted(glob.glob(str(ROOT / "logs" / "strategy_performance.csv*")))
    for f in files:
        try:
            with open(f, newline="") as fh:
                for r in csv.DictReader(fh):
                    ts, sym = r.get("timestamp"), r.get("symbol")
                    if not ts or not sym:
                        continue
                    blob = (r.get("notes") or "") + "|" + (r.get("reasons") or "")
                    m = IMB_RE.search(blob)
                    if not m:
                        continue
                    key = (sym, ts[:19])
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        t = datetime.fromisoformat(ts)
                        v = float(m.group(1))
                    except ValueError:
                        continue
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=timezone.utc)
                    sp = SPR_RE.search(blob)
                    rows.append({"t": t, "sym": sym.upper(), "imb": v,
                                 "spread": float(sp.group(1)) if sp else 0.0})
        except Exception as exc:
            print(f"  skip {f}: {exc}")
    rows.sort(key=lambda x: (x["sym"], x["t"]))
    return rows

def fetch_candles(client, product, symbol: str) -> tuple[list[int], list[dict]]:
    raw = client.get_candles(symbol=symbol, product_type=product,
                             granularity="15m", limit=CAND_LIMIT).get("data") or []
    cs = sorted(
        ({"t": int(r[0]), "o": float(r[1]), "c": float(r[4])} for r in raw if len(r) >= 5),
        key=lambda x: x["t"],
    )
    return [c["t"] for c in cs], cs

def thin(snaps: list[dict], minutes: int) -> list[dict]:
    """Max 1 snapshot per symbool per horizon-venster (non-overlapping)."""
    out, last = [], {}
    for s in snaps:
        k = s["sym"]
        if k not in last or (s["t"] - last[k]).total_seconds() >= minutes * 60:
            out.append(s)
            last[k] = s["t"]
    return out

def mean(xs): return sum(xs) / len(xs) if xs else 0.0

def cluster_t(spreads_per_bucket: list[float]) -> tuple[float, float, float]:
    """t-stat van gemiddelde per-tijdsbucket Q5-Q1 spread (cluster-robuust)."""
    n = len(spreads_per_bucket)
    if n < 5:
        return 0.0, 0.0, 1.0
    m = mean(spreads_per_bucket)
    var = sum((x - m) ** 2 for x in spreads_per_bucket) / (n - 1)
    se = math.sqrt(var / n) if var > 0 else 1e-12
    t = m / se
    # tweezijdige p via normale benadering (n>100 in praktijk)
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2))))
    return m, t, p

def run() -> None:
    print("H-4D-1 — snapshots laden...")
    snaps = load_snapshots()
    print(f"  snapshots: {len(snaps)} | symbolen: {len({s['sym'] for s in snaps})}"
          f" | {min(s['t'] for s in snaps):%m-%d %H:%M} -> {max(s['t'] for s in snaps):%m-%d %H:%M}")

    settings = Settings()
    client = BitgetRestClient(settings=settings)
    product = settings.bitget_product_type
    symbols = sorted({s["sym"] for s in snaps})
    candles: dict[str, tuple[list[int], list[dict]]] = {}
    print(f"  15m-candles ophalen voor {len(symbols)} symbolen...")
    for sym in symbols:
        try:
            candles[sym] = fetch_candles(client, product, sym)
        except Exception:
            candles[sym] = ([], [])

    results = {}
    for hname, nfwd in HORIZONS.items():
        thinned = thin(snaps, {"15m": 15, "1h": 60, "4h": 240}[hname])
        obs = []
        for s in thinned:
            ts_ms = int(s["t"].timestamp() * 1000)
            times, cs = candles.get(s["sym"], ([], []))
            i = bisect_right(times, ts_ms)  # eerste candle die NA snapshot opent
            if i + nfwd >= len(cs):
                continue  # horizon valt buiten data (of candle-historie dekt snapshot niet)
            entry = cs[i]["o"]
            exit_ = cs[i + nfwd - 1]["c"]
            if entry <= 0:
                continue
            obs.append({"t": s["t"], "sym": s["sym"], "imb": s["imb"],
                        "spread": s["spread"], "ret": math.log(exit_ / entry)})
        dev = [o for o in obs if o["t"] < DEV_END]
        rep = [o for o in obs if o["t"] >= DEV_END]

        # kwintielgrenzen op DEV
        if len(dev) < 500:
            results[hname] = {"error": f"te weinig DEV-obs ({len(dev)})", "n_dev": len(dev), "n_rep": len(rep)}
            continue
        vals = sorted(o["imb"] for o in dev)
        q = lambda p: vals[int(p * (len(vals) - 1))]
        lo, hi = q(0.2), q(0.8)

        def spread_series(rows):
            buckets = defaultdict(lambda: {"q5": [], "q1": []})
            for o in rows:
                b = o["t"].strftime("%m-%d %H") + f":{(o['t'].minute // 15) * 15:02d}"
                if o["imb"] >= hi:
                    buckets[b]["q5"].append(o["ret"])
                elif o["imb"] <= lo:
                    buckets[b]["q1"].append(o["ret"])
            return [mean(v["q5"]) - mean(v["q1"]) for v in buckets.values()
                    if v["q5"] and v["q1"]]

        out = {}
        for label, rows in (("dev", dev), ("rep", rep)):
            sp = spread_series(rows)
            m, t, p = cluster_t(sp)
            out[label] = {"n_obs": len(rows), "n_clusters": len(sp),
                          "spread_bps": m * 1e4, "t": t, "p": p}
        results[hname] = out

    # Benjamini-Hochberg over de 3 primaire DEV-tests
    prim = [(h, results[h]["dev"]["p"]) for h in HORIZONS if "dev" in results.get(h, {})]
    prim.sort(key=lambda x: x[1])
    m = len(prim)
    bh = {}
    for rank, (h, p) in enumerate(prim, 1):
        bh[h] = min(1.0, p * m / rank)

    print("\n=== RESULTATEN (Q5-Q1 spread, bps, cluster-robuust) ===")
    print(f"{'horizon':8} {'DEV n/cl':>12} {'DEV bps':>9} {'t':>6} {'p(BH)':>8} | {'REP n/cl':>12} {'REP bps':>9} {'t':>6}")
    for h in HORIZONS:
        r = results.get(h, {})
        if "error" in r:
            print(f"{h:8} {r['error']}")
            continue
        d, rp = r["dev"], r["rep"]
        print(f"{h:8} {d['n_obs']:>6}/{d['n_clusters']:<5} {d['spread_bps']:>9.2f} {d['t']:>6.2f} "
              f"{bh.get(h, 1):>8.4f} | {rp['n_obs']:>6}/{rp['n_clusters']:<5} {rp['spread_bps']:>9.2f} {rp['t']:>6.2f}")

    print("\nSuccescriteria (pre-registratie): BH-p<0.05 in DEV; zelfde teken + |t|>=2 in REP;")
    print(">=15bps @1h of >=8bps @15m. Anders: VERWERPEN.")

if __name__ == "__main__":
    run()
