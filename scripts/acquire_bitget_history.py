from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from historical_data.bitget_archive import acquire_dataset

SYMBOLS = ["ADAUSDT", "AVAXUSDT", "BTCUSDT", "ETHUSDT", "LINKUSDT", "SOLUSDT", "SUIUSDT", "WIFUSDT"]


def utc_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include UTC timezone")
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True, help="exclusive UTC boundary")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite dataset: {args.output}")
    manifest = acquire_dataset(SYMBOLS, utc_ms(args.start), utc_ms(args.end), args.output)
    print(manifest["dataset_hash"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
