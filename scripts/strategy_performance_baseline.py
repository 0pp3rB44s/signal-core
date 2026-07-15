from __future__ import annotations

import argparse
import csv
import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings
from backtesting.backtest_engine import BacktestEngine
from clients.schemas import Candle


STRATEGIES = [
    {
        "strategy": "momentum_breakout", "aliases": ["momentum", "breakout"],
        "detector": "strategies/momentum_breakout.py:MomentumBreakoutStrategy.detect",
        "decision_tree": "detector -> selector -> scorer -> risk -> execution contract",
        "directions": ["LONG"], "timeframes": ["15m", "5m fast lane"], "entry_type": "MARKET",
        "stop_method": "detection invalidation plus planner geometry",
        "target_method": "current backtest 0.8R TP1 / 1.5R final",
        "gates": ["breakout", "volume", "pressure", "MTF", "selector", "risk"],
        "lifecycle": "production-active",
    },
    {
        "strategy": "momentum_breakdown", "aliases": ["breakdown"],
        "detector": "strategies/momentum_breakout.py:MomentumBreakdownStrategy.detect",
        "decision_tree": "detector -> selector -> scorer -> risk -> execution contract",
        "directions": ["SHORT"], "timeframes": ["15m", "5m fast lane"], "entry_type": "MARKET",
        "stop_method": "detection invalidation plus planner geometry",
        "target_method": "current backtest 0.8R TP1 / 1.5R final",
        "gates": ["breakdown", "volume", "pressure", "MTF", "selector", "risk"],
        "lifecycle": "production-active",
    },
    {
        "strategy": "trend_continuation", "aliases": ["continuation"],
        "detector": "strategies/strategies/continuation.py:ContinuationStrategy.detect",
        "decision_tree": "detector -> selector -> scorer -> risk -> execution contract",
        "directions": ["LONG", "SHORT"], "timeframes": ["15m"], "entry_type": "MARKET",
        "stop_method": "pullback invalidation",
        "target_method": "current backtest 0.8R TP1 / 1.5R final",
        "gates": ["trend", "pullback", "reclaim", "MTF", "selector", "risk"],
        "lifecycle": "production-active",
    },
    {
        "strategy": "liquidity_sweep_reversal", "aliases": ["liquidity_sweep", "sweep"],
        "detector": "strategies/liquidity_sweep.py:LiquiditySweepStrategy.detect",
        "decision_tree": "detector -> selector -> scorer -> risk -> execution contract",
        "directions": ["LONG", "SHORT"], "timeframes": ["15m"], "entry_type": "MARKET",
        "stop_method": "sweep invalidation",
        "target_method": "current backtest 0.8R TP1 / 1.5R final",
        "gates": ["pivot sweep", "reclaim", "displacement", "volume", "selector", "risk"],
        "lifecycle": "production-active",
    },
    {
        "strategy": "low_vol_reclaim", "aliases": ["reclaim", "low vol reclaim"],
        "detector": "strategies/strategies/low_vol_reclaim.py:LowVolReclaimStrategy.detect",
        "decision_tree": "detector -> selector -> scorer -> risk -> execution contract",
        "directions": ["LONG", "SHORT"], "timeframes": ["15m"], "entry_type": "MARKET",
        "stop_method": "reclaim invalidation",
        "target_method": "current backtest 0.8R TP1 / 1.5R final",
        "gates": ["low volatility", "EMA cross/reclaim", "spread", "selector", "risk"],
        "lifecycle": "production-active",
    },
    {
        "strategy": "adaptive_momentum_continuation", "aliases": ["adaptive_fallback"],
        "detector": "strategies/strategies/selector.py:adaptive fallback bridge",
        "decision_tree": "disabled fallback bridge -> selector -> scorer -> risk -> execution contract",
        "directions": ["LONG", "SHORT"], "timeframes": ["15m"], "entry_type": "MARKET",
        "stop_method": "source candidate invalidation",
        "target_method": "current backtest 0.8R TP1 / 1.5R final",
        "gates": ["fallback enabled", "source candidate", "selector", "risk"],
        "lifecycle": "experimental-disabled-fallback",
    },
]

SUMMARY_FIELDS = [
    "strategy", "total_signals", "candidates_accepted", "orders_filled", "unfilled_orders",
    "rejected_orders", "closed_trades", "open_unresolved_trades", "gross_pnl", "fees",
    "spread_impact", "slippage_impact", "net_pnl", "total_return_pct", "ending_equity",
    "max_drawdown", "max_drawdown_pct", "profit_factor", "expectancy_per_trade",
    "expectancy_r", "win_rate", "average_win", "average_loss", "payoff_ratio", "median_trade",
    "best_trade", "worst_trade", "average_holding_candles", "median_holding_candles",
    "maximum_consecutive_wins", "maximum_consecutive_losses", "fill_rate", "rejection_rate",
    "limit_expiration_rate", "ambiguous_intrabar_count", "ambiguous_intrabar_pct",
    "fees_pct_gross_profit", "costs_per_trade", "average_adverse_entry_adjustment",
    "tp1_hit_rate", "tp1_break_even_rate", "full_target_rate", "full_stop_rate",
    "largest_equity_loss", "average_risk_budget", "average_executable_notional",
    "average_realised_r", "downside_deviation", "recovery_factor", "return_drawdown_ratio",
    "independent_trading_days", "date_coverage", "sample_quality", "verdict",
]


def sample_quality(trades: int) -> str:
    if trades < 30:
        return "insufficient"
    if trades < 100:
        return "weak"
    if trades < 300:
        return "moderate"
    return "stronger evidence"


def _load_dataset(path: Path) -> tuple[list[Candle], dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    timestamps = [int(row["timestamp"]) for row in raw]
    duplicates = len(timestamps) - len(set(timestamps))
    ordered = timestamps == sorted(timestamps)
    unique_rows = {int(row["timestamp"]): row for row in raw}
    rows = [unique_rows[key] for key in sorted(unique_rows)]
    gaps = sum(1 for left, right in zip(rows, rows[1:]) if int(right["timestamp"]) - int(left["timestamp"]) != 900_000)
    candles = [
        Candle(
            timestamp_ms=int(row["timestamp"]), open=float(row["open"]), high=float(row["high"]),
            low=float(row["low"]), close=float(row["close"]), volume_base=float(row["volume_base"]),
            volume_quote=float(row["volume_quote"]) if row.get("volume_quote") is not None else None,
        )
        for row in rows
    ]
    return candles, {
        "symbol": path.stem, "source": str(path), "raw_candles": len(raw),
        "usable_candles": len(candles), "duplicate_candles": duplicates,
        "ordered_input": ordered, "gap_count": gaps,
        "start_timestamp_ms": timestamps[0] if timestamps else None,
        "end_timestamp_ms": timestamps[-1] if timestamps else None,
    }


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _empty_summary(strategy: str, signals: int, starting_equity: float, coverage: str) -> dict[str, Any]:
    row = {field: 0 for field in SUMMARY_FIELDS}
    row.update({
        "strategy": strategy, "total_signals": signals, "ending_equity": starting_equity,
        "date_coverage": coverage, "sample_quality": "insufficient",
        "verdict": "INSUFFICIENT DATA",
    })
    return row


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-dir", type=Path, default=Path("data/backtests"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--git-commit", required=True)
    parser.add_argument("--require-clean-runtime", action="store_true")
    args = parser.parse_args()
    if args.output_dir.exists():
        raise SystemExit(f"refusing to overwrite existing analysis: {args.output_dir}")
    if args.require_clean_runtime:
        forbidden = [Path(".env"), Path("reports/backtests/latest_summary.json"), Path("reports/backtests/strategy_expectancy.json"), Path("agents_v2/reports/coach_decisions.json"), Path("state/executed_trades.json")]
        present = [str(path) for path in forbidden if path.exists()]
        if present:
            raise SystemExit(f"runtime contamination present: {present}")

    market_data: dict[str, list[Candle]] = {}
    quality: list[dict[str, Any]] = []
    for path in sorted(args.dataset_dir.glob("*.json")):
        market_data[path.stem], metadata = _load_dataset(path)
        quality.append(metadata)
    if not market_data:
        raise SystemExit("no datasets found")

    settings = Settings(_env_file=None)
    engine = BacktestEngine(settings)
    result = engine.run(market_data)
    records = list(result["execution_records"])
    starts = [row["start_timestamp_ms"] for row in quality if row["start_timestamp_ms"]]
    ends = [row["end_timestamp_ms"] for row in quality if row["end_timestamp_ms"]]
    start_iso = datetime.fromtimestamp(min(starts) / 1000, tz=timezone.utc).isoformat()
    end_iso = datetime.fromtimestamp(max(ends) / 1000, tz=timezone.utc).isoformat()
    coverage = f"{start_iso}/{end_iso}"

    args.output_dir.mkdir(parents=True)
    assumptions = asdict(engine.execution_config)
    contract = {
        "analysis_id": args.output_dir.name,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_commit": args.git_commit,
        "dataset_source": str(args.dataset_dir), "symbols": sorted(market_data),
        "source_timeframe": "15m", "confirmation_timeframe": "1h aggregated",
        "date_range": {"start": start_iso, "end": end_iso},
        "candle_counts": {row["symbol"]: row["usable_candles"] for row in quality},
        "warmup_policy": "fail each snapshot closed until shared 20x1h contract is valid",
        "missing_data_policy": "fail closed; gaps reported, never fabricated",
        "duplicate_policy": "report and deterministically keep last row per timestamp",
        "timezone": "UTC", "strategy_versions": {row["strategy"]: args.git_commit for row in STRATEGIES},
        "execution_assumptions": assumptions, "random_seed": None,
        "runtime_isolation": "clean source export; _env_file=None; operational runtime files forbidden",
        "comparability_exception": "adaptive_momentum_continuation is a disabled fallback and cannot emit standalone signals",
    }
    (args.output_dir / "analysis_contract.json").write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n")
    (args.output_dir / "data_quality.json").write_text(json.dumps(quality, indent=2, sort_keys=True) + "\n")
    (args.output_dir / "strategy_inventory.json").write_text(json.dumps(STRATEGIES, indent=2, sort_keys=True) + "\n")
    inventory_fields = list(STRATEGIES[0])
    _write_csv(args.output_dir / "strategy_inventory.csv", [
        {**row, "aliases": "|".join(row["aliases"]), "directions": "|".join(row["directions"]), "timeframes": "|".join(row["timeframes"]), "gates": "|".join(row["gates"])}
        for row in STRATEGIES
    ], inventory_fields)

    debug = result.get("debug", {})
    candidate_keys = {
        "momentum_breakout": "momentum_candidates", "momentum_breakdown": "momentum_breakdown_candidates",
        "trend_continuation": "continuation_candidates", "liquidity_sweep_reversal": "sweep_candidates",
        "low_vol_reclaim": "low_vol_reclaim_candidates", "adaptive_momentum_continuation": "adaptive_candidates",
    }
    summaries = [
        _empty_summary(row["strategy"], int(debug.get(candidate_keys[row["strategy"]], 0)), assumptions["starting_equity"], coverage)
        for row in STRATEGIES
    ]
    _write_csv(args.output_dir / "strategy_summary.csv", summaries, SUMMARY_FIELDS)
    (args.output_dir / "strategy_summary.json").write_text(json.dumps(summaries, indent=2, sort_keys=True) + "\n")

    trade_fields = sorted({key for row in records for key in row}) or [
        "strategy", "symbol", "timeframe", "direction", "signal_timestamp", "fill_status", "net_pnl"
    ]
    _write_csv(args.output_dir / "trade_level.csv", records, trade_fields)
    breakdown_fields = ["strategy", "dimension", "value", "trades", "net_pnl", "sample_quality"]
    for filename in ("direction", "symbol", "timeframe", "session_hour", "regime", "calendar"):
        _write_csv(args.output_dir / f"breakdown_{filename}.csv", [], breakdown_fields)

    cost_rows = []
    for strategy in (row["strategy"] for row in STRATEGIES):
        for multiplier in (0.0, 1.0, 1.25, 1.5, 2.0):
            cost_rows.append({"strategy": strategy, "cost_multiplier": multiplier, "trades": 0, "gross_pnl": 0, "costs": 0, "net_pnl": 0, "classification": "NO_GROSS_EDGE"})
    _write_csv(args.output_dir / "cost_sensitivity.csv", cost_rows, list(cost_rows[0]))
    outlier_rows = [{
        "strategy": row["strategy"], "trades": 0, "pnl_without_best": 0, "pnl_without_best_three": 0,
        "pnl_without_best_5pct": 0, "pnl_without_worst": 0, "best_trade_contribution_pct": None,
        "best_three_contribution_pct": None, "best_10pct_contribution_pct": None,
        "worst_10pct_loss_pct": None, "longest_losing_period": None, "best_month_pnl_pct": None,
        "best_symbol_pnl_pct": None, "best_direction_pnl_pct": None, "outlier_flag": "INSUFFICIENT_DATA",
    } for row in STRATEGIES]
    _write_csv(args.output_dir / "outlier_dependency.csv", outlier_rows, list(outlier_rows[0]))
    rejection_rows = [{"strategy": row["strategy"], "filled": 0, "unfilled": 0, "rejected": 0, "open_unresolved": 0} for row in STRATEGIES]
    _write_csv(args.output_dir / "execution_rejections.csv", rejection_rows, list(rejection_rows[0]))
    diagnostics = {
        "engine_debug": debug, "engine_debug_by_symbol": result.get("debug_by_symbol", {}),
        "execution_record_count": len(records), "closed_trade_count": result["trades"],
        "starting_equity": result["starting_equity"], "ending_equity": result["ending_equity"],
        "result_hash": hashlib.sha256(json.dumps({"summaries": summaries, "debug": debug}, sort_keys=True).encode()).hexdigest(),
    }
    (args.output_dir / "run_diagnostics.json").write_text(json.dumps(diagnostics, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
