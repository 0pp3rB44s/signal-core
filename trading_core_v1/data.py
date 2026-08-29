"""Historical candle loading, canonical OHLCV, integrity checks, sequential feed.

Reuses the repo's existing Bitget REST transport (clients/bitget_rest.py,
clients/bitget_market_client.py) rather than duplicating HTTP/auth/rate-limit
logic: this module imports `BitgetRestClient` and `api_granularity` and nothing
else from the production tree. That client's `get_candles()` wrapper only
returns the latest `limit` bars (no startTime/endTime pagination) AND hits
`/api/v2/mix/market/candles`, which Bitget only serves for a rolling recent
window (empirically confirmed empty for endTime older than ~60 days on
BTCUSDT USDT-FUTURES 1H, 2026-08-29). For historical backfill beyond that
window this module instead calls the shared `_request()` transport directly
against `/api/v2/mix/market/history-candles` -- Bitget's dedicated endpoint
for older candles, confirmed to return data back to at least 2019-07 for
BTCUSDT 1H and to also cover the present (so it is used unconditionally,
never mixed with the recent-only endpoint). Same auth/rate-limit/retry
plumbing as get_candles, just a different path and our own `endTime`
pagination. Only public, unauthenticated market data endpoints are used
(`private=False`); no API keys are read or required for this module to
function.

Pagination correctness note: Bitget's `endTime` is an EXCLUSIVE upper bound
(confirmed empirically: requesting endTime=<a candle's exact open timestamp>
does NOT include that candle in the response). The previous version of this
module computed the next page's endTime as `oldest - step`, which is exactly
one step too low: since endTime is exclusive, requesting endTime=oldest is
what continues seamlessly from `oldest` backward (it returns candles strictly
before `oldest`, i.e. up to and including the candle at oldest-step). Using
oldest-step instead silently dropped the candle at (oldest - step) on every
page boundary. Fixed here to `next_cursor = oldest`.

No-lookahead guarantee: `sequential_feed()` is the ONLY supported way a strategy
or the backtester should walk candles. For index i it yields
(history=candles[:i], current_bar=candles[i]) -- history never contains
candles[i] or anything after it.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Iterator, Sequence

# The production repo root, so we can import its existing Bitget REST client
# without duplicating it. trading_core_v1/ lives at <repo_root>/trading_core_v1.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger(__name__)

# Bitget's dedicated historical-candles endpoint. Unlike
# /api/v2/mix/market/candles (get_candles()'s endpoint), this one serves data
# arbitrarily far back in time as well as the present -- verified live against
# both endpoints on 2026-08-29: /market/candles returned an empty page for
# endTime >= ~60 days ago, while /market/history-candles returned real data
# for endTime as old as 2019-07 and as new as "now".
_HISTORY_CANDLES_PATH = "/api/v2/mix/market/history-candles"

# Bitget rejects limit outside [1, 200] on this endpoint (verified: limit=300
# returns error code 40053 "Value range verification failed").
_MAX_PAGE_LIMIT = 200

# Number of consecutive transient page failures load_historical_candles will
# tolerate (via the shared client's own retry/backoff in `_request`) before
# giving up on a single page and re-raising. `_request` already retries
# internally per HTTP call; this is an *additional* outer bound on retrying a
# single page from scratch if `_request` itself raises (e.g. after exhausting
# its own internal attempts) -- belt and suspenders, not a replacement.
_MAX_PAGE_RETRIES = 3
_PAGE_RETRY_SLEEP_SECONDS = 1.0


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


@dataclass(frozen=True)
class FetchCoverage:
    """Explicit, non-silent report of what a historical fetch actually covered.

    Attached as the `.coverage` attribute of the list returned by
    `load_historical_candles` (see `CandleList`) so callers get it "for free"
    without changing the return type, while `load_historical_candles_report`
    exposes the same object directly when a caller wants it front and center.
    """

    requested_start_ms: int
    requested_end_ms: int
    actual_start_ms: int
    actual_end_ms: int
    bar_count: int
    truncated: bool
    truncation_reason: str = ""
    served_from_cache: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


class CandleList(list):
    """A plain list of Candle with one extra attribute: `.coverage`.

    Exists solely so `load_historical_candles` can keep returning "a list of
    Candle" (the contract every existing caller -- main.py, tests -- already
    relies on) while also making fetch/truncation metadata impossible to miss
    for any caller that checks `.coverage`. Never silently drops data either
    way: a truncated fetch still raises loudly (see below) unless the
    truncation is an already-logged, expected exchange boundary.
    """

    coverage: FetchCoverage | None = None


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
    ascending timestamps, no duplicate timestamps, constant timeframe spacing
    (gap detection against the expected interval).
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
                    f"between ts={prev_ts} and ts={c.timestamp} "
                    f"(missing {gap // expected_step - 1} candle(s) -- a real exchange gap, "
                    f"not silently filled)"
                )
        prev_ts = c.timestamp


def detect_gaps(candles: Sequence[Candle], granularity: str) -> list[dict]:
    """Non-raising companion to validate_candles: report every gap larger than
    one expected step, without throwing. Used for reporting GAP_COUNT /
    LARGEST_GAP without turning every real exchange gap into a hard failure
    (validate_candles is the strict, raising integrity gate used at load time;
    this is for after-the-fact reporting on already-loaded, valid-per-se data).
    """
    if len(candles) < 2:
        return []
    step = timeframe_to_ms(granularity)
    gaps = []
    for prev, cur in zip(candles, candles[1:]):
        gap = cur.timestamp - prev.timestamp
        if gap > step:
            gaps.append({
                "after_ts": prev.timestamp,
                "before_ts": cur.timestamp,
                "gap_ms": gap,
                "missing_candles": gap // step - 1,
            })
    return gaps


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
    tmp_file = cache_file.with_suffix(cache_file.suffix + ".tmp")
    tmp_file.write_text(json.dumps(payload))
    tmp_file.replace(cache_file)  # atomic: never leaves a half-written cache file


def _parse_row(row: Sequence) -> Candle:
    return Candle(
        timestamp=int(row[0]),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=float(row[5]),
    )


def _fetch_range_from_api(
    *,
    client,
    symbol: str,
    granularity: str,
    api_gran: str,
    product_type: str,
    start_ms: int,
    end_ms: int,
    page_limit: int,
) -> tuple[dict[int, Candle], bool, str]:
    """Fetch [start_ms, end_ms) from the exchange via backward endTime
    pagination against the historical-candles endpoint.

    Returns (by_ts, hit_exchange_limit, reason). `hit_exchange_limit=True`
    means the exchange returned an empty page while we still had range left
    to cover -- i.e. we reached the true edge of what the exchange serves,
    not a bug. This is reported, never silently swallowed.
    """
    step = timeframe_to_ms(granularity)
    page_limit = min(page_limit, _MAX_PAGE_LIMIT)
    by_ts: dict[int, Candle] = {}
    cursor_end = end_ms
    hit_exchange_limit = False
    reason = ""
    guard = 0
    guard_max = 20000  # generous: 20000 pages * 200 bars = 4M bars = far beyond any real request

    while cursor_end > start_ms:
        guard += 1
        if guard > guard_max:
            raise DataIntegrityError(
                f"pagination guard tripped fetching {symbol} {granularity}: "
                f"{guard_max} pages fetched without reaching start_ms={start_ms}"
            )

        params = {
            "symbol": symbol.upper(),
            "productType": product_type,
            "granularity": api_gran,
            "endTime": str(cursor_end),
            "limit": page_limit,
        }

        payload = None
        last_exc: Exception | None = None
        for page_attempt in range(1, _MAX_PAGE_RETRIES + 1):
            try:
                # Reuses the shared, rate-limited, retrying transport
                # (_request) of the existing BitgetRestClient -- which itself
                # retries transient HTTP failures with backoff and raises
                # BitgetRetryableError/BitgetAPIError (unrecoverable) rather
                # than returning a partial/empty payload on error. We do not
                # catch those here beyond this bounded outer retry: an
                # unrecoverable error must propagate and fail the whole fetch
                # loudly, never silently truncate history.
                payload = client._request(
                    "GET", _HISTORY_CANDLES_PATH, params=params, private=False
                )
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001 - re-raised below if retries exhausted
                last_exc = exc
                logger.warning(
                    "trading_core_v1.data: page fetch failed (attempt %s/%s) "
                    "symbol=%s granularity=%s endTime=%s: %s",
                    page_attempt, _MAX_PAGE_RETRIES, symbol, granularity, cursor_end, exc,
                )
                if page_attempt < _MAX_PAGE_RETRIES:
                    time.sleep(_PAGE_RETRY_SLEEP_SECONDS * page_attempt)

        if last_exc is not None:
            # Retries exhausted: explicit, loud failure. Never return a
            # partial result silently.
            raise DataIntegrityError(
                f"unrecoverable API error fetching {symbol} {granularity} "
                f"page endTime={cursor_end} after {_MAX_PAGE_RETRIES} attempts: {last_exc}"
            ) from last_exc

        rows = payload.get("data") or []
        if not rows:
            # Empty page while range remains: this IS the exchange's true
            # boundary for this endpoint (verified empirically: the recent
            # `/market/candles` endpoint does this at ~60 days back, and
            # `/market/history-candles` does it at the symbol's true listing
            # date, e.g. BTCUSDT 1H futures has no data before ~2019-07).
            hit_exchange_limit = True
            reason = (
                f"exchange returned no data for endTime<={cursor_end} "
                f"(requested start_ms={start_ms}); this is the true boundary "
                f"of available history on this endpoint, not a fetch bug"
            )
            logger.info("trading_core_v1.data: %s", reason)
            break

        parsed = [_parse_row(r) for r in rows]
        oldest = min(c.timestamp for c in parsed)
        for c in parsed:
            if start_ms <= c.timestamp < end_ms:
                by_ts[c.timestamp] = c

        # endTime is EXCLUSIVE (verified empirically): requesting
        # endTime=<a candle's own open ts> excludes that candle and returns
        # candles strictly before it. So continuing from `oldest` means the
        # next page's endTime must be `oldest` itself, NOT `oldest - step`
        # (which would skip the candle at oldest-step entirely).
        next_cursor = oldest
        if next_cursor >= cursor_end:
            break  # no progress; avoid infinite loop
        cursor_end = next_cursor

        if len(rows) < page_limit:
            # Short page: exchange has nothing older than this page's oldest
            # candle (this is how history-candles signals "no more data"
            # without necessarily returning a fully empty next page too).
            hit_exchange_limit = cursor_end > start_ms
            if hit_exchange_limit:
                reason = (
                    f"short page ({len(rows)} < requested {page_limit}) at "
                    f"endTime={params['endTime']}; exchange has no further "
                    f"history before ts={oldest}"
                )
                logger.info("trading_core_v1.data: %s", reason)
            break

        time.sleep(0.15)  # be polite to the exchange even though the client rate-limits too

    return by_ts, hit_exchange_limit, reason


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
) -> CandleList:
    """Return validated, sorted, deduplicated candles covering [start_ms, end_ms).

    Serves from disk cache when it already covers the requested range (so
    repeated runs never re-hit Bitget for data already fetched and
    validated). When the cache only partially covers the range, only the
    missing portion(s) are fetched from the exchange and merged in -- the
    cache is never thrown away and refetched wholesale.

    The returned CandleList carries a `.coverage` attribute (FetchCoverage)
    describing exactly what was requested vs. what was actually returned, and
    whether an exchange boundary was hit. No truncation is ever silent: if
    fewer bars come back than requested, `.coverage.truncated` is True and
    `.coverage.truncation_reason` explains why (also logged).
    """
    if start_ms >= end_ms:
        raise DataIntegrityError(f"start_ms={start_ms} must be < end_ms={end_ms}")

    step = timeframe_to_ms(granularity)
    cache_file = _cache_path(cache_dir, symbol, granularity, product_type)
    cached = _read_cache(cache_file)

    c_start, c_end, c_candles = cached if cached is not None else (None, None, [])
    cache_by_ts: dict[int, Candle] = {c.timestamp: c for c in c_candles}

    fully_cached = (
        cached is not None
        and c_candles
        and c_start is not None
        and c_end is not None
        and c_start <= start_ms
        and c_end >= end_ms
    )

    hit_exchange_limit = False
    reason = ""
    served_from_cache = False

    if fully_cached:
        served_from_cache = True
    else:
        if client is None:
            from app.config import Settings  # existing production Settings loader
            from clients.bitget_rest import BitgetRestClient

            settings = settings or Settings()
            client = BitgetRestClient(settings)

        from clients.bitget_market_client import api_granularity

        api_gran = api_granularity(granularity)

        # Only fetch the sub-range(s) genuinely missing from the cache.
        missing_ranges: list[tuple[int, int]] = []
        if not cache_by_ts:
            missing_ranges.append((start_ms, end_ms))
        else:
            if start_ms < c_start:
                missing_ranges.append((start_ms, c_start))
            if end_ms > c_end:
                missing_ranges.append((c_end, end_ms))

        for range_start, range_end in missing_ranges:
            fetched, limit_hit, limit_reason = _fetch_range_from_api(
                client=client,
                symbol=symbol,
                granularity=granularity,
                api_gran=api_gran,
                product_type=product_type,
                start_ms=range_start,
                end_ms=range_end,
                page_limit=page_limit,
            )
            cache_by_ts.update(fetched)
            if limit_hit:
                hit_exchange_limit = True
                reason = limit_reason

    candles = sorted(cache_by_ts.values(), key=lambda c: c.timestamp)
    if not candles:
        raise DataIntegrityError(
            f"no candles returned for {symbol} {granularity} in [{start_ms}, {end_ms})"
        )
    validate_candles(candles, granularity)

    # Persist the UNION of old cache + newly fetched data (never shrinks the
    # cache), spanning the widest range we now know about.
    new_c_start = min(c_start, start_ms) if c_start is not None else candles[0].timestamp
    new_c_end = max(c_end, end_ms) if c_end is not None else candles[-1].timestamp + step
    _write_cache(cache_file, min(new_c_start, candles[0].timestamp), max(new_c_end, candles[-1].timestamp + step), candles)

    window = [c for c in candles if start_ms <= c.timestamp < end_ms]
    if not window:
        raise DataIntegrityError(
            f"no candles within requested window [{start_ms}, {end_ms}) for {symbol} {granularity} "
            f"(data exists outside the window but none inside it)"
        )

    actual_start = window[0].timestamp
    actual_end = window[-1].timestamp + step
    truncated = hit_exchange_limit or actual_start > start_ms or actual_end < end_ms
    if truncated and not reason:
        reason = (
            f"requested [{start_ms}, {end_ms}) but only [{actual_start}, {actual_end}) "
            f"is available from the exchange"
        )
    if truncated:
        logger.warning(
            "trading_core_v1.data: TRUNCATED fetch for %s %s: requested=[%s,%s) actual=[%s,%s) reason=%s",
            symbol, granularity, start_ms, end_ms, actual_start, actual_end, reason,
        )

    result = CandleList(window)
    result.coverage = FetchCoverage(
        requested_start_ms=start_ms,
        requested_end_ms=end_ms,
        actual_start_ms=actual_start,
        actual_end_ms=actual_end,
        bar_count=len(window),
        truncated=truncated,
        truncation_reason=reason,
        served_from_cache=served_from_cache,
    )
    return result


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
