from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

from backtesting.execution_contract import BacktestExecutionConfig, BacktestExecutionContract, ExecutionRecord
from research.liquidity_sweep_confirmation import (
    CONFIRMATION_WINDOW_CANDLES, CONTROL_STRATEGY, EXPERIMENTAL_STRATEGY,
    confirmation_entry, execute_confirmation, original_geometry,
)
from scripts.phase2e_liquidity_sweep_validation import SEED, RESAMPLES, statistics
from scripts.strategy_diagnosis import load_candles

SWEEP = "liquidity_sweep_reversal"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = sorted({key for row in rows for key in row}) if rows else ["empty"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)


def _record(record: ExecutionRecord) -> dict[str, Any]:
    return asdict(record)


def accepted_candidates(report: Path) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    events = load_json(report / "candidate_events.json")
    items = {
        (event["symbol"], item["direction"], int(item["signal_timestamp"])): (event, item)
        for event in events for item in event["detectors"] if item["strategy"] == SWEEP
    }
    decisions = load_json(report / "risk_gate_decisions.json")
    result = []
    for decision in decisions:
        if decision["strategy"] != SWEEP or not decision["allowed"]:
            continue
        key = (decision["symbol"], decision["direction"], int(decision["signal_timestamp"]))
        if key not in items:
            raise ValueError(f"accepted candidate missing detector identity: {key}")
        result.append(items[key])
    return result


def run_variant(report: Path, canonical: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candles = load_candles(canonical)
    contract = BacktestExecutionContract(BacktestExecutionConfig())
    equity = contract.config.starting_equity
    rows, decisions = [], []
    for event, item in accepted_candidates(report):
        series = candles[event["symbol"]]
        signal_timestamp = int(item["signal_timestamp"])
        index = next((i for i, candle in enumerate(series) if candle.timestamp_ms == signal_timestamp), None)
        if index is None:
            raise ValueError("signal candle missing from canonical history")
        decision = confirmation_entry(series, index, item["direction"])
        decisions.append({
            "candidate_id": item["candidate_id"], "symbol": event["symbol"], "direction": item["direction"],
            "signal_timestamp": signal_timestamp, **asdict(decision),
        })
        if decision.status != "CONFIRMED" or decision.entry_index is None:
            continue
        record = execute_confirmation(
            contract, symbol=event["symbol"], direction=item["direction"],
            signal_timestamp=signal_timestamp, entry_hint=float(item["entry"]),
            invalidation=float(item["stop"]), candles=series,
            entry_index=decision.entry_index, equity=equity,
        )
        row = _record(record)
        row.update({"candidate_id": item["candidate_id"], "confirmation_offset": decision.confirmation_offset, "confirmation_timestamp_ms": decision.confirmation_timestamp_ms})
        rows.append(row)
        if record.fill_status == "FILLED" and record.final_exit_reason not in {"", "OPEN_AT_DATA_END"}:
            equity = record.equity_after
    return rows, decisions


def control_rows(report: Path) -> list[dict[str, Any]]:
    return [
        {**row, "strategy": CONTROL_STRATEGY}
        for row in load_csv(report / "trade_level.csv")
        if row["strategy"] == SWEEP
    ]


def closed(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row["fill_status"] == "FILLED" and row["final_exit_reason"] not in {"", "OPEN_AT_DATA_END"}]


def excursion(row: dict[str, Any], series, horizon: int = 16) -> dict[str, Any]:
    fill = int(row["fill_timestamp"])
    index = next(i for i, candle in enumerate(series) if candle.timestamp_ms == fill)
    path = series[index + 1:index + 1 + horizon]
    direction = row["direction"]
    entry = float(row["executed_entry"]); stop = float(row["initial_stop"])
    risk = abs(entry - stop)
    favourable = [(c.high - entry if direction == "LONG" else entry - c.low) for c in path]
    adverse = [(entry - c.low if direction == "LONG" else c.high - entry) for c in path]
    first_favourable = next((i for i, value in enumerate(favourable, 1) if value > 0), None)
    first_adverse = next((i for i, value in enumerate(adverse, 1) if value > 0), None)
    tp1 = float(row["tp1_price"])
    requested = float(row["requested_entry"])
    _, final = original_geometry(direction, requested, stop)
    directional = lambda price: price - entry if direction == "LONG" else entry - price
    result = {
        "mfe_r": max([0.0] + favourable) / risk if risk else 0.0,
        "mae_r": max([0.0] + adverse) / risk if risk else 0.0,
        "adverse_first": bool(first_adverse and (not first_favourable or first_adverse <= first_favourable)),
        "favourable_first": bool(first_favourable and (not first_adverse or first_favourable < first_adverse)),
        "tp1_capable": any(value >= directional(tp1) for value in favourable) if directional(tp1) > 0 else False,
        "final_capable": any(value >= directional(final) for value in favourable) if directional(final) > 0 else False,
    }
    for offset in (1, 2, 4):
        result[f"close_displacement_{offset}_r"] = directional(path[offset - 1].close) / risk if len(path) >= offset and risk else None
    return result


def metrics(rows: list[dict[str, Any]], canonical: Path) -> dict[str, Any]:
    values = closed(rows); candles = load_candles(canonical)
    pnl = [float(row["net_pnl"]) for row in values]
    excursions = [excursion(row, candles[row["symbol"]]) for row in values]
    gains = sum(max(0.0, value) for value in pnl); losses = -sum(min(0.0, value) for value in pnl)
    wins = [value for value in pnl if value > 0]; losing = [value for value in pnl if value < 0]
    gross = sum(float(row["gross_pnl"]) for row in values)
    fees = sum(float(row["total_fees"]) for row in values)
    spread = sum(float(row["spread_cost"]) + float(row["exit_slippage"]) * 2 / 3 for row in values)
    slippage = sum(float(row["entry_slippage"]) + float(row["exit_slippage"]) / 3 for row in values)
    pure = gross + spread + slippage
    equity = peak = 1000.0; drawdown = 0.0
    for value in pnl:
        equity += value; peak = max(peak, equity); drawdown = max(drawdown, peak - equity)
    average = lambda key: mean([float(row[key]) for row in excursions]) if excursions else 0.0
    signal_displacements=[];reward_bps=[]
    for row in values:
        signal=next(candle for candle in candles[row["symbol"]] if candle.timestamp_ms==int(row["signal_timestamp"]))
        executed=float(row["executed_entry"]);direction=row["direction"]
        directional_displacement=executed-signal.close if direction=="LONG" else signal.close-executed
        signal_displacements.append(directional_displacement/signal.close*10_000)
        directional_reward=float(row["tp1_price"])-executed if direction=="LONG" else executed-float(row["tp1_price"])
        reward_bps.append(directional_reward/executed*10_000)
    return {
        "candidates": len(rows), "trades": len(values), "gross_price_pnl": pure,
        "execution_adjusted_gross": gross, "fees": fees, "spread": spread, "slippage": slippage,
        "total_costs": fees + spread + slippage, "net_pnl": sum(pnl),
        "profit_factor": gains / losses if losses else None, "expectancy": mean(pnl) if pnl else 0.0,
        "expectancy_r": mean([float(row["r_multiple"]) for row in values]) if values else 0.0,
        "win_rate": len(wins) / len(pnl) if pnl else 0.0,
        "payoff_ratio": mean(wins) / abs(mean(losing)) if wins and losing else None,
        "average_win": mean(wins) if wins else 0.0, "average_loss": mean(losing) if losing else 0.0,
        "maximum_drawdown": drawdown, "average_mfe_r": average("mfe_r"), "average_mae_r": average("mae_r"),
        "adverse_first_pct": sum(row["adverse_first"] for row in excursions) / len(excursions) * 100 if excursions else 0.0,
        "favourable_first_pct": sum(row["favourable_first"] for row in excursions) / len(excursions) * 100 if excursions else 0.0,
        "average_holding_candles": mean([int(row["candles_held"]) for row in values]) if values else 0.0,
        "tp1_capable_pct": sum(row["tp1_capable"] for row in excursions) / len(excursions) * 100 if excursions else 0.0,
        "final_capable_pct": sum(row["final_capable"] for row in excursions) / len(excursions) * 100 if excursions else 0.0,
        "stop_rate": sum(row["final_exit_reason"] == "STOP_LOSS" for row in values) / len(values) if values else 0.0,
        "tp1_rate": sum(float(row["tp1_quantity"]) > 0 for row in values) / len(values) if values else 0.0,
        "final_target_rate": sum(row["final_exit_reason"] in {"FINAL_TARGET", "TAKE_PROFIT"} for row in values) / len(values) if values else 0.0,
        "entry_displacement_from_signal": mean([abs(float(row["executed_entry"]) - float(row["requested_entry"])) for row in values]) if values else 0.0,
        "average_entry_displacement_bps_from_signal_close": mean(signal_displacements) if signal_displacements else 0.0,
        "average_tp1_reward_bps_from_entry": mean(reward_bps) if reward_bps else 0.0,
        "average_executed_entry": mean([float(row["executed_entry"]) for row in values]) if values else 0.0,
        "close_displacement_1_r": average("close_displacement_1_r"), "close_displacement_2_r": average("close_displacement_2_r"), "close_displacement_4_r": average("close_displacement_4_r"),
        "average_gross_edge_per_trade": pure / len(values) if values else 0.0,
        "average_cost_per_trade": (fees + spread + slippage) / len(values) if values else 0.0,
        "gross_edge_to_cost": pure / (fees + spread + slippage) if fees + spread + slippage else None,
        "costs_pct_gross_profit": (fees + spread + slippage) / gains * 100 if gains else None,
        "without_best": sum(pnl) - sum(sorted(pnl)[-1:]), "without_worst": sum(pnl) - sum(sorted(pnl)[:1]),
    }


def opportunity_cost(control: list[dict[str, Any]], decisions: list[dict[str, Any]], canonical: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    by_key = {(row["symbol"], row["direction"], int(row["signal_timestamp"])): row for row in control}
    candles = load_candles(canonical); details = []
    for decision in decisions:
        if decision["status"] != "CONFIRMATION_EXPIRED": continue
        key = (decision["symbol"], decision["direction"], int(decision["signal_timestamp"]))
        row = by_key.get(key)
        if not row or row["fill_status"] != "FILLED" or row["final_exit_reason"] in {"", "OPEN_AT_DATA_END"}: continue
        series = candles[decision["symbol"]]; signal_index = next(i for i,c in enumerate(series) if c.timestamp_ms == int(decision["signal_timestamp"]))
        path = series[signal_index + 3:signal_index + 19]
        entry=float(row["executed_entry"]); direction=decision["direction"]
        favourable=[c.high-entry if direction=="LONG" else entry-c.low for c in path]
        adverse=[entry-c.low if direction=="LONG" else c.high-entry for c in path]
        tp1=float(row["tp1_price"]); distance=tp1-entry if direction=="LONG" else entry-tp1
        pnl=float(row["net_pnl"])
        details.append({**decision,"control_net_pnl":pnl,"post_expiry_mfe":max([0.0]+favourable),"post_expiry_mae":max([0.0]+adverse),"control_profitable":pnl>0,"later_reached_tp1":any(x>=distance for x in favourable) if distance>0 else False,"confirmation_avoided_loser":pnl<0,"confirmation_missed_winner":pnl>0})
    avoided=-sum(min(0.0,float(row["control_net_pnl"])) for row in details); missed=sum(max(0.0,float(row["control_net_pnl"])) for row in details)
    return {"reconstructable_expired":len(details),"losers_avoided":sum(row["confirmation_avoided_loser"] for row in details),"winners_missed":sum(row["confirmation_missed_winner"] for row in details),"net_pnl_avoided":avoided,"net_pnl_missed":missed,"net_opportunity_cost_effect":avoided-missed},details


def paired_statistics(control: list[dict[str, Any]], variant: list[dict[str, Any]]) -> dict[str, Any]:
    left={(row["symbol"],row["direction"],int(row["signal_timestamp"])):float(row["net_pnl"]) for row in closed(control)}
    right={(row["symbol"],row["direction"],int(row["signal_timestamp"])):float(row["net_pnl"]) for row in closed(variant)}
    keys=sorted(left.keys() & right.keys());diffs=[right[key]-left[key] for key in keys]
    if not diffs:return {"paired_candidates":0,"mean_difference":0.0,"bootstrap_difference_ci95":[0.0,0.0]}
    rng=random.Random(SEED);samples=[mean([diffs[rng.randrange(len(diffs))] for _ in diffs]) for _ in range(RESAMPLES)];samples.sort()
    pick=lambda p:samples[round((len(samples)-1)*p)]
    return {"paired_candidates":len(keys),"mean_difference":mean(diffs),"bootstrap_difference_ci95":[pick(.025),pick(.975)]}


def bootstrap_mean_difference(control: list[dict[str, Any]], variant: list[dict[str, Any]]) -> dict[str, Any]:
    left=[float(row["net_pnl"]) for row in closed(control)];right=[float(row["net_pnl"]) for row in closed(variant)]
    if not left or not right:return {"mean_difference":0.0,"ci95":[0.0,0.0]}
    rng=random.Random(SEED);samples=[]
    for _ in range(RESAMPLES):
        a=mean([left[rng.randrange(len(left))] for _ in left]);b=mean([right[rng.randrange(len(right))] for _ in right]);samples.append(b-a)
    samples.sort();pick=lambda p:samples[round((len(samples)-1)*p)]
    return {"mean_difference":mean(right)-mean(left),"ci95":[pick(.025),pick(.975)]}


def max_drawdown(values: list[float]) -> float:
    equity=peak=1000.0;maximum=0.0
    for value in values:
        equity+=value;peak=max(peak,equity);maximum=max(maximum,peak-equity)
    return maximum


def breakdown(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped=defaultdict(list)
    for row in closed(rows):
        dt=datetime.fromtimestamp(int(row["signal_timestamp"])/1000,timezone.utc)
        for dimension,value in (("symbol",row["symbol"]),("month",dt.strftime("%Y-%m"))):grouped[(dimension,value)].append(float(row["net_pnl"]))
    return [{"dimension":key[0],"value":key[1],"trades":len(values),"net_pnl":sum(values)} for key,values in sorted(grouped.items())]


def improvement_concentration(control: list[dict[str, Any]], variant: list[dict[str, Any]]) -> dict[str, Any]:
    left={(row["symbol"],row["direction"],int(row["signal_timestamp"])):float(row["net_pnl"]) for row in closed(control)}
    right={(row["symbol"],row["direction"],int(row["signal_timestamp"])):float(row["net_pnl"]) for row in closed(variant)}
    grouped=defaultdict(float)
    keys=left.keys() | right.keys()
    for key in keys:
        dt=datetime.fromtimestamp(key[2]/1000,timezone.utc);difference=right.get(key,0.0)-left.get(key,0.0)
        grouped[("symbol",key[0])]+=difference;grouped[("month",dt.strftime("%Y-%m"))]+=difference
    total=sum(right.get(key,0.0)-left.get(key,0.0) for key in keys)
    largest=max([value for value in grouped.values() if value>0],default=0.0)
    return {"paired_total_improvement":total,"largest_positive_symbol_or_month_contribution":largest,"largest_share_of_positive_improvement":largest/total if total>0 else None,"passes_not_concentrated":bool(total>0 and largest<total*.8)}


def main() -> int:
    parser=argparse.ArgumentParser();parser.add_argument("--prior-report",type=Path,required=True);parser.add_argument("--prior-canonical",type=Path,required=True);parser.add_argument("--independent-report",type=Path,required=True);parser.add_argument("--independent-canonical",type=Path,required=True);parser.add_argument("--output",type=Path,required=True);args=parser.parse_args()
    if args.output.exists():raise SystemExit("refusing to overwrite Phase 3A output")
    args.output.mkdir(parents=True)
    cohorts={"prior":(args.prior_report,args.prior_canonical),"independent":(args.independent_report,args.independent_canonical)};results={};all_control=[];all_variant=[];all_decisions=[]
    for name,(report,canonical) in cohorts.items():
        control=control_rows(report);variant,decisions=run_variant(report,canonical);all_control+=control;all_variant+=variant;all_decisions+=decisions
        control_metric=metrics(control,canonical);variant_metric=metrics(variant,canonical);opportunity,details=opportunity_cost(control,decisions,canonical)
        result={"funnel":{"detector_hits":sum(sum(item["strategy"]==SWEEP for item in event["detectors"]) for event in load_json(report/"candidate_events.json")),"selected_candidates":sum(event["selected_strategy"]==SWEEP for event in load_json(report/"candidate_events.json")),"risk_accepted":len(decisions),"confirmation_triggered":sum(row["status"]=="CONFIRMED" for row in decisions),"confirmation_expired":sum(row["status"]=="CONFIRMATION_EXPIRED" for row in decisions),"control_execution_attempted":len(control),"variant_execution_attempted":len(variant),"control_filled":sum(row["fill_status"]=="FILLED" for row in control),"variant_filled":sum(row["fill_status"]=="FILLED" for row in variant),"control_closed":len(closed(control)),"variant_closed":len(closed(variant))},"control":control_metric,"confirmation":variant_metric,"difference":{key:(variant_metric[key]-control_metric[key]) for key in control_metric if isinstance(control_metric[key],(int,float)) and isinstance(variant_metric[key],(int,float))},"opportunity_cost":opportunity,"paired":paired_statistics(control,variant),"bootstrap_mean_difference":bootstrap_mean_difference(control,variant),"control_statistics":statistics([float(row["net_pnl"]) for row in closed(control)]),"variant_statistics":statistics([float(row["net_pnl"]) for row in closed(variant)])};results[name]=result
        write_json(args.output/f"{name}_comparison.json",result);write_csv(args.output/f"{name}_control_trades.csv",control);write_csv(args.output/f"{name}_confirmation_trades.csv",variant);write_csv(args.output/f"{name}_confirmation_decisions.csv",decisions);write_csv(args.output/f"{name}_opportunity_cost.csv",details);write_csv(args.output/f"{name}_breakdowns.csv",breakdown(variant))
    combined={}
    for label in ("control","confirmation"):
        p=results["prior"][label];i=results["independent"][label];n=p["trades"]+i["trades"]
        combined[label]={k:(p[k]+i[k] if k in {"candidates","trades","gross_price_pnl","execution_adjusted_gross","fees","spread","slippage","total_costs","net_pnl"} else ((p[k]*p["trades"]+i[k]*i["trades"])/n if n and isinstance(p[k],(int,float)) and isinstance(i[k],(int,float)) else None)) for k in p}
        pnl=[float(row["net_pnl"]) for row in closed(all_control if label=="control" else all_variant)];g=sum(max(0,x) for x in pnl);l=-sum(min(0,x) for x in pnl);wins=[x for x in pnl if x>0];losses=[x for x in pnl if x<0]
        combined[label].update({"profit_factor":g/l if l else None,"expectancy":mean(pnl) if pnl else 0.0,"win_rate":len(wins)/len(pnl) if pnl else 0.0,"average_win":mean(wins) if wins else 0.0,"average_loss":mean(losses) if losses else 0.0,"payoff_ratio":mean(wins)/abs(mean(losses)) if wins and losses else None,"maximum_drawdown":max_drawdown(pnl),"average_gross_edge_per_trade":combined[label]["gross_price_pnl"]/len(pnl) if pnl else 0.0,"average_cost_per_trade":combined[label]["total_costs"]/len(pnl) if pnl else 0.0,"gross_edge_to_cost":combined[label]["gross_price_pnl"]/combined[label]["total_costs"] if combined[label]["total_costs"] else None,"costs_pct_gross_profit":combined[label]["total_costs"]/g*100 if g else None,"without_best":sum(pnl)-sum(sorted(pnl)[-1:]),"without_worst":sum(pnl)-sum(sorted(pnl)[:1])})
    combined["difference"]={k:(combined["confirmation"][k]-combined["control"][k]) for k in combined["control"] if isinstance(combined["control"][k],(int,float)) and isinstance(combined["confirmation"][k],(int,float))};combined["paired"]=paired_statistics(all_control,all_variant);combined["bootstrap_mean_difference"]=bootstrap_mean_difference(all_control,all_variant);combined["control_statistics"]=statistics([float(row["net_pnl"]) for row in closed(all_control)]);combined["variant_statistics"]=statistics([float(row["net_pnl"]) for row in closed(all_variant)])
    combined["opportunity_cost"]={key:results["prior"]["opportunity_cost"][key]+results["independent"]["opportunity_cost"][key] for key in results["prior"]["opportunity_cost"]};combined["improvement_concentration"]=improvement_concentration(all_control,all_variant)
    write_json(args.output/"combined_comparison.json",combined)
    write_json(args.output/"control_results.json",{"prior":results["prior"]["control"],"independent":results["independent"]["control"],"combined":combined["control"]})
    write_json(args.output/"experimental_results.json",{"prior":results["prior"]["confirmation"],"independent":results["independent"]["confirmation"],"combined":combined["confirmation"]})
    write_json(args.output/"yearly_comparison.json",{"prior":results["prior"],"independent":results["independent"]})
    entry_keys=("average_entry_displacement_bps_from_signal_close","average_tp1_reward_bps_from_entry","average_mfe_r","average_mae_r","adverse_first_pct","favourable_first_pct","close_displacement_1_r","close_displacement_2_r","close_displacement_4_r","tp1_capable_pct","final_capable_pct","stop_rate","tp1_rate","final_target_rate")
    write_json(args.output/"entry_quality_analysis.json",{period:{label:{key:data[label][key] for key in entry_keys} for label in ("control","confirmation")} for period,data in {**results,"combined":combined}.items()})
    cost_keys=("average_gross_edge_per_trade","average_cost_per_trade","gross_edge_to_cost","costs_pct_gross_profit","average_entry_displacement_bps_from_signal_close","average_tp1_reward_bps_from_entry")
    write_json(args.output/"cost_efficiency_analysis.json",{period:{label:{key:data[label][key] for key in cost_keys} for label in ("control","confirmation")} for period,data in {**results,"combined":combined}.items()})
    write_json(args.output/"opportunity_cost_analysis.json",{"prior":results["prior"]["opportunity_cost"],"independent":results["independent"]["opportunity_cost"],"combined":combined["opportunity_cost"]})
    write_json(args.output/"paired_candidate_analysis.json",{"prior":results["prior"]["paired"],"independent":results["independent"]["paired"],"combined":combined["paired"]})
    write_json(args.output/"statistical_uncertainty.json",{period:{key:data[key] for key in ("control_statistics","variant_statistics","bootstrap_mean_difference","paired")} for period,data in {**results,"combined":combined}.items()})
    contract={"control":CONTROL_STRATEGY,"experimental":EXPERIMENTAL_STRATEGY,"only_change":"wait at most two closed 15m candles for close beyond signal-candle extreme; enter following candle open","confirmation_window_candles":CONFIRMATION_WINDOW_CANDLES,"geometry":"absolute signal-time invalidation and 0.8R/1.5R targets retained","seed":SEED,"resamples":RESAMPLES,"research_only":True,"production_registered":False};write_json(args.output/"hypothesis_contract.json",contract)
    c=combined["confirmation"];ind=results["independent"]["confirmation"];entry_improved=combined["difference"].get("average_mae_r",0)<0 or combined["difference"].get("adverse_first_pct",0)<0
    supported=(results["prior"]["funnel"]["risk_accepted"]==len(accepted_candidates(args.prior_report)) and results["independent"]["funnel"]["risk_accepted"]==len(accepted_candidates(args.independent_report)) and ind["expectancy"]>results["independent"]["control"]["expectancy"] and c["expectancy"]>0 and c["profit_factor"] and c["profit_factor"]>1 and c["gross_price_pnl"]>0 and c["gross_edge_to_cost"]>combined["control"]["gross_edge_to_cost"] and c["without_best"]>0 and c["maximum_drawdown"]<=combined["control"]["maximum_drawdown"]*1.1 and combined["improvement_concentration"]["passes_not_concentrated"] and c["trades"]>=10)
    if supported:verdict="HYPOTHESIS SUPPORTED"
    elif entry_improved and not (ind["expectancy"]<0 and ind["expectancy"]<=results["independent"]["control"]["expectancy"]):verdict="HYPOTHESIS PARTIALLY SUPPORTED"
    else:verdict="HYPOTHESIS REJECTED"
    write_json(args.output/"final_verdict.json",{"verdict":verdict,"production_promotion":False})
    digest=hashlib.sha256(b"".join(path.read_bytes() for path in sorted(args.output.iterdir()) if path.name!="artifact_hash.txt")).hexdigest();(args.output/"artifact_hash.txt").write_text(digest+"\n");print(digest);return 0


if __name__=="__main__":raise SystemExit(main())
