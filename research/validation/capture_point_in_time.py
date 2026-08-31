"""Append a hash-chained public Bitget universe snapshot after the spec freeze.

This creates prospective membership evidence only. It never backfills, changes
specs, accesses credentials, or sends orders.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
SPEC = HERE / "FROZEN_SPECS.json"
LEDGER = HERE / "prospective/universe_snapshots.jsonl"
OBSERVATIONS = HERE / "prospective/observations.jsonl"
BASE = "https://api.bitget.com"


def public_get(path: str, params: dict) -> list:
    response = requests.get(BASE + path, params=params, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != "00000":
        raise RuntimeError(str(payload)[:300])
    return payload["data"]


def previous_hash() -> str | None:
    if not LEDGER.exists():
        return None
    lines = [line for line in LEDGER.read_text().splitlines() if line.strip()]
    return json.loads(lines[-1])["record_sha256"] if lines else None


def append_record(record: dict) -> None:
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
    record["record_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as handle:
        handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    OBSERVATIONS.touch(exist_ok=True)


def main() -> None:
    contracts = public_get("/api/v2/mix/market/contracts", {"productType": "USDT-FUTURES"})
    tickers = public_get("/api/v2/mix/market/tickers", {"productType": "USDT-FUTURES"})
    ticker_by_symbol = {row["symbol"]: row for row in tickers}
    markets = []
    for contract in contracts:
        symbol = contract["symbol"]
        ticker = ticker_by_symbol.get(symbol, {})
        bid, ask = float(ticker.get("bidPr") or 0), float(ticker.get("askPr") or 0)
        spread = ((ask - bid) / ((ask + bid) / 2) * 1e4) if bid > 0 and ask >= bid else None
        markets.append({
            "symbol": symbol, "status": contract.get("symbolStatus"),
            "launch_time_raw": contract.get("launchTime"), "off_time_raw": contract.get("offTime"),
            "min_trade_usdt": contract.get("minTradeUSDT"), "taker_fee_rate": contract.get("takerFeeRate"),
            "turnover_24h": ticker.get("usdtVolume"), "funding_rate": ticker.get("fundingRate"),
            "bid": ticker.get("bidPr"), "ask": ticker.get("askPr"), "spread_bps": spread,
        })
    spec_sha = hashlib.sha256(SPEC.read_bytes()).hexdigest()
    record = {
        "schema": "cgc_point_in_time_universe_v1", "captured_utc": datetime.now(timezone.utc).isoformat(),
        "spec_sha256": spec_sha, "previous_record_sha256": previous_hash(),
        "market_count": len(markets), "markets": sorted(markets, key=lambda row: row["symbol"]),
        "public_data_only": True, "orders_allowed": False,
    }
    append_record(record)
    print(json.dumps({"captured_utc": record["captured_utc"], "market_count": len(markets),
                      "spec_sha256": spec_sha, "record_sha256": record["record_sha256"]}, indent=2))


if __name__ == "__main__":
    main()
