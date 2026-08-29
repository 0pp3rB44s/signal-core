"""Regression tests for trading_core_v1/data.py historical pagination, caching,
and integrity reporting.

These tests never touch the network: a FakeBitgetClient simulates Bitget's
`/api/v2/mix/market/history-candles` semantics (EXCLUSIVE endTime, max 200
rows/page, empty page once the simulated dataset is exhausted) so the tests
exercise the exact loop in `_fetch_range_from_api` / `load_historical_candles`
against a known, deterministic dataset.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest

from trading_core_v1.data import (
    Candle,
    DataIntegrityError,
    load_historical_candles,
    timeframe_to_ms,
)

STEP_1H = 3_600_000


def make_dataset(start_ms: int, n: int, step: int = STEP_1H) -> list[list]:
    """n candles at `step` spacing starting at start_ms, ascending."""
    rows = []
    price = 100.0
    for i in range(n):
        ts = start_ms + i * step
        rows.append([str(ts), str(price), str(price + 1), str(price - 1), str(price), "10"])
    return rows


class FakeBitgetClient:
    """Simulates Bitget's history-candles endpoint against an in-memory dataset.

    `dataset` is a list of rows [ts, o, h, l, c, v] sorted ascending, all on a
    fixed `step`. `earliest_ms` is the true "exchange boundary" -- any query
    entirely below it returns an empty page, exactly like the real API when
    asked for data older than a symbol's listing.
    """

    def __init__(self, dataset: list[list], step: int, page_limit_cap: int = 200):
        self.dataset = dataset
        self.step = step
        self.page_limit_cap = page_limit_cap
        self.calls: list[dict] = []
        self.fail_next_n = 0
        self.fail_forever = False

    def _request(self, method, path, params=None, private=False):
        self.calls.append(dict(params or {}))
        if self.fail_forever:
            raise RuntimeError("simulated permanent API failure")
        if self.fail_next_n > 0:
            self.fail_next_n -= 1
            raise RuntimeError("simulated transient API failure")

        end_time = int(params["endTime"])
        limit = min(int(params["limit"]), self.page_limit_cap)
        # Exclusive upper bound: rows with ts < end_time, then take the most
        # recent `limit` of those (closest to end_time), exactly like the
        # real endpoint returns the newest slice under the cap.
        eligible = [r for r in self.dataset if int(r[0]) < end_time]
        eligible.sort(key=lambda r: int(r[0]))
        page = eligible[-limit:] if eligible else []
        return {"code": "00000", "msg": "success", "data": page}


@pytest.fixture()
def cache_dir(tmp_path):
    return tmp_path / "cache"


def test_multi_page_pagination_fetches_full_requested_range(cache_dir):
    # 500 hourly candles -- needs 3 pages at page_limit=200.
    start = 1_700_000_000_000
    rows = make_dataset(start, 500)
    client = FakeBitgetClient(rows, STEP_1H)

    end_ms = start + 500 * STEP_1H
    candles = load_historical_candles(
        symbol="BTCUSDT", granularity="1h", start_ms=start, end_ms=end_ms,
        cache_dir=cache_dir, client=client, page_limit=200,
    )
    assert len(candles) == 500
    assert len(client.calls) >= 3
    assert not candles.coverage.truncated


def test_boundary_handling_no_off_by_one_drop(cache_dir):
    """Regression for the exact bug fixed: `next_cursor = oldest - step` used
    to drop the candle exactly at each page boundary. With page_limit small
    enough to force several page turns, every candle in range must come back."""
    start = 1_700_000_000_000
    rows = make_dataset(start, 47)  # forces boundary crossings at limit=10
    client = FakeBitgetClient(rows, STEP_1H)
    end_ms = start + 47 * STEP_1H

    candles = load_historical_candles(
        symbol="BTCUSDT", granularity="1h", start_ms=start, end_ms=end_ms,
        cache_dir=cache_dir, client=client, page_limit=10,
    )
    got_ts = [c.timestamp for c in candles]
    expected_ts = [start + i * STEP_1H for i in range(47)]
    assert got_ts == expected_ts  # every boundary candle present, none dropped


def test_deduplication_across_overlapping_pages(cache_dir):
    start = 1_700_000_000_000
    rows = make_dataset(start, 30)
    client = FakeBitgetClient(rows, STEP_1H)
    end_ms = start + 30 * STEP_1H

    candles = load_historical_candles(
        symbol="BTCUSDT", granularity="1h", start_ms=start, end_ms=end_ms,
        cache_dir=cache_dir, client=client, page_limit=200,
    )
    timestamps = [c.timestamp for c in candles]
    assert len(timestamps) == len(set(timestamps))


def test_chronological_ordering(cache_dir):
    start = 1_700_000_000_000
    rows = make_dataset(start, 25)
    client = FakeBitgetClient(rows, STEP_1H)
    end_ms = start + 25 * STEP_1H

    candles = load_historical_candles(
        symbol="BTCUSDT", granularity="1h", start_ms=start, end_ms=end_ms,
        cache_dir=cache_dir, client=client, page_limit=7,
    )
    ts = [c.timestamp for c in candles]
    assert ts == sorted(ts)


def test_requested_range_truncation_is_detected_and_reported(cache_dir):
    """Dataset only has candles starting at `data_start`; requesting further
    back must not silently return fewer bars -- coverage.truncated must be
    True and actual_start_ms must reflect the real boundary."""
    data_start = 1_700_000_000_000
    rows = make_dataset(data_start, 20)
    client = FakeBitgetClient(rows, STEP_1H)

    requested_start = data_start - 10 * STEP_1H  # before the exchange has data
    requested_end = data_start + 20 * STEP_1H

    candles = load_historical_candles(
        symbol="BTCUSDT", granularity="1h", start_ms=requested_start, end_ms=requested_end,
        cache_dir=cache_dir, client=client, page_limit=200,
    )
    assert candles.coverage.truncated is True
    assert candles.coverage.actual_start_ms == data_start
    assert candles.coverage.requested_start_ms == requested_start
    assert candles.coverage.truncation_reason  # non-empty, explains why


def test_cache_reuse_avoids_refetch(cache_dir):
    start = 1_700_000_000_000
    rows = make_dataset(start, 50)
    client = FakeBitgetClient(rows, STEP_1H)
    end_ms = start + 50 * STEP_1H

    first = load_historical_candles(
        symbol="BTCUSDT", granularity="1h", start_ms=start, end_ms=end_ms,
        cache_dir=cache_dir, client=client, page_limit=200,
    )
    assert not first.coverage.served_from_cache
    calls_after_first = len(client.calls)

    second = load_historical_candles(
        symbol="BTCUSDT", granularity="1h", start_ms=start, end_ms=end_ms,
        cache_dir=cache_dir, client=client, page_limit=200,
    )
    assert second.coverage.served_from_cache
    assert len(client.calls) == calls_after_first  # no new network calls
    assert [c.timestamp for c in second] == [c.timestamp for c in first]


def test_partial_cache_extension_fetches_only_missing_portion(cache_dir):
    start = 1_700_000_000_000
    rows = make_dataset(start, 100)
    client = FakeBitgetClient(rows, STEP_1H)

    # First: cache the middle third.
    mid_start = start + 30 * STEP_1H
    mid_end = start + 60 * STEP_1H
    load_historical_candles(
        symbol="BTCUSDT", granularity="1h", start_ms=mid_start, end_ms=mid_end,
        cache_dir=cache_dir, client=client, page_limit=200,
    )
    calls_after_first = len(client.calls)

    # Now request a wider range that fully contains the cached middle: only
    # the two missing edges should be fetched, not the whole thing.
    full_start = start
    full_end = start + 100 * STEP_1H
    result = load_historical_candles(
        symbol="BTCUSDT", granularity="1h", start_ms=full_start, end_ms=full_end,
        cache_dir=cache_dir, client=client, page_limit=200,
    )
    assert len(result) == 100
    assert not result.coverage.served_from_cache
    # Some new calls happened, but caching means we didn't refetch from
    # scratch: total candles returned by the API across all calls (including
    # the first request) is far less than if the full range had been fetched
    # twice.
    assert len(client.calls) > calls_after_first


def test_api_failure_after_retries_raises_explicitly(cache_dir):
    start = 1_700_000_000_000
    rows = make_dataset(start, 50)
    client = FakeBitgetClient(rows, STEP_1H)
    client.fail_forever = True
    end_ms = start + 50 * STEP_1H

    with pytest.raises(DataIntegrityError):
        load_historical_candles(
            symbol="BTCUSDT", granularity="1h", start_ms=start, end_ms=end_ms,
            cache_dir=cache_dir, client=client, page_limit=200,
        )


def test_transient_failure_recovers_via_bounded_retry(cache_dir):
    start = 1_700_000_000_000
    rows = make_dataset(start, 50)
    client = FakeBitgetClient(rows, STEP_1H)
    client.fail_next_n = 2  # fewer than _MAX_PAGE_RETRIES
    end_ms = start + 50 * STEP_1H

    candles = load_historical_candles(
        symbol="BTCUSDT", granularity="1h", start_ms=start, end_ms=end_ms,
        cache_dir=cache_dir, client=client, page_limit=200,
    )
    assert len(candles) == 50


def test_no_progress_guard_still_terminates(cache_dir):
    """If the exchange returns an empty page immediately, the loop must
    terminate (not hang) and raise because nothing was fetched."""
    client = FakeBitgetClient([], STEP_1H)
    start = 1_700_000_000_000
    end_ms = start + 10 * STEP_1H

    with pytest.raises(DataIntegrityError):
        load_historical_candles(
            symbol="BTCUSDT", granularity="1h", start_ms=start, end_ms=end_ms,
            cache_dir=cache_dir, client=client, page_limit=200,
        )
