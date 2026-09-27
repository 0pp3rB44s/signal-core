"""6H candle production for adaptive_trend_tsmom_v1.

Consumes raw closed candles (any source that can supply Candle6h-shaped OHLC
at native resolution or already-6H resolution) and produces the exact,
deduplicated, boundary-aligned, warmup-aware sequence the strategy is allowed
to see. This module owns "what is a closed 6H candle" so that fact is decided
in exactly one place, not re-derived by every caller.

Restart continuity: `last_processed_close_ms` is the only state this module
needs to resume correctly after a crash/restart. It is a plain integer,
persisted by the caller via the existing `JsonStateStore` pattern -- this
module does not do its own I/O, matching every other pure-logic module here.
"""

from __future__ import annotations

from dataclasses import dataclass

from strategies.adaptive_trend_tsmom import (
    ATR_PERIOD,
    Candle6h,
    MOM_LOOKBACK,
    SIGNAL_TIMEFRAME_MS,
    candle_6h_boundary,
)

WARMUP_CANDLES = MOM_LOOKBACK + ATR_PERIOD + 1


def aggregate_to_6h(raw_candles: list[Candle6h] | list[tuple], *, source_interval_ms: int) -> list[Candle6h]:
    """Aggregate finer-resolution OHLC into 6H candles.

    `raw_candles` may already be 6H (source_interval_ms == SIGNAL_TIMEFRAME_MS,
    returned as-is after boundary normalization) or any finer resolution that
    divides evenly into 6H. A partially-formed trailing bucket (the candle
    still accumulating "now") is dropped here -- callers must not rely on this
    function alone for the close-boundary guarantee; `closed_only` below is
    the actual gate.
    """
    if source_interval_ms == SIGNAL_TIMEFRAME_MS:
        return sorted(raw_candles, key=lambda c: c.open_ms)
    if SIGNAL_TIMEFRAME_MS % source_interval_ms != 0:
        raise ValueError("source_interval_ms must divide evenly into 6H")

    buckets: dict[int, list] = {}
    for c in sorted(raw_candles, key=lambda c: c.open_ms):
        bucket_start = candle_6h_boundary(c.open_ms)
        buckets.setdefault(bucket_start, []).append(c)

    out = []
    complete_buckets_needed = SIGNAL_TIMEFRAME_MS // source_interval_ms
    for bucket_start in sorted(buckets):
        members = buckets[bucket_start]
        if len(members) < complete_buckets_needed:
            continue  # incomplete 6H window -- not a closed candle yet
        out.append(Candle6h(
            open_ms=bucket_start,
            close_ms=bucket_start + SIGNAL_TIMEFRAME_MS,
            open=members[0].open,
            high=max(m.high for m in members),
            low=min(m.low for m in members),
            close=members[-1].close,
        ))
    return out


def closed_only(candles: list[Candle6h], *, now_ms: int) -> list[Candle6h]:
    """Every candle whose close boundary has actually arrived. No look-ahead."""
    return [c for c in candles if c.close_ms <= now_ms]


def unprocessed_since(candles: list[Candle6h], *, last_processed_close_ms: int | None) -> list[Candle6h]:
    """New closed candles the caller has not yet evaluated, oldest first.

    This is the restart-continuity contract: on a fresh process with
    `last_processed_close_ms=None`, everything closed is "new" (the caller
    still needs `WARMUP_CANDLES` of history before it can compute a real
    signal, but this function's job is only "what closed since we last
    looked", not warmup sufficiency). After a restart, passing back the
    persisted `last_processed_close_ms` resumes exactly where evaluation
    left off -- no re-evaluation of an already-processed candle, and no
    silently skipped candle that closed while the process was down.
    """
    if last_processed_close_ms is None:
        return list(candles)
    return [c for c in candles if c.close_ms > last_processed_close_ms]


@dataclass(slots=True)
class DataHealth:
    ok: bool
    reason: str = ""
    latest_close_ms: int | None = None
    staleness_ms: int | None = None


def check_data_health(
    candles: list[Candle6h], *, now_ms: int, max_staleness_ms: int = SIGNAL_TIMEFRAME_MS + 30 * 60 * 1000,
    min_candles: int = WARMUP_CANDLES,
) -> DataHealth:
    """Stale/incomplete data must produce NO trade, not a guess.

    A closed 6H candle should be visible within one candle-width plus a
    generous grace period; if the freshest closed candle is older than that,
    something upstream (the collector, the exchange feed) is unhealthy and
    this strategy must refuse to signal on data it cannot trust.

    `min_candles` defaults to the full signal-generation warmup
    (MOM_LOOKBACK + ATR_PERIOD + 1), but callers that only need ATR history
    -- the trailing-stop path on an already-open position, which needs no
    momentum lookback at all -- pass a smaller, explicit requirement rather
    than being held to the signal path's stricter one.
    """
    if not candles:
        return DataHealth(ok=False, reason="no_candles")
    if len(candles) < min_candles:
        return DataHealth(ok=False, reason="insufficient_warmup",
                           latest_close_ms=candles[-1].close_ms)
    latest = candles[-1].close_ms
    staleness = now_ms - latest
    if staleness > max_staleness_ms:
        return DataHealth(ok=False, reason="stale_data", latest_close_ms=latest, staleness_ms=staleness)
    return DataHealth(ok=True, latest_close_ms=latest, staleness_ms=staleness)
