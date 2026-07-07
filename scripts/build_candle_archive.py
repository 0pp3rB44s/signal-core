"""Bouwt/onderhoudt het historische candle-archief voor de validatie-motor.

Gebruik:  .venv/bin/python scripts/build_candle_archive.py [--days 90]

Schrijft data/history/{SYMBOL}_15m.json (list van candle-dicts, oplopend op
timestamp). Hervatbaar: bestaande bestanden worden aangevuld, niet opnieuw
gedownload. Publieke Bitget endpoints, ~0.12s tussen requests.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "history"

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT",
    "DOGEUSDT", "XLMUSDT", "BNBUSDT", "TRXUSDT", "FILUSDT", "UNIUSDT",
]

GRANULARITY = "15m"
MS_PER_CANDLE = 15 * 60 * 1000


def fetch_page(symbol: str, end_ms: int | None = None) -> list[dict]:
    params = {
        "symbol": symbol,
        "productType": "usdt-futures",
        "granularity": GRANULARITY,
        "limit": "200",
    }
    if end_ms:
        params["endTime"] = str(end_ms)
    response = requests.get(
        "https://api.bitget.com/api/v2/mix/market/history-candles",
        params=params,
        timeout=15,
    )
    rows = response.json().get("data") or []
    return [
        {
            "timestamp": int(r[0]),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume_base": float(r[5]),
        }
        for r in rows
    ]


def build_symbol(symbol: str, target_candles: int) -> int:
    path = OUT_DIR / f"{symbol}_{GRANULARITY}.json"
    existing: dict[int, dict] = {}
    if path.exists():
        try:
            for c in json.loads(path.read_text()):
                existing[int(c["timestamp"])] = c
        except Exception:
            existing = {}

    # nieuwer dan wat we hebben (vult bij tot nu)
    end: int | None = None
    for _ in range(3):
        rows = fetch_page(symbol, end)
        if not rows:
            break
        new = [r for r in rows if r["timestamp"] not in existing]
        for r in rows:
            existing[r["timestamp"]] = r
        if not new:
            break
        end = min(r["timestamp"] for r in rows) - 1
        time.sleep(0.12)

    # ouder dan wat we hebben (tot target bereikt)
    while len(existing) < target_candles:
        oldest = min(existing) if existing else None
        rows = fetch_page(symbol, oldest - 1 if oldest else None)
        if not rows:
            break
        added = 0
        for r in rows:
            if r["timestamp"] not in existing:
                existing[r["timestamp"]] = r
                added += 1
        if added == 0:
            break
        time.sleep(0.12)

    candles = sorted(existing.values(), key=lambda c: c["timestamp"])
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(candles))
    return len(candles)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    args = parser.parse_args()
    target = args.days * 24 * 4  # 15m candles per dag

    for symbol in SYMBOLS:
        try:
            count = build_symbol(symbol, target)
            days = count / 96
            print(f"{symbol}: {count} candles (~{days:.0f} dagen)")
        except Exception as exc:
            print(f"{symbol}: FAILED ({exc})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
