

"""Decision rules built on top of the Learning Engine."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agents_v2.learning.learning_service import learning_service

BASE_PATH = Path(__file__).resolve().parents[2]
OUTPUT_PATH = BASE_PATH / "agents_v2" / "reports" / "coach_decisions.json"


def _decision(level: str, action: str, reason: str, target: str | None = None) -> dict[str, Any]:
    return {
        "level": level,
        "action": action,
        "target": target,
        "reason": reason,
    }


def build_coach_decisions(summary: dict[str, Any]) -> dict[str, Any]:
    """Convert detected learning patterns into actionable decisions."""
    decisions: list[dict[str, Any]] = []

    diagnosis = summary.get("diagnosis", {})
    recommendations = diagnosis.get("recommendations", [])
    warnings = diagnosis.get("warnings", [])

    worst_strategy = summary.get("worst_strategy") or {}
    worst_symbol = summary.get("worst_symbol") or {}
    best_strategy = summary.get("best_strategy") or {}
    best_symbol = summary.get("best_symbol") or {}

    if worst_strategy:
        name = worst_strategy.get("name")
        stats = worst_strategy.get("stats", {})
        if stats.get("expectancy", 0) < 0:
            decisions.append(
                _decision(
                    "danger",
                    "reduce_strategy_exposure",
                    f"Negative expectancy detected for strategy {name}.",
                    name,
                )
            )

    if worst_symbol:
        name = worst_symbol.get("name")
        stats = worst_symbol.get("stats", {})
        if stats.get("expectancy", 0) < 0:
            decisions.append(
                _decision(
                    "warning",
                    "avoid_symbol_until_improved",
                    f"Negative expectancy detected for symbol {name}.",
                    name,
                )
            )

    if best_strategy:
        name = best_strategy.get("name")
        decisions.append(
            _decision(
                "info",
                "monitor_best_strategy",
                f"Best strategy by current learning data is {name}.",
                name,
            )
        )

    if best_symbol:
        name = best_symbol.get("name")
        decisions.append(
            _decision(
                "info",
                "watchlist_positive_symbol",
                f"Best symbol by current learning data is {name}. Treat as watchlist-positive, not automatic entry approval.",
                name,
            )
        )

    for warning in warnings:
        decisions.append(_decision("warning", "review_warning", warning))

    for recommendation in recommendations:
        decisions.append(_decision("info", "learning_recommendation", recommendation))

    return {
        "version": 1,
        "decision_count": len(decisions),
        "decisions": decisions,
    }


def run() -> dict[str, Any]:
    learning_service.reload()
    summary = learning_service.get_summary()
    report = build_coach_decisions(summary)
    OUTPUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))