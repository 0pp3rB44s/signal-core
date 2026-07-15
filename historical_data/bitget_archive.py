from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import requests

INTERVAL_MS = 900_000
MAX_PAGE = 200
MAX_RANGE_MS = 90 * 24 * 60 * 60 * 1000
ENDPOINT = "https://api.bitget.com/api/v2/mix/market/history-candles"


@dataclass(frozen=True)
class DatasetQuality:
    symbol: str
    requested_start_ms: int
    requested_end_ms: int
    actual_first_ms: int | None
    actual_last_ms: int | None
    candle_count: int
    expected_within_available: int
    duplicate_count: int
    missing_count: int
    longest_gap_candles: int
    out_of_order_count: int
    invalid_ohlc_count: int
    zero_volume_count: int
    negative_volume_count: int
    timestamp_alignment_failures: int
    file_hash: str
    gap_classification: str


def stable_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode()


def content_hash(value: Any) -> str:
    return hashlib.sha256(stable_json_bytes(value)).hexdigest()


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def parse_exchange_row(row: list[Any]) -> dict[str, Any]:
    if len(row) < 6:
        raise ValueError("historical candle row has fewer than six fields")
    return {
        "timestamp": int(row[0]), "open": float(row[1]), "high": float(row[2]),
        "low": float(row[3]), "close": float(row[4]), "volume_base": float(row[5]),
        "volume_quote": float(row[6]) if len(row) > 6 and row[6] not in (None, "") else None,
    }


def canonicalize_rows(raw_rows: list[list[Any]], start_ms: int, end_ms: int) -> tuple[list[dict[str, Any]], int, int]:
    parsed = [parse_exchange_row(row) for row in raw_rows]
    filtered = [row for row in parsed if start_ms <= row["timestamp"] < end_ms]
    out_of_order = sum(left["timestamp"] > right["timestamp"] for left, right in zip(filtered, filtered[1:]))
    by_timestamp: dict[int, dict[str, Any]] = {}
    for row in filtered:
        by_timestamp[row["timestamp"]] = row
    duplicates = len(filtered) - len(by_timestamp)
    return [by_timestamp[key] for key in sorted(by_timestamp)], duplicates, out_of_order


def validate_candles(symbol: str, rows: list[dict[str, Any]], start_ms: int, end_ms: int, duplicates: int, out_of_order: int) -> tuple[DatasetQuality, list[dict[str, Any]]]:
    invalid = 0
    zero_volume = 0
    negative_volume = 0
    alignment = 0
    gaps: list[dict[str, Any]] = []
    for row in rows:
        o, h, l, c, volume = (float(row[key]) for key in ("open", "high", "low", "close", "volume_base"))
        if min(o, h, l, c) <= 0 or h < max(o, c, l) or l > min(o, c, h):
            invalid += 1
        zero_volume += volume == 0
        negative_volume += volume < 0
        alignment += int(row["timestamp"]) % INTERVAL_MS != 0
    longest = 0
    missing = 0
    for left, right in zip(rows, rows[1:]):
        delta = int(right["timestamp"]) - int(left["timestamp"])
        if delta > INTERVAL_MS:
            count = delta // INTERVAL_MS - 1
            missing += count
            longest = max(longest, count)
            gaps.append({
                "symbol": symbol, "after_timestamp_ms": left["timestamp"],
                "before_timestamp_ms": right["timestamp"], "missing_candles": count,
                "classification": "UNKNOWN",
            })
    first = rows[0]["timestamp"] if rows else None
    last = rows[-1]["timestamp"] if rows else None
    expected = ((last - first) // INTERVAL_MS + 1) if first is not None and last is not None else 0
    quality = DatasetQuality(
        symbol=symbol, requested_start_ms=start_ms, requested_end_ms=end_ms,
        actual_first_ms=first, actual_last_ms=last, candle_count=len(rows),
        expected_within_available=expected, duplicate_count=duplicates, missing_count=missing,
        longest_gap_candles=longest, out_of_order_count=out_of_order,
        invalid_ohlc_count=invalid, zero_volume_count=zero_volume,
        negative_volume_count=negative_volume, timestamp_alignment_failures=alignment,
        file_hash=content_hash(rows), gap_classification="NONE" if not gaps else "UNKNOWN",
    )
    return quality, gaps


def paginate(fetch_page: Callable[[str, int, int], list[list[Any]]], symbol: str, start_ms: int, end_ms: int) -> list[list[Any]]:
    cursor = end_ms
    collected: list[list[Any]] = []
    while cursor > start_ms:
        window_start = max(start_ms, cursor - MAX_RANGE_MS)
        page = fetch_page(symbol, window_start, cursor)
        in_window = [row for row in page if start_ms <= int(row[0]) < cursor]
        if not in_window:
            cursor = window_start
            continue
        collected.extend(in_window)
        next_cursor = min(int(row[0]) for row in in_window)
        if next_cursor >= cursor:
            raise RuntimeError(f"pagination did not advance for {symbol}")
        cursor = next_cursor
    return collected


class BitgetHistorySource:
    def __init__(self, *, retries: int = 4, pause_seconds: float = 0.05) -> None:
        self.retries = retries
        self.pause_seconds = pause_seconds
        self.session = requests.Session()

    def fetch_page(self, symbol: str, start_ms: int, end_ms: int) -> list[list[Any]]:
        params = {
            "symbol": symbol, "productType": "USDT-FUTURES", "granularity": "15m",
            "startTime": str(start_ms), "endTime": str(end_ms), "limit": str(MAX_PAGE),
        }
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.get(ENDPOINT, params=params, timeout=20)
                response.raise_for_status()
                payload = response.json()
                if str(payload.get("code")) != "00000":
                    raise RuntimeError(f"Bitget public error code={payload.get('code')} msg={payload.get('msg')}")
                time.sleep(self.pause_seconds)
                return payload.get("data") or []
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(min(2.0, 0.25 * attempt))
        raise RuntimeError(f"historical download failed for {symbol}: {last_error}")


def common_window(qualities: list[DatasetQuality]) -> tuple[int | None, int | None]:
    firsts = [row.actual_first_ms for row in qualities if row.actual_first_ms is not None]
    lasts = [row.actual_last_ms for row in qualities if row.actual_last_ms is not None]
    if len(firsts) != len(qualities) or len(lasts) != len(qualities):
        return None, None
    start, end = max(firsts), min(lasts)
    return (start, end) if start <= end else (None, None)


def acquire_dataset(symbols: list[str], start_ms: int, end_ms: int, output: Path, source: BitgetHistorySource | None = None) -> dict[str, Any]:
    source = source or BitgetHistorySource()
    qualities: list[DatasetQuality] = []
    all_gaps: list[dict[str, Any]] = []
    for symbol in symbols:
        raw = paginate(source.fetch_page, symbol, start_ms, end_ms)
        canonical, duplicates, out_of_order = canonicalize_rows(raw, start_ms, end_ms)
        quality, gaps = validate_candles(symbol, canonical, start_ms, end_ms, duplicates, out_of_order)
        if quality.invalid_ohlc_count or quality.negative_volume_count or quality.timestamp_alignment_failures:
            raise ValueError(f"invalid historical data for {symbol}: {asdict(quality)}")
        atomic_write(output / "raw" / f"{symbol}.json", stable_json_bytes(raw))
        atomic_write(output / "canonical" / f"{symbol}.json", stable_json_bytes(canonical))
        qualities.append(quality)
        all_gaps.extend(gaps)
    common_start, common_end = common_window(qualities)
    manifest = {
        "schema_version": 1, "exchange": "BITGET", "market_type": "USDT-FUTURES",
        "candle_type": "MARKET", "timeframe": "15m", "timestamp_semantics": "CANDLE_OPEN_UTC_MS",
        "requested_start_ms": start_ms, "requested_end_ms_exclusive": end_ms,
        "endpoint": ENDPOINT, "symbols": symbols, "quality": [asdict(row) for row in qualities],
        "common_window": {"start_ms": common_start, "end_ms_inclusive": common_end},
        "gap_count": len(all_gaps),
    }
    manifest["dataset_hash"] = content_hash({"quality": manifest["quality"], "common_window": manifest["common_window"]})
    atomic_write(output / "dataset_manifest.json", stable_json_bytes(manifest))
    atomic_write(output / "data_quality.json", stable_json_bytes([asdict(row) for row in qualities]))
    atomic_write(output / "gaps.json", stable_json_bytes(all_gaps))
    atomic_write(output / "common_window.json", stable_json_bytes(manifest["common_window"]))
    return manifest
