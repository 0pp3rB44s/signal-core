from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import requests

from historical_data.bitget_archive import atomic_write, stable_json_bytes
from scripts.acquire_bitget_history import SYMBOLS

ENDPOINT = "https://api.bitget.com/api/v2/mix/market/contracts"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite availability metadata: {args.output}")
    response = requests.get(ENDPOINT, params={"productType": "USDT-FUTURES"}, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if str(payload.get("code")) != "00000":
        raise SystemExit(f"Bitget public error: {payload.get('code')} {payload.get('msg')}")
    contracts = {row["symbol"]: row for row in payload.get("data", [])}
    rows = []
    for symbol in SYMBOLS:
        contract = contracts.get(symbol)
        if contract is None:
            rows.append({"symbol": symbol, "exchange_availability": "NOT_LISTED", "listing_start_ms": None})
            continue
        open_time = int(contract["openTime"]) if str(contract.get("openTime", "")).isdigit() else None
        rows.append({
            "symbol": symbol, "exchange_availability": contract.get("symbolStatus", "UNKNOWN"),
            "listing_start_ms": open_time,
            "listing_start_utc": datetime.fromtimestamp(open_time / 1000, timezone.utc).isoformat() if open_time else None,
            "symbol_type": contract.get("symbolType"), "source_endpoint": ENDPOINT,
        })
    atomic_write(args.output, stable_json_bytes({"exchange": "BITGET", "market_type": "USDT-FUTURES", "contracts": rows}))
    print(json.dumps(rows, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
