from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def dimensions(row: dict[str, str]) -> dict[str, str]:
    dt = datetime.fromtimestamp(int(row["signal_timestamp_ms"]) / 1000, timezone.utc)
    hour = dt.hour
    atr_pct = float(row["atr14"]) / float(row["signal_close"]) * 100
    stop = float(row["stop_distance_atr"]); escape = float(row["escape_distance_atr"])
    return {
        "direction": row["direction"], "symbol": row["symbol"], "month": dt.strftime("%Y-%m"),
        "utc_hour": str(hour), "session": "ASIA" if hour < 8 else "EUROPE" if hour < 16 else "US",
        "htf_context": row["htf_relationship"],
        "volatility_regime": "LOW" if atr_pct < .35 else "MEDIUM" if atr_pct < .75 else "HIGH",
        "stop_distance_bucket": "LE_1_ATR" if stop <= 1 else "LE_1_5_ATR" if stop <= 1.5 else "GT_1_5_ATR",
        "escape_distance_bucket": "LE_0_25_ATR" if escape <= .25 else "LE_0_5_ATR" if escape <= .5 else "GT_0_5_ATR",
    }


def complete_breakdowns(period: Path) -> list[dict[str, Any]]:
    candidates = load_csv(period / "candidate_records.csv")
    counts: Counter[tuple[str, str]] = Counter()
    for row in candidates:
        for dimension, value in dimensions(row).items():
            counts[(dimension, value)] += 1
    existing = {(row["dimension"], row["value"]): row for row in load_json(period / "breakdowns.json")}
    result = []
    for key, count in sorted(counts.items()):
        row = existing.get(key, {})
        result.append({
            "dimension": key[0], "value": key[1], "candidates": count,
            "trades": row.get("closed_trades", 0), "gross_pnl": row.get("gross_price_pnl", 0.0),
            "net_pnl": row.get("net_pnl", 0.0), "profit_factor": row.get("profit_factor"),
            "expectancy": row.get("net_expectancy", 0.0), "win_rate": row.get("win_rate", 0.0),
            "average_mfe_r": row.get("average_mfe_r", 0.0), "average_mae_r": row.get("average_mae_r", 0.0),
        })
    return result


def main() -> int:
    parser=argparse.ArgumentParser();parser.add_argument("--development",type=Path,required=True);parser.add_argument("--validation",type=Path,required=True);parser.add_argument("--output",type=Path,required=True);args=parser.parse_args()
    if args.output.exists():raise SystemExit("refusing to overwrite Phase 3C final report")
    args.output.mkdir(parents=True)
    development=load_json(args.development/"performance.json");validation=load_json(args.validation/"performance.json")
    funnel_dev=load_json(args.development/"funnel.json");funnel_val=load_json(args.validation/"funnel.json")
    cost=load_json(args.validation/"cost_dependency.json");uncertainty=load_json(args.validation/"statistical_uncertainty.json")
    breakdown=complete_breakdowns(args.validation);write_json(args.output/"validation_breakdowns_complete.json",breakdown)
    symbols=[row for row in breakdown if row["dimension"]=="symbol"]
    sufficiently_positive=sum(row["trades"]>=5 and row["gross_pnl"]>0 for row in symbols)
    criteria=[
        ("closed_trades >= 30",validation["closed_trades"],validation["closed_trades"]>=30),
        ("gross_price_expectancy > 0",validation["gross_expectancy"],validation["gross_expectancy"]>0),
        ("net_expectancy > 0",validation["net_expectancy"],validation["net_expectancy"]>0),
        ("profit_factor > 1.15",validation["profit_factor"],bool(validation["profit_factor"] and validation["profit_factor"]>1.15)),
        ("net_result_without_best_trade > 0",cost["without_best_trade"],cost["without_best_trade"]>0),
        ("largest_profitable_symbol_share <= 0.50",cost["best_symbol_positive_profit_share"],bool(cost["best_symbol_positive_profit_share"] is not None and cost["best_symbol_positive_profit_share"]<=.5)),
        ("largest_profitable_month_share <= 0.40",cost["best_month_positive_profit_share"],bool(cost["best_month_positive_profit_share"] is not None and cost["best_month_positive_profit_share"]<=.4)),
        ("total_costs / gross_profit < 0.70",cost["costs_pct_gross_profit"]/100 if cost["costs_pct_gross_profit"] is not None else None,bool(cost["costs_pct_gross_profit"] is not None and cost["costs_pct_gross_profit"]<70)),
        ("maximum_drawdown_pct <= 0.05",validation["maximum_drawdown_pct"]/100,validation["maximum_drawdown_pct"]<=5),
        ("maximum_drawdown / total_net_profit <= 1.50",None,validation["net_pnl"]>0 and validation["maximum_drawdown"]<=1.5*validation["net_pnl"]),
        ("development_and_validation_gross_expectancy_have_same_sign",{"development":development["gross_expectancy"],"validation":validation["gross_expectancy"]},development["gross_expectancy"]*validation["gross_expectancy"]>0),
        ("bootstrap_95pct_mean_net_expectancy_lower_bound_r >= -0.05",uncertainty["bootstrap_mean_r_ci95"][0],uncertainty["bootstrap_mean_r_ci95"][0]>=-.05),
        ("at_least_3_symbols_each_have_5_trades_and_positive_gross_price_expectancy",sufficiently_positive,sufficiently_positive>=3),
    ]
    matrix=[{"criterion":name,"required":name,"actual":actual,"status":"PASS" if passed else "FAIL"} for name,actual,passed in criteria]
    write_json(args.output/"acceptance_criteria_matrix.json",matrix)
    comparison={
        "development":development,"validation":validation,
        "funnel":{"development":funnel_dev,"validation":funnel_val},
        "trade_frequency_ratio_validation_to_development":validation["closed_trades"]/development["closed_trades"],
        "gross_expectancy_signs":{"development":"POSITIVE" if development["gross_expectancy"]>0 else "NEGATIVE","validation":"POSITIVE" if validation["gross_expectancy"]>0 else "NEGATIVE"},
        "net_expectancy_signs":{"development":"POSITIVE" if development["net_expectancy"]>0 else "NEGATIVE","validation":"POSITIVE" if validation["net_expectancy"]>0 else "NEGATIVE"},
    }
    write_json(args.output/"development_validation_comparison.json",comparison)
    verdict="VALIDATED — ELIGIBLE FOR FORWARD-PAPER DESIGN" if all(row["status"]=="PASS" for row in matrix) else "REJECTED — FAILED LOCKED VALIDATION"
    write_json(args.output/"final_verdict.json",{"verdict":verdict,"failed_criteria":sum(row["status"]=="FAIL" for row in matrix),"production_promotion":False})
    digest=hashlib.sha256(b"".join(path.read_bytes() for path in sorted(args.output.iterdir()) if path.name!="artifact_hash.txt")).hexdigest();(args.output/"artifact_hash.txt").write_text(digest+"\n");print(digest);return 0


if __name__=="__main__":raise SystemExit(main())
