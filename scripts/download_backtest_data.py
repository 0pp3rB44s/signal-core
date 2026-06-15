from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import List

import requests

# Ensure project root on path (so you can reuse schemas later if needed)
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


BASE_URL = "https://api.bitget.com"
KLINE_ENDPOINT = "/api/v2/mix/market/history-candles"

OUT_DIR = Path("data/backtests")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Symbols to download (USDT perpetuals)
SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "SUIUSDT",
    "WIFUSDT",
]

# Timeframe mapping (Bitget uses strings like 5m, 15m, 1H)
GRANULARITY = "15m"  # change to "5m" or "1H" if needed

# Number of candles to fetch (per symbol)
LIMIT = 200


def _fetch_candles(symbol: str, granularity: str, limit: int) -> List[dict]:
    params = {
        "symbol": symbol,
        "productType": "usdt-futures",
        "granularity": granularity,
        "limit": limit,
    }

    url = BASE_URL + KLINE_ENDPOINT
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()

    if data.get("code") != "00000":
        raise RuntimeError(f"Bitget error for {symbol}: {data}")

    rows = data.get("data", [])

    # Bitget returns: [timestamp, open, high, low, close, volume, quoteVolume]
    candles: List[dict] = []
    for row in rows:
        try:
            ts, o, h, l, c, v, qv = row
        except Exception:
            # Fallback if shape differs
            continue

        candles.append(
            {
                "timestamp": int(ts),
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume_base": float(v),
                "volume_quote": float(qv),
            }
        )

    # Oldest -> newest
    candles.sort(key=lambda x: x["timestamp"])
    return candles


def save_symbol(symbol: str) -> None:
    print(f"Downloading {symbol} ({GRANULARITY})...")
    candles = _fetch_candles(symbol, GRANULARITY, LIMIT)

    if len(candles) < 200:
        print(f"[WARN] Too few candles for {symbol}: {len(candles)}")

    out_path = OUT_DIR / f"{symbol}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(candles, f)

    print(f"Saved {symbol}: {len(candles)} candles -> {out_path}")


def main() -> None:
    start = time.time()

    for sym in SYMBOLS:
        try:
            save_symbol(sym)
            time.sleep(0.3)  # light rate limit
        except Exception as e:
            print(f"[ERROR] {sym}: {e}")

    dur = time.time() - start
    print(f"Done in {dur:.2f}s")
    print(f"Files in: {OUT_DIR.resolve()}")


if __name__ == "__main__":
    main()