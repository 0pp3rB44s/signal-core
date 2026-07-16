from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


@dataclass(frozen=True)
class FundingObservation:
    symbol: str
    exchange: str
    market_type: str
    funding_timestamp_ms: int
    funding_rate: float
    funding_interval_hours: int
    source_type: str
    source_retrieval_timestamp_ms: int
    raw_source_reference: str
    predicted_funding_rate: float | None = None
    mark_price: float | None = None
    index_price: float | None = None


@dataclass(frozen=True)
class OpenInterestObservation:
    symbol: str
    exchange: str
    market_type: str
    timestamp_ms: int
    open_interest_value: float
    unit: str
    contract_size: float | None
    conversion_basis: str
    notional_oi_usdt: float | None
    source_retrieval_timestamp_ms: int
    raw_source_reference: str


@dataclass(frozen=True)
class SynchronizedPositioning:
    candle_open_timestamp_ms: int
    funding_rate: float | None
    funding_age_seconds: int | None
    funding_available: bool
    open_interest_value: float | None
    open_interest_notional_usdt: float | None
    oi_age_seconds: int | None
    oi_available: bool
    oi_stale: bool


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def infer_funding_interval_hours(timestamps_ms: Sequence[int]) -> int:
    unique = sorted(set(int(value) for value in timestamps_ms))
    intervals = [(right - left) // 3_600_000 for left, right in zip(unique, unique[1:]) if right > left]
    if not intervals:
        raise ValueError("at least two funding timestamps are required")
    interval = max(set(intervals), key=lambda item: (intervals.count(item), -item))
    if interval not in {1, 2, 4, 8}:
        raise ValueError(f"unsupported funding interval: {interval}")
    return interval


def canonicalize_bitget_funding(
    symbol: str,
    rows: Sequence[dict[str, Any]],
    retrieval_timestamp_ms: int,
    raw_reference: str,
) -> list[FundingObservation]:
    timestamps = [int(row["fundingTime"]) for row in rows]
    if len(timestamps) != len(set(timestamps)):
        raise ValueError("duplicate funding timestamp")
    interval = infer_funding_interval_hours(timestamps)
    observations = []
    for row in sorted(rows, key=lambda item: int(item["fundingTime"])):
        rate = float(row["fundingRate"])
        if not math.isfinite(rate):
            raise ValueError("invalid funding rate")
        observations.append(FundingObservation(
            symbol=symbol.upper(), exchange="BITGET", market_type="USDT-FUTURES",
            funding_timestamp_ms=int(row["fundingTime"]), funding_rate=rate,
            funding_interval_hours=interval, source_type="REALISED_SETTLEMENT",
            source_retrieval_timestamp_ms=int(retrieval_timestamp_ms),
            raw_source_reference=raw_reference,
        ))
    return observations


def canonicalize_tardis_ticker(
    row: dict[str, Any], retrieval_timestamp_ms: int, raw_reference: str,
    contract_size: float = 1.0,
) -> OpenInterestObservation:
    timestamp_ms = int(row["timestamp"]) // 1_000
    value = float(row["open_interest"])
    price = float(row["mark_price"])
    if not all(math.isfinite(item) and item >= 0 for item in (value, price, contract_size)):
        raise ValueError("invalid OI normalization input")
    return OpenInterestObservation(
        symbol=str(row["symbol"]).upper(), exchange="BITGET", market_type="USDT-FUTURES",
        timestamp_ms=timestamp_ms, open_interest_value=value, unit="BASE_ASSET",
        contract_size=contract_size, conversion_basis="base OI * contract size * mark price",
        notional_oi_usdt=value * contract_size * price,
        source_retrieval_timestamp_ms=int(retrieval_timestamp_ms), raw_source_reference=raw_reference,
    )


def detect_duplicates(timestamps: Iterable[int]) -> int:
    values = list(timestamps)
    return len(values) - len(set(values))


def detect_gaps(timestamps: Iterable[int], expected_interval_ms: int) -> list[dict[str, int]]:
    values = sorted(set(int(value) for value in timestamps)); gaps = []
    for left, right in zip(values, values[1:]):
        if right - left > expected_interval_ms:
            gaps.append({"after_ms": left, "before_ms": right, "missing_intervals": (right-left)//expected_interval_ms-1})
    return gaps


def stable_page_walk(fetch_page: Callable[[int], Sequence[dict[str, Any]]], page_size: int, max_pages: int = 100) -> list[dict[str, Any]]:
    rows = []
    for page in range(1, max_pages + 1):
        batch = list(fetch_page(page)); rows.extend(batch)
        if len(batch) < page_size:
            return rows
    raise ValueError("pagination did not terminate")


def latest_at_or_before(observations: Sequence[Any], timestamp_field: str, as_of_ms: int) -> Any | None:
    eligible = [item for item in observations if int(getattr(item, timestamp_field)) <= as_of_ms]
    return max(eligible, key=lambda item: int(getattr(item, timestamp_field))) if eligible else None


def synchronize_positioning(
    candle_open_timestamp_ms: int,
    timeframe_ms: int,
    funding: Sequence[FundingObservation],
    open_interest: Sequence[OpenInterestObservation],
    max_oi_age_ms: int,
) -> SynchronizedPositioning:
    closed_at = int(candle_open_timestamp_ms) + int(timeframe_ms)
    funding_item = latest_at_or_before(funding, "funding_timestamp_ms", closed_at)
    oi_item = latest_at_or_before(open_interest, "timestamp_ms", closed_at)
    funding_age = (closed_at - funding_item.funding_timestamp_ms) // 1_000 if funding_item else None
    oi_age = (closed_at - oi_item.timestamp_ms) // 1_000 if oi_item else None
    stale = oi_item is not None and oi_age is not None and oi_age * 1_000 > max_oi_age_ms
    return SynchronizedPositioning(
        candle_open_timestamp_ms=int(candle_open_timestamp_ms),
        funding_rate=funding_item.funding_rate if funding_item else None,
        funding_age_seconds=funding_age, funding_available=funding_item is not None,
        open_interest_value=oi_item.open_interest_value if oi_item and not stale else None,
        open_interest_notional_usdt=oi_item.notional_oi_usdt if oi_item and not stale else None,
        oi_age_seconds=oi_age, oi_available=oi_item is not None and not stale, oi_stale=stale,
    )


def funding_event_window(event_index: int, candle_count: int) -> dict[str, list[int] | int]:
    if event_index < 4 or event_index + 16 >= candle_count:
        raise ValueError("complete funding-event window unavailable")
    return {"before": list(range(event_index - 4, event_index)), "funding": event_index,
            "after": [event_index + offset for offset in (1, 2, 4, 8, 16)]}


def validate_primary_hypotheses(hypotheses: Sequence[dict[str, Any]]) -> None:
    if len(hypotheses) > 8:
        raise ValueError("more than eight primary hypotheses")
    required = {"id", "family", "feature", "direction", "horizon", "expected_sign", "rationale", "minimum_sample", "contradiction_rule", "analysis_family"}
    for hypothesis in hypotheses:
        if set(hypothesis) < required or hypothesis["analysis_family"] != "PRIMARY_PREREGISTERED":
            raise ValueError("invalid primary hypothesis")


def split_analysis_families(rows: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    primary = [row for row in rows if row.get("analysis_family") == "PRIMARY_PREREGISTERED"]
    exploratory = [row for row in rows if row.get("analysis_family") == "SECONDARY_EXPLORATORY"]
    if len(primary) + len(exploratory) != len(rows):
        raise ValueError("unknown analysis family")
    return primary, exploratory


def write_atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n"); temporary.replace(path)


def observation_dicts(values: Sequence[Any]) -> list[dict[str, Any]]:
    return [asdict(value) for value in values]
