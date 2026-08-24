"""6H candle production correctness: boundaries, dedup, restart continuity,
warmup, and stale-data refusal."""

from __future__ import annotations

from strategies.adaptive_trend_candles import (
    WARMUP_CANDLES,
    aggregate_to_6h,
    check_data_health,
    closed_only,
    unprocessed_since,
)
from strategies.adaptive_trend_tsmom import Candle6h, SIGNAL_TIMEFRAME_MS

SIX_H = SIGNAL_TIMEFRAME_MS


def c6(open_ms, o, h, l, c):
    return Candle6h(open_ms=open_ms, close_ms=open_ms + SIX_H, open=o, high=h, low=l, close=c)


def test_aggregate_passthrough_when_already_6h():
    raw = [c6(0, 1, 2, 0.5, 1.5), c6(SIX_H, 1.5, 2.5, 1, 2)]
    out = aggregate_to_6h(raw, source_interval_ms=SIX_H)
    assert len(out) == 2
    assert out[0].close_ms == SIX_H


def test_aggregate_from_5min_drops_incomplete_bucket():
    interval = 5 * 60 * 1000
    n_per_bucket = SIX_H // interval
    # first bucket: fully populated
    full = [Candle6h(open_ms=i * interval, close_ms=(i + 1) * interval,
                      open=100 + i, high=101 + i, low=99 + i, close=100 + i)
            for i in range(n_per_bucket)]
    # second bucket: only partially populated (still forming)
    partial = [Candle6h(open_ms=SIX_H + i * interval, close_ms=SIX_H + (i + 1) * interval,
                         open=200, high=201, low=199, close=200)
               for i in range(3)]
    out = aggregate_to_6h(full + partial, source_interval_ms=interval)
    assert len(out) == 1
    assert out[0].open_ms == 0
    assert out[0].high == max(c.high for c in full)
    assert out[0].low == min(c.low for c in full)
    assert out[0].close == full[-1].close


def test_aggregate_rejects_non_divisible_interval():
    import pytest
    with pytest.raises(ValueError):
        aggregate_to_6h([], source_interval_ms=SIX_H - 1)


def test_closed_only_excludes_future_candles():
    candles = [c6(0, 1, 1, 1, 1), c6(SIX_H, 1, 1, 1, 1), c6(2 * SIX_H, 1, 1, 1, 1)]
    # candle 2 (open_ms=2*SIX_H) closes at 3*SIX_H, still in the future.
    closed = closed_only(candles, now_ms=2 * SIX_H + 1)
    assert len(closed) == 2


def test_unprocessed_since_none_returns_everything():
    candles = [c6(0, 1, 1, 1, 1), c6(SIX_H, 1, 1, 1, 1)]
    assert len(unprocessed_since(candles, last_processed_close_ms=None)) == 2


def test_unprocessed_since_excludes_already_seen_candle_no_duplicate_evaluation():
    candles = [c6(0, 1, 1, 1, 1), c6(SIX_H, 1, 1, 1, 1)]
    seen_first = candles[0].close_ms
    remaining = unprocessed_since(candles, last_processed_close_ms=seen_first)
    assert len(remaining) == 1
    assert remaining[0].open_ms == SIX_H


def test_unprocessed_since_after_restart_does_not_skip_a_candle_that_closed_while_down():
    # Process closed candle 0, "restart", a new candle closed while down.
    candles = [c6(0, 1, 1, 1, 1), c6(SIX_H, 1, 1, 1, 1), c6(2 * SIX_H, 1, 1, 1, 1)]
    last_processed = candles[0].close_ms  # persisted before crash
    resumed = unprocessed_since(candles, last_processed_close_ms=last_processed)
    assert [c.open_ms for c in resumed] == [SIX_H, 2 * SIX_H]


def test_data_health_insufficient_warmup():
    candles = [c6(i * SIX_H, 1, 1, 1, 1) for i in range(3)]
    h = check_data_health(candles, now_ms=candles[-1].close_ms)
    assert h.ok is False
    assert h.reason == "insufficient_warmup"


def test_data_health_stale_refuses_signal():
    candles = [c6(i * SIX_H, 1, 1, 1, 1) for i in range(WARMUP_CANDLES + 1)]
    far_future = candles[-1].close_ms + 10 * SIX_H
    h = check_data_health(candles, now_ms=far_future)
    assert h.ok is False
    assert h.reason == "stale_data"


def test_data_health_ok_with_fresh_sufficient_data():
    candles = [c6(i * SIX_H, 1, 1, 1, 1) for i in range(WARMUP_CANDLES + 1)]
    h = check_data_health(candles, now_ms=candles[-1].close_ms + 60_000)
    assert h.ok is True


def test_data_health_no_candles_at_all():
    h = check_data_health([], now_ms=0)
    assert h.ok is False
    assert h.reason == "no_candles"
