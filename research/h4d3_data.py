#!/usr/bin/env python3
"""H-4D-3: databouw + kwaliteitsaudit, 15m OHLCV (pre-registratie in journal).

Zelfde universum en venster als H-4D-2 (vast, ex ante):
  12 symbolen | [2024-07-17T00:00Z, 2026-07-17T00:00Z) | 70.080 candles/symbool
Zelfde regels: UTC, geen forward-fill, invalide candles geteld en uitgesloten.

Cache:  data/historical/h4d3_{SYMBOL}_15m.json   (gitignored)
Audit:  reports/analysis/h4d3_vwap/data_audit.json (gitignored)
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings
from clients.bitget_rest import BitgetRestClient

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
           "LINKUSDT", "AVAXUSDT", "ADAUSDT", "SUIUSDT", "LTCUSDT", "DOTUSDT"]
START_MS = int(datetime(2024, 7, 17, tzinfo=timezone.utc).timestamp() * 1000)
END_MS = int(datetime(2026, 7, 17, tzinfo=timezone.utc).timestamp() * 1000)
STEP_MS = 900_000
EXPECTED = (END_MS - START_MS) // STEP_MS

CACHE_DIR = ROOT / "data" / "historical"
AUDIT_DIR = ROOT / "reports" / "analysis" / "h4d3_vwap"


def fetch_symbol(client, product: str, symbol: str) -> tuple[list[list[float]], int]:
    rows: dict[int, list[float]] = {}
    duplicates = 0
    end = END_MS
    for _ in range(1200):
        payload = client._request(
            "GET", "/api/v2/mix/market/history-candles",
            params={"symbol": symbol, "productType": product,
                    "granularity": "15m", "limit": "200", "endTime": str(end)},
        )
        data = payload.get("data") or []
        if not data:
            break
        oldest = None
        for r in data:
            ts = int(r[0])
            oldest = ts if oldest is None else min(oldest, ts)
            if ts < START_MS or ts >= END_MS:
                continue
            row = [ts, float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                   float(r[5]) if len(r) > 5 else 0.0,
                   float(r[6]) if len(r) > 6 else 0.0]
            if ts in rows:
                duplicates += 1
                continue
            rows[ts] = row
        if oldest is None or oldest <= START_MS or oldest >= end:
            break
        end = oldest
        time.sleep(0.05)
    return [rows[k] for k in sorted(rows)], duplicates


def validate(candles: list[list[float]]) -> tuple[list[list[float]], int]:
    valid, invalid = [], 0
    for c in candles:
        ts, o, h, l, cl, vb = c[0], c[1], c[2], c[3], c[4], c[5]
        ok = (ts % STEP_MS == 0
              and all(math.isfinite(x) and x > 0 for x in (o, h, l, cl))
              and math.isfinite(vb) and vb >= 0
              and h >= max(o, cl) and l <= min(o, cl))
        if ok:
            valid.append(c)
        else:
            invalid += 1
    return valid, invalid


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def run() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    settings = Settings()
    client = BitgetRestClient(settings=settings)
    product = settings.bitget_product_type

    audit = {"generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "exchange": "BITGET", "product_type": product, "granularity": "15m",
             "endpoint": "/api/v2/mix/market/history-candles",
             "window_utc": [iso(START_MS), iso(END_MS)],
             "expected_per_symbol": EXPECTED, "forward_fill": "NOOIT", "symbols": {}}

    print(f"H-4D-3 databouw 15m | {iso(START_MS)} -> {iso(END_MS)} | verwacht {EXPECTED}/symbool")
    for sym in SYMBOLS:
        raw, dups = fetch_symbol(client, product, sym)
        valid, invalid = validate(raw)
        ts_list = [int(c[0]) for c in valid]
        gap_count = sum(1 for a, b in zip(ts_list, ts_list[1:]) if b - a > STEP_MS)
        missing = EXPECTED - len(valid)
        path = CACHE_DIR / f"h4d3_{sym}_15m.json"
        path.write_text(json.dumps(
            {"symbol": sym, "exchange": "BITGET", "product_type": product,
             "granularity": "15m", "window_utc": [iso(START_MS), iso(END_MS)],
             "retrieved_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "candles": valid}, separators=(",", ":")))
        audit["symbols"][sym] = {
            "n_valid": len(valid), "n_invalid": invalid, "duplicates": dups,
            "missing_vs_expected": missing, "gap_count": gap_count,
            "first_utc": iso(ts_list[0]) if ts_list else None,
            "last_utc": iso(ts_list[-1]) if ts_list else None,
            "zero_volume_candles": sum(1 for c in valid if c[5] == 0),
            "cache_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        a = audit["symbols"][sym]
        print(f"  {sym:9} n={a['n_valid']:6} missing={missing:5} dup={dups:3} "
              f"invalid={invalid:3} gaps={gap_count:3} vol0={a['zero_volume_candles']:4}")

    out = AUDIT_DIR / "data_audit.json"
    out.write_text(json.dumps(audit, indent=2))
    print(f"\naudit -> {out}")


if __name__ == "__main__":
    run()
