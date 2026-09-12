"""Focused tests for binance_metrics_context's causal alignment logic.

Pure, network-free -- every test constructs MetricsRow lists directly and
calls build_metrics_context with an explicit decision_time_ms. No mocking of
HTTP is needed for these; that is exercised separately in
test_binance_metrics_context_provider.py.
"""
from __future__ import annotations

import pytest

from binance_metrics_context.causal import MetricsRow, build_metrics_context

PERIOD_5M_MS = 5 * 60 * 1000


def _rows(*pairs: tuple[int, float]) -> list[MetricsRow]:
    return [MetricsRow(timestamp_ms=ts, value=val) for ts, val in pairs]


# --- 1. causal timestamp selection ------------------------------------------

def test_selects_latest_fully_closed_bucket_not_the_freshest_row():
    """Rows exist at 0, 5m, 10m, 15m, 20m. Signal fires at 22m -- the 20m
    bucket (closes at 25m) has not fully closed yet, so 15m must be picked."""
    oi = _rows((0, 100.0), (5 * 60_000, 101.0), (10 * 60_000, 102.0),
               (15 * 60_000, 103.0), (20 * 60_000, 999.0))
    ctx = build_metrics_context(
        oi_rows=oi, taker_ratio_rows=[], top_trader_ratio_rows=[], global_ratio_rows=[],
        period_ms=PERIOD_5M_MS, decision_time_ms=22 * 60_000,
    )
    assert ctx.metrics_available is True
    assert ctx.binance_metrics_timestamp_ms == 15 * 60_000
    assert ctx.open_interest == 103.0


def test_5m_and_15m_deltas_use_the_correct_reference_buckets():
    oi = _rows((0, 100.0), (5 * 60_000, 110.0), (10 * 60_000, 120.0), (15 * 60_000, 130.0))
    ctx = build_metrics_context(
        oi_rows=oi, taker_ratio_rows=[], top_trader_ratio_rows=[], global_ratio_rows=[],
        period_ms=PERIOD_5M_MS, decision_time_ms=20 * 60_000,
    )
    assert ctx.binance_metrics_timestamp_ms == 15 * 60_000
    assert ctx.open_interest == 130.0
    assert ctx.open_interest_change_5m == 10.0   # 130 - 120 (the 10m bucket)
    assert ctx.open_interest_change_15m == 30.0  # 130 - 100 (the 0m bucket)


def test_latest_causal_ratio_row_selected_independently_of_oi():
    oi = _rows((0, 100.0))
    taker = _rows((0, 1.1), (5 * 60_000, 1.3))
    ctx = build_metrics_context(
        oi_rows=oi, taker_ratio_rows=taker, top_trader_ratio_rows=[], global_ratio_rows=[],
        period_ms=PERIOD_5M_MS, decision_time_ms=11 * 60_000,
    )
    # both taker rows have closed by 11m; the later one (5m, closes 10m) wins
    assert ctx.taker_buy_sell_volume_ratio == 1.3


# --- 2. stale-data handling --------------------------------------------------

def test_old_but_present_data_is_flagged_stale_not_discarded():
    oi = _rows((0, 100.0))
    ctx = build_metrics_context(
        oi_rows=oi, taker_ratio_rows=[], top_trader_ratio_rows=[], global_ratio_rows=[],
        period_ms=PERIOD_5M_MS, decision_time_ms=20 * 60_000,  # 20 min after the only bucket
        max_fresh_age_s=600.0,  # 10 min freshness window
    )
    assert ctx.metrics_available is True
    assert ctx.metrics_stale is True
    assert ctx.metrics_age_seconds == pytest.approx(20 * 60)
    assert ctx.open_interest == 100.0


def test_fresh_data_is_not_flagged_stale():
    oi = _rows((0, 100.0))
    ctx = build_metrics_context(
        oi_rows=oi, taker_ratio_rows=[], top_trader_ratio_rows=[], global_ratio_rows=[],
        period_ms=PERIOD_5M_MS, decision_time_ms=6 * 60_000,
        max_fresh_age_s=600.0,
    )
    assert ctx.metrics_available is True
    assert ctx.metrics_stale is False


# --- 3. missing-data handling -------------------------------------------------

def test_no_oi_rows_at_all_is_fully_unavailable():
    ctx = build_metrics_context(
        oi_rows=[], taker_ratio_rows=_rows((0, 1.0)),
        top_trader_ratio_rows=[], global_ratio_rows=[],
        period_ms=PERIOD_5M_MS, decision_time_ms=10 * 60_000,
    )
    assert ctx.metrics_available is False
    assert ctx.metrics_stale is False
    assert ctx.open_interest is None
    assert ctx.taker_buy_sell_volume_ratio is None
    assert ctx.binance_metrics_timestamp_ms is None


def test_oi_present_but_ratio_feeds_missing_reports_partial_availability():
    oi = _rows((0, 100.0))
    ctx = build_metrics_context(
        oi_rows=oi, taker_ratio_rows=[], top_trader_ratio_rows=[], global_ratio_rows=[],
        period_ms=PERIOD_5M_MS, decision_time_ms=6 * 60_000,
    )
    assert ctx.metrics_available is True
    assert ctx.open_interest == 100.0
    assert ctx.taker_buy_sell_volume_ratio is None
    assert ctx.top_trader_long_short_ratio is None
    assert ctx.global_long_short_ratio is None


def test_insufficient_history_leaves_deltas_none_not_zero():
    """Only the current bucket exists -- no 5m/15m-ago reference. The delta
    must be None, not silently computed against itself as a false '0 change'."""
    oi = _rows((10 * 60_000, 500.0))
    ctx = build_metrics_context(
        oi_rows=oi, taker_ratio_rows=[], top_trader_ratio_rows=[], global_ratio_rows=[],
        period_ms=PERIOD_5M_MS, decision_time_ms=16 * 60_000,
    )
    assert ctx.open_interest == 500.0
    assert ctx.open_interest_change_5m is None
    assert ctx.open_interest_change_15m is None


# --- SUNDAY_SHADOW_HARDENING: source-candle vs decision-time separation -----

def test_decision_time_may_be_much_later_than_source_candle():
    """The defining real-world scenario this hardening fixes: a fast-lane
    (5m) signal detected using an older (e.g. 15m) source candle. The
    causal reference must be the DECISION time, not the source candle time
    -- causal.py has no notion of "source candle" at all, it only ever sees
    decision_time_ms, which is exactly the fix (see forward_paper/service.py:
    open_trade() now computes DECISION_TIMESTAMP from wall-clock "now" and
    passes THAT here, never the source candle's own, possibly much older,
    timestamp)."""
    decision_time = 20 * 60_000  # signal's own source candle could be from t=0
    # A bucket that closed at 15m -- long after a hypothetical source candle
    # at t=0, but still safely before this 20m decision time.
    oi = _rows((10 * 60_000, 4321.0))
    ctx = build_metrics_context(
        oi_rows=oi, taker_ratio_rows=[], top_trader_ratio_rows=[], global_ratio_rows=[],
        period_ms=PERIOD_5M_MS, decision_time_ms=decision_time,
    )
    assert ctx.metrics_available is True
    assert ctx.open_interest == 4321.0


def test_bucket_closed_after_source_candle_but_before_decision_is_allowed():
    """A bucket that did not exist yet at the (older) source-candle time --
    i.e. is NEWER information than the signal itself -- is still perfectly
    legitimate to use, because what matters is whether it closed before the
    DECISION was made, not before the signal candle formed. This is the
    precise case the old (buggy) implementation got wrong: it would have
    used the source candle as the causal reference and wrongly excluded
    this bucket as "not yet existing" relative to signal formation."""
    source_candle_time = 0
    decision_time = 20 * 60_000
    bucket_start, bucket_close = 12 * 60_000, 12 * 60_000 + PERIOD_5M_MS  # 12m-17m
    assert bucket_start > source_candle_time          # newer than the signal itself
    assert bucket_close <= decision_time              # but fully closed before the decision
    oi = _rows((bucket_start, 999.0))
    ctx = build_metrics_context(
        oi_rows=oi, taker_ratio_rows=[], top_trader_ratio_rows=[], global_ratio_rows=[],
        period_ms=PERIOD_5M_MS, decision_time_ms=decision_time,
    )
    assert ctx.metrics_available is True
    assert ctx.open_interest == 999.0


# --- 4. no lookahead ----------------------------------------------------------

def test_bucket_not_closed_by_decision_time_is_forbidden():
    """A row at t=decision_time - 1ms (bucket has barely started, nowhere
    near closed) must not leak into the result even though its timestamp is
    <= decision_time -- only a FULLY CLOSED bucket qualifies."""
    decision_time = 100 * 60_000
    oi = _rows((90 * 60_000, 200.0), (decision_time - 1, 99999.0))
    ctx = build_metrics_context(
        oi_rows=oi, taker_ratio_rows=[], top_trader_ratio_rows=[], global_ratio_rows=[],
        period_ms=PERIOD_5M_MS, decision_time_ms=decision_time,
    )
    assert ctx.open_interest == 200.0
    assert ctx.binance_metrics_timestamp_ms == 90 * 60_000


def test_row_timestamped_after_decision_time_can_never_leak():
    decision_time = 50 * 60_000
    oi = _rows((40 * 60_000, 111.0), (60 * 60_000, 222.0))
    ctx = build_metrics_context(
        oi_rows=oi, taker_ratio_rows=[], top_trader_ratio_rows=[], global_ratio_rows=[],
        period_ms=PERIOD_5M_MS, decision_time_ms=decision_time,
    )
    assert ctx.open_interest == 111.0


def test_reference_bucket_search_never_selects_a_future_or_current_row():
    """Regression guard for the self-match bug: with only the current bucket
    causally available, the 5m-ago search must not match the current row
    against itself (diff would equal period_ms) and must return None."""
    oi = _rows((100 * 60_000, 777.0))
    ctx = build_metrics_context(
        oi_rows=oi, taker_ratio_rows=[], top_trader_ratio_rows=[], global_ratio_rows=[],
        period_ms=PERIOD_5M_MS, decision_time_ms=106 * 60_000,
    )
    assert ctx.open_interest == 777.0
    assert ctx.open_interest_change_5m is None
