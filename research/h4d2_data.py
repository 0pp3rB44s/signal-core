#!/usr/bin/env python3
"""H-4D-2: databouw + kwaliteitsaudit (pre-registratie: docs/RESEARCH_JOURNAL.md).

Vast universum (12 symbolen, vooraf geregistreerd), 1H OHLCV, Bitget
USDT-FUTURES, public API /api/v2/mix/market/history-candles (gepagineerd).

Periode VAST (ex ante, voor enige testrun):
  [2024-07-17T00:00Z, 2026-07-17T00:00Z)  = 730 dagen = 17.520 uurcandles/symbool
  DEV = [2024-07-17, 2025-07-17)  |  REP = [2025-07-17, 2026-07-17)

Regels: UTC overal; GEEN forward-fill van prijzen; ontbrekende candles blijven
ontbreken en worden gerapporteerd; ongeldige candles worden geteld en
uitgesloten, nooit gerepareerd.

Cache:  data/historical/h4d2_{SYMBOL}_1H.json   (gitignored)
Audit:  reports/analysis/h4d2_time_of_day/data_audit.json (gitignored)
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
HOUR_MS = 3_600_000
EXPECTED = (END_MS - START_MS) // HOUR_MS

CACHE_DIR = ROOT / "data" / "historical"
AUDIT_DIR = ROOT / "reports" / "analysis" / "h4d2_time_of_day"


def fetch_symbol(client, product: str, symbol: str) -> tuple[list[list[float]], int]:
    """Pagineer terug vanaf END_MS; ruwe rijen [ts,o,h,l,c,vBase,vQuote]."""
    rows: dict[int, list[float]] = {}
    duplicates = 0
    end = END_MS
    for _ in range(400):  # harde bovengrens ~80k candles
        payload = client._request(
            "GET", "/api/v2/mix/market/history-candles",
            params={"symbol": symbol, "productType": product,
                    "granularity": "1H", "limit": "200", "endTime": str(end)},
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
        if oldest is None or oldest <= START_MS:
            break
        if oldest >= end:
            break  # geen voortgang -> stop (voorkomt oneindige lus)
        end = oldest
        time.sleep(0.06)
    return [rows[k] for k in sorted(rows)], duplicates


def validate(candles: list[list[float]]) -> tuple[list[list[float]], list[dict]]:
    valid, invalid = [], []
    for c in candles:
        ts, o, h, l, cl = c[0], c[1], c[2], c[3], c[4]
        ok = (ts % HOUR_MS == 0
              and all(math.isfinite(x) and x > 0 for x in (o, h, l, cl))
              and h >= max(o, cl) and l <= min(o, cl))
        (valid if ok else invalid).append(c if ok else {"ts": ts, "row": c})
    return valid, invalid


def gaps(timestamps: list[int]) -> list[dict]:
    out = []
    for a, b in zip(timestamps, timestamps[1:]):
        if b - a > HOUR_MS:
            out.append({"after_utc": iso(a), "before_utc": iso(b),
                        "missing": (b - a) // HOUR_MS - 1})
    return out


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    settings = Settings()
    client = BitgetRestClient(settings=settings)
    product = settings.bitget_product_type

    contracts_meta = {}
    try:
        payload = client.get_contracts(product_type=product)
        for row in payload.get("data") or []:
            sym = str(row.get("symbol", "")).upper()
            if sym in SYMBOLS:
                contracts_meta[sym] = {k: row.get(k) for k in
                                       ("symbol", "baseCoin", "quoteCoin", "symbolType",
                                        "launchTime", "deliveryMode", "symbolStatus")}
    except Exception as exc:
        print(f"  contracts-meta niet opgehaald: {exc}")

    audit = {"generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "exchange": "BITGET", "product_type": product, "granularity": "1H",
             "endpoint": "/api/v2/mix/market/history-candles",
             "window_utc": [iso(START_MS), iso(END_MS)], "expected_per_symbol": EXPECTED,
             "timezone": "UTC (epoch-ms candle-open)", "forward_fill": "NOOIT",
             "contracts": contracts_meta, "symbols": {}}

    print(f"H-4D-2 databouw | {iso(START_MS)} -> {iso(END_MS)} | verwacht {EXPECTED}/symbool")
    for sym in SYMBOLS:
        raw, dups = fetch_symbol(client, product, sym)
        valid, invalid = validate(raw)
        ts_list = [int(c[0]) for c in valid]
        g = gaps(ts_list)
        missing = EXPECTED - len(valid)
        path = CACHE_DIR / f"h4d2_{sym}_1H.json"
        path.write_text(json.dumps(
            {"symbol": sym, "exchange": "BITGET", "product_type": product,
             "granularity": "1H", "endpoint": "/api/v2/mix/market/history-candles",
             "retrieved_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "window_utc": [iso(START_MS), iso(END_MS)],
             "candles": valid}, separators=(",", ":")))
        audit["symbols"][sym] = {
            "n_valid": len(valid), "n_invalid": len(invalid), "duplicates": dups,
            "missing_vs_expected": missing,
            "first_utc": iso(ts_list[0]) if ts_list else None,
            "last_utc": iso(ts_list[-1]) if ts_list else None,
            "gap_count": len(g), "largest_gaps": sorted(g, key=lambda x: -x["missing"])[:10],
            "cache_sha256": sha256(path),
        }
        print(f"  {sym:9} n={len(valid):5} missing={missing:4} dup={dups:3} "
              f"invalid={len(invalid):2} gaps={len(g):3} "
              f"[{audit['symbols'][sym]['first_utc']} .. {audit['symbols'][sym]['last_utc']}]")

    out = AUDIT_DIR / "data_audit.json"
    out.write_text(json.dumps(audit, indent=2))
    print(f"\naudit -> {out}")


if __name__ == "__main__":
    run()
