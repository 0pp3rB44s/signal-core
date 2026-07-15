from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any


def canonical_timestamp_utc(value: datetime | int | float | str) -> str:
    """Return one millisecond UTC representation for candidate candle opens."""
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            raise ValueError("candidate timestamp datetime must be timezone-aware")
    elif isinstance(value, (int, float)) or str(value).strip().lstrip("-").isdigit():
        numeric = float(value)
        milliseconds = numeric * 1000.0 if abs(numeric) < 100_000_000_000 else numeric
        parsed = datetime.fromtimestamp(milliseconds / 1000.0, tz=timezone.utc)
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            raise ValueError("candidate timestamp string must include a timezone")
    normalized = parsed.astimezone(timezone.utc)
    return normalized.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical_json(fields: list[tuple[str, Any]]) -> str:
    # A list of pairs makes field order part of the explicit identity contract.
    return json.dumps(fields, ensure_ascii=False, separators=(",", ":"))


def deterministic_candidate_id(
    strategy: str,
    symbol: str,
    direction: str,
    candidate_candle_open_timestamp: datetime | int | float | str,
) -> str:
    material = _canonical_json([
        ("strategy", str(strategy).strip().lower()),
        ("symbol", str(symbol).strip().upper()),
        ("direction", str(direction).strip().upper()),
        ("candidate_candle_open_timestamp_utc", canonical_timestamp_utc(candidate_candle_open_timestamp)),
    ])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def lifecycle_key(candidate_id: str, event_type: str) -> str:
    if not candidate_id or not event_type:
        raise ValueError("candidate_id and event_type are required")
    return f"{candidate_id}:{str(event_type).strip().upper()}"


def deterministic_plan_id(candidate_id: str) -> str:
    if not candidate_id:
        raise ValueError("candidate_id is required")
    return "plan_" + hashlib.sha256(f"candidate-plan:{candidate_id}".encode("utf-8")).hexdigest()[:24]
