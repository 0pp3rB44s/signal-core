"""Pure, network-free causal alignment of Binance futures metrics.

Telemetry only. Nothing here places, cancels, or modifies orders; nothing
here is consulted by any trade decision. It answers one question: given a
batch of periodic Binance observations (open interest, taker flow ratio,
positioning ratios) and a signal timestamp, what was CAUSALLY KNOWN at that
instant -- i.e. which buckets had already fully closed, with zero leakage
from a bucket still in progress.

A bucket with start time `timestamp_ms` and duration `period_ms` is only
usable for a signal at `signal_time_ms` if `timestamp_ms + period_ms <=
signal_time_ms` -- the bucket must have fully closed, not merely started,
before the signal. This mirrors the "trade at open of the bar AFTER the
signal bar closed" discipline used throughout this project's OHLCV research.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetricsRow:
    """One periodic observation. `value` is the metric's raw numeric reading
    (OI level, buy/sell ratio, or long/short ratio) at bucket start
    `timestamp_ms`."""
    timestamp_ms: int
    value: float


@dataclass(frozen=True)
class MetricsContext:
    metrics_available: bool
    metrics_stale: bool
    metrics_age_seconds: float | None
    binance_metrics_timestamp_ms: int | None
    open_interest: float | None
    open_interest_change_5m: float | None
    open_interest_change_15m: float | None
    taker_buy_sell_volume_ratio: float | None
    top_trader_long_short_ratio: float | None
    global_long_short_ratio: float | None

    def as_payload(self) -> dict:
        return {
            "binance_metrics_timestamp": self.binance_metrics_timestamp_ms,
            "open_interest": self.open_interest,
            "open_interest_change_5m": self.open_interest_change_5m,
            "open_interest_change_15m": self.open_interest_change_15m,
            "taker_buy_sell_volume_ratio": self.taker_buy_sell_volume_ratio,
            "top_trader_long_short_ratio": self.top_trader_long_short_ratio,
            "global_long_short_ratio": self.global_long_short_ratio,
            "metrics_age_seconds": self.metrics_age_seconds,
            "metrics_available": self.metrics_available,
            "metrics_stale": self.metrics_stale,
        }


#: Returned whenever no causally-usable open-interest reading exists at all.
#: OI is the anchor metric: without it there is no trustworthy reference
#: timestamp to report the ratio fields against, so the whole context is
#: reported unavailable rather than partially populated with an orphaned
#: ratio and no timestamp to anchor it to.
UNAVAILABLE = MetricsContext(
    metrics_available=False, metrics_stale=False, metrics_age_seconds=None,
    binance_metrics_timestamp_ms=None, open_interest=None,
    open_interest_change_5m=None, open_interest_change_15m=None,
    taker_buy_sell_volume_ratio=None, top_trader_long_short_ratio=None,
    global_long_short_ratio=None,
)


def _causal_rows(rows: list[MetricsRow], signal_time_ms: int, period_ms: int) -> list[MetricsRow]:
    """Rows whose bucket has FULLY closed by signal_time_ms. Strict: a bucket
    that merely started before signal_time_ms but has not yet closed is
    excluded -- this is the no-lookahead guarantee."""
    return [r for r in rows if r.timestamp_ms + period_ms <= signal_time_ms]


def _latest_causal_row(rows: list[MetricsRow], signal_time_ms: int, period_ms: int) -> MetricsRow | None:
    causal = _causal_rows(rows, signal_time_ms, period_ms)
    return max(causal, key=lambda r: r.timestamp_ms) if causal else None


def _reference_row_at_offset(
    causal_rows: list[MetricsRow], current_ts_ms: int, offset_ms: int, period_ms: int,
) -> MetricsRow | None:
    """The causal row STRICTLY OLDER than `current_ts_ms` whose timestamp is
    closest to `current_ts_ms - offset_ms`, within one bucket-width of
    tolerance. Returns None if no sufficiently close prior observation
    exists yet (e.g. not enough history collected, or a gap in the feed) --
    a delta is only reported when a genuine reference point was found, never
    guessed or defaulted to the current row itself."""
    target = current_ts_ms - offset_ms
    best: MetricsRow | None = None
    best_diff: int | None = None
    for row in causal_rows:
        if row.timestamp_ms >= current_ts_ms:
            continue
        diff = abs(row.timestamp_ms - target)
        if diff <= period_ms and (best_diff is None or diff < best_diff):
            best, best_diff = row, diff
    return best


def build_metrics_context(
    *,
    oi_rows: list[MetricsRow],
    taker_ratio_rows: list[MetricsRow],
    top_trader_ratio_rows: list[MetricsRow],
    global_ratio_rows: list[MetricsRow],
    period_ms: int,
    signal_time_ms: int,
    max_fresh_age_s: float = 600.0,
) -> MetricsContext:
    """Build the causally-valid metrics context for one signal.

    `signal_time_ms` is the only clock this function trusts: every row is
    filtered against it before anything else is computed, so a row dated
    after (or straddling) the signal time can never influence the result.
    """
    oi_causal = sorted(_causal_rows(oi_rows, signal_time_ms, period_ms), key=lambda r: r.timestamp_ms)
    if not oi_causal:
        return UNAVAILABLE

    current = oi_causal[-1]
    age_s = (signal_time_ms - current.timestamp_ms) / 1000.0
    stale = age_s > max_fresh_age_s

    ref_5m = _reference_row_at_offset(oi_causal, current.timestamp_ms, 5 * 60 * 1000, period_ms)
    ref_15m = _reference_row_at_offset(oi_causal, current.timestamp_ms, 15 * 60 * 1000, period_ms)

    taker_row = _latest_causal_row(taker_ratio_rows, signal_time_ms, period_ms)
    top_trader_row = _latest_causal_row(top_trader_ratio_rows, signal_time_ms, period_ms)
    global_row = _latest_causal_row(global_ratio_rows, signal_time_ms, period_ms)

    return MetricsContext(
        metrics_available=True,
        metrics_stale=stale,
        metrics_age_seconds=age_s,
        binance_metrics_timestamp_ms=current.timestamp_ms,
        open_interest=current.value,
        open_interest_change_5m=(current.value - ref_5m.value) if ref_5m else None,
        open_interest_change_15m=(current.value - ref_15m.value) if ref_15m else None,
        taker_buy_sell_volume_ratio=taker_row.value if taker_row else None,
        top_trader_long_short_ratio=top_trader_row.value if top_trader_row else None,
        global_long_short_ratio=global_row.value if global_row else None,
    )
