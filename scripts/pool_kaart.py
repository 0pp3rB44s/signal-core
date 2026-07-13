#!/usr/bin/env python3
"""Dagelijkse pool-kaart (fase 1, spoor A) — PLAN_VOORUIT.md.

Toont per coin de onaangeroerde liquiditeit-pools (buy-side boven, sell-side
onder) op 1H en 4H, met sterkte (touches) en afstand. Gebruik elke ochtend:

    python3 scripts/pool_kaart.py                 # standaard coins
    python3 scripts/pool_kaart.py SOLUSDT XRPUSDT # eigen keuze
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from clients.bitget_rest import BitgetRestClient
from market_data.liquidity_pools import detect_liquidity_pools

DEFAULT_SYMBOLS = ["SOLUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT"]
STRONG = 3  # touches vanaf hier = sterke pool


def fetch(client, product, symbol: str, granularity: str, limit: int) -> list[dict]:
    raw = client.get_candles(symbol=symbol, product_type=product, granularity=granularity, limit=limit).get("data") or []
    rows = [
        {"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4])}
        for r in raw
        if len(r) >= 5
    ]
    rows.sort(key=lambda x: x["t"])
    return rows


def kaart(symbol: str, client, product) -> None:
    print(f"\n{'=' * 62}\n  {symbol}\n{'=' * 62}")
    for tf, limit in (("4H", 200), ("1H", 300)):
        rows = fetch(client, product, symbol, tf, limit)
        if len(rows) < 50:
            print(f"  {tf}: onvoldoende data")
            continue
        pools = detect_liquidity_pools(rows)
        price = rows[-1]["c"]
        unswept = [p for p in pools if not p.swept]
        above = sorted((p for p in unswept if p.price > price), key=lambda p: p.price)[:4]
        below = sorted((p for p in unswept if p.price < price), key=lambda p: -p.price)[:4]
        print(f"\n  [{tf}]  prijs {price:.6g}")
        print("    buy-side (boven — targets voor up-move / sweep-zones):")
        for p in above:
            d = (p.price - price) / price * 100
            tag = "  <<< STERK" if p.touches >= STRONG else ""
            print(f"      {p.price:.6g}   +{d:.2f}%   x{p.touches}{tag}")
        if not above:
            print("      (geen onaangeroerde pools boven)")
        print("    sell-side (onder — targets voor down-move / sweep-zones):")
        for p in below:
            d = (p.price - price) / price * 100
            tag = "  <<< STERK" if p.touches >= STRONG else ""
            print(f"      {p.price:.6g}   {d:.2f}%   x{p.touches}{tag}")
        if not below:
            print("      (geen onaangeroerde pools onder)")


def main() -> None:
    symbols = [s.upper() for s in sys.argv[1:]] or DEFAULT_SYMBOLS
    settings = Settings()
    client = BitgetRestClient(settings=settings)
    product = settings.bitget_product_type
    print("POOL-KAART — onaangeroerde liquiditeit (x = aantal equal highs/lows)")
    print("A+ setup = sweep van een sterke pool + rejection; TP = tegenoverliggende pool.")
    for sym in symbols:
        try:
            kaart(sym, client, product)
        except Exception as exc:
            print(f"\n  {sym}: FOUT {exc}")
    print()


if __name__ == "__main__":
    main()
