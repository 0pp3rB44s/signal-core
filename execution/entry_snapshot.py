"""Immutable pre-entry snapshot: what the bot knew, in units it can defend.

Research only. Nothing here gates, sizes, prices or rejects anything, and a
failure to build a field yields ``None`` rather than an exception, so a missing
research value can never stop a trade that production would otherwise take.

WHY THE VOLATILITY FIELDS LOOK REDUNDANT
----------------------------------------
They are not redundant, they are a disambiguation.

``market_features.engine.volatility_rank`` is::

    atr * 100.0 if atr <= 5.0 else atr, clamped to [0, 100]

with ``atr_percent = (average_range / close) * 100``. So for every value this
system has ever produced, ``volatility_rank`` **is ATR expressed in basis
points**, clamped at 100 — not a percentile, despite the name. Observed range
over 3173 scans: p25 11.2, median 18.2, max 51.0, i.e. 11-51 bps of ATR.

That matters because thresholds were set against it as if it ranked. The
strategy gate ``LOW_VOL_MAX_RANK = 55.0`` reads as "below the 55th percentile"
and means "ATR below 55 bps", which rejected 0 of 3173 observed candidates.

B1 does not touch any of that. It records ``atr_percent_raw`` and ``atr_bps``
alongside the legacy value so a later release can re-threshold against a unit
that says what it is, and so the two can be proven identical rather than
assumed.
"""

from __future__ import annotations

from typing import Any

#: Stated on every snapshot so a reader never has to infer it.
VOLATILITY_RANK_LEGACY_SEMANTICS = (
    "ATR-percent transformed by legacy volatility_rank(), NOT percentile rank. "
    "Numerically equals atr_bps clamped to [0, 100]."
)

#: No estimator exists yet. Recorded explicitly so its absence is data.
EXPECTED_MOVE_MODEL_VERSION = "NONE"


def _number(value: Any) -> float | None:
    """A finite number, or None. Never a substituted zero."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and result not in (float("inf"), float("-inf")) else None


def atr_bps_from_percent(atr_percent: Any) -> float | None:
    """ATR in basis points from ATR in percent.

    ``atr_percent`` is ``(average_range / close) * 100``, so 0.18 means 0.18%
    of price, which is 18 bps. One multiplication, stated here once so no
    caller has to rediscover the unit.
    """
    percent = _number(atr_percent)
    if percent is None or percent < 0:
        return None
    return round(percent * 100.0, 4)


def volatility_observability(market: Any) -> dict[str, Any]:
    """Raw volatility inputs plus the legacy value, with its meaning attached."""
    primary = getattr(market, "primary", None)
    atr_percent = _number(getattr(primary, "atr_percent", None))
    return {
        "atr_percent_raw": atr_percent,
        "atr_bps": atr_bps_from_percent(atr_percent),
        "volatility_rank_legacy": _number(getattr(market, "volatility_rank", None)),
        "volatility_rank_legacy_semantics": VOLATILITY_RANK_LEGACY_SEMANTICS,
    }


def economic_hurdle_observability(
    plan_notes: dict[str, Any],
    *,
    historical_execution_drag_bps: float | None = None,
) -> dict[str, Any]:
    """The gate as it stands today, plus the quantities needed to judge it.

    The production gate compares ``tp1_move_bps`` -- the distance to a target
    the planner chose -- against costs. It never asks whether the market is
    likely to travel that far, and its cost budget omits execution drag
    entirely. Both the current numbers and the fuller cost picture are recorded
    so the comparison can be made later without changing anything now.
    """
    tp1_move = _number(plan_notes.get("tp1_move_bps"))
    spread = _number(plan_notes.get("spread_bps"))
    fee = _number(plan_notes.get("estimated_roundtrip_fee_bps"))
    buffer_bps = _number(plan_notes.get("minimum_net_edge_buffer_bps"))
    minimum = _number(plan_notes.get("minimum_tp1_move_bps"))

    drag = _number(historical_execution_drag_bps)
    all_in = None
    if spread is not None and fee is not None:
        all_in = round(spread + fee + (drag or 0.0), 4)

    return {
        # exactly what production evaluates today
        "tp1_move_bps": tp1_move,
        "spread_bps": spread,
        "estimated_roundtrip_fee_bps": fee,
        "minimum_net_edge_buffer_bps": buffer_bps,
        "minimum_tp1_move_bps": minimum,
        "hurdle_margin_bps": (
            round(tp1_move - minimum, 4)
            if tp1_move is not None and minimum is not None else None
        ),
        # research only; nothing reads these
        "historical_expected_execution_drag_bps": drag,
        "estimated_all_in_cost_bps": all_in,
        "expected_favorable_move_bps": None,
        "expected_move_model_version": EXPECTED_MOVE_MODEL_VERSION,
    }


#: Availability contract per field, so missingness is interpretable.
FIELD_AVAILABILITY: dict[str, str] = {
    "strategy": "ALWAYS_AVAILABLE",
    "side": "ALWAYS_AVAILABLE",
    "symbol": "ALWAYS_AVAILABLE",
    "score": "ALWAYS_AVAILABLE",
    "planned_entry": "ALWAYS_AVAILABLE",
    "planned_tp1": "ALWAYS_AVAILABLE",
    "planned_sl": "ALWAYS_AVAILABLE",
    "tp1_move_bps": "ALWAYS_AVAILABLE",
    "sl_distance_bps": "DERIVED",
    "minimum_tp1_move_bps": "ALWAYS_AVAILABLE",
    "estimated_roundtrip_fee_bps": "ALWAYS_AVAILABLE",
    "atr_percent_raw": "CONDITIONALLY_AVAILABLE",
    "atr_bps": "DERIVED",
    "volatility_rank_legacy": "ALWAYS_AVAILABLE",
    "entry_quality_directional": "CONDITIONALLY_AVAILABLE",
    "pressure_score": "CONDITIONALLY_AVAILABLE",
    "expansion_probability": "CONDITIONALLY_AVAILABLE",
    "volume_ratio": "CONDITIONALLY_AVAILABLE",
    "spread_bps": "CONDITIONALLY_AVAILABLE",
    "mtf_alignment": "ALWAYS_AVAILABLE",
    "primary_trend": "ALWAYS_AVAILABLE",
    "confirmation_trend": "ALWAYS_AVAILABLE",
    "candidate_rank": "CONDITIONALLY_AVAILABLE",
    "portfolio_winner_rank": "CONDITIONALLY_AVAILABLE",
    "expected_favorable_move_bps": "UNKNOWN_ALLOWED",
    "historical_expected_execution_drag_bps": "UNKNOWN_ALLOWED",
    "realized_volatility_bps_lookback": "UNKNOWN_ALLOWED",
    "recent_range_bps": "UNKNOWN_ALLOWED",
    "recent_impulse_bps": "UNKNOWN_ALLOWED",
}


def missingness(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Which declared fields are absent, split by how bad that is.

    ``UNKNOWN_ALLOWED`` fields are expected to be missing in B1 -- there is no
    estimator behind them yet -- so they are counted separately. Only a missing
    ALWAYS_AVAILABLE field indicates a real capture defect.
    """
    unexpected: list[str] = []
    expected: list[str] = []
    for field, availability in FIELD_AVAILABILITY.items():
        if snapshot.get(field) is not None:
            continue
        if availability in ("UNKNOWN_ALLOWED", "CONDITIONALLY_AVAILABLE"):
            expected.append(field)
        else:
            unexpected.append(field)
    declared = len(FIELD_AVAILABILITY)
    return {
        "declared_fields": declared,
        "missing_expected": sorted(expected),
        "missing_unexpected": sorted(unexpected),
        "completeness_pct": round((declared - len(expected) - len(unexpected)) / declared * 100, 1),
    }
