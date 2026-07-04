

"""Detect high-level patterns from the Learning Engine knowledge base."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

BASE_PATH = Path(__file__).resolve().parents[2]
LEARNING_PATH = BASE_PATH / "agents_v2" / "reports" / "learning.json"
OUTPUT_PATH = BASE_PATH / "agents_v2" / "reports" / "patterns.json"

# Candidate filtering constants
MIN_TRADES = 3
EXCLUDED_STRATEGIES = {"dataset_write_test"}
EXCLUDED_SYMBOLS = {"TESTUSDT"}


def _load_learning() -> dict[str, Any]:
    if not LEARNING_PATH.exists():
        return {}
    return json.loads(LEARNING_PATH.read_text(encoding="utf-8"))


def detect_patterns(learning: dict[str, Any]) -> dict[str, Any]:
    strategies = learning.get("strategies", {})
    symbols = learning.get("symbols", {})

    def eligible_items(items: dict[str, Any], excluded: set[str]):
        return {
            name: stats
            for name, stats in items.items()
            if name not in excluded and stats.get("trades", 0) >= MIN_TRADES
        }

    strategy_pool = eligible_items(strategies, EXCLUDED_STRATEGIES)
    symbol_pool = eligible_items(symbols, EXCLUDED_SYMBOLS)

    def best(pool: dict[str, Any], key: str):
        if not pool:
            return None
        return max(pool.items(), key=lambda x: x[1].get(key, float("-inf")))

    def worst(pool: dict[str, Any], key: str):
        if not pool:
            return None
        return min(pool.items(), key=lambda x: x[1].get(key, float("inf")))

    best_strategy = best(strategy_pool, "expectancy")
    worst_strategy = worst(strategy_pool, "expectancy")
    best_symbol = best(symbol_pool, "expectancy")
    worst_symbol = worst(symbol_pool, "expectancy")

    strengths = []
    weaknesses = []
    warnings = []
    recommendations = []

    # Rule 1: Best strategy
    if best_strategy:
        strengths.append(f"Best strategy by expectancy: {best_strategy[0]}")
    # Rule 2: Worst strategy
    if worst_strategy:
        weaknesses.append(f"Worst strategy by expectancy: {worst_strategy[0]}")
    # Rule 3: Best symbol
    if best_symbol:
        strengths.append(f"Best symbol by expectancy: {best_symbol[0]}")
    # Rule 4: Worst symbol
    if worst_symbol:
        weaknesses.append(f"Worst symbol by expectancy: {worst_symbol[0]}")

    # Rule 5: Loop through all strategies in strategy_pool
    for name, stats in strategy_pool.items():
        pf = stats.get("profit_factor")
        exp = stats.get("expectancy")
        if pf is not None and pf < 1.0:
            warnings.append(f"Review strategy {name}: profit factor below 1.0.")
        if exp is not None and exp < 0:
            recommendations.append(f"Reduce exposure to {name} until edge improves.")

    report = {
        "version": 2,
        "filters": {
            "min_trades": MIN_TRADES,
            "excluded_strategies": sorted(EXCLUDED_STRATEGIES),
            "excluded_symbols": sorted(EXCLUDED_SYMBOLS),
        },
        "best_strategy": {"name": best_strategy[0], "stats": best_strategy[1]} if best_strategy else None,
        "worst_strategy": {"name": worst_strategy[0], "stats": worst_strategy[1]} if worst_strategy else None,
        "best_symbol": {"name": best_symbol[0], "stats": best_symbol[1]} if best_symbol else None,
        "worst_symbol": {"name": worst_symbol[0], "stats": worst_symbol[1]} if worst_symbol else None,
    }
    report["diagnosis"] = {
        "strengths": strengths,
        "weaknesses": weaknesses,
        "warnings": warnings,
        "recommendations": recommendations,
    }
    return report


def run() -> dict[str, Any]:
    learning = _load_learning()
    patterns = detect_patterns(learning)
    OUTPUT_PATH.write_text(json.dumps(patterns, indent=2), encoding="utf-8")
    return patterns


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))