from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

import requests

from research.derivatives_data import (
    canonical_hash, canonicalize_bitget_funding, detect_duplicates, detect_gaps,
    observation_dicts, write_atomic_json,
)

ENDPOINT = "https://api.bitget.com/api/v2/mix/market/history-fund-rate"
SYMBOLS = ("ADAUSDT", "AVAXUSDT", "BTCUSDT", "ETHUSDT", "LINKUSDT", "SOLUSDT", "SUIUSDT", "WIFUSDT")


def fetch_page(symbol: str, page: int, retries: int = 3) -> dict:
    parameters = {"symbol": symbol, "productType": "usdt-futures", "pageSize": 100, "pageNo": page}
    for attempt in range(retries):
        response = requests.get(ENDPOINT, params=parameters, timeout=30)
        if response.status_code == 429:
            time.sleep(min(2 ** attempt, 5)); continue
        response.raise_for_status(); payload = response.json()
        if payload.get("code") != "00000":
            raise RuntimeError(f"Bitget error {payload.get('code')}: {payload.get('msg')}")
        return payload
    raise RuntimeError("Bitget funding retries exhausted")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def acquire(symbol: str, root: Path, start_ms: int, end_ms: int, retrieval_ms: int) -> dict:
    raw_directory = root / "raw" / symbol; raw_directory.mkdir(parents=True, exist_ok=True); rows = []; raw_files = []
    for page in range(1, 101):
        target = raw_directory / f"page_{page:03d}.json"
        if target.exists():
            payload = json.loads(target.read_text())
        else:
            payload = fetch_page(symbol, page); write_atomic_json(target, payload)
        batch = payload["data"]; rows.extend(batch); raw_files.append({"path":str(target.relative_to(root)),"sha256":sha256(target),"records":len(batch)})
        if len(batch) < 100: break
        time.sleep(0.06)
    filtered = [row for row in rows if start_ms <= int(row["fundingTime"]) < end_ms]
    observations = canonicalize_bitget_funding(symbol, filtered, retrieval_ms, f"raw/{symbol}/page_*.json")
    canonical_path = root / "canonical" / f"{symbol}.json"; write_atomic_json(canonical_path, observation_dicts(observations))
    timestamps = [item.funding_timestamp_ms for item in observations]; interval_ms = observations[0].funding_interval_hours * 3_600_000 if observations else 0; gaps = detect_gaps(timestamps, interval_ms) if interval_ms else []
    return {"symbol":symbol,"requested_start_ms":start_ms,"requested_end_ms":end_ms,"actual_first_ms":min(timestamps) if timestamps else None,"actual_last_ms":max(timestamps) if timestamps else None,"record_count":len(observations),"duplicates":detect_duplicates(timestamps),"missing_intervals":sum(gap["missing_intervals"] for gap in gaps),"longest_gap_ms":max((gap["before_ms"]-gap["after_ms"] for gap in gaps),default=0),"invalid_values":0,"funding_interval_hours":observations[0].funding_interval_hours if observations else None,"canonical_path":str(canonical_path.relative_to(root)),"canonical_sha256":sha256(canonical_path),"raw_files":raw_files,"coverage_pct_requested":len(observations)/(((end_ms-start_ms)//interval_ms) if interval_ms else 1)*100}


def main() -> int:
    parser=argparse.ArgumentParser();parser.add_argument("--root",type=Path,required=True);parser.add_argument("--start-ms",type=int,required=True);parser.add_argument("--end-ms",type=int,required=True);parser.add_argument("--retrieval-ms",type=int,required=True);args=parser.parse_args()
    reports=[acquire(symbol,args.root,args.start_ms,args.end_ms,args.retrieval_ms) for symbol in SYMBOLS]
    manifest={"schema_version":1,"exchange":"BITGET","market_type":"USDT-FUTURES","source_endpoint":ENDPOINT,"source_semantics":"realised settlement funding","retrieval_timestamp_ms":args.retrieval_ms,"symbols":list(SYMBOLS),"reports":reports}
    manifest["manifest_hash"]=canonical_hash(manifest);write_atomic_json(args.root/"funding_manifest.json",manifest);print(manifest["manifest_hash"]);return 0


if __name__ == "__main__": raise SystemExit(main())
