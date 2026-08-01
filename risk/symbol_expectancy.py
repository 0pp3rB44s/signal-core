"""Directional symbol expectancy derived from validated LIVE closed trades.

Replaces the ``by_symbol`` block of ``reports/backtests/latest_summary.json`` as
the input to the symbol kill-switch. That file was offline *backtest* output
generated 2026-07-05 and was treated as an indefinite LIVE hard pause. It had
four defects:

  * offline simulation results gated live money;
  * it was never refreshed, so the pause could not be re-earned — a paused
    symbol produces no new trades and therefore stays paused forever (the same
    argument ``RiskManager._strategy_weighting_gate`` already makes one level
    up, which is why that gate probes instead of freezing);
  * LONG and SHORT history were pooled, so short-side losses blocked long-side
    decisions on the same symbol;
  * it carried no freshness or sample-size metadata, so a stale verdict was
    indistinguishable from a current one.

This module keys expectancy on ``(symbol, direction)``, reads only
exchange-confirmed closed trades, and attaches provenance to every record so a
verdict can always be traced to its evidence.
"""

from __future__ import annotations

import csv
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from telemetry.close_record_sources import (
    ECONOMIC_CLOSE_EVENT_TYPES,
    EXCHANGE_CONFIRMED_CLOSE_SOURCES,
)

BASE_PATH = Path(__file__).resolve().parents[1]
DATASET_PATH = BASE_PATH / "logs" / "trade_dataset_v2.csv"
SOURCE_NAME = "trade_dataset_v2.csv"

logger = logging.getLogger("symbol_expectancy")

#: Only closes are evidence; opens and amendments say nothing about outcome.
#: Both sets now come from ``telemetry/close_record_sources.py`` so this module
#: and the weekly-PnL kill-switch cannot drift apart. The local names are kept
#: because callers and tests already refer to them.
CLOSE_EVENT_TYPES = ECONOMIC_CLOSE_EVENT_TYPES
EXCHANGE_CONFIRMED_SOURCES = EXCHANGE_CONFIRMED_CLOSE_SOURCES

#: Rolling evidence window, matching ``expectancy_window_days`` already used by
#: the strategy-level dataset so the two levels describe the same period.
WINDOW_DAYS = 30

#: Minimum closes per (symbol, direction) before expectancy may gate anything.
#: The strategy level uses 5; the retired symbol gate used 3, low enough that a
#: single bad trade could pause a symbol indefinitely. At the live trade sizes in
#: this account a 30%-winrate signal is not separable from noise below ~10.
MIN_SAMPLE = 10

# --- freshness ----------------------------------------------------------
FRESH = "FRESH"
AGING = "AGING"
STALE = "STALE"
EXPIRED = "EXPIRED"

FRESH_MAX_DAYS = 7
AGING_MAX_DAYS = 14
STALE_MAX_DAYS = 30

#: Freshness states that may still impose a hard pause. Beyond these the
#: evidence is too old to justify blocking live money — this is the specific
#: defect being repaired, so it is expressed as data rather than buried in a
#: conditional.
BLOCKING_FRESHNESS = frozenset({FRESH, AGING})

# --- status -------------------------------------------------------------
SUFFICIENT_OK = "SUFFICIENT_OK"
SUFFICIENT_NEGATIVE = "SUFFICIENT_NEGATIVE"
INSUFFICIENT_LIVE_DATA = "INSUFFICIENT_LIVE_DATA"
SOURCE_ABSENT = "SOURCE_ABSENT"
SOURCE_MALFORMED = "SOURCE_MALFORMED"

# --- confidence ---------------------------------------------------------
HIGH = "HIGH"
MEDIUM = "MEDIUM"
LOW = "LOW"

#: Columns the file must have for any of this to mean anything. A file missing
#: one of these is malformed, not merely empty.
REQUIRED_COLUMNS = ("event_type", "symbol", "direction", "net_pnl", "closed_at")

TP1_HIT_RATE_FLOOR = 0.25
TP1_MIN_SAMPLE = MIN_SAMPLE


@dataclass(frozen=True)
class SymbolExpectancyRecord:
    """One (symbol, direction) verdict plus the provenance behind it."""

    symbol: str
    direction: str
    source: str
    generated_at: str
    last_trade_at: str | None
    window_days: int
    sample_size: int
    expectancy: float | None
    winrate: float | None
    tp1_hit_rate: float | None
    freshness_state: str
    confidence: str
    status: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def freshness_for_age(age_days: float | None) -> str:
    if age_days is None:
        return EXPIRED
    if age_days <= FRESH_MAX_DAYS:
        return FRESH
    if age_days <= AGING_MAX_DAYS:
        return AGING
    if age_days <= STALE_MAX_DAYS:
        return STALE
    return EXPIRED


def confidence_for(sample_size: int, freshness_state: str) -> str:
    """Confidence needs both enough trades and recent ones; either alone is not
    evidence about how the symbol behaves today."""
    if sample_size < MIN_SAMPLE:
        return LOW
    if freshness_state == FRESH and sample_size >= MIN_SAMPLE * 2:
        return HIGH
    if freshness_state in BLOCKING_FRESHNESS:
        return MEDIUM
    return LOW


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in ("true", "1", "yes")


def _sentinel(symbol: str, direction: str, status: str, generated_at: str) -> SymbolExpectancyRecord:
    return SymbolExpectancyRecord(
        symbol=symbol.upper(), direction=direction.upper(), source=SOURCE_NAME,
        generated_at=generated_at, last_trade_at=None, window_days=WINDOW_DAYS,
        sample_size=0, expectancy=None, winrate=None, tp1_hit_rate=None,
        freshness_state=EXPIRED, confidence=LOW, status=status,
    )


class _Cache:
    """60s TTL, matching RiskManager's weekly-PnL cache. The gate runs once per
    candidate per scan; re-reading the file every time would be wasteful without
    being any more correct."""

    TTL_SECONDS = 60.0

    def __init__(self) -> None:
        self._at: float | None = None
        self._value: tuple[dict[tuple[str, str], SymbolExpectancyRecord], str] | None = None

    def get(self) -> tuple[dict[tuple[str, str], SymbolExpectancyRecord], str] | None:
        if self._value is None or self._at is None:
            return None
        if (time.monotonic() - self._at) >= self.TTL_SECONDS:
            return None
        return self._value

    def set(self, value: tuple[dict[tuple[str, str], SymbolExpectancyRecord], str]) -> None:
        self._at = time.monotonic()
        self._value = value

    def clear(self) -> None:
        self._at = None
        self._value = None


_CACHE = _Cache()
_MIGRATION_LOGGED = False


def reset_cache() -> None:
    """Test seam; also lets an operator force a reload without a restart."""
    _CACHE.clear()


def log_migration_once() -> None:
    """Record, once per process, that symbol gating no longer reads the offline
    backtest. Deployment evidence: absence of this line means the old path is
    still live."""
    global _MIGRATION_LOGGED
    if _MIGRATION_LOGGED:
        return
    _MIGRATION_LOGGED = True
    logger.info(
        "SYMBOL_EXPECTANCY_MIGRATION | from=reports/backtests/latest_summary.json:by_symbol "
        "| to=logs/%s | keyed_by=(symbol,direction) | window_days=%s | min_sample=%s "
        "| sources=%s",
        SOURCE_NAME, WINDOW_DAYS, MIN_SAMPLE, ",".join(sorted(EXCHANGE_CONFIRMED_SOURCES)),
    )


def _accumulate(rows: Iterable[dict[str, Any]], cutoff: datetime) -> dict[tuple[str, str], dict[str, Any]]:
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if str(row.get("event_type") or "").upper() not in CLOSE_EVENT_TYPES:
            continue
        if str(row.get("sync_source") or "") not in EXCHANGE_CONFIRMED_SOURCES:
            continue
        closed = _parse_ts(row.get("closed_at") or row.get("timestamp"))
        if closed is None or closed < cutoff:
            continue
        symbol = str(row.get("symbol") or "").upper()
        direction = str(row.get("direction") or "").upper()
        if not symbol or direction not in ("LONG", "SHORT"):
            continue
        try:
            net = float(row.get("net_pnl") or row.get("pnl") or 0.0)
        except (TypeError, ValueError):
            continue
        bucket = buckets.setdefault(
            (symbol, direction),
            {"pnl": [], "wins": 0, "tp1": 0, "last": closed},
        )
        bucket["pnl"].append(net)
        if net > 0:
            bucket["wins"] += 1
        if _truthy(row.get("tp1_hit")):
            bucket["tp1"] += 1
        if closed > bucket["last"]:
            bucket["last"] = closed
    return buckets


def load_records(
    *, now: datetime | None = None, dataset_path: Path | None = None, use_cache: bool = True,
) -> tuple[dict[tuple[str, str], SymbolExpectancyRecord], str]:
    """Returns ({(symbol, direction): record}, source_status).

    ``source_status`` is one of "" (fine), ``SOURCE_ABSENT`` or
    ``SOURCE_MALFORMED`` and describes the file, not any single symbol.
    """
    path = dataset_path or DATASET_PATH
    if use_cache and dataset_path is None:
        cached = _CACHE.get()
        if cached is not None:
            return cached

    generated_at = (now or _utc_now()).isoformat()

    if not path.exists():
        result: tuple[dict[tuple[str, str], SymbolExpectancyRecord], str] = ({}, SOURCE_ABSENT)
        if use_cache and dataset_path is None:
            _CACHE.set(result)
        return result

    reference = now or _utc_now()
    cutoff = reference - timedelta(days=WINDOW_DAYS)

    try:
        with path.open("r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            fieldnames = reader.fieldnames or []
            missing = [c for c in REQUIRED_COLUMNS if c not in fieldnames]
            if missing:
                raise ValueError(f"missing columns: {','.join(missing)}")
            buckets = _accumulate(reader, cutoff)
    except Exception as exc:
        # Fail closed. A present-but-unreadable file means corruption or
        # tampering; silently dropping symbol protection is the one outcome that
        # must not happen. An *absent* file is a different, legitimate state
        # (nothing has traded yet) and is handled above.
        logger.error(
            "SYMBOL_EXPECTANCY_SOURCE_MALFORMED | path=%s | error_type=%s | error=%s "
            "| action=fail_closed",
            path, type(exc).__name__, exc,
        )
        result = ({}, SOURCE_MALFORMED)
        if use_cache and dataset_path is None:
            _CACHE.set(result)
        return result

    records: dict[tuple[str, str], SymbolExpectancyRecord] = {}
    for (symbol, direction), bucket in buckets.items():
        pnl: list[float] = bucket["pnl"]
        n = len(pnl)
        expectancy = sum(pnl) / n
        winrate = bucket["wins"] / n
        tp1_hit_rate = bucket["tp1"] / n
        last: datetime = bucket["last"]
        age_days = (reference - last).total_seconds() / 86400.0
        freshness = freshness_for_age(age_days)

        if n < MIN_SAMPLE:
            status = INSUFFICIENT_LIVE_DATA
        elif expectancy < 0 or (n >= TP1_MIN_SAMPLE and tp1_hit_rate < TP1_HIT_RATE_FLOOR):
            status = SUFFICIENT_NEGATIVE
        else:
            status = SUFFICIENT_OK

        records[(symbol, direction)] = SymbolExpectancyRecord(
            symbol=symbol, direction=direction, source=SOURCE_NAME,
            generated_at=generated_at, last_trade_at=last.isoformat(),
            window_days=WINDOW_DAYS, sample_size=n, expectancy=expectancy,
            winrate=winrate, tp1_hit_rate=tp1_hit_rate,
            freshness_state=freshness,
            confidence=confidence_for(n, freshness), status=status,
        )

    result = (records, "")
    if use_cache and dataset_path is None:
        _CACHE.set(result)
    return result


def record_for(
    symbol: str, direction: str, *, now: datetime | None = None,
    dataset_path: Path | None = None, use_cache: bool = True,
) -> SymbolExpectancyRecord:
    """The verdict for one (symbol, direction). Never raises."""
    log_migration_once()
    generated_at = (now or _utc_now()).isoformat()
    records, source_status = load_records(now=now, dataset_path=dataset_path, use_cache=use_cache)
    if source_status:
        return _sentinel(symbol, direction, source_status, generated_at)
    key = (str(symbol or "").upper(), str(direction or "").upper())
    found = records.get(key)
    if found is not None:
        return found
    # Symbol/direction has no exchange-confirmed closes in the window. This is
    # explicitly *not* evidence of a positive edge; it is recorded as its own
    # state with LOW confidence.
    return _sentinel(symbol, direction, INSUFFICIENT_LIVE_DATA, generated_at)


def evaluate(record: SymbolExpectancyRecord) -> tuple[bool, str | None]:
    """Returns (blocked, reason). Reason wording is load-bearing: the funnel
    classifier maps these prefixes onto hard reason codes."""
    if record.status == SOURCE_MALFORMED:
        return True, (
            f"kill-switch: symbol expectancy source malformed "
            f"({record.symbol} {record.direction}, source={record.source})"
        )

    if record.status != SUFFICIENT_NEGATIVE:
        return False, None

    if record.freshness_state not in BLOCKING_FRESHNESS:
        # The whole point of the repair: aged-out evidence is reported, never
        # enforced, so a pause can always be re-earned by fresh results.
        return False, None

    if record.tp1_hit_rate is not None and record.tp1_hit_rate < TP1_HIT_RATE_FLOOR:
        return True, (
            f"kill-switch: symbol failed TP1 too often "
            f"({record.symbol} {record.direction}, n={record.sample_size}, "
            f"tp1={record.tp1_hit_rate:.3f}, freshness={record.freshness_state})"
        )

    return True, (
        f"kill-switch: symbol paused by expectancy "
        f"({record.symbol} {record.direction}, n={record.sample_size}, "
        f"exp={record.expectancy:.4f}, freshness={record.freshness_state}, live)"
    )


def observability_note(record: SymbolExpectancyRecord) -> str:
    """Soft, always-emitted provenance line. Must not match any hard reason
    prefix in telemetry.funnel — it describes evidence, not a decision."""
    exp = "n/a" if record.expectancy is None else f"{record.expectancy:.4f}"
    return (
        f"symbol expectancy source={record.source} ({record.symbol} {record.direction}, "
        f"n={record.sample_size}, exp={exp}, status={record.status}, "
        f"freshness={record.freshness_state}, confidence={record.confidence})"
    )


def all_records(**kwargs: Any) -> list[SymbolExpectancyRecord]:
    """Every (symbol, direction) record, for the dashboard."""
    records, source_status = load_records(**kwargs)
    if source_status:
        return []
    return sorted(records.values(), key=lambda r: (r.symbol, r.direction))


__all__ = [
    "AGING", "BLOCKING_FRESHNESS", "EXCHANGE_CONFIRMED_SOURCES", "EXPIRED", "FRESH",
    "HIGH", "INSUFFICIENT_LIVE_DATA", "LOW", "MEDIUM", "MIN_SAMPLE", "SOURCE_ABSENT",
    "SOURCE_MALFORMED", "STALE", "SUFFICIENT_NEGATIVE", "SUFFICIENT_OK", "SOURCE_NAME",
    "SymbolExpectancyRecord", "WINDOW_DAYS", "all_records", "confidence_for", "evaluate",
    "freshness_for_age", "load_records", "log_migration_once", "observability_note",
    "record_for", "reset_cache",
]
