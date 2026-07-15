from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings
from backtesting.backtest_engine import BacktestEngine
from risk.historical_policy import HistoricalProxyConfig, ResearchRiskMode
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


def _safe_div(value: float, divisor: float) -> float:
    return value / divisor if divisor else 0.0


def _max_streak(values: list[float], positive: bool) -> int:
    longest = current = 0
    for value in values:
        matched = value > 0 if positive else value < 0
        current = current + 1 if matched else 0
        longest = max(longest, current)
    return longest


def summarize_strategy(strategy: str, signals: int, records: list[dict[str, Any]], trades: list[dict[str, Any]], starting_equity: float, coverage: str) -> dict[str, Any]:
    strategy_records = [row for row in records if row["strategy"] == strategy]
    strategy_trades = [row for row in trades if row["strategy"] == strategy]
    closed_keys = {(row["symbol"], row["signal_timestamp"]) for row in strategy_trades}
    closed_records = [row for row in strategy_records if (row["symbol"], row["signal_timestamp"]) in closed_keys]
    pnl = [float(row["net_pnl"]) for row in closed_records]
    wins = [value for value in pnl if value > 0]
    losses = [value for value in pnl if value < 0]
    gross_profit, gross_loss = sum(wins), -sum(losses)
    equity = starting_equity
    peak = equity
    max_dd = 0.0
    largest_loss = 0.0
    for value in pnl:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        largest_loss = min(largest_loss, value)
    realised_r = [float(row["r_multiple"]) for row in closed_records]
    downside = [value for value in pnl if value < 0]
    downside_deviation = statistics.pstdev(downside) if len(downside) > 1 else (abs(downside[0]) if downside else 0.0)
    total_net = sum(pnl)
    filled = sum(row["fill_status"] == "FILLED" for row in strategy_records)
    rejected = sum(row["fill_status"] == "REJECTED" for row in strategy_records)
    unfilled = sum(row["fill_status"] == "UNFILLED" for row in strategy_records)
    ambiguous = sum(bool(row["intrabar_ambiguous"]) for row in closed_records)
    count = len(closed_records)
    fees = sum(float(row["total_fees"]) for row in closed_records)
    spread = sum(float(row["spread_cost"]) for row in closed_records)
    slippage = sum(float(row["entry_slippage"]) + float(row["exit_slippage"]) for row in closed_records)
    full_targets = sum(row["final_exit_reason"] in {"FINAL_TARGET", "TAKE_PROFIT"} for row in closed_records)
    full_stops = sum(row["final_exit_reason"] == "STOP_LOSS" for row in closed_records)
    be_after_tp1 = sum(row["final_exit_reason"] == "BREAK_EVEN_STOP" and float(row["tp1_quantity"]) > 0 for row in closed_records)
    total_return = _safe_div(total_net, starting_equity) * 100
    pf = _safe_div(gross_profit, gross_loss)
    quality = sample_quality(count)
    if count < 30:
        verdict = "INSUFFICIENT DATA"
    elif total_net <= 0 and sum(float(row["gross_pnl"]) for row in closed_records) > 0:
        verdict = "COST-LIMITED"
    elif total_net <= 0 or pf <= 1:
        verdict = "NO EDGE"
    elif count < 100:
        verdict = "DIAGNOSE FURTHER"
    else:
        verdict = "CANDIDATE FOR PHASE 3"
    row = _empty_summary(strategy, signals, starting_equity, coverage)
    row.update({
        "candidates_accepted": len(strategy_records), "orders_filled": filled,
        "unfilled_orders": unfilled, "rejected_orders": rejected, "closed_trades": count,
        "open_unresolved_trades": len(strategy_records) - count - rejected - unfilled,
        "gross_pnl": sum(float(item["gross_pnl"]) for item in closed_records), "fees": fees,
        "spread_impact": spread, "slippage_impact": slippage, "net_pnl": total_net,
        "total_return_pct": total_return, "ending_equity": starting_equity + total_net,
        "max_drawdown": max_dd, "max_drawdown_pct": _safe_div(max_dd, peak) * 100,
        "profit_factor": pf, "expectancy_per_trade": _safe_div(total_net, count),
        "expectancy_r": _safe_div(sum(realised_r), count), "win_rate": _safe_div(len(wins), count),
        "average_win": _safe_div(sum(wins), len(wins)), "average_loss": _safe_div(sum(losses), len(losses)),
        "payoff_ratio": _safe_div(_safe_div(sum(wins), len(wins)), abs(_safe_div(sum(losses), len(losses)))),
        "median_trade": statistics.median(pnl) if pnl else 0, "best_trade": max(pnl, default=0),
        "worst_trade": min(pnl, default=0),
        "average_holding_candles": _safe_div(sum(int(item["candles_held"]) for item in strategy_trades), count),
        "median_holding_candles": statistics.median([int(item["candles_held"]) for item in strategy_trades]) if strategy_trades else 0,
        "maximum_consecutive_wins": _max_streak(pnl, True), "maximum_consecutive_losses": _max_streak(pnl, False),
        "fill_rate": _safe_div(filled, len(strategy_records)), "rejection_rate": _safe_div(rejected, len(strategy_records)),
        "limit_expiration_rate": _safe_div(unfilled, len(strategy_records)),
        "ambiguous_intrabar_count": ambiguous, "ambiguous_intrabar_pct": _safe_div(ambiguous, count) * 100,
        "fees_pct_gross_profit": _safe_div(fees, gross_profit) * 100, "costs_per_trade": _safe_div(fees + spread + slippage, count),
        "average_adverse_entry_adjustment": _safe_div(sum(float(item["spread_cost"]) + float(item["entry_slippage"]) for item in closed_records), count),
        "tp1_hit_rate": _safe_div(sum(float(item["tp1_quantity"]) > 0 for item in closed_records), count),
        "tp1_break_even_rate": _safe_div(be_after_tp1, count), "full_target_rate": _safe_div(full_targets, count),
        "full_stop_rate": _safe_div(full_stops, count), "largest_equity_loss": largest_loss,
        "average_risk_budget": _safe_div(sum(float(item["risk_budget"]) for item in closed_records), count),
        "average_executable_notional": _safe_div(sum(float(item["notional"]) for item in closed_records), count),
        "average_realised_r": _safe_div(sum(realised_r), count), "downside_deviation": downside_deviation,
        "recovery_factor": _safe_div(total_net, max_dd), "return_drawdown_ratio": _safe_div(total_return, _safe_div(max_dd, peak) * 100),
        "independent_trading_days": len({datetime.fromtimestamp(int(item["signal_timestamp"]) / 1000, tz=timezone.utc).date().isoformat() for item in closed_records if item["signal_timestamp"]}),
        "sample_quality": quality, "verdict": verdict,
    })
    return row


def cost_sensitivity(strategy: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    closed = [row for row in records if row["strategy"] == strategy and row["final_exit_reason"] not in {"", "OPEN_AT_DATA_END"}]
    zero_cost = sum(float(row["net_pnl"]) + float(row["total_fees"]) + float(row["spread_cost"]) + float(row["entry_slippage"]) + float(row["exit_slippage"]) for row in closed)
    baseline_cost = sum(float(row["total_fees"]) + float(row["spread_cost"]) + float(row["entry_slippage"]) + float(row["exit_slippage"]) for row in closed)
    rows = []
    for multiplier in (0.0, 1.0, 1.25, 1.5, 2.0):
        net = zero_cost - baseline_cost * multiplier
        classification = "NO_GROSS_EDGE" if zero_cost <= 0 else "COST_LIMITED" if net <= 0 else "PROFITABLE"
        rows.append({"strategy": strategy, "cost_multiplier": multiplier, "trades": len(closed), "gross_pnl": zero_cost, "costs": baseline_cost * multiplier, "net_pnl": net, "classification": classification})
    return rows


def outlier_summary(strategy: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    pnl = sorted(float(row["net_pnl"]) for row in records if row["strategy"] == strategy and row["final_exit_reason"] not in {"", "OPEN_AT_DATA_END"})
    total = sum(pnl)
    n = len(pnl)
    top5 = max(1, math.ceil(n * 0.05)) if n else 0
    top10 = max(1, math.ceil(n * 0.10)) if n else 0
    losses = [value for value in pnl if value < 0]
    return {
        "strategy": strategy, "trades": n, "pnl_without_best": total - sum(pnl[-1:]),
        "pnl_without_best_three": total - sum(pnl[-3:]), "pnl_without_best_5pct": total - sum(pnl[-top5:]) if top5 else total,
        "pnl_without_worst": total - sum(pnl[:1]), "best_trade_contribution_pct": _safe_div(sum(pnl[-1:]), total) * 100,
        "best_three_contribution_pct": _safe_div(sum(pnl[-3:]), total) * 100,
        "best_10pct_contribution_pct": _safe_div(sum(pnl[-top10:]), total) * 100 if top10 else 0,
        "worst_10pct_loss_pct": _safe_div(abs(sum(losses[:max(1, math.ceil(len(losses) * .1))])), abs(sum(losses))) * 100 if losses else 0,
        "longest_losing_period": None, "best_month_pnl_pct": None, "best_symbol_pnl_pct": None,
        "best_direction_pnl_pct": None, "outlier_flag": "INSUFFICIENT_DATA" if n < 30 else ("DEPENDENT" if total > 0 and total - sum(pnl[-top5:]) <= 0 else "NOT_DEPENDENT"),
    }


def build_funnel_rows(debug: dict[str, Any], summaries: list[dict[str, Any]], candidate_keys: dict[str, str]) -> list[dict[str, Any]]:
    snapshots = int(debug.get("snapshots_evaluated", 0))
    rows = []
    for inventory in STRATEGIES:
        true_count = int(debug.get(candidate_keys[inventory["strategy"]], 0))
        selected = int(debug.get(f"selected_strategy::{inventory['strategy']}", 0))
        summary = next(item for item in summaries if item["strategy"] == inventory["strategy"])
        enabled = inventory["lifecycle"] != "experimental-disabled-fallback"
        rows.append({
            "strategy": inventory["strategy"], "snapshots_evaluated": snapshots,
            "detector_invoked": snapshots if enabled else 0,
            "raw_detector_false": snapshots - true_count if enabled else 0,
            "raw_detector_true": true_count, "unattributed_detector_false": snapshots - true_count if enabled else 0,
            "candidate_produced": true_count, "selector_accepted": selected,
            "risk_accepted": summary["candidates_accepted"],
            "planner_accepted": "NOT_EVALUATED_BY_FROZEN_BACKTEST_PATH",
            "order_produced": summary["candidates_accepted"], "order_filled": summary["orders_filled"],
            "trade_closed": summary["closed_trades"],
            "main_rejection_reason": (
                "DISABLED_FALLBACK" if not enabled else
                "DOWNSTREAM_RISK_BLOCKED_ORDERBOOK_RISK_OFF" if selected and not summary["candidates_accepted"] else
                "UNATTRIBUTED_DETECTOR_FALSE"
            ),
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-dir", type=Path, default=Path("data/backtests"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--execution-commit", required=True)
    parser.add_argument("--analysis-tooling-commit", required=True)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--dataset-hash", required=True)
    parser.add_argument(
        "--research-risk-mode",
        choices=[mode.value for mode in ResearchRiskMode],
        default=ResearchRiskMode.PRODUCTION.value,
    )
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
    risk_mode = ResearchRiskMode(args.research_risk_mode)
    proxy_config = HistoricalProxyConfig()
    engine = BacktestEngine(
        settings,
        research_risk_mode=risk_mode,
        historical_proxy_config=proxy_config,
    )
    result = engine.run(market_data)
    records = list(result["execution_records"])
    trades = list(result.get("trade_log", []))
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
        "execution_commit": args.execution_commit,
        "analysis_tooling_commit": args.analysis_tooling_commit,
        "dataset_version": args.dataset_version, "dataset_hash": args.dataset_hash,
        "dataset_source": str(args.dataset_dir), "symbols": sorted(market_data),
        "source_timeframe": "15m", "confirmation_timeframe": "1h aggregated",
        "date_range": {"start": start_iso, "end": end_iso},
        "candle_counts": {row["symbol"]: row["usable_candles"] for row in quality},
        "warmup_policy": "fail each snapshot closed until shared 20x1h contract is valid",
        "missing_data_policy": "fail closed; gaps reported, never fabricated",
        "duplicate_policy": "report and deterministically keep last row per timestamp",
        "timezone": "UTC", "strategy_versions": {row["strategy"]: args.execution_commit for row in STRATEGIES},
        "execution_assumptions": assumptions, "random_seed": None,
        "research_risk_mode": risk_mode.value,
        "historical_proxy": {
            "configuration": proxy_config.canonical_payload(),
            "configuration_hash": proxy_config.configuration_hash,
            "activation": "explicit CLI choice only; no Settings or environment binding",
        } if risk_mode is ResearchRiskMode.HISTORICAL_CONSERVATIVE_PROXY else None,
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
    summaries = [summarize_strategy(
        row["strategy"], int(debug.get(candidate_keys[row["strategy"]], 0)),
        records, trades, assumptions["starting_equity"], coverage,
    ) for row in STRATEGIES]
    _write_csv(args.output_dir / "strategy_summary.csv", summaries, SUMMARY_FIELDS)
    (args.output_dir / "strategy_summary.json").write_text(json.dumps(summaries, indent=2, sort_keys=True) + "\n")

    trade_fields = sorted({key for row in records for key in row}) or [
        "strategy", "symbol", "timeframe", "direction", "signal_timestamp", "fill_status", "net_pnl"
    ]
    _write_csv(args.output_dir / "trade_level.csv", records, trade_fields)
    breakdown_fields = ["strategy", "dimension", "value", "trades", "net_pnl", "sample_quality"]
    trade_lookup = {(row["strategy"], row["symbol"], row["signal_timestamp"]): row for row in trades}
    dimensions: dict[str, list[dict[str, Any]]] = {name: [] for name in ("direction", "symbol", "timeframe", "session_hour", "regime", "calendar")}
    grouped: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for record in records:
        if record["final_exit_reason"] in {"", "OPEN_AT_DATA_END"}:
            continue
        trade = trade_lookup.get((record["strategy"], record["symbol"], record["signal_timestamp"]), {})
        dt = datetime.fromtimestamp(int(record["signal_timestamp"]) / 1000, tz=timezone.utc)
        hour = dt.hour
        session = "ASIA" if hour < 8 else "EUROPE" if hour < 16 else "US"
        values = {
            "direction": record["direction"], "symbol": record["symbol"],
            "timeframe": record["timeframe"] or "15m", "session_hour": f"{hour:02d}:00|{session}",
            "regime": trade.get("regime", "UNAVAILABLE"), "calendar": f"{dt.year}-Q{(dt.month - 1) // 3 + 1}|{dt:%Y-%m}",
        }
        for dimension, value in values.items():
            grouped[(record["strategy"], dimension, value)].append(float(record["net_pnl"]))
    for (strategy, dimension, value), pnl_values in grouped.items():
        dimensions[dimension].append({"strategy": strategy, "dimension": dimension, "value": value, "trades": len(pnl_values), "net_pnl": sum(pnl_values), "sample_quality": sample_quality(len(pnl_values))})
    for filename, rows in dimensions.items():
        _write_csv(args.output_dir / f"breakdown_{filename}.csv", sorted(rows, key=lambda row: (row["strategy"], row["value"])), breakdown_fields)

    cost_rows = [item for row in STRATEGIES for item in cost_sensitivity(row["strategy"], records)]
    _write_csv(args.output_dir / "cost_sensitivity.csv", cost_rows, list(cost_rows[0]))
    outlier_rows = [outlier_summary(row["strategy"], records) for row in STRATEGIES]
    _write_csv(args.output_dir / "outlier_dependency.csv", outlier_rows, list(outlier_rows[0]))
    rejection_rows = []
    for strategy in (row["strategy"] for row in STRATEGIES):
        strategy_records = [row for row in records if row["strategy"] == strategy]
        rejection_rows.append({
            "strategy": strategy, "filled": sum(row["fill_status"] == "FILLED" for row in strategy_records),
            "unfilled": sum(row["fill_status"] == "UNFILLED" for row in strategy_records),
            "rejected": sum(row["fill_status"] == "REJECTED" for row in strategy_records),
            "open_unresolved": sum(row["final_exit_reason"] == "OPEN_AT_DATA_END" for row in strategy_records),
        })
    _write_csv(args.output_dir / "execution_rejections.csv", rejection_rows, list(rejection_rows[0]))
    diagnostics = {
        "engine_debug": debug, "engine_debug_by_symbol": result.get("debug_by_symbol", {}),
        "execution_record_count": len(records), "closed_trade_count": result["trades"],
        "starting_equity": result["starting_equity"], "ending_equity": result["ending_equity"],
        "result_hash": hashlib.sha256(json.dumps({"summaries": summaries, "debug": debug}, sort_keys=True).encode()).hexdigest(),
    }
    funnel_rows = build_funnel_rows(debug, summaries, candidate_keys)
    _write_csv(args.output_dir / "detector_funnel.csv", funnel_rows, list(funnel_rows[0]))
    (args.output_dir / "detector_funnel.json").write_text(json.dumps(funnel_rows, indent=2, sort_keys=True) + "\n")
    gate_decisions = list(result.get("gate_decisions", []))
    (args.output_dir / "risk_gate_decisions.json").write_text(json.dumps(gate_decisions, indent=2, sort_keys=True) + "\n")
    gate_fields = ["strategy", "symbol", "direction", "signal_timestamp", "risk_policy", "allowed", "reasons", "proxy_values"]
    _write_csv(args.output_dir / "risk_gate_decisions.csv", gate_decisions, gate_fields)
    (args.output_dir / "run_diagnostics.json").write_text(json.dumps(diagnostics, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
