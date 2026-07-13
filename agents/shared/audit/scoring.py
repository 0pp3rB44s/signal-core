

"""Overall audit scoring for the Audit Engine."""

from __future__ import annotations

from typing import Any


def _score(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _grade(score: int) -> str:
    if score >= 98:
        return "A+"
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    return "D"


def build_overall_score(
    trade_integrity: dict[str, Any],
    performance: dict[str, Any],
    runtime_health: dict[str, Any],
) -> dict[str, Any]:
    """Build one overall health score from all audit modules."""
    trade_score = _score(trade_integrity.get("score"), 0)
    runtime_score = _score(runtime_health.get("score"), 0)

    profit_factor = performance.get("profit_factor")
    expectancy = performance.get("expectancy")

    performance_score = 50
    try:
        if profit_factor is not None:
            pf = float(profit_factor)
            performance_score = min(100, max(0, int(pf * 50)))
        if expectancy is not None and float(expectancy) < 0:
            performance_score = min(performance_score, 60)
    except (TypeError, ValueError):
        performance_score = 50

    overall_score = round(
        trade_score * 0.30
        + performance_score * 0.45
        + runtime_score * 0.25
    )

    contributors = {
        "trade_integrity": trade_score,
        "performance": performance_score,
        "runtime_health": runtime_score,
    }

    weakest_module = min(contributors.items(), key=lambda item: item[1])[0]
    strongest_module = max(contributors.items(), key=lambda item: item[1])[0]

    primary_risk = "No dominant risk detected."
    if weakest_module == "performance":
        primary_risk = "Negative or weak trading expectancy."
    elif weakest_module == "runtime_health":
        primary_risk = f"Runtime instability: {runtime_health.get('dominant_issue') or 'unknown issue'}."
    elif weakest_module == "trade_integrity":
        primary_risk = "Dataset integrity issues reduce audit confidence."

    primary_strength = "No dominant strength detected."
    if strongest_module == "trade_integrity":
        primary_strength = "Dataset integrity is strong enough for reliable analysis."
    elif strongest_module == "runtime_health":
        primary_strength = "Runtime stability is currently the strongest module."
    elif strongest_module == "performance":
        primary_strength = "Trading performance is currently the strongest module."

    return {
        "overall_score": overall_score,
        "overall_grade": _grade(overall_score),
        "contributors": contributors,
        "weakest_module": weakest_module,
        "strongest_module": strongest_module,
        "primary_risk": primary_risk,
        "primary_strength": primary_strength,
    }