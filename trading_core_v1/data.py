"""Historical candle loading, canonical OHLCV, integrity checks, sequential feed.

Reuses the repo's existing Bitget REST transport (clients/bitget_rest.py,
clients/bitget_market_client.py) rather than duplicating HTTP/auth/rate-limit
logic: this module imports `BitgetRestClient` and `api_granularity` and nothing
else from the production tree. That client's `get_candles()` wrapper only
returns the latest `limit` bars (no startTime/endTime pagination), so for
historical backfill we call the shared `_request()` transport directly with our
own `endTime` pagination -- same auth/rate-limit/retry plumbing, just exposing a
parameter the existing wrapper does not. Only public, unauthenticated market
data endpoints are used (`private=False`); no API keys are read or required for
this module to function.

No-lookahead guarantee: `sequential_feed()` is the ONLY supported way a strategy
or the backtester should walk candles. For index i it yields
(history=candles[:i], current_bar=candles[i]) -- history never contains
candles[i] or anything after it.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterator, Sequence

# The production repo root, so we can import its existing Bitget REST client
# without duplicating it. trading_core_v1/ lives at <repo_root>/trading_core_v1.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class DataIntegrityError(ValueError):
    """Historical data failed an integrity check. Trading Core V1 NEVER silently
    "fixes" candle data (that would change the economics of a backtest) -- it
    always fails loudly instead."""


@dataclass(frozen=True)
class Candle:
    """Canonical OHLCV bar."""

    timestamp: int  # candle OPEN time, ms UTC
    open: float
    high: float
    low: float
    close: float
    volume: float


_UNIT_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 7 * 86_400_000}


def timeframe_to_ms(granularity: str) -> int:
    g = granularity.strip().lower()
    if not g or not g[-1].isalpha():
        raise DataIntegrityError(f"cannot parse granularity={granularity!r}")
    unit = g[-1]
    digits = g[:-1]
    n = int(digits) if digits.isdigit() else 1
    if unit not in _UNIT_MS:
        raise DataIntegrityError(f"unsupported granularity unit in {granularity!r}")
    return n * _UNIT_MS[unit]


def validate_candles(candles: Sequence[Candle], granularity: str) -> None:
    """Explicit failure on any integrity violation. Never silently repairs data.

    Checks: non-empty, high>=low, open/close within [low, high], strictly
    ascending timestamps, no duplicate timestamps, constant timeframe spacing.
    """
    if not candles:
        raise DataIntegrityError("no candles to validate (empty series)")

    expected_step = timeframe_to_ms(granularity)
    seen: set[int] = set()
    prev_ts: int | None = None

    for c in candles:
        if c.high < c.low:
            raise DataIntegrityError(f"malformed OHLC: high < low at ts={c.timestamp}")
        if not (c.low <= c.open <= c.high):
            raise DataIntegrityError(f"malformed OHLC: open outside [low,high] at ts={c.timestamp}")
        if not (c.low <= c.close <= c.high):
            raise DataIntegrityError(f"malformed OHLC: close outside [low,high] at ts={c.timestamp}")
        if c.timestamp in seen:
            raise DataIntegrityError(f"duplicate candle timestamp={c.timestamp}")
        seen.add(c.timestamp)

        if prev_ts is not None:
            if c.timestamp <= prev_ts:
                raise DataIntegrityError(
                    f"timestamps not strictly ascending: {prev_ts} -> {c.timestamp}"
                )
            gap = c.timestamp - prev_ts
            if gap != expected_step:
                raise DataIntegrityError(
                    f"timeframe inconsistency: gap={gap}ms expected={expected_step}ms "
                    f"between ts={prev_ts} and ts={c.timestamp}"
                )
        prev_ts = c.timestamp


def _cache_path(cache_dir: Path, symbol: str, granularity: str, product_type: str) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe_symbol = symbol.upper()
    return cache_dir / f"{safe_symbol}_{product_type}_{granularity}.json"


def _read_cache(cache_file: Path) -> tuple[int, int, list[Candle]] | None:
    if not cache_file.exists():
        return None
    try:
        payload = json.loads(cache_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    candles = [Candle(**row) for row in payload.get("candles", [])]
    return payload.get("start_ms", 0), payload.get("end_ms", 0), candles


def _write_cache(cache_file: Path, start_ms: int, end_ms: int, candles: Sequence[Candle]) -> None:
    payload = {
        "start_ms": start_ms,
        "end_ms": end_ms,
        "candles": [asdict(c) for c in candles],
    }
    cache_file.write_text(json.dumps(payload))


def _parse_row(row: Sequence) -> Candle:
    return Candle(
        timestamp=int(row[0]),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=float(row[5]),
    )


def load_historical_candles(
    *,
    symbol: str,
    granularity: str,
    start_ms: int,
    end_ms: int,
    product_type: str = "USDT-FUTURES",
    cache_dir: Path,
    page_limit: int = 200,
    settings=None,
    client=None,
) -> list[Candle]:
    """Return validated, sorted, deduplicated candles covering [start_ms, end_ms).

    Serves from disk cache when the cache already fully covers the requested
    range (so repeated runs never re-hit Bitget). Otherwise fetches the full
    range via the shared, rate-limited BitgetRestClient transport, validates,
    caches, and returns it.
    """
    cache_file = _cache_path(cache_dir, symbol, granularity, product_type)
    cached = _read_cache(cache_file)
    if cached is not None:
        c_start, c_end, c_candles = cached
        if c_candles and c_start <= start_ms and c_end >= end_ms:
            window = [c for c in c_candles if start_ms <= c.timestamp < end_ms]
            if window:
                return window

    if client is None:
        from app.config import Settings  # existing production Settings loader
        from clients.bitget_rest import BitgetRestClient

        settings = settings or Settings()
        client = BitgetRestClient(settings)

    from clients.bitget_market_client import api_granularity

    api_gran = api_granularity(granularity)
    step = timeframe_to_ms(granularity)

    by_ts: dict[int, Candle] = {}
    cursor_end = end_ms
    guard = 0
    while cursor_end > start_ms:
        guard += 1
        if guard > 5000:
            raise DataIntegrityError("pagination guard tripped fetching historical candles")
        params = {
            "symbol": symbol.upper(),
            "productType": product_type,
            "granularity": api_gran,
            "endTime": str(cursor_end),
            "limit": page_limit,
        }
        # Reuses the shared, rate-limited, retrying transport (_request) of the
        # existing BitgetRestClient. Public endpoint: private=False.
        payload = client._request("GET", "/api/v2/mix/market/candles", params=params, private=False)
        rows = payload.get("data") or []
        if not rows:
            break
        parsed = [_parse_row(r) for r in rows]
        oldest = min(c.timestamp for c in parsed)
        for c in parsed:
            if start_ms <= c.timestamp < end_ms:
                by_ts[c.timestamp] = c
        next_cursor = oldest - step
        if next_cursor >= cursor_end:
            break  # no progress; avoid infinite loop
        cursor_end = next_cursor
        time.sleep(0.1)  # be polite to the exchange even though the client rate-limits too

    candles = sorted(by_ts.values(), key=lambda c: c.timestamp)
    if not candles:
        raise DataIntegrityError(
            f"no candles returned for {symbol} {granularity} in [{start_ms}, {end_ms})"
        )
    validate_candles(candles, granularity)
    _write_cache(cache_file, candles[0].timestamp, candles[-1].timestamp + step, candles)
    return candles


def sequential_feed(candles: Sequence[Candle]) -> Iterator[tuple[Sequence[Candle], Candle]]:
    """Yield (history, current_bar) with no future-bar leakage.

    history == candles[:i] (bars strictly before current_bar). A strategy
    evaluated on this feed can never see a bar that had not closed yet.
    """
    for i in range(len(candles)):
        yield candles[:i], candles[i]


def chronological_split(
    candles: Sequence[Candle], *, train: float, validation: float, test: float
) -> dict[str, list[Candle]]:
    """Split candles into TRAIN/VALIDATION/TEST chronologically (never shuffled).

    Fractions must sum to 1.0 (within floating point tolerance). This is
    structure only -- V1 does not run walk-forward or optimization on top of it.
    """
    total = train + validation + test
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"train+validation+test must sum to 1.0, got {total}")
    n = len(candles)
    n_train = int(n * train)
    n_val = int(n * validation)
    return {
        "TRAIN": list(candles[:n_train]),
        "VALIDATION": list(candles[n_train:n_train + n_val]),
        "TEST": list(candles[n_train + n_val:]),
    }
