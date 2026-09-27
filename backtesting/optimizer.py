
from __future__ import annotations

import copy
import json
from itertools import product
from pathlib import Path
from typing import Any

from backtesting.backtest_engine import BacktestEngine


REPORT_PATH = Path("reports/backtests/optimizer_report.json")


PARAM_GRID = {
    "momentum_min_breakout_pct": [0.08, 0.10, 0.12, 0.15],
    "momentum_min_volume_ratio": [0.9, 1.0, 1.2, 1.4],
    "momentum_max_pullback_bps": [25, 35, 45, 60],
    "momentum_breakdown_min_breakdown_pct": [0.08, 0.10, 0.12, 0.15],
    "momentum_breakdown_min_volume_ratio": [0.9, 1.0, 1.2, 1.4],
    "momentum_breakdown_max_reclaim_bps": [25, 35, 45, 60],
}


def _score_result(result: dict[str, Any]) -> float:
    """Rank configs: profit first, but punish low sample size and poor winrate."""
    trades = float(result.get("trades", 0) or 0)
    pnl = float(result.get("pnl", 0.0) or 0.0)
    winrate = float(result.get("winrate", 0.0) or 0.0)

    if trades < 8:
        return -9999.0

    score = pnl
    score += winrate * 5.0
    score -= max(0.0, 12.0 - trades) * 0.5
    return round(score, 4)


def _apply_params(engine: BacktestEngine, params: dict[str, Any]) -> None:
    """Apply optimizer params to strategy instances only. Does not touch live settings."""
    engine.momentum.min_breakout_pct = params["momentum_min_breakout_pct"]
    engine.momentum.min_volume_ratio = params["momentum_min_volume_ratio"]
    engine.momentum.max_pullback_bps = params["momentum_max_pullback_bps"]

    engine.momentum_breakdown.min_breakdown_pct = params[
        "momentum_breakdown_min_breakdown_pct"
    ]
    engine.momentum_breakdown.min_volume_ratio = params[
        "momentum_breakdown_min_volume_ratio"
    ]
    engine.momentum_breakdown.max_reclaim_bps = params[
        "momentum_breakdown_max_reclaim_bps"
    ]


def _param_combinations() -> list[dict[str, Any]]:
    keys = list(PARAM_GRID.keys())
    combos = []
    for values in product(*(PARAM_GRID[k] for k in keys)):
        combos.append(dict(zip(keys, values)))
    return combos


def optimize(settings: Any, market_data: dict[str, Any], top_n: int = 10) -> dict[str, Any]:
    """Run a safe offline grid search and return ranked parameter suggestions."""
    results: list[dict[str, Any]] = []

    for params in _param_combinations():
        engine = BacktestEngine(settings=copy.deepcopy(settings))
        _apply_params(engine, params)
        result = engine.run(market_data)
        rank_score = _score_result(result)

        results.append(
            {
                "rank_score": rank_score,
                "params": params,
                "result": result,
            }
        )

    results.sort(key=lambda row: row["rank_score"], reverse=True)

    report = {
        "mode": "offline_optimizer",
        "warning": "Suggestions only. Do not auto-apply live without manual approval.",
        "tested_configs": len(results),
        "top": results[:top_n],
    }

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    return report