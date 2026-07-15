from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

from historical_data.bitget_archive import INTERVAL_MS, content_hash
from scripts.strategy_diagnosis import excursion, load_candles

SEED = 20260715
RESAMPLES = 5000
SWEEP = "liquidity_sweep_reversal"
EXPECTED_PROXY_HASH = "722bb6962e575931e5d4b2ee58ce175413729c587f9eed5a796b69930a349cbc"
REQUESTED_START_MS = 1721001600000
REQUESTED_END_MS = 1752537600000
PRIOR_START_MS = REQUESTED_END_MS


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = sorted({key for row in rows for key in row}) if rows else ["empty"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def validate_boundaries(manifest: dict[str, Any]) -> None:
    if manifest["exchange"] != "BITGET" or manifest["market_type"] != "USDT-FUTURES":
        raise ValueError("independent data must be Bitget USDT-FUTURES")
    if manifest["timeframe"] != "15m" or manifest["requested_start_ms"] != REQUESTED_START_MS:
        raise ValueError("independent data has wrong timeframe or start boundary")
    if manifest["requested_end_ms_exclusive"] != REQUESTED_END_MS:
        raise ValueError("independent data has wrong exclusive end boundary")
    if manifest["requested_end_ms_exclusive"] > PRIOR_START_MS:
        raise ValueError("independent and prior datasets overlap")


def validate_frozen_contract(independent: dict[str, Any], prior: dict[str, Any]) -> None:
    for contract in (independent, prior):
        proxy = contract.get("historical_proxy") or {}
        if proxy.get("configuration_hash") != EXPECTED_PROXY_HASH:
            raise ValueError("frozen proxy configuration hash changed")
    if independent["execution_assumptions"] != prior["execution_assumptions"]:
        raise ValueError("frozen execution assumptions changed between periods")


def build_universes(manifest: dict[str, Any]) -> dict[str, Any]:
    quality = manifest["quality"]
    available = [row for row in quality if row["candle_count"]]
    common_start = max(row["actual_first_ms"] for row in available)
    common_last = min(row["actual_last_ms"] for row in available)
    full_last = REQUESTED_END_MS - INTERVAL_MS
    core = sorted(row["symbol"] for row in available if row["actual_first_ms"] == REQUESTED_START_MS and row["actual_last_ms"] == full_last)
    return {
        "universe_a_all_symbol_common_window": {
            "symbols": sorted(row["symbol"] for row in available),
            "start_ms": common_start,
            "end_ms_inclusive": common_last,
        },
        "universe_b_core_full_year": {
            "symbols": core,
            "start_ms": REQUESTED_START_MS,
            "end_ms_inclusive": full_last,
        },
        "universe_c_per_symbol_maximum": {
            row["symbol"]: {"start_ms": row["actual_first_ms"], "end_ms_inclusive": row["actual_last_ms"]}
            for row in quality
        },
        "objective_exclusions_from_core": {
            row["symbol"]: "BITGET_FUTURES_HISTORY_UNAVAILABLE_FOR_FULL_REQUESTED_YEAR"
            for row in available if row["symbol"] not in core
        },
    }


def _closed(report: Path) -> list[dict[str, str]]:
    return [
        row for row in read_csv(report / "trade_level.csv")
        if row["strategy"] == SWEEP and row["fill_status"] == "FILLED"
        and row["final_exit_reason"] not in {"", "OPEN_AT_DATA_END"}
    ]


def funnel(report: Path, structural: Path, symbols: set[str] | None = None) -> dict[str, int]:
    events = read_json(report / "candidate_events.json")
    proxy = read_json(report / "risk_gate_decisions.json")
    structural_rows = read_json(structural / "risk_gate_decisions.json")
    allowed = lambda symbol: symbols is None or symbol in symbols
    records = [row for row in read_csv(report / "trade_level.csv") if row["strategy"] == SWEEP and allowed(row["symbol"])]
    detected = [(event, item) for event in events if allowed(event["symbol"]) for item in event["detectors"] if item["strategy"] == SWEEP]
    selected = [event for event in events if allowed(event["symbol"]) and event["selected_strategy"] == SWEEP]
    return {
        "snapshots": (int(read_json(report / "run_diagnostics.json")["engine_debug"].get("snapshots_evaluated", 0)) if symbols is None else sum(int(value.get("snapshots_evaluated", 0)) for symbol, value in read_json(report / "run_diagnostics.json").get("engine_debug_by_symbol", {}).items() if symbol in symbols)),
        "detector_hits": len(detected),
        "selected_candidates": len(selected),
        "selector_alignment_rejections": sum(event["selected_strategy"] != SWEEP for event, _ in detected),
        "structural_accepted": sum(row["strategy"] == SWEEP and allowed(row["symbol"]) and row["allowed"] for row in structural_rows),
        "entry_position_rejections": sum(row["strategy"] == SWEEP and allowed(row["symbol"]) and any("entry too" in reason.lower() and "candle" in reason.lower() for reason in row["reasons"]) for row in structural_rows),
        "proxy_accepted": sum(row["strategy"] == SWEEP and allowed(row["symbol"]) and row["allowed"] for row in proxy),
        "tp1_cost_floor_rejections": sum(row["strategy"] == SWEEP and allowed(row["symbol"]) and any("tp1" in reason.lower() for reason in row["reasons"]) for row in proxy),
        "execution_attempted": len(records),
        "rejected_orders": sum(row["fill_status"] == "REJECTED" for row in records),
        "fills": sum(row["fill_status"] == "FILLED" for row in records),
        "unresolved_trades": sum(row["final_exit_reason"] == "OPEN_AT_DATA_END" for row in records),
        "closed_trades": sum(allowed(row["symbol"]) for row in _closed(report)),
    }


def _max_drawdown(values: list[float], starting: float = 1000.0) -> float:
    equity = peak = starting
    maximum = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        maximum = max(maximum, peak - equity)
    return maximum


def _percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def statistics(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"trades": 0, "seed": SEED, "resamples": RESAMPLES, "mean_ci95": [0.0, 0.0], "profit_factor_ci95": None, "wilson_win_rate_ci95": [0.0, 0.0], "trade_order_ending_equity_ci95": [1000.0, 1000.0], "maximum_drawdown_ci95": [0.0, 0.0]}
    rng = random.Random(SEED)
    means, pfs = [], []
    for _ in range(RESAMPLES):
        sample = [values[rng.randrange(len(values))] for _ in values]
        means.append(mean(sample))
        gain = sum(max(0.0, value) for value in sample)
        loss = -sum(min(0.0, value) for value in sample)
        if loss:
            pfs.append(gain / loss)
    rng = random.Random(SEED)
    drawdowns, endings = [], []
    for _ in range(RESAMPLES):
        ordered = values.copy()
        rng.shuffle(ordered)
        endings.append(1000.0 + sum(ordered))
        drawdowns.append(_max_drawdown(ordered))
    wins, n = sum(value > 0 for value in values), len(values)
    z = 1.959963984540054
    center = (wins / n + z * z / (2 * n)) / (1 + z * z / n)
    margin = z * math.sqrt((wins / n * (1 - wins / n) + z * z / (4 * n)) / n) / (1 + z * z / n)
    return {
        "trades": n, "seed": SEED, "resamples": RESAMPLES,
        "mean_ci95": [_percentile(means, .025), _percentile(means, .975)],
        "profit_factor_ci95": [_percentile(pfs, .025), _percentile(pfs, .975)] if pfs else None,
        "wilson_win_rate_ci95": [max(0.0, center - margin), min(1.0, center + margin)],
        "trade_order_ending_equity_ci95": [_percentile(endings, .025), _percentile(endings, .975)],
        "maximum_drawdown_ci95": [_percentile(drawdowns, .025), _percentile(drawdowns, .975)],
    }


def performance(report: Path, canonical: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    rows = _closed(report)
    candles = load_candles(canonical)
    excursions = [excursion(row, candles[row["symbol"]]) for row in rows]
    values = [float(row["net_pnl"]) for row in rows]
    gross = sum(float(row["gross_pnl"]) for row in rows)
    fees = sum(float(row["total_fees"]) for row in rows)
    entry_spread = sum(float(row["spread_cost"]) for row in rows)
    entry_slippage = sum(float(row["entry_slippage"]) for row in rows)
    exit_adverse = sum(float(row["exit_slippage"]) for row in rows)
    price_gross = gross + entry_spread + entry_slippage + exit_adverse
    gains, losses = sum(max(0.0, value) for value in values), -sum(min(0.0, value) for value in values)
    wins, loss_values = [value for value in values if value > 0], [value for value in values if value < 0]
    r_values = [float(row["r_multiple"]) for row in rows]
    metric = {
        "closed_trades": len(rows), "gross_price_pnl": price_gross,
        "execution_adjusted_gross_pnl": gross, "entry_fees": sum(float(row["entry_fee"]) for row in rows),
        "exit_fees": fees - sum(float(row["entry_fee"]) for row in rows), "spread_impact": entry_spread + exit_adverse * 2 / 3,
        "slippage_impact": entry_slippage + exit_adverse / 3, "total_transaction_cost": entry_spread + entry_slippage + exit_adverse + fees,
        "net_pnl": sum(values), "total_return_pct": sum(values) / 1000 * 100, "ending_equity": 1000 + sum(values),
        "profit_factor": gains / losses if losses else None, "expectancy": mean(values) if values else 0.0,
        "expectancy_r": mean(r_values) if r_values else 0.0, "win_rate": len(wins) / len(values) if values else 0.0,
        "payoff_ratio": (mean(wins) / abs(mean(loss_values))) if wins and loss_values else None,
        "average_win": mean(wins) if wins else 0.0, "average_loss": mean(loss_values) if loss_values else 0.0,
        "median_trade": median(values) if values else 0.0, "best_trade": max(values, default=0.0), "worst_trade": min(values, default=0.0),
        "maximum_drawdown": _max_drawdown(values), "tp1_hit_rate": sum(float(row["tp1_quantity"]) > 0 for row in rows) / len(rows) if rows else 0.0,
        "break_even_exit_rate": sum(row["final_exit_reason"] == "BREAK_EVEN_STOP" for row in rows) / len(rows) if rows else 0.0,
        "final_target_rate": sum(row["final_exit_reason"] in {"FINAL_TARGET", "TAKE_PROFIT"} for row in rows) / len(rows) if rows else 0.0,
        "full_stop_rate": sum(row["final_exit_reason"] == "STOP_LOSS" for row in rows) / len(rows) if rows else 0.0,
        "average_mfe_r": mean([row["mfe_r"] for row in excursions]) if excursions else 0.0,
        "average_mae_r": mean([row["mae_r"] for row in excursions]) if excursions else 0.0,
        "adverse_first_pct": sum(row["adverse_first"] for row in excursions) / len(excursions) * 100 if excursions else 0.0,
        "average_holding_candles": mean([int(row["candles_held"]) for row in rows]) if rows else 0.0,
        "pnl_without_best": sum(values) - sum(sorted(values)[-1:]), "pnl_without_best_three": sum(values) - sum(sorted(values)[-3:]),
        "pnl_without_worst": sum(values) - sum(sorted(values)[:1]),
    }
    trades = [{**row, **next((item for item in excursions if item["symbol"] == row["symbol"] and item["signal_timestamp"] == int(row["signal_timestamp"])), {})} for row in rows]
    return metric, trades, excursions


def breakdowns(trades: list[dict[str, Any]], report: Path) -> list[dict[str, Any]]:
    regime_lookup = {(row["strategy"], row["symbol"], int(row["signal_timestamp"])): row.get("regime", "UNAVAILABLE") for row in read_json(report / "trade_log.json")}
    grouped: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in trades:
        timestamp = int(row["signal_timestamp"])
        dt = datetime.fromtimestamp(timestamp / 1000, timezone.utc)
        hour = dt.hour
        dimensions = {
            "direction": row["direction"], "symbol": row["symbol"], "month": dt.strftime("%Y-%m"),
            "utc_hour": f"{hour:02d}", "session": "ASIA" if hour < 8 else "EUROPE" if hour < 16 else "US",
            "regime": regime_lookup.get((SWEEP, row["symbol"], timestamp), "UNAVAILABLE"),
        }
        for dimension, value in dimensions.items():
            grouped[(dimension, value)].append(float(row["net_pnl"]))
    return [{"dimension": dimension, "value": value, "trades": len(values), "net_pnl": sum(values), "expectancy": mean(values), "tiny_sample": len(values) < 30} for (dimension, value), values in sorted(grouped.items())]


def combine_breakdowns(*periods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: {"trades": 0, "net_pnl": 0.0})
    for rows in periods:
        for row in rows:
            target = grouped[(row["dimension"], row["value"])]
            target["trades"] += int(row["trades"])
            target["net_pnl"] += float(row["net_pnl"])
    return [{"dimension": key[0], "value": key[1], **value, "expectancy": value["net_pnl"] / value["trades"] if value["trades"] else 0.0, "tiny_sample": value["trades"] < 30} for key, value in sorted(grouped.items())]


def classification(metric: dict[str, Any], uncertainty: dict[str, Any]) -> str:
    n = metric["closed_trades"]
    if n == 0:
        return "INCONCLUSIVE"
    if metric["gross_price_pnl"] < 0 or (n >= 30 and metric["expectancy"] < 0) or (metric["profit_factor"] is not None and metric["profit_factor"] < 1):
        return "FAILED INDEPENDENT VALIDATION"
    if metric["net_pnl"] > 0 and n < 30 and uncertainty["mean_ci95"][0] <= 0 <= uncertainty["mean_ci95"][1]:
        return "DIRECTIONALLY SUPPORTIVE BUT UNDERPOWERED"
    if n >= 30 and metric["expectancy"] > 0 and metric["profit_factor"] and metric["profit_factor"] > 1 and metric["gross_price_pnl"] > 0 and metric["pnl_without_best"] > 0:
        return "INDEPENDENTLY POSITIVE"
    return "INCONCLUSIVE"


def final_decision(independent: dict[str, Any], combined: dict[str, Any], breakdown: list[dict[str, Any]]) -> str:
    independent_class = independent["classification"]
    if independent_class == "FAILED INDEPENDENT VALIDATION":
        return "FAILED INDEPENDENT VALIDATION — REJECT CURRENT STRATEGY"
    if independent_class == "INCONCLUSIVE" and independent["performance"]["closed_trades"] < 10:
        return "INCONCLUSIVE DUE TO DATA AVAILABILITY"
    metric = combined["performance"]
    concentration = max((row["net_pnl"] for row in breakdown if row["dimension"] in {"symbol", "month"}), default=0.0)
    if (metric["closed_trades"] >= 30 and independent_class in {"INDEPENDENTLY POSITIVE", "DIRECTIONALLY SUPPORTIVE BUT UNDERPOWERED"}
            and metric["gross_price_pnl"] > 0 and metric["expectancy"] > 0 and metric["profit_factor"] and metric["profit_factor"] > 1
            and metric["pnl_without_best"] > 0 and metric["net_pnl"] > metric["total_transaction_cost"] * 0
            and (metric["net_pnl"] <= 0 or concentration < metric["net_pnl"] * .8)):
        return "PROMOTE TO PHASE 3 HYPOTHESIS TESTING"
    return "EXPAND SAMPLE AGAIN WITHOUT CHANGING LOGIC"


def artifact_hash(directory: Path) -> str:
    return hashlib.sha256(b"".join(path.read_bytes() for path in sorted(directory.iterdir()) if path.name != "artifact_hash.txt")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--availability", type=Path, required=True)
    parser.add_argument("--canonical", type=Path, required=True)
    parser.add_argument("--independent-proxy", type=Path, required=True)
    parser.add_argument("--independent-structural", type=Path, required=True)
    parser.add_argument("--prior-proxy", type=Path, required=True)
    parser.add_argument("--prior-structural", type=Path, required=True)
    parser.add_argument("--prior-canonical", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("refusing to overwrite Phase 2E output")
    manifest = read_json(args.manifest)
    validate_boundaries(manifest)
    independent_contract = read_json(args.independent_proxy / "analysis_contract.json")
    prior_contract = read_json(args.prior_proxy / "analysis_contract.json")
    validate_frozen_contract(independent_contract, prior_contract)
    args.output.mkdir(parents=True)
    universes = build_universes(manifest)
    write_json(args.output / "analysis_universes.json", universes)
    availability = read_json(args.availability)
    quality_by_symbol = {row["symbol"]: row for row in manifest["quality"]}
    availability_rows = []
    for contract in availability["contracts"]:
        quality = quality_by_symbol[contract["symbol"]]
        availability_rows.append({
            **contract, "requested_first_ms": REQUESTED_START_MS,
            "actual_first_ms": quality["actual_first_ms"], "actual_last_ms": quality["actual_last_ms"],
            "candle_count": quality["candle_count"], "gaps": quality["missing_count"],
            "duplicates": quality["duplicate_count"], "invalid_candles": quality["invalid_ohlc_count"],
        })
    write_json(args.output / "availability.json", availability_rows)
    independent_metric, independent_trades, _ = performance(args.independent_proxy, args.canonical)
    prior_metric, prior_trades, _ = performance(args.prior_proxy, args.prior_canonical)
    independent_stats = statistics([float(row["net_pnl"]) for row in independent_trades])
    prior_funnel = funnel(args.prior_proxy, args.prior_structural)
    independent_funnel = funnel(args.independent_proxy, args.independent_structural)
    independent_breakdown = breakdowns(independent_trades, args.independent_proxy)
    prior_breakdown = breakdowns(prior_trades, args.prior_proxy)
    independent_class = classification(independent_metric, independent_stats)
    combined_trades = prior_trades + independent_trades
    combined_values = [float(row["net_pnl"]) for row in combined_trades]
    combined_metric = dict(independent_metric)
    for key in ("gross_price_pnl", "execution_adjusted_gross_pnl", "entry_fees", "exit_fees", "spread_impact", "slippage_impact", "total_transaction_cost", "net_pnl"):
        combined_metric[key] = prior_metric[key] + independent_metric[key]
    combined_wins = [value for value in combined_values if value > 0]
    combined_losses = [value for value in combined_values if value < 0]
    combined_metric.update({
        "closed_trades": len(combined_trades), "ending_equity": 1000 + sum(combined_values), "total_return_pct": sum(combined_values) / 10,
        "expectancy": mean(combined_values) if combined_values else 0.0,
        "profit_factor": sum(max(0.0, value) for value in combined_values) / -sum(min(0.0, value) for value in combined_values) if any(value < 0 for value in combined_values) else None,
        "win_rate": sum(value > 0 for value in combined_values) / len(combined_values) if combined_values else 0.0,
        "expectancy_r": ((prior_metric["expectancy_r"] * prior_metric["closed_trades"] + independent_metric["expectancy_r"] * independent_metric["closed_trades"]) / len(combined_values)) if combined_values else 0.0,
        "average_win": mean(combined_wins) if combined_wins else 0.0, "average_loss": mean(combined_losses) if combined_losses else 0.0,
        "payoff_ratio": mean(combined_wins) / abs(mean(combined_losses)) if combined_wins and combined_losses else None,
        "median_trade": median(combined_values) if combined_values else 0.0, "best_trade": max(combined_values, default=0.0), "worst_trade": min(combined_values, default=0.0),
        "maximum_drawdown": _max_drawdown(combined_values), "pnl_without_best": sum(combined_values) - sum(sorted(combined_values)[-1:]),
        "pnl_without_best_three": sum(combined_values) - sum(sorted(combined_values)[-3:]), "pnl_without_worst": sum(combined_values) - sum(sorted(combined_values)[:1]),
    })
    for key in ("tp1_hit_rate", "break_even_exit_rate", "final_target_rate", "full_stop_rate", "average_mfe_r", "average_mae_r", "adverse_first_pct", "average_holding_candles"):
        combined_metric[key] = ((prior_metric[key] * prior_metric["closed_trades"] + independent_metric[key] * independent_metric["closed_trades"]) / len(combined_values)) if combined_values else 0.0
    combined_stats = statistics(combined_values)
    combined_breakdown = combine_breakdowns(prior_breakdown, independent_breakdown)
    independent = {"classification": independent_class, "performance": independent_metric, "statistics": independent_stats, "funnel": independent_funnel}
    combined = {"performance": combined_metric, "statistics": combined_stats}
    decision = final_decision(independent, combined, combined_breakdown)
    write_json(args.output / "dataset_manifest_reference.json", {"dataset_hash": manifest["dataset_hash"], "manifest_hash": content_hash(manifest), "source": str(args.manifest)})
    write_json(args.output / "independent_funnel.json", independent_funnel)
    core_symbols = set(universes["universe_b_core_full_year"]["symbols"])
    all_symbols = set(universes["universe_a_all_symbol_common_window"]["symbols"])
    write_json(args.output / "universe_funnels.json", {
        "universe_a_all_symbol_common_window": funnel(args.independent_proxy, args.independent_structural, all_symbols),
        "universe_b_core_full_year": funnel(args.independent_proxy, args.independent_structural, core_symbols),
        "universe_c_per_symbol_maximum": {symbol: funnel(args.independent_proxy, args.independent_structural, {symbol}) for symbol in sorted(all_symbols)},
        "window_note": "A and B use this report only when their manifest windows equal the report coverage; unequal-history datasets require separately clipped frozen runs.",
    })
    write_csv(args.output / "independent_trades.csv", independent_trades)
    write_json(args.output / "independent_summary.json", independent)
    write_csv(args.output / "independent_breakdowns.csv", independent_breakdown)
    write_json(args.output / "independent_statistics.json", independent_stats)
    write_json(args.output / "prior_summary.json", {"performance": prior_metric, "funnel": prior_funnel})
    write_csv(args.output / "prior_breakdowns.csv", prior_breakdown)
    write_json(args.output / "prior_vs_independent.json", {"prior": {"funnel": prior_funnel, "performance": prior_metric}, "independent": independent})
    write_json(args.output / "combined_two_year_summary.json", combined)
    write_csv(args.output / "combined_breakdowns.csv", combined_breakdown)
    write_json(args.output / "final_decision.json", {"liquidity_sweep_decision": decision, "trend_continuation": "REJECTED_FOR_RESEARCH_AS_CURRENTLY_DEFINED", "continuation_included": False})
    write_json(args.output / "frozen_contract.json", {"strategy": SWEEP, "proxy_hash": EXPECTED_PROXY_HASH, "seed": SEED, "resamples": RESAMPLES, "execution_assumptions": independent_contract["execution_assumptions"], "no_threshold_mutation": True, "continuation_excluded": True})
    digest = artifact_hash(args.output)
    (args.output / "artifact_hash.txt").write_text(digest + "\n", encoding="utf-8")
    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
